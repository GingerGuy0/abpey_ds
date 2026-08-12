from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from typing import Optional

from sqlalchemy.ext.asyncio import AsyncSession
from app.db.models import User, Invoice
from app.services import repo, amounts
from app.chains.base import IncomingTx, TxNotFound, ChainError, ChainRateLimited, ChainAuthError
from app.chains.utils import extract_hash_from_input
from app.chains.registry import registry
from app.config import NETWORKS, PLANS, INVOICE_TTL_MINUTES, MAX_HASH_ATTEMPTS, HASH_COOLDOWN_SEC

class PaymentError(Exception):
    pass

@dataclass
class CheckResult:
    status: str
    invoice: Optional[Invoice] = None
    tx: Optional[IncomingTx] = None
    message_key: str = ""
    extra: dict = None

@dataclass
class SettleResult:
    status: str
    invoice: Optional[Invoice] = None
    tx: Optional[IncomingTx] = None
    message_key: str = ""
    extra: dict = None

async def create_invoice(session: AsyncSession, user: User, plan: str, network: str) -> Invoice:
    if user.is_banned:
        raise PaymentError("Користувач заблокований")
        
    active_inv = await repo.get_active_invoice(session, user.id)
    if active_inv:
        return active_inv
        
    address = await repo.get_wallet(session, network)
    if not address:
        raise PaymentError("Адреса для цієї мережі не налаштована")
        
    base_price = await repo.get_price(session, plan)
    expected = await amounts.allocate_unique_amount(session, network, base_price)
    
    cfg = NETWORKS[network]
    expected_units = amounts.to_units(expected, cfg.decimals)
    
    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(minutes=INVOICE_TTL_MINUTES)
    
    return await repo.create_invoice(
        session=session,
        user_id=user.id,
        plan=plan,
        network=network,
        base_amount_usd=base_price,
        expected_amount=expected,
        expected_units=expected_units,
        pay_address=address,
        status="pending",
        expires_at=expires_at
    )

async def check_by_hash(session: AsyncSession, invoice: Invoice, raw_input: str) -> CheckResult:
    if invoice.status not in ("pending", "confirming"):
        return CheckResult(status="wrong_state")
        
    if invoice.attempts >= MAX_HASH_ATTEMPTS:
        return CheckResult(status="too_many_attempts")
        
    now = datetime.now(timezone.utc)
    if invoice.last_attempt_at and (now - invoice.last_attempt_at).total_seconds() < HASH_COOLDOWN_SEC:
        return CheckResult(status="cooldown")
        
    client = registry.get(invoice.network)
    try:
        raw_hash = extract_hash_from_input(raw_input)
        normalized_hash = client.normalize_hash(raw_hash)
    except ValueError:
        return CheckResult(status="bad_hash")
        
    if await repo.is_tx_processed(session, normalized_hash):
        return CheckResult(status="already_used")
        
    try:
        tx = await client.get_tx_by_hash(normalized_hash, invoice.pay_address)
    except TxNotFound:
        await repo.bump_attempt(session, invoice.id)
        return CheckResult(status="not_found")
    except ChainRateLimited:
        return CheckResult(status="chain_rate_limited")
    except ChainAuthError as e:
        import logging
        logging.error("check_by_hash auth error inv=%s net=%s hash=%s: %s", invoice.id, invoice.network, normalized_hash, e, exc_info=True)
        return CheckResult(status="chain_auth_error", extra={"detail": str(e)})
    except ChainError as e:
        import logging
        logging.error("check_by_hash chain error inv=%s net=%s hash=%s: %s", invoice.id, invoice.network, normalized_hash, e, exc_info=True)
        return CheckResult(status="chain_unavailable", extra={"detail": str(e)})
        
    await repo.bump_attempt(session, invoice.id)
        
    if invoice.network in ("bsc", "eth"):
        addr_match = tx.to_address.lower() == invoice.pay_address.lower()
    elif invoice.network == "ton":
        addr_match = await client.ton_addr_equal(tx.to_address, invoice.pay_address)
    else:
        addr_match = tx.to_address == invoice.pay_address
        
    if not addr_match:
        return CheckResult(status="wrong_address")
        
    if not tx.is_success:
        return CheckResult(status="tx_failed")
        
    grace_start = invoice.created_at - timedelta(minutes=5)
    grace_end = invoice.expires_at + timedelta(minutes=15)
    if not (grace_start <= tx.timestamp <= grace_end):
        return CheckResult(status="out_of_window")
        
    # Інвойс тут відомий напевно (людина відкрила саме його), тому міряємо
    # суму відносно нього, а не шукаємо «кому б це підійшло».
    #
    # Рахуємо НАКОПИЧЕНО, а не по одній транзакції: людина, яка недоплатила,
    # може докинути залишок другим переказом. Раніше кожен переказ мірявся
    # окремо проти повної ціни, тож доплата вічно виглядала недоплатою.
    min_conf = NETWORKS[invoice.network].min_confirmations
    credited = await repo.get_credited_amount(session, invoice.id)
    total = credited + tx.amount

    if not amounts.is_amount_acceptable(total, invoice.expected_amount):
        # Непідтверджену транзакцію не зараховуємо: реорг мережі відкотив би
        # її, а в нас вона вже лежала б як зарахована частина.
        if tx.confirmations < min_conf:
            # Транзакція справжня й адресована саме цьому інвойсу — питання
            # лише в часі. Такі перевірки не мають з'їдати ліміт спроб.
            await repo.reset_attempts(session, invoice.id)
            return CheckResult(
                status="partial_unconfirmed",
                extra={
                    "received": tx.amount,
                    "expected": invoice.expected_amount,
                    "missing": invoice.expected_amount - total,
                }
            )

        if not await repo.credit_partial(session, invoice.id, tx):
            return CheckResult(status="already_used")

        # Статус НЕ міняємо: інвойс лишається pending, щоб людина могла
        # докинути залишок. Позначка "underpaid" зробила б інвойс мертвим —
        # далі check_by_hash одразу повертав би wrong_state.
        return CheckResult(
            status="underpaid",
            invoice=invoice,
            tx=tx,
            extra={
                "received": tx.amount,
                "credited": total,
                "expected": invoice.expected_amount,
                "missing": invoice.expected_amount - total,
                "topup": credited > 0,
            }
        )

    overpay = total - invoice.expected_amount

    if tx.confirmations < min_conf:
        invoice.status = "confirming"
        invoice.tx_hash = normalized_hash
        await session.commit()
        return CheckResult(status="confirming")

    success = await repo.settle_invoice(session, invoice.id, tx)
    if success:
        return CheckResult(
            status="paid", invoice=invoice, tx=tx,
            extra={"overpay": overpay} if overpay > 0 else None,
        )
    else:
        return CheckResult(status="already_used")

def pick_invoice(tx: IncomingTx, candidates: list[Invoice]) -> tuple[Optional[Invoice], str]:
    """Кому належить платіж, якщо все, що ми знаємо — це сума.

    Тут навмисно немає «виберемо найближчий інвойс»: при десятку активних
    інвойсів найближчий запросто виявиться чужим, і людина, яка округлила
    суму, подарує підписку незнайомцю. Тому — або однозначний збіг, або
    ручний розбір (orphan).

    Недоплата/переплата НЕ обробляється автоматично: для цього є перевірка за
    хешем, де інвойс відомий напевно.
    """
    exact = [inv for inv in candidates if amounts.matches_exactly(tx.amount, inv.expected_amount)]

    if len(exact) == 1:
        return exact[0], "exact"
    if len(exact) > 1:
        return None, "ambiguous"
    return None, "none"

from aiogram import Bot
from app.config import settings
import time

async def scan_network(session: AsyncSession, network: str, bot: Optional[Bot] = None) -> list[SettleResult]:
    candidates = await repo.list_pending_by_network(session, network)
    if not candidates:
        return []
        
    earliest = min(inv.created_at for inv in candidates)
    since = earliest - timedelta(minutes=10)
    
    address = await repo.get_wallet(session, network)
    if not address:
        return []
        
    client = registry.get(network)
    try:
        txs = await client.get_incoming_transfers(address, since)
    except ChainAuthError as e:
        import logging
        logging.error(f"Auth error scanning network {network}: {e}", exc_info=True)
        if bot and settings.admin_id_list:
            last_alert_str = await repo.get_setting(session, f"last_alert_{network}")
            now = time.time()
            if not last_alert_str or now - float(last_alert_str) > 3600:
                try:
                    from app.services.notify import notify_admins
                    await notify_admins(bot, f"⚠️ Помилка доступу до RPC/API блокчейну для {network}:\n{e}")
                    await repo.set_setting(session, f"last_alert_{network}", str(now))
                except Exception:
                    pass
        return []
    except ChainError as e:
        import logging
        logging.error(f"Error scanning network {network}: {e}", exc_info=True)
        return []
        
    results = []
    for tx in txs:
        if await repo.is_tx_processed(session, tx.tx_hash):
            continue
            
        min_conf = NETWORKS[network].min_confirmations
        if not tx.is_success or tx.confirmations < min_conf:
            continue
            
        valid_cands = []
        for inv in candidates:
            grace_start = inv.created_at - timedelta(minutes=5)
            grace_end = inv.expires_at + timedelta(minutes=15)
            if grace_start <= tx.timestamp <= grace_end:
                valid_cands.append(inv)
                
        inv, reason = pick_invoice(tx, valid_cands)

        if reason == "exact":
            success = await repo.settle_invoice(session, inv.id, tx)
            if success:
                results.append(SettleResult(status="paid", invoice=inv, tx=tx))
                candidates.remove(inv)
        elif reason == "ambiguous":
            await repo.record_orphan(
                session=session,
                network=network,
                tx_hash=tx.tx_hash,
                amount=tx.amount,
                from_address=tx.from_address,
                tx_time=tx.timestamp,
                resolved=False
            )
            results.append(SettleResult(status="manual_review", tx=tx))
        else:
            await repo.record_orphan(
                session=session,
                network=network,
                tx_hash=tx.tx_hash,
                amount=tx.amount,
                from_address=tx.from_address,
                tx_time=tx.timestamp,
                resolved=False
            )
            results.append(SettleResult(status="orphan", tx=tx))
            
    return results

async def finalize_confirming(session: AsyncSession) -> list[SettleResult]:
    confirming = await repo.list_confirming(session)
    results = []
    for inv in confirming:
        if not inv.tx_hash:
            continue
        client = registry.get(inv.network)
        try:
            tx = await client.get_tx_by_hash(inv.tx_hash, inv.pay_address)
            min_conf = NETWORKS[inv.network].min_confirmations
            if tx.is_success and tx.confirmations >= min_conf:
                credited = await repo.get_credited_amount(session, inv.id)
                overpay = credited + tx.amount - inv.expected_amount
                success = await repo.settle_invoice(session, inv.id, tx)
                if success:
                    results.append(SettleResult(
                        status="paid", invoice=inv, tx=tx,
                        extra={"overpay": overpay} if overpay > 0 else None,
                    ))
        except (TxNotFound, ChainError):
            continue
    return results
