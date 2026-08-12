"""Унікальні суми і зіставлення платежів.

Кожному активному інвойсу видається своя сума (29.46, 29.48, ...), тож вхідну
транзакцію можна прив'язати до конкретного інвойса просто за сумою — без
довіри до хеша, який людина могла підгледіти в експлорері.

Суми видаються по сітці з кроком AMOUNT_STEP, а не випадковим хвостом до
0.0001. Причина: вікно допуску мусить бути вужчим за відстань між сумами,
інакше один платіж «підходить» кільком інвойсам і автосканер може зарахувати
його чужому користувачу. Сітка гарантує цю відстань, випадковий хвіст — ні.
"""

from decimal import Decimal, getcontext, ROUND_DOWN
import random
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import func
from app.db.models import Invoice
from app.config import AMOUNT_STEP, AUTO_MATCH_WINDOW, MAX_UNDERPAY_USD

getcontext().prec = 40

# Скільки кроків сітки міститься в одиниці валюти: 1 / 0.02 = 50 слотів,
# з них 1..49 (нульовий хвіст не беремо, щоб сума не збігалася з рівною ціною).
_SLOTS = int(Decimal("1") / AMOUNT_STEP)

class AmountAllocationError(Exception):
    pass

def to_units(amount: Decimal, decimals: int) -> int:
    return int(amount.scaleb(decimals).to_integral_value(rounding=ROUND_DOWN))

def from_units(units: int | str, decimals: int) -> Decimal:
    return Decimal(units).scaleb(-decimals)

async def allocate_unique_amount(session: AsyncSession, network: str, base_price: Decimal) -> Decimal:
    stmt = select(Invoice.expected_amount).where(
        Invoice.network == network,
        Invoice.status.in_(["pending", "confirming"]),
        Invoice.expires_at > func.now()
    )
    result = await session.execute(stmt)
    active_amounts = {Decimal(row[0]) for row in result.all()}

    free_slots = []
    for slot in range(1, _SLOTS):
        candidate = (base_price + AMOUNT_STEP * slot).quantize(Decimal("0.0001"))
        # Слот зайнятий, якщо якийсь активний інвойс стоїть до нього ближче,
        # ніж на крок сітки (сюди ж потрапляють інвойси зі старою, «випадковою»
        # сумою, які ще живуть у БД після оновлення).
        if any(abs(candidate - a) < AMOUNT_STEP for a in active_amounts):
            continue
        free_slots.append(candidate)

    if not free_slots:
        raise AmountAllocationError(
            f"Немає вільних сум для {network}: активних інвойсів {len(active_amounts)}, слотів {_SLOTS - 1}"
        )

    return random.choice(free_slots)

def is_amount_acceptable(received: Decimal, expected: Decimal) -> bool:
    """Чи достатньо грошей прийшло по КОНКРЕТНОМУ інвойсу.

    Використовується там, де інвойс уже відомий (перевірка за хешем). Недоплату
    прощаємо в межах комісії бірж, переплату не обмежуємо взагалі — людина
    отримує доступ, а різницю за бажанням поверне підтримка.
    """
    return received >= expected - MAX_UNDERPAY_USD

def matches_exactly(received: Decimal, expected: Decimal) -> bool:
    """Чи це та сама сума з точністю до вікна автозіставлення."""
    return abs(received - expected) <= AUTO_MATCH_WINDOW

def match_score(received: Decimal, expected: Decimal) -> Decimal:
    return abs(received - expected)

def format_amount(amount: Decimal) -> str:
    return f"{amount:.4f}"
