import logging
from datetime import datetime, timezone
from aiogram import Router, F, Bot
from aiogram.types import Message, CallbackQuery
from aiogram.filters import CommandStart, Command, CommandObject, StateFilter
from aiogram.exceptions import TelegramBadRequest
from sqlalchemy.ext.asyncio import AsyncSession
from app.db.models import User
from app.locales.uk import t
from app.keyboards import main_kb, plans_kb, networks_kb, invoice_kb, MenuCB, PlanCB, NetworkCB, InvoiceCB
from app.keyboards import BTN_BUY, BTN_SUB, BTN_SUPPORT, BTN_LINK
from app.services import repo, payments, qr
from app.services.amounts import format_amount
from app.services.access import reissue_invite, AccessError
from app.config import PLANS

router = Router(name="user")

async def get_has_sub(session, user_id):
    sub = await repo.get_active_subscription(session, user_id)
    return sub is not None

async def render(message: Message, text: str, kb):
    try:
        await message.edit_text(text, reply_markup=kb)
    except TelegramBadRequest as e:
        if "message is not modified" in str(e).lower():
            pass
        else:
            try:
                await message.delete()
            except Exception:
                pass
            await message.answer(text, reply_markup=kb)
    except Exception:
        try:
            await message.delete()
        except Exception:
            pass
        await message.answer(text, reply_markup=kb)

async def screen_main(message: Message, session: AsyncSession, user: User):
    has_sub = await get_has_sub(session, user.id)
    await message.answer(t("start"), reply_markup=main_kb(has_sub))

async def screen_plans(message: Message, session: AsyncSession, edit: bool = False):
    prices = {}
    for plan in PLANS:
        prices[plan] = await repo.get_price(session, plan)
    
    text = t("choose_plan")
    kb = plans_kb(prices)
    
    if edit:
        await render(message, text, kb)
    else:
        await message.answer(text, reply_markup=kb)

async def screen_my_sub(message: Message, session: AsyncSession, user: User):
    sub = await repo.get_active_subscription(session, user.id)
    if sub:
        now = datetime.now(timezone.utc)
        days = max(0, (sub.expires_at - now).days)
        date_str = sub.expires_at.strftime("%Y-%m-%d %H:%M")
        text = t("sub_active", date=date_str, days=days)
        has_sub = True
    else:
        text = t("sub_inactive")
        has_sub = False
        
    await message.answer(text, reply_markup=main_kb(has_sub))

async def show_invoice(query: CallbackQuery, session: AsyncSession, user: User, plan: str, network: str):
    inv = await payments.create_invoice(session, user, plan, network)
    
    from app.config import NETWORKS
    cfg = NETWORKS[inv.network]
    now = datetime.now(timezone.utc)
    minutes_left = max(0, int((inv.expires_at - now).total_seconds() / 60))
    
    text = t("invoice", 
             network=cfg.title, 
             address=inv.pay_address, 
             amount=format_amount(inv.expected_amount),
             minutes=minutes_left)
             
    dl = qr.deep_link(inv.network, inv.pay_address, format_amount(inv.expected_amount), inv.expected_units)
    kb = invoice_kb(inv.id, dl)
    
    qr_file = await qr.make_address_qr(inv.pay_address)
    
    try:
        await query.message.delete()
    except Exception:
        pass
    await query.message.answer_photo(photo=qr_file, caption=text, reply_markup=kb)

@router.message(CommandStart())
async def cmd_start(message: Message, session: AsyncSession, user: User, command: CommandObject):
    try:
        if command.args == "buy":
            await screen_plans(message, session, False)
        elif command.args == "my_sub":
            await screen_my_sub(message, session, user)
        else:
            await screen_main(message, session, user)
    except Exception as e:
        logging.error(f"Error in start: {e}", exc_info=True)
        await message.answer(t("error_generic"))

@router.message(Command("menu"))
async def cmd_menu(message: Message, session: AsyncSession, user: User):
    await cmd_start(message, session, user, CommandObject(prefix="/", command="menu", mention=""))

@router.message(F.text == BTN_BUY)
async def handle_buy(message: Message, session: AsyncSession):
    try:
        await screen_plans(message, session, False)
    except Exception as e:
        logging.error(f"Error in buy: {e}", exc_info=True)
        await message.answer(t("error_generic"))

@router.message(F.text == BTN_SUB)
async def handle_my_sub(message: Message, session: AsyncSession, user: User):
    try:
        await screen_my_sub(message, session, user)
    except Exception as e:
        logging.error(f"Error in my_sub: {e}", exc_info=True)
        await message.answer(t("error_generic"))

@router.message(F.text == BTN_LINK)
async def handle_get_link(message: Message, session: AsyncSession, user: User, bot: Bot):
    try:
        sub = await repo.get_active_subscription(session, user.id)
        if not sub:
            await message.answer(t("sub_inactive"))
            return
            
        link = await reissue_invite(bot, session, sub)
        await message.answer(t("success_invite", link=link))
    except AccessError as e:
        if "too_frequent" in str(e):
            await message.answer("Запит посилання можливий 1 раз на годину")
        else:
            logging.error(f"Error reissuing invite: {e}", exc_info=True)
            await message.answer(t("error_generic"))
    except Exception as e:
        logging.error(f"Error in get_link: {e}", exc_info=True)
        await message.answer(t("error_generic"))

@router.message(F.text == BTN_SUPPORT)
@router.message(Command("help", "support"))
async def handle_support(message: Message):
    try:
        await message.answer("Зв'язок з підтримкою: @d")
    except Exception as e:
        logging.error(f"Error in support: {e}", exc_info=True)
        await message.answer(t("error_generic"))

@router.callback_query(PlanCB.filter(F.action == "select"))
async def cb_plan_select(query: CallbackQuery, callback_data: PlanCB, session: AsyncSession, user: User, bot: Bot):
    try:
        from app.chains.registry import registry
        nets = await registry.available(session)
        if len(nets) == 0:
            from aiogram.exceptions import TelegramAPIError
            try:
                await query.answer(t("payment_unavailable"), show_alert=True)
            except TelegramAPIError:
                pass
            from app.services.notify import notify_admins
            await notify_admins(bot, "⚠️ Платежі недоступні: жодна мережа не налаштована або немає гаманця!")
            return
        elif len(nets) == 1:
            await show_invoice(query, session, user, callback_data.plan, nets[0])
        else:
            await render(query.message, t("choose_network"), networks_kb(callback_data.plan, nets))
    except Exception as e:
        logging.error(f"Error in plan_select: {e}", exc_info=True)
        await query.message.answer(t("error_generic"))

@router.callback_query(PlanCB.filter(F.action == "back"))
async def cb_plan_back(query: CallbackQuery, session: AsyncSession):
    await screen_plans(query.message, session, edit=True)

@router.callback_query(MenuCB.filter(F.action == "close"))
async def cb_menu_close(query: CallbackQuery):
    try:
        await query.message.delete()
    except Exception:
        pass

@router.callback_query(NetworkCB.filter())
async def cb_network_select(query: CallbackQuery, callback_data: NetworkCB, session: AsyncSession, user: User):
    try:
        from app.chains.registry import registry
        nets = await registry.available(session)
        if callback_data.network not in nets:
            from aiogram.exceptions import TelegramAPIError
            try:
                await query.answer(t("network_unavailable"), show_alert=True)
            except TelegramAPIError:
                pass
            return
        await show_invoice(query, session, user, callback_data.plan, callback_data.network)
    except payments.PaymentError as e:
        from aiogram.exceptions import TelegramAPIError
        try:
            await query.answer(str(e), show_alert=True)
        except TelegramAPIError:
            pass
    except Exception as e:
        logging.error(f"Error creating invoice: {e}", exc_info=True)
        await query.message.answer(t("error_generic"))

@router.callback_query(InvoiceCB.filter(F.action == "cancel"))
async def cb_invoice_cancel(query: CallbackQuery, callback_data: InvoiceCB, session: AsyncSession, user: User):
    try:
        inv = await repo.get_invoice(session, callback_data.invoice_id)
        if inv and inv.status in ("pending", "confirming"):
            inv.status = "cancelled"
            await session.commit()
            
        try:
            await query.message.delete()
        except Exception:
            pass
        await screen_main(query.message, session, user)
    except Exception as e:
        logging.error(f"Error cancelling invoice: {e}", exc_info=True)
        await query.message.answer(t("error_generic"))

@router.message(StateFilter(None))
async def fallback(message: Message, session: AsyncSession, user: User):
    await screen_main(message, session, user)
