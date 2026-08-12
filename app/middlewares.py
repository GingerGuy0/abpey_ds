import logging
import time
from aiogram import BaseMiddleware
from aiogram.types import TelegramObject, Update, CallbackQuery
from typing import Callable, Dict, Any, Awaitable
from sqlalchemy.ext.asyncio import async_sessionmaker
from app.services import repo

class LoggingMiddleware(BaseMiddleware):
    async def __call__(self, handler: Callable[[Update, Dict[str, Any]], Awaitable[Any]], event: Update, data: Dict[str, Any]) -> Any:
        start_time = time.time()
        user_id = event.message.from_user.id if event.message else (event.callback_query.from_user.id if event.callback_query else None)
        event_type = event.event_type
        
        try:
            result = await handler(event, data)
            process_time = time.time() - start_time
            logging.info(f"Update {event.update_id} | Type: {event_type} | User: {user_id} | Time: {process_time:.3f}s")
            return result
        except Exception as e:
            process_time = time.time() - start_time
            logging.error(f"Error processing update {event.update_id} | User: {user_id} | Time: {process_time:.3f}s | Error: {e}")
            raise

class DbSessionMiddleware(BaseMiddleware):
    def __init__(self, session_maker: async_sessionmaker):
        self.session_maker = session_maker

    async def __call__(self, handler: Callable[[Update, Dict[str, Any]], Awaitable[Any]], event: Update, data: Dict[str, Any]) -> Any:
        async with self.session_maker() as session:
            data["session"] = session
            return await handler(event, data)

class UserMiddleware(BaseMiddleware):
    async def __call__(self, handler: Callable[[Update, Dict[str, Any]], Awaitable[Any]], event: Update, data: Dict[str, Any]) -> Any:
        tg_user = None
        if event.message:
            tg_user = event.message.from_user
        elif event.callback_query:
            tg_user = event.callback_query.from_user
            
        if not tg_user:
            return await handler(event, data)
            
        session = data["session"]
        user = await repo.get_or_create_user(session, tg_user)
        
        if user.is_banned:
            if event.callback_query:
                await event.callback_query.answer("Ви заблоковані.", show_alert=True)
            return # Ignore updates from banned users
            
        data["user"] = user
        return await handler(event, data)

class ThrottleMiddleware(BaseMiddleware):
    def __init__(self, rate_limit: float = 1.5, ttl: float = 300.0):
        self.rate_limit = rate_limit
        self.ttl = ttl
        self.cache: Dict[str, float] = {}
        self.last_cb_data: Dict[int, str] = {}
        self._last_cleanup = 0.0

    def _cleanup(self, now: float) -> None:
        if now - self._last_cleanup < self.ttl:
            return
        self._last_cleanup = now
        stale = [k for k, ts in self.cache.items() if now - ts > self.ttl]
        for k in stale:
            self.cache.pop(k, None)

    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: Dict[str, Any],
    ) -> Any:
        tg_user = data.get("event_from_user")
        if tg_user is not None:
            now = time.time()
            self._cleanup(now)
            
            if isinstance(event, CallbackQuery):
                cb_data = event.data or ""
                last_time = self.cache.get(f"{tg_user.id}:cb", 0.0)
                last_data = self.last_cb_data.get(tg_user.id, "")
                
                # Debounce: ignore same button within 0.7s
                if now - last_time < 0.7 and cb_data == last_data:
                    await event.answer()
                    return
                    
                self.cache[f"{tg_user.id}:cb"] = now
                self.last_cb_data[tg_user.id] = cb_data
            else:
                last_time = self.cache.get(f"{tg_user.id}:msg", 0.0)
                if now - last_time < self.rate_limit:
                    return
                self.cache[f"{tg_user.id}:msg"] = now

        return await handler(event, data)
