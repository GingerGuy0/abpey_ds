import asyncio
import logging
import signal
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.utils.callback_answer import CallbackAnswerMiddleware
from aiogram.types import BotCommand, MenuButtonCommands
from app.config import settings
from app.db.session import init_db, close_db, async_session
from app.chains.registry import registry
from app.services import access
from app.services.notify import notify_admins
from app.workers import setup_scheduler, shutdown_scheduler
from app.middlewares import LoggingMiddleware, DbSessionMiddleware, UserMiddleware, ThrottleMiddleware
from app.handlers import admin, join, payment, user

async def seed_wallets():
    try:
        from app.services import repo
        from app.config import NETWORKS
        import re
        
        seeded = []
        async with async_session() as session:
            for key, cfg in NETWORKS.items():
                env_val = settings.wallet_from_env(key)
                if not env_val:
                    continue
                    
                existing = await repo.get_wallet(session, key)
                if existing:
                    continue
                    
                if not re.match(cfg.address_regex, env_val):
                    logging.error(f"Invalid wallet address format for {key} in .env: {env_val}")
                    continue
                    
                await repo.set_wallet(session, key, env_val)
                seeded.append(key)
                
        if seeded:
            logging.info(f"Seeded initial wallets for networks: {', '.join(seeded)}")
    except Exception as e:
        logging.error(f"Error seeding wallets: {e}", exc_info=True)

async def _shutdown(dp: Dispatcher, sig) -> None:
    logging.info("Отримано %s — коректно зупиняю бота", getattr(sig, "name", sig))
    await dp.stop_polling()


async def main():
    logging.basicConfig(level=settings.log_level, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")

    for noisy in ("aiosqlite", "sqlalchemy.engine", "apscheduler.scheduler",
                  "apscheduler.executors.default", "aiohttp.client", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    
    bot = Bot(token=settings.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    
    dp.update.middleware(LoggingMiddleware())
    dp.update.middleware(DbSessionMiddleware(async_session))
    dp.update.middleware(UserMiddleware())
    dp.message.middleware(ThrottleMiddleware(1.5))
    dp.callback_query.middleware(ThrottleMiddleware(1.5))
    dp.callback_query.middleware(CallbackAnswerMiddleware())
    
    dp.include_router(admin.router)
    dp.include_router(join.router)
    dp.include_router(payment.router)
    dp.include_router(user.router)
    
    await init_db()
    await seed_wallets()
    registry.start()
    
    await bot.set_my_commands([
        BotCommand(command="start", description="Головне меню"),
        BotCommand(command="menu", description="Головне меню"),
        BotCommand(command="help", description="Підтримка")
    ])
    await bot.set_chat_menu_button(menu_button=MenuButtonCommands())
    
    if settings.channel_id != 0:
        missing_perms = await access.check_bot_permissions(bot, settings.channel_id)
        if missing_perms:
            logging.error(f"Missing permissions in channel: {missing_perms}")
            await notify_admins(bot, f"⚠️ Боту бракує прав у каналі: {missing_perms}")

    setup_scheduler(bot)

    admins = settings.admin_id_list
    logging.info("Адміни: %s", ", ".join(str(a) for a in admins) or "не задані")
    await notify_admins(bot, f"Бот запущено. Адмінів: {len(admins)}")
    
    # Хостинг зупиняє контейнер через SIGTERM (Koyeb, Docker, systemd). Без
    # обробника процес просто вбивають, і блок finally нижче не виконується:
    # HTTP-сесії й з'єднання з БД лишаються обірваними на півслові.
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, lambda s=sig: asyncio.create_task(_shutdown(dp, s)))
        except NotImplementedError:
            pass  # Windows

    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types(), drop_pending_updates=True)
    finally:
        shutdown_scheduler()
        await registry.close()
        await close_db()
        await bot.session.close()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logging.info("Bot stopped")
