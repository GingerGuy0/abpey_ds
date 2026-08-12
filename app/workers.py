import asyncio
import logging
from decimal import Decimal
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from aiogram import Bot
from app.config import NETWORKS, settings
from app.services import repo, payments, access, delivery
from app.services.notify import notify_admins
from app.db.session import async_session
from app.locales.uk import t
from app.services.amounts import format_amount
from aiogram.exceptions import TelegramForbiddenError

scheduler = AsyncIOScheduler()

async def scan_payments(bot: Bot):
    try:
        from app.chains.registry import registry, NetworkDisabled
        async with async_session() as session:
            nets = await registry.available(session)
            for network in nets:
                try:
                    pending = await repo.list_pending_by_network(session, network)
                    if not pending:
                        continue
                    results = await payments.scan_network(session, network, bot=bot)
                    for res in results:
                        # Оплата знайдена сканером — людина нічого не натискала
                        # і без цього повідомлення ніяк не дізнається, що
                        # доступ уже є.
                        if res.status == "paid" and res.invoice is not None:
                            await delivery.deliver_access(
                                bot, session, res.invoice, tx=res.tx,
                                overpay=(res.extra or {}).get("overpay") or Decimal(0),
                                source="скан гаманця",
                            )
                            continue

                        # Платіж дійшов, але автоматично прив'язати його до
                        # інвойса неможливо. Мовчати не можна: гроші вже на
                        # гаманці, а людина сидить без підписки.
                        if res.status in ("orphan", "manual_review"):
                            reason = ("сума не збігається з жодним активним інвойсом"
                                      if res.status == "orphan"
                                      else "сума підходить кільком інвойсам одразу")
                            logging.warning("Платіж без власника (%s): %s %s USDT, hash=%s",
                                            res.status, network, res.tx.amount, res.tx.tx_hash)
                            await notify_admins(
                                bot,
                                f"❓ Платіж без власника ({network})\n"
                                f"Сума: {res.tx.amount} USDT\n"
                                f"Від: <code>{res.tx.from_address}</code>\n"
                                f"Хеш: <code>{res.tx.tx_hash}</code>\n\n"
                                f"Причина: {reason}.\n"
                                f"Попроси людину надіслати хеш у бота або видай доступ через /grant."
                            )
                except NetworkDisabled:
                    logging.info(f"Network {network} is disabled, skipping scan")
                except Exception as e:
                    logging.error(f"Error scanning {network}: {e}", exc_info=True)
    except Exception as e:
        logging.error(f"Error in scan_payments job: {e}", exc_info=True)

async def finalize_confirming(bot: Bot):
    """Дочекатись підтверджень мережі й ОБОВ'ЯЗКОВО сповістити людину.

    Користувач бачив «чекаю підтверджень, сповіщу автоматично» — ця обіцянка
    виконується саме тут.
    """
    try:
        async with async_session() as session:
            confirming = await repo.list_confirming(session)
            if not confirming:
                return
            results = await payments.finalize_confirming(session)
            for res in results:
                if res.status == "paid" and res.invoice is not None:
                    await delivery.deliver_access(
                        bot, session, res.invoice, tx=res.tx,
                        overpay=(res.extra or {}).get("overpay") or Decimal(0),
                        source="підтвердження мережі",
                    )
    except Exception as e:
        logging.error(f"Error finalize_confirming: {e}", exc_info=True)

async def expire_invoices(bot: Bot):
    """Погасити протухлі інвойси — але не мовчки, якщо по них є гроші.

    Часткова оплата, яку не встигли докинути, лишається на гаманці. Якщо
    просто закрити інвойс, ці гроші зникнуть з поля зору всіх: і юзера, і
    адміна. Тому спершу збираємо такі випадки й розсилаємо попередження.
    """
    try:
        async with async_session() as session:
            stale_with_credit = await repo.list_stale_invoices_with_credit(session)

            pending_alerts = []
            for inv in stale_with_credit:
                credited = await repo.get_credited_amount(session, inv.id)
                pending_alerts.append((inv, credited))

            await repo.expire_stale_invoices(session)

            for inv, credited in pending_alerts:
                tg_id = inv.user.tg_id if getattr(inv, "user", None) else None
                if tg_id is not None:
                    try:
                        await bot.send_message(tg_id, t(
                            "expired_with_credit",
                            credited=format_amount(credited),
                            expected=format_amount(inv.expected_amount),
                        ))
                    except TelegramForbiddenError:
                        await repo.mark_bot_blocked(session, inv.user_id)
                    except Exception as e:
                        logging.info("Не вдалося повідомити %s про протухлий інвойс: %s", tg_id, e)

                await notify_admins(
                    bot,
                    f"⏳ Інвойс #{inv.id} протух із частковою оплатою.\n"
                    f"Мережа: {inv.network}\n"
                    f"Зараховано: {format_amount(credited)} з {format_amount(inv.expected_amount)} USDT\n"
                    f"Юзер: {tg_id or inv.user_id}\n"
                    f"Треба або добити доступ через /grant, або повернути гроші."
                )
                await asyncio.sleep(0.05)
    except Exception as e:
        logging.error(f"Error expire_invoices: {e}", exc_info=True)

async def refresh_invoice_timers():
    pass

async def _send_reminder(bot: Bot, session, sub, kind: str) -> bool:
    """Надіслати нагадування й позначити його як показане.

    Прапорець ставимо лише після успішної відправки — інакше нагадування
    «згорить» у базі, а людина його ніколи не побачить. Виняток — коли юзер
    заблокував бота: повторні спроби нічого не дадуть, тому теж позначаємо.
    """
    date_str = sub.expires_at.strftime("%Y-%m-%d %H:%M")
    try:
        await bot.send_message(sub.user.tg_id, t(f"reminder_{kind}", date=date_str))
    except TelegramForbiddenError:
        await repo.mark_bot_blocked(session, sub.user_id)
        await repo.mark_notified(session, sub.id, kind)
        return False
    except Exception as e:
        logging.warning("Нагадування %s для %s не надіслано, спробуємо пізніше: %s",
                        kind, sub.user.tg_id, e)
        return False

    await repo.mark_notified(session, sub.id, kind)
    return True

async def subscription_reminders(bot: Bot):
    try:
        async with async_session() as session:
            sent = 0
            for kind, days in (("3d", 3), ("1d", 1)):
                for sub in await repo.list_expiring(session, days=days):
                    if getattr(sub, f"notified_{kind}"):
                        continue
                    if await _send_reminder(bot, session, sub, kind):
                        sent += 1
                    await asyncio.sleep(0.05)
            if sent:
                logging.info("Надіслано нагадувань про закінчення підписки: %d", sent)
    except Exception as e:
        logging.error(f"Error reminders: {e}", exc_info=True)

async def kick_expired(bot: Bot):
    """Видалити з каналу тих, у кого підписка закінчилась.

    Позначку «викинуто» ставимо ТІЛЬКИ якщо видалення реально спрацювало.
    Інакше бот вважав би, що прибрав людину, а вона й далі сиділа б у каналі
    безкоштовно — і ніхто б про це не дізнався.
    """
    try:
        async with async_session() as session:
            failures = []
            kicked = 0

            for sub in await repo.list_expired_not_kicked(session):
                tg_id = sub.user.tg_id
                try:
                    await access.kick_user(bot, settings.channel_id, tg_id)
                except Exception as e:
                    logging.error("Не вдалося видалити %s з каналу: %s", tg_id, e, exc_info=True)
                    failures.append(f"{tg_id}: {e}")
                    continue

                await repo.mark_notified(session, sub.id, "kick")
                kicked += 1

                try:
                    await bot.send_message(tg_id, t("kicked"))
                except TelegramForbiddenError:
                    await repo.mark_bot_blocked(session, sub.user_id)
                except Exception as e:
                    logging.info("Не вдалося повідомити %s про закінчення підписки: %s", tg_id, e)

                await asyncio.sleep(0.05)

            if kicked:
                logging.info("Видалено з каналу після закінчення підписки: %d", kicked)

            if failures:
                await notify_admins(
                    bot,
                    "⚠️ Не вдалося видалити з каналу після закінчення підписки:\n"
                    + "\n".join(failures[:10])
                    + (f"\n...та ще {len(failures) - 10}" if len(failures) > 10 else "")
                    + "\n\nПеревір права бота: потрібне «Блокування учасників»."
                )
    except Exception as e:
        logging.error(f"Error kicking: {e}", exc_info=True)

async def health_log():
    logging.info("Health log: process is alive")

def setup_scheduler(bot: Bot) -> AsyncIOScheduler:
    scheduler.add_job(scan_payments, 'interval', seconds=40, args=[bot], max_instances=1, coalesce=True)
    scheduler.add_job(finalize_confirming, 'interval', seconds=60, args=[bot], max_instances=1, coalesce=True)
    scheduler.add_job(expire_invoices, 'interval', minutes=5, args=[bot], max_instances=1, coalesce=True)
    scheduler.add_job(refresh_invoice_timers, 'interval', minutes=5, max_instances=1, coalesce=True)
    scheduler.add_job(subscription_reminders, 'interval', hours=1, args=[bot], max_instances=1, coalesce=True)
    scheduler.add_job(kick_expired, 'interval', hours=1, args=[bot], max_instances=1, coalesce=True)
    scheduler.add_job(health_log, 'interval', minutes=15, max_instances=1, coalesce=True)
    scheduler.start()
    return scheduler

def shutdown_scheduler():
    scheduler.shutdown(wait=False)
