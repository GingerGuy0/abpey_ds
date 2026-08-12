import logging
from datetime import datetime, timezone
from decimal import Decimal
from aiogram import Router, F, Bot
from aiogram.types import Message, CallbackQuery
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from sqlalchemy.ext.asyncio import AsyncSession
from app.db.models import User
from app.locales.uk import t
from app.keyboards import InvoiceCB, cancel_kb, MenuCB, main_kb, retry_hash_kb
from app.services import repo, payments, delivery
from app.services.amounts import format_amount
from app.services.notify import notify_admins

router = Router(name="payment")

class PaymentStates(StatesGroup):
    waiting_hash = State()

@router.callback_query(InvoiceCB.filter(F.action == "paid"))
async def cb_paid(query: CallbackQuery, callback_data: InvoiceCB, state: FSMContext, session: AsyncSession):
    try:
        inv = await repo.get_invoice(session, callback_data.invoice_id)
        if not inv or inv.status not in ("pending", "confirming"):
            from aiogram.exceptions import TelegramAPIError
            try:
                await query.answer(t("wrong_state"), show_alert=True)
            except TelegramAPIError:
                pass
            return
            
        await state.update_data(invoice_id=inv.id)
        await state.set_state(PaymentStates.waiting_hash)
        await query.message.answer(t("ask_hash"), reply_markup=cancel_kb())
    except Exception as e:
        logging.error(f"Error in paid cb: {e}", exc_info=True)
        await query.message.answer(t("error_generic"))

@router.callback_query(MenuCB.filter(F.action == "cancel"), PaymentStates.waiting_hash)
async def cb_cancel_hash(query: CallbackQuery, state: FSMContext, session: AsyncSession, user: User):
    try:
        await state.clear()
        try:
            await query.message.delete()
        except Exception:
            pass
        sub = await repo.get_active_subscription(session, user.id)
        await query.message.answer(t("start"), reply_markup=main_kb(sub is not None))
    except Exception as e:
        logging.error(f"Error cancelling hash input: {e}", exc_info=True)
        await query.message.answer(t("error_generic"))

@router.message(PaymentStates.waiting_hash)
async def process_hash(message: Message, state: FSMContext, session: AsyncSession, bot: Bot):
    if not message.text:
        await message.answer("Будь ласка, надішли текст (хеш або посилання).", reply_markup=cancel_kb())
        return
        
    try:
        data = await state.get_data()
        invoice_id = data.get("invoice_id")
        if not invoice_id:
            await state.clear()
            return
            
        inv = await repo.get_invoice(session, invoice_id)
        if not inv:
            await state.clear()
            return
            
        msg = await message.answer(t("checking"))
        
        result = await payments.check_by_hash(session, inv, message.text)
        
        if result.status == "paid":
            await state.clear()
            try:
                await msg.delete()
            except Exception:
                pass
            # Ту саму доставку робить і фоновий сканер — тримаємо її в одному
            # місці, щоб повідомлення не розходились між шляхами.
            await delivery.deliver_access(
                bot, session, inv, tx=result.tx,
                overpay=(result.extra or {}).get("overpay") or Decimal(0),
                source="хеш від юзера",
            )
        elif result.status == "confirming":
            await state.clear()
            await msg.edit_text(t("confirming"))
        elif result.status == "underpaid":
            extra = result.extra or {}
            left = inv.expires_at - datetime.now(timezone.utc)
            minutes = max(0, int(left.total_seconds() // 60))
            await msg.edit_text(t(
                "underpaid",
                expected=format_amount(extra["expected"]),
                credited=format_amount(extra["credited"]),
                missing=format_amount(extra["missing"]),
                minutes=minutes,
            ), reply_markup=retry_hash_kb(inv.id))
            kind = "Доплата, все ще не вистачає" if extra.get("topup") else "Недоплата"
            await notify_admins(
                bot,
                f"⚠️ {kind}\nЮзер: {message.from_user.full_name} (@{message.from_user.username})\n"
                f"Мережа: {inv.network}\nОчікувалось: {format_amount(extra['expected'])} USDT\n"
                f"Цим переказом: {format_amount(extra['received'])} USDT\n"
                f"Зараховано разом: {format_amount(extra['credited'])} USDT\n"
                f"Не вистачає: {format_amount(extra['missing'])} USDT\n"
                f"Інвойс #{inv.id} лишається активним."
            )
        elif result.status == "partial_unconfirmed":
            extra = result.extra or {}
            await msg.edit_text(
                t("partial_unconfirmed", received=format_amount(extra["received"])),
                reply_markup=retry_hash_kb(inv.id),
            )
        elif result.status in ("chain_rate_limited", "chain_unavailable", "chain_auth_error", "chain_error"):
            text = t(result.status) + "\n\n(Не хвилюйся, платіж не загубиться — бот сам сканує гаманець кожні 40 секунд.)"
            await msg.edit_text(text, reply_markup=retry_hash_kb(inv.id))
            if result.status == "chain_auth_error":
                await notify_admins(bot, f"⚠️ Помилка доступу до API блокчейну для {inv.network}:\n{result.extra.get('detail')}")
        else:
            await msg.edit_text(t(result.status))
            
    except Exception as e:
        logging.error(f"Error processing hash: {e}", exc_info=True)
        await message.answer(t("error_generic"))
