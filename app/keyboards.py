from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardMarkup, KeyboardButton
from aiogram.filters.callback_data import CallbackData

BTN_BUY = "💳 Купити підписку"
BTN_SUB = "📋 Моя підписка"
BTN_SUPPORT = "🆘 Підтримка"
BTN_LINK = "🔗 Отримати посилання"

class PlanCB(CallbackData, prefix="plan"):
    action: str
    plan: str = ""

class NetworkCB(CallbackData, prefix="net"):
    plan: str
    network: str

class InvoiceCB(CallbackData, prefix="inv"):
    action: str
    invoice_id: int

class AdminCB(CallbackData, prefix="adm"):
    action: str
    arg: str = ""

class MenuCB(CallbackData, prefix="menu"):
    action: str

def main_kb(has_sub: bool) -> ReplyKeyboardMarkup:
    kb = [
        [KeyboardButton(text=BTN_BUY), KeyboardButton(text=BTN_SUB)],
        [KeyboardButton(text=BTN_SUPPORT)]
    ]
    if has_sub:
        kb.insert(1, [KeyboardButton(text=BTN_LINK)])
    return ReplyKeyboardMarkup(
        keyboard=kb,
        resize_keyboard=True,
        is_persistent=True
    )

def plans_kb(prices: dict) -> InlineKeyboardMarkup:
    from app.config import PLANS
    buttons = []
    for plan, price in prices.items():
        title = PLANS.get(plan, {}).get("title", plan)
        buttons.append([InlineKeyboardButton(text=f"{title} - {price} usdt", callback_data=PlanCB(action="select", plan=plan).pack())])
    buttons.append([InlineKeyboardButton(text="Назад", callback_data=MenuCB(action="close").pack())])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def networks_kb(plan: str, available: list[str]) -> InlineKeyboardMarkup:
    from app.config import NETWORKS
    buttons = []
    for net in available:
        cfg = NETWORKS[net]
        buttons.append([InlineKeyboardButton(text=f"{cfg.emoji} {cfg.title}", callback_data=NetworkCB(plan=plan, network=net).pack())])
    buttons.append([InlineKeyboardButton(text="Назад", callback_data=PlanCB(action="back").pack())])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def invoice_kb(invoice_id: int, deep_link: str | None) -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton(text="✅ Я оплатив", callback_data=InvoiceCB(action="paid", invoice_id=invoice_id).pack())]
    ]
    if deep_link and deep_link.startswith(("http://", "https://", "tg://")):
        buttons.append([InlineKeyboardButton(text="🔗 Відкрити в гаманці", url=deep_link)])
    buttons.append([InlineKeyboardButton(text="❌ Скасувати", callback_data=InvoiceCB(action="cancel", invoice_id=invoice_id).pack())])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def retry_hash_kb(invoice_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔄 Спробувати ще раз", callback_data=InvoiceCB(action="paid", invoice_id=invoice_id).pack())],
        [InlineKeyboardButton(text="❌ Скасувати", callback_data=MenuCB(action="cancel").pack())]
    ])

def cancel_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ Скасувати", callback_data=MenuCB(action="cancel").pack())]
    ])

def admin_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Статистика", callback_data=AdminCB(action="stats").pack())],
        [InlineKeyboardButton(text="Розсилка", callback_data=AdminCB(action="broadcast").pack())]
    ])

def extendall_confirm_kb(days: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"✅ Продовжити на {days} дн.",
                              callback_data=AdminCB(action="extendall_confirm", arg=str(days)).pack())],
        [InlineKeyboardButton(text="❌ Скасувати",
                              callback_data=AdminCB(action="extendall_cancel").pack())]
    ])

def broadcast_confirm_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Підтвердити", callback_data=AdminCB(action="broadcast_confirm").pack())],
        [InlineKeyboardButton(text="❌ Скасувати", callback_data=AdminCB(action="broadcast_cancel").pack())]
    ])
