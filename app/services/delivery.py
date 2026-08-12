"""Доставка доступу після зарахування оплати.

Єдине місце, де людина дізнається, що оплата пройшла. Раніше повідомлення
формувалось лише в обробнику хеша, тому два інші шляхи зарахування —
фоновий скан гаманця (`scan_payments`) і дочекування підтверджень
(`finalize_confirming`) — мовчали: підписка створювалась, гроші лежали на
гаманці, а користувач про це не знав і дізнавався випадково.

Подвійних повідомлень тут бути не може: `repo.settle_invoice` переводить
інвойс у `paid` умовним UPDATE, тож рівно один шлях отримує success=True.
"""

import logging
from decimal import Decimal
from typing import Optional

from aiogram import Bot
from aiogram.exceptions import TelegramForbiddenError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import OVERPAY_ALERT_USD, settings
from app.db.models import Invoice
from app.locales.uk import t
from app.services import access, repo
from app.services.amounts import format_amount
from app.services.notify import notify_admins


def _user_label(invoice: Invoice) -> str:
    user = getattr(invoice, "user", None)
    if user is None:
        return f"user_id={invoice.user_id}"
    name = user.first_name or "—"
    username = f" (@{user.username})" if user.username else ""
    return f"{name}{username} [{user.tg_id}]"


async def deliver_access(
    bot: Bot,
    session: AsyncSession,
    invoice: Invoice,
    tx=None,
    overpay: Decimal = Decimal(0),
    source: str = "auto",
) -> bool:
    """Видати посилання й сповістити користувача та адмінів.

    Повертає True, якщо повідомлення дійшло до користувача. False означає,
    що доступ уже є в базі, але людина його не побачила — про це окремо
    сповіщаємо адмінів, щоб ніхто не лишився без каналу мовчки.
    """
    tg_id = invoice.user.tg_id if getattr(invoice, "user", None) else None

    sub = await repo.get_active_subscription(session, invoice.user_id)
    if sub is None:
        logging.error("Інвойс #%s оплачено, але активної підписки немає", invoice.id)
        await notify_admins(
            bot,
            f"⚠️ Інвойс #{invoice.id} оплачено, але підписка не знайдена.\n"
            f"Юзер: {_user_label(invoice)}\nВидай доступ вручну через /grant.",
        )
        return False

    # Посилання може не видатись (у бота забрали права в каналі) — це не привід
    # мовчати: людина має знати, що оплату зараховано.
    link: Optional[str] = None
    try:
        link = await access.issue_invite(bot, settings.channel_id, sub.id, session)
    except Exception as e:
        logging.error("Не вдалося видати посилання для інвойса #%s: %s", invoice.id, e, exc_info=True)
        await notify_admins(
            bot,
            f"⚠️ Оплата зарахована, але посилання не видалось.\n"
            f"Юзер: {_user_label(invoice)}\nІнвойс #{invoice.id}\nПомилка: {e}",
        )

    # Про переплату юзеру не пишемо: доступ відкрито, і зайвий рядок про
    # «поверни різницю» лише плодить звернення в підтримку. Адміни її бачать
    # у своєму сповіщенні нижче.
    body = t("success_invite", link=link) if link else t("invite_later")
    text = f"{t('paid')}\n\n{body}"

    delivered = False
    if tg_id is not None:
        try:
            from app.keyboards import main_kb

            await bot.send_message(tg_id, text, reply_markup=main_kb(True))
            delivered = True
        except TelegramForbiddenError:
            logging.warning("Юзер %s заблокував бота — не сповістили про оплату", tg_id)
            await repo.mark_bot_blocked(session, invoice.user_id)
            await notify_admins(
                bot,
                f"⚠️ Оплата зарахована, але юзер заблокував бота.\n"
                f"Юзер: {_user_label(invoice)}\nІнвойс #{invoice.id}",
            )
        except Exception as e:
            logging.error("Не вдалося сповістити %s про оплату: %s", tg_id, e, exc_info=True)
            await notify_admins(
                bot,
                f"⚠️ Оплата зарахована, але повідомлення не дійшло.\n"
                f"Юзер: {_user_label(invoice)}\nІнвойс #{invoice.id}\nПомилка: {e}",
            )

    amount = tx.amount if tx is not None else invoice.received_amount
    note = f"\n⚠️ Переплата: {format_amount(overpay)} USDT" if overpay >= OVERPAY_ALERT_USD else ""
    await notify_admins(
        bot,
        f"💰 Нова оплата ({source})!\nЮзер: {_user_label(invoice)}\n"
        f"План: {invoice.plan}\nМережа: {invoice.network}\n"
        f"Сума: {format_amount(amount) if amount is not None else '—'} USDT\n"
        f"Очікувалось: {format_amount(invoice.expected_amount)} USDT{note}",
    )

    return delivered
