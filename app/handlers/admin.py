import logging
import asyncio
from aiogram import Router, F, Bot
from aiogram.types import Message, CallbackQuery
from aiogram.filters import Command, BaseFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from sqlalchemy.ext.asyncio import AsyncSession
from app.db.models import User
from app.locales.uk import t
from app.keyboards import admin_menu, AdminCB, broadcast_confirm_kb, extendall_confirm_kb
from app.services import repo, access
from app.services.amounts import format_amount
from app.config import settings, NETWORKS, PLANS
from decimal import Decimal

router = Router(name="admin")

class IsAdmin(BaseFilter):
    """Власник + усі з ADMIN_IDS. Права однакові."""

    async def __call__(self, event) -> bool:
        user_id = event.from_user.id if getattr(event, "from_user", None) else None
        return settings.is_admin(user_id)

router.message.filter(IsAdmin())
router.callback_query.filter(IsAdmin())

class BroadcastStates(StatesGroup):
    waiting_text = State()

@router.message(Command("admin"))
async def cmd_admin(message: Message):
    await message.answer(t("admin_menu"), reply_markup=admin_menu())

@router.message(Command("grant"))
async def cmd_grant(message: Message, session: AsyncSession, bot: Bot):
    args = message.text.split()
    if len(args) != 3:
        await message.answer(t("usage_grant"))
        return
        
    target, days_str = args[1], args[2]
    try:
        days = int(days_str)
        if not 1 <= days <= 3650:
            raise ValueError()
    except ValueError:
        await message.answer("Days must be 1..3650")
        return
        
    if target.startswith("@"):
        target_user = await repo.get_user_by_username(session, target)
    else:
        try:
            target_user = await repo.get_user_by_tg_id(session, int(target))
        except ValueError:
            target_user = None
            
    if not target_user:
        await message.answer("User not found in DB")
        return
        
    sub = await repo.create_or_extend_subscription(session, target_user.id, days, "admin_gift", "custom")
    link = await access.issue_invite(bot, settings.channel_id, sub.id, session)
    
    await repo.log_admin_action(session, admin_tg_id=message.from_user.id, action="grant", target_user_id=target_user.id, payload={"days": days})
    
    try:
        await bot.send_message(target_user.tg_id, t("success_invite", link=link))
    except Exception as e:
        await message.answer(f"Sub granted but failed to send message: {e}")
        
    await message.answer(t("granted", days=days))

@router.message(Command("setwallet"))
async def cmd_setwallet(message: Message, session: AsyncSession):
    args = message.text.split()
    if len(args) != 3:
        await message.answer(t("usage_setwallet"))
        return
        
    net, address = args[1].lower(), args[2]
    if net not in NETWORKS:
        await message.answer(f"Unknown network. Valid: {list(NETWORKS.keys())}")
        return
        
    import re
    if not re.match(NETWORKS[net].address_regex, address):
        await message.answer("Invalid address format for this network")
        return
        
    old = await repo.get_wallet(session, net)
    await repo.set_wallet(session, net, address)
    
    from app.chains.registry import registry
    registry.invalidate_available_cache()
    
    await repo.log_admin_action(session, admin_tg_id=message.from_user.id, action="setwallet", target_user_id=None, payload={"network": net, "address": address})
    
    await message.answer(t("wallet_updated", network=net, old=old or "None", new=address))

@router.callback_query(AdminCB.filter(F.action == "stats"))
async def cb_stats(query: CallbackQuery, session: AsyncSession):
    try:
        data = await repo.get_stats(session)
        data["revenue_day"] = format_amount(Decimal(data["revenue_day"]))
        data["revenue_month"] = format_amount(Decimal(data["revenue_month"]))
        await query.message.answer(t("stats", **data))
    except Exception as e:
        logging.error(f"Error building stats: {e}", exc_info=True)
        await query.message.answer(t("error_generic"))


@router.message(Command("stats"))
async def cmd_stats(message: Message, session: AsyncSession):
    data = await repo.get_stats(session)
    data["revenue_day"] = format_amount(Decimal(data["revenue_day"]))
    data["revenue_month"] = format_amount(Decimal(data["revenue_month"]))
    await message.answer(t("stats", **data))


@router.message(Command("extendall"))
async def cmd_extendall(message: Message, session: AsyncSession, bot: Bot):
    """Крок 1: рахуємо, кого це зачепить, і показуємо прев'ю.

    Продовжуємо лише тим, хто РЕАЛЬНО в каналі, а не всім, хто колись відкрив
    бота. Тому кожну активну підписку звіряємо через get_chat_member —
    списку учасників каналу Bot API не дає.
    """
    args = message.text.split()
    if len(args) != 2:
        await message.answer(t("usage_extendall"))
        return

    try:
        days = int(args[1])
        if not 1 <= days <= 365:
            raise ValueError()
    except ValueError:
        await message.answer("Днів має бути ціле число 1..365")
        return

    if not settings.channel_id:
        await message.answer("CHANNEL_ID не налаштовано")
        return

    subs = await repo.list_active_subscriptions(session)
    if not subs:
        await message.answer("Активних підписок немає")
        return

    status_msg = await message.answer(f"Перевіряю, хто з {len(subs)} підписників зараз у каналі...")

    members = 0
    for sub in subs:
        if await access.is_channel_member(bot, settings.channel_id, sub.user.tg_id):
            members += 1
        await asyncio.sleep(0.05)

    await status_msg.edit_text(
        t("extendall_preview", days=days, total=len(subs), members=members, missing=len(subs) - members),
        reply_markup=extendall_confirm_kb(days),
    )


@router.callback_query(AdminCB.filter(F.action == "extendall_confirm"))
async def cb_extendall_confirm(query: CallbackQuery, callback_data: AdminCB, session: AsyncSession, bot: Bot):
    try:
        days = int(callback_data.arg)
    except (TypeError, ValueError):
        await query.answer("Некоректна кількість днів", show_alert=True)
        return

    await query.message.edit_reply_markup()
    await query.message.answer(f"Продовжую на {days} дн...")

    extended = notified = failed = 0

    for sub in await repo.list_active_subscriptions(session):
        tg_id = sub.user.tg_id
        # Перевіряємо ще раз: між прев'ю і підтвердженням хтось міг вийти.
        if not await access.is_channel_member(bot, settings.channel_id, tg_id):
            continue

        new_expires = await repo.extend_subscription(session, sub.id, days)
        if new_expires is None:
            continue
        extended += 1

        try:
            await bot.send_message(
                tg_id,
                t("extended_notice", days=days, date=new_expires.strftime("%Y-%m-%d %H:%M")),
            )
            notified += 1
        except Exception as e:
            failed += 1
            logging.warning("Не вдалося повідомити %s про продовження: %s", tg_id, e)

        await asyncio.sleep(0.05)

    await repo.log_admin_action(
        session, admin_tg_id=query.from_user.id, action="extendall",
        target_user_id=None, payload={"days": days, "extended": extended},
    )

    await query.message.answer(t("extendall_done", extended=extended, notified=notified, failed=failed))


@router.callback_query(AdminCB.filter(F.action == "extendall_cancel"))
async def cb_extendall_cancel(query: CallbackQuery):
    await query.message.edit_reply_markup()
    await query.answer("Скасовано")


@router.callback_query(AdminCB.filter(F.action == "broadcast"))
async def cb_broadcast(query: CallbackQuery, state: FSMContext):
    await state.set_state(BroadcastStates.waiting_text)
    await query.message.answer("Надішли текст для розсилки (HTML підтримується):")
    await query.answer()

@router.message(BroadcastStates.waiting_text)
async def process_broadcast_text(message: Message, state: FSMContext, session: AsyncSession):
    text = message.html_text
    await state.update_data(text=text)
    
    users_count = await repo.count_users(session)
    await message.answer(f"Прев'ю:\n\n{text}\n\nОдержувачів: ~{users_count}\nПідтвердити?", reply_markup=broadcast_confirm_kb())

@router.callback_query(AdminCB.filter(F.action == "broadcast_confirm"))
async def cb_broadcast_confirm(query: CallbackQuery, state: FSMContext, session: AsyncSession, bot: Bot):
    data = await state.get_data()
    text = data.get("text")
    if not text:
        await query.answer("Немає тексту", show_alert=True)
        return
        
    await state.clear()
    await query.message.edit_reply_markup()
    await query.message.answer(t("broadcast_started"))
    
    ok, blocked, failed = 0, 0, 0
    
    async for tg_id in repo.iter_active_user_ids(session):
        try:
            await bot.send_message(tg_id, text)
            ok += 1
        except Exception as e:
            if "Forbidden" in str(e):
                blocked += 1
                user = await repo.get_user_by_tg_id(session, tg_id)
                if user:
                    await repo.mark_bot_blocked(session, user.id)
            else:
                failed += 1
        await asyncio.sleep(0.04)
        
    await query.message.answer(t("broadcast_done", ok=ok, blocked=blocked, failed=failed))
    
@router.callback_query(AdminCB.filter(F.action == "broadcast_cancel"))
async def cb_broadcast_cancel(query: CallbackQuery, state: FSMContext):
    await state.clear()
    await query.message.edit_reply_markup()
    await query.answer("Скасовано")
