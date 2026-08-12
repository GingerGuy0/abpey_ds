"""Сповіщення адмінам.

Окремий модуль, щоб не дублювати try/except навколо кожного send_message:
адмін міг не стартувати бота або заблокувати його, і це не привід валити
обробку платежу.
"""

import asyncio
import logging

from aiogram import Bot

from app.config import settings


async def notify_admins(bot: Bot, text: str) -> int:
    """Надіслати повідомлення всім адмінам. Повертає кількість доставлених."""
    delivered = 0
    for tg_id in settings.admin_id_list:
        try:
            await bot.send_message(tg_id, text)
            delivered += 1
        except Exception as e:
            logging.warning("Не вдалося сповістити адміна %s: %s", tg_id, e)
        await asyncio.sleep(0.05)
    return delivered
