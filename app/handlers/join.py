"""Схвалення заявок на вступ у канал.

Посилання видається з creates_join_request=True, тож саме по собі воно нікого
в канал не пускає — створюється заявка, і рішення ухвалює бот. Схвалюємо лише
тоді, коли в акаунта, який постукав, є власна активна підписка. Через це
переслане третій особі посилання нічого не дає.
"""

import logging

from aiogram import Bot, Router
from aiogram.types import ChatJoinRequest
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.locales.uk import t
from app.services import access, repo

router = Router(name="join")


async def _notify(bot: Bot, tg_id: int, key: str) -> None:
    """Пояснити причину. Якщо людина не стартувала бота — просто мовчимо."""
    try:
        await bot.send_message(tg_id, t(key))
    except Exception as e:
        logging.info("Не вдалося повідомити %s про рішення по заявці: %s", tg_id, e)


@router.chat_join_request()
async def handle_join_request(event: ChatJoinRequest, bot: Bot, session: AsyncSession):
    tg_id = event.from_user.id
    chat_id = event.chat.id

    if settings.channel_id and chat_id != settings.channel_id:
        logging.warning("Заявка в чужий чат %s від %s — ігнорую", chat_id, tg_id)
        return

    used_link = event.invite_link.invite_link if event.invite_link else None

    try:
        user = await repo.get_user_by_tg_id(session, tg_id)
        sub = await repo.get_active_subscription(session, user.id) if user else None

        if sub is None:
            await bot.decline_chat_join_request(chat_id=chat_id, user_id=tg_id)
            logging.info("Заявку %s відхилено: немає активної підписки (посилання %s)", tg_id, used_link)
            await _notify(bot, tg_id, "join_declined_no_sub")
            return

        # Підписка є, але постукали чужим посиланням — значить його переслали.
        # Пускати не можна: інакше одне посилання обслуговує двох.
        if used_link and sub.invite_link and used_link != sub.invite_link:
            await bot.decline_chat_join_request(chat_id=chat_id, user_id=tg_id)
            logging.warning("Заявку %s відхилено: чуже посилання %s", tg_id, used_link)
            await _notify(bot, tg_id, "join_declined_foreign_link")
            return

        await bot.approve_chat_join_request(chat_id=chat_id, user_id=tg_id)
        logging.info("Заявку %s схвалено (підписка #%s до %s)", tg_id, sub.id, sub.expires_at)

        # Посилання відпрацювало — гасимо його, щоб ним більше ніхто не скористався.
        if sub.invite_link:
            await access.revoke_invite(bot, chat_id, sub.invite_link)
            await repo.clear_invite_link(session, sub.id)

        await _notify(bot, tg_id, "join_approved")

    except Exception as e:
        logging.error("Помилка обробки заявки від %s: %s", tg_id, e, exc_info=True)
