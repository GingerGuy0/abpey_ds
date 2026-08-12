import time
from datetime import datetime, timezone
from decimal import Decimal
from sqlalchemy import select, update, exc, func
from sqlalchemy.ext.asyncio import AsyncSession
from app.db.models import User, Invoice, Subscription, ProcessedTx, OrphanPayment, Setting, AdminAction
from app.chains.base import IncomingTx

async def get_or_create_user(session: AsyncSession, tg_user) -> User:
    stmt = select(User).where(User.tg_id == tg_user.id)
    result = await session.execute(stmt)
    user = result.scalar_one_or_none()
    
    if user:
        user.username = tg_user.username
        user.first_name = tg_user.first_name
        user.language_code = tg_user.language_code
    else:
        user = User(
            tg_id=tg_user.id,
            username=tg_user.username,
            first_name=tg_user.first_name,
            language_code=tg_user.language_code
        )
        session.add(user)
        
    await session.commit()
    return user

async def get_user_by_tg_id(session: AsyncSession, tg_id: int) -> User | None:
    stmt = select(User).where(User.tg_id == tg_id)
    return (await session.execute(stmt)).scalar_one_or_none()

async def get_user_by_username(session: AsyncSession, username: str) -> User | None:
    stmt = select(User).where(User.username == username.replace("@", ""))
    return (await session.execute(stmt)).scalar_one_or_none()

async def set_banned(session: AsyncSession, user_id: int, is_banned: bool):
    await session.execute(update(User).where(User.id == user_id).values(is_banned=is_banned))
    await session.commit()

async def mark_bot_blocked(session: AsyncSession, user_id: int):
    await session.execute(update(User).where(User.id == user_id).values(is_blocked_bot=True))
    await session.commit()

async def count_users(session: AsyncSession) -> int:
    return (await session.execute(select(func.count(User.id)))).scalar()

async def iter_active_user_ids(session: AsyncSession):
    stmt = select(User.tg_id).where(User.is_banned == False, User.is_blocked_bot == False)
    result = await session.stream(stmt.execution_options(yield_per=500))
    async for row in result:
        yield row[0]

async def get_active_invoice(session: AsyncSession, user_id: int) -> Invoice | None:
    stmt = select(Invoice).where(
        Invoice.user_id == user_id,
        Invoice.status.in_(["pending", "confirming"])
    )
    return (await session.execute(stmt)).scalar_one_or_none()

async def create_invoice(session: AsyncSession, **kwargs) -> Invoice:
    inv = Invoice(**kwargs)
    session.add(inv)
    await session.commit()
    return inv

async def get_invoice(session: AsyncSession, id: int) -> Invoice | None:
    return await session.get(Invoice, id)

async def list_pending_by_network(session: AsyncSession, network: str) -> list[Invoice]:
    stmt = select(Invoice).where(Invoice.network == network, Invoice.status == "pending")
    return list((await session.execute(stmt)).scalars().all())

async def list_confirming(session: AsyncSession) -> list[Invoice]:
    stmt = select(Invoice).where(Invoice.status == "confirming")
    return list((await session.execute(stmt)).scalars().all())

async def get_credited_amount(session: AsyncSession, invoice_id: int) -> Decimal:
    """Скільки вже реально прийшло по цьому інвойсу.

    Джерело правди — processed_txs: там лежать усі зараховані транзакції,
    зокрема часткові. Окрема колонка в invoices була б другою копією тієї ж
    інформації, яку легко розсинхронити.
    """
    stmt = select(func.coalesce(func.sum(ProcessedTx.amount), 0)).where(
        ProcessedTx.invoice_id == invoice_id
    )
    return Decimal(str((await session.execute(stmt)).scalar() or 0))


async def reset_attempts(session: AsyncSession, invoice_id: int):
    """Обнулити лічильник спроб, не чіпаючи кулдаун.

    Потрібно, коли людина принесла справжню транзакцію по цьому інвойсу, але
    зарахувати її ще рано (мало підтверджень). Без цього п'ять перевірок
    «ну як там?» вичерпали б ліміт і замкнули б людину без доступу. Кулдаун
    лишаємо — він захищає RPC від спаму.
    """
    await session.execute(
        update(Invoice)
        .where(Invoice.id == invoice_id)
        .values(attempts=0)
        .execution_options(synchronize_session=False)
    )
    await session.commit()


async def credit_partial(session: AsyncSession, invoice_id: int, tx: IncomingTx) -> bool:
    """Зарахувати часткову оплату, не закриваючи інвойс.

    Унікальний індекс на tx_hash не дає порахувати ту саму транзакцію двічі,
    навіть якщо людина надішле хеш повторно.
    """
    session.add(ProcessedTx(
        tx_hash=tx.tx_hash,
        network=tx.network,
        invoice_id=invoice_id,
        amount=tx.amount,
        from_address=tx.from_address,
        tx_time=tx.timestamp,
    ))
    # Ліміт спроб існує проти перебору чужих хешів. Людина, яка щойно принесла
    # справжню транзакцію, під це не підпадає — інакше на доплату просто не
    # лишиться спроб (їх усього MAX_HASH_ATTEMPTS), та ще й з кулдауном.
    await session.execute(
        update(Invoice)
        .where(Invoice.id == invoice_id)
        .values(attempts=0, last_attempt_at=None)
        .execution_options(synchronize_session=False)
    )
    try:
        await session.commit()
    except exc.IntegrityError:
        await session.rollback()
        return False
    return True


async def list_stale_invoices_with_credit(session: AsyncSession) -> list[Invoice]:
    """Інвойси, що от-от протухнуть, але гроші по них уже прийшли.

    Такі не можна гасити мовчки: часткова оплата лежить на гаманці, і без
    сигналу про неї ніхто не дізнається.
    """
    stmt = (
        select(Invoice)
        .join(ProcessedTx, ProcessedTx.invoice_id == Invoice.id)
        .where(Invoice.status == "pending", Invoice.expires_at < func.now())
        .distinct()
    )
    return list((await session.execute(stmt)).scalars().all())


async def expire_stale_invoices(session: AsyncSession):
    stmt = update(Invoice).where(
        Invoice.status == "pending",
        Invoice.expires_at < func.now()
    ).values(status="expired")
    await session.execute(stmt)
    await session.commit()

async def bump_attempt(session: AsyncSession, invoice_id: int):
    stmt = update(Invoice).where(Invoice.id == invoice_id).values(
        attempts=Invoice.attempts + 1,
        last_attempt_at=func.now()
    )
    await session.execute(stmt)
    await session.commit()

async def get_active_subscription(session: AsyncSession, user_id: int) -> Subscription | None:
    stmt = select(Subscription).where(
        Subscription.user_id == user_id,
        Subscription.is_active == True,
        Subscription.expires_at > func.now()
    ).order_by(Subscription.expires_at.desc())
    return (await session.execute(stmt)).scalar_one_or_none()

async def create_or_extend_subscription(session: AsyncSession, user_id: int, days: int, source: str, plan: str) -> Subscription:
    from datetime import timedelta
    current_sub = await get_active_subscription(session, user_id)
    now = datetime.now(timezone.utc)
    
    if current_sub and current_sub.expires_at > now:
        new_expires_at = current_sub.expires_at + timedelta(days=days)
        current_sub.expires_at = new_expires_at
        current_sub.plan = plan
        current_sub.source = source
        current_sub.notified_3d = False
        current_sub.notified_1d = False
        await session.commit()
        return current_sub
    else:
        new_sub = Subscription(
            user_id=user_id,
            plan=plan,
            source=source,
            starts_at=now,
            expires_at=now + timedelta(days=days)
        )
        session.add(new_sub)
        await session.commit()
        return new_sub

async def list_expiring(session: AsyncSession, days: int) -> list[Subscription]:
    from datetime import timedelta
    now = datetime.now(timezone.utc)
    target = now + timedelta(days=days)
    window_start = target - timedelta(hours=1)
    window_end = target + timedelta(hours=1)
    
    stmt = select(Subscription).where(
        Subscription.is_active == True,
        Subscription.expires_at >= window_start,
        Subscription.expires_at <= window_end
    )
    return list((await session.execute(stmt)).scalars().all())

async def get_stats(session: AsyncSession) -> dict:
    """Зведення для адмінки. Один прохід, без важких вибірок."""
    from datetime import timedelta

    now = datetime.now(timezone.utc)
    day_ago = now - timedelta(days=1)
    month_ago = now - timedelta(days=30)

    async def scalar(stmt):
        return (await session.execute(stmt)).scalar() or 0

    paid = Invoice.status == "paid"

    return {
        "users": await scalar(select(func.count(User.id))),
        "blocked_bot": await scalar(select(func.count(User.id)).where(User.is_blocked_bot == True)),
        "banned": await scalar(select(func.count(User.id)).where(User.is_banned == True)),
        "subs_active": await scalar(select(func.count(Subscription.id)).where(
            Subscription.is_active == True, Subscription.expires_at > now)),
        "subs_expiring_3d": await scalar(select(func.count(Subscription.id)).where(
            Subscription.is_active == True,
            Subscription.expires_at > now,
            Subscription.expires_at <= now + timedelta(days=3))),
        "paid_day": await scalar(select(func.count(Invoice.id)).where(paid, Invoice.paid_at >= day_ago)),
        "paid_month": await scalar(select(func.count(Invoice.id)).where(paid, Invoice.paid_at >= month_ago)),
        "revenue_day": await scalar(select(func.sum(Invoice.received_amount)).where(paid, Invoice.paid_at >= day_ago)),
        "revenue_month": await scalar(select(func.sum(Invoice.received_amount)).where(paid, Invoice.paid_at >= month_ago)),
        "invoices_pending": await scalar(select(func.count(Invoice.id)).where(
            Invoice.status.in_(["pending", "confirming"]), Invoice.expires_at > now)),
        "orphans": await scalar(select(func.count(OrphanPayment.id)).where(OrphanPayment.resolved == False)),
    }

async def list_active_subscriptions(session: AsyncSession) -> list[Subscription]:
    """Усі чинні підписки. Саме ці люди мають бути в каналі."""
    stmt = select(Subscription).where(
        Subscription.is_active == True,
        Subscription.expires_at > func.now()
    ).order_by(Subscription.expires_at)
    return list((await session.execute(stmt)).scalars().all())

async def extend_subscription(session: AsyncSession, sub_id: int, days: int) -> datetime | None:
    """Додати днів до наявної підписки. Повертає нову дату закінчення."""
    from datetime import timedelta

    sub = await session.get(Subscription, sub_id)
    if sub is None:
        return None

    sub.expires_at = sub.expires_at + timedelta(days=days)
    # Нагадування треба показати заново — дата змінилась.
    sub.notified_3d = False
    sub.notified_1d = False
    await session.commit()
    return sub.expires_at

async def list_expired_not_kicked(session: AsyncSession) -> list[Subscription]:
    stmt = select(Subscription).where(
        Subscription.is_active == True,
        Subscription.expires_at < func.now(),
        Subscription.kicked_at.is_(None)
    )
    return list((await session.execute(stmt)).scalars().all())

async def deactivate(session: AsyncSession, sub_id: int):
    await session.execute(update(Subscription).where(Subscription.id == sub_id).values(is_active=False))
    await session.commit()

async def set_invite_link(session: AsyncSession, sub_id: int, link: str):
    await session.execute(update(Subscription).where(Subscription.id == sub_id).values(
        invite_link=link,
        invite_link_created_at=func.now()
    ))
    await session.commit()

async def clear_invite_link(session: AsyncSession, sub_id: int):
    """Посилання вже використане й погашене — прибираємо з БД."""
    await session.execute(update(Subscription).where(Subscription.id == sub_id).values(
        invite_link=None
    ))
    await session.commit()

async def mark_notified(session: AsyncSession, sub_id: int, kind: str):
    if kind == "3d":
        await session.execute(update(Subscription).where(Subscription.id == sub_id).values(notified_3d=True))
    elif kind == "1d":
        await session.execute(update(Subscription).where(Subscription.id == sub_id).values(notified_1d=True))
    elif kind == "kick":
        await session.execute(update(Subscription).where(Subscription.id == sub_id).values(
            is_active=False,
            kicked_at=func.now()
        ))
    await session.commit()

async def is_tx_processed(session: AsyncSession, tx_hash: str) -> bool:
    stmt = select(ProcessedTx.id).where(ProcessedTx.tx_hash == tx_hash)
    return (await session.execute(stmt)).scalar_one_or_none() is not None

async def record_processed_tx(session: AsyncSession, **kwargs):
    session.add(ProcessedTx(**kwargs))
    await session.commit()

async def record_orphan(session: AsyncSession, **kwargs):
    session.add(OrphanPayment(**kwargs))
    try:
        await session.commit()
    except exc.IntegrityError:
        await session.rollback()

_settings_cache = {}
_settings_cache_ts = {}

async def get_setting(session: AsyncSession, key: str, default=None):
    now = time.time()
    if key in _settings_cache and now - _settings_cache_ts.get(key, 0) < 60:
        return _settings_cache[key]
        
    stmt = select(Setting.value).where(Setting.key == key)
    val = (await session.execute(stmt)).scalar_one_or_none()
    if val is not None:
        _settings_cache[key] = val
        _settings_cache_ts[key] = now
        return val
    return default

async def set_setting(session: AsyncSession, key: str, value: str):
    stmt = select(Setting).where(Setting.key == key)
    setting = (await session.execute(stmt)).scalar_one_or_none()
    if setting:
        setting.value = value
    else:
        session.add(Setting(key=key, value=value))
    await session.commit()
    if key in _settings_cache:
        del _settings_cache[key]

async def get_wallet(session: AsyncSession, network: str) -> str | None:
    return await get_setting(session, f"wallet_{network}")

async def set_wallet(session: AsyncSession, network: str, address: str):
    await set_setting(session, f"wallet_{network}", address)

async def get_price(session: AsyncSession, plan: str) -> Decimal:
    val = await get_setting(session, f"price_{plan}")
    if val:
        return Decimal(val)
    from app.config import PLANS
    return PLANS[plan]["price_usd"]

async def set_price(session: AsyncSession, plan: str, usd: Decimal):
    await set_setting(session, f"price_{plan}", str(usd))

async def log_admin_action(session: AsyncSession, **kwargs):
    session.add(AdminAction(**kwargs))
    await session.commit()

async def settle_invoice(session: AsyncSession, invoice_id: int, tx: IncomingTx) -> bool:
    try:
        # 1. Insert into processed_txs
        ptx = ProcessedTx(
            tx_hash=tx.tx_hash,
            network=tx.network,
            invoice_id=invoice_id,
            amount=tx.amount,
            from_address=tx.from_address,
            tx_time=tx.timestamp
        )
        session.add(ptx)
        await session.flush()

        # received_amount — сума ВСІХ зарахованих транзакцій, а не лише
        # останньої: інакше після доплати в базі лишалась би тільки друга
        # частина, і звітність показувала б недоплату там, де її немає.
        total_received = await get_credited_amount(session, invoice_id)

        # 2. Update invoice
        stmt = update(Invoice).where(
            Invoice.id == invoice_id,
            Invoice.status.in_(["pending", "confirming"])
        ).values(
            status="paid",
            tx_hash=tx.tx_hash,
            from_address=tx.from_address,
            received_amount=total_received,
            received_units=tx.units,
            confirmations=tx.confirmations,
            paid_at=func.now()
        ).execution_options(synchronize_session=False)
        
        result = await session.execute(stmt)
        if result.rowcount == 0:
            await session.rollback()
            return False
            
        # 3. Create or extend subscription
        inv_stmt = select(Invoice).where(Invoice.id == invoice_id)
        inv = (await session.execute(inv_stmt)).scalar_one()
        from app.config import PLANS
        days = PLANS[inv.plan]["days"]
        
        current_sub = await get_active_subscription(session, inv.user_id)
        now = datetime.now(timezone.utc)
        from datetime import timedelta
        
        if current_sub and current_sub.expires_at > now:
            current_sub.expires_at = current_sub.expires_at + timedelta(days=days)
            current_sub.plan = inv.plan
            current_sub.source = "payment"
            current_sub.notified_3d = False
            current_sub.notified_1d = False
        else:
            new_sub = Subscription(
                user_id=inv.user_id,
                plan=inv.plan,
                source="payment",
                starts_at=now,
                expires_at=now + timedelta(days=days)
            )
            session.add(new_sub)
            
        await session.commit()
        return True
    except exc.IntegrityError:
        await session.rollback()
        return False
