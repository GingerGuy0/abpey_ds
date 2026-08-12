from datetime import datetime, timezone, timedelta
from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from app.services import repo
from app.config import INVITE_TTL_HOURS
import asyncio

class AccessError(Exception):
    pass

async def _safe_api_call(coro):
    try:
        return await coro
    except TelegramRetryAfter as e:
        await asyncio.sleep(e.retry_after)
        try:
            return await coro
        except Exception as ex:
            import logging
            logging.error(f"telegram_api_error_retry: {ex}")
            raise AccessError(str(ex))
    except (TelegramBadRequest, TelegramForbiddenError) as e:
        import logging
        logging.error(f"telegram_api_error: {e}")
        raise AccessError(str(e))
    except Exception as e:
        import logging
        logging.error(f"telegram_api_error_unknown: {e}")
        raise AccessError(str(e))

async def issue_invite(bot: Bot, channel_id: int, sub_id: int, session) -> str:
    """Персональне посилання-заявка.

    creates_join_request замість member_limit=1: посилання не пускає в канал
    саме по собі, а створює заявку, яку бот схвалює лише якщо в того, хто
    постукав, є активна підписка (див. app/handlers/join.py). Тому переслане
    другові посилання марне — заявку відхилять.

    ВАЖЛИВО: member_limit і creates_join_request взаємовиключні в Bot API,
    тому member_limit тут не передаємо.
    """
    now = datetime.now(timezone.utc)
    expire_date = now + timedelta(hours=INVITE_TTL_HOURS)

    link_obj = await _safe_api_call(bot.create_chat_invite_link(
        chat_id=channel_id,
        name=f"sub-{sub_id}",
        creates_join_request=True,
        expire_date=expire_date
    ))

    await repo.set_invite_link(session, sub_id, link_obj.invite_link)
    return link_obj.invite_link


async def revoke_invite(bot: Bot, channel_id: int, link: str) -> None:
    """Погасити посилання. Помилку ковтаємо: воно могло вже протухнути."""
    if not link:
        return
    try:
        await _safe_api_call(bot.revoke_chat_invite_link(chat_id=channel_id, invite_link=link))
    except AccessError:
        pass

async def reissue_invite(bot: Bot, session, sub) -> str:
    from app.config import settings
    if sub.invite_link:
        try:
            await _safe_api_call(bot.revoke_chat_invite_link(
                chat_id=settings.channel_id,
                invite_link=sub.invite_link
            ))
        except AccessError:
            pass
            
    now = datetime.now(timezone.utc)
    if sub.invite_link_created_at and (now - sub.invite_link_created_at).total_seconds() < 3600:
        raise AccessError("too_frequent")
        
    return await issue_invite(bot, settings.channel_id, sub.id, session)

async def is_channel_member(bot: Bot, channel_id: int, tg_id: int) -> bool:
    """Чи людина зараз у каналі.

    Список учасників каналу Bot API не віддає, тому перевіряємо поштучно.
    Помилку не ковтаємо тихо — повертаємо False, але викликач має розуміти,
    що це може бути й тимчасовий збій, а не «вийшов з каналу».
    """
    try:
        member = await bot.get_chat_member(chat_id=channel_id, user_id=tg_id)
    except Exception:
        return False
    return member.status in ("creator", "administrator", "member", "restricted")

async def kick_user(bot: Bot, channel_id: int, tg_id: int):
    await _safe_api_call(bot.ban_chat_member(chat_id=channel_id, user_id=tg_id))
    await _safe_api_call(bot.unban_chat_member(chat_id=channel_id, user_id=tg_id, only_if_banned=True))

async def check_bot_permissions(bot: Bot, channel_id: int) -> list[str]:
    try:
        member = await bot.get_chat_member(chat_id=channel_id, user_id=bot.id)
    except Exception:
        return ["not_in_channel"]
        
    if member.status != "administrator":
        return ["not_admin"]
        
    missing = []
    if not getattr(member, 'can_invite_users', False):
        missing.append("can_invite_users")
    if not getattr(member, 'can_restrict_members', False):
        missing.append("can_restrict_members")
        
    return missing
