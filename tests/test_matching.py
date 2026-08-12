"""Зіставлення платежів за сумою.

Головна вимога: краще віддати платіж на ручний розбір, ніж зарахувати його
чужому користувачу. Тому автосканер приймає лише однозначний збіг.
"""

import sys
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.chains.base import IncomingTx  # noqa: E402
from app.config import AMOUNT_STEP  # noqa: E402
from app.db.models import Invoice  # noqa: E402
from app.services.payments import pick_invoice  # noqa: E402


def tx(amount: str) -> IncomingTx:
    return IncomingTx("hash", "bsc", "from", "to", Decimal(amount), 0, 10,
                      datetime.now(timezone.utc), True)


# Так виглядає сітка сум при ціні 29 USDT і кроці 0.02.
BUSY_GRID = [Invoice(id=i, expected_amount=Decimal("29") + AMOUNT_STEP * i) for i in range(1, 11)]


def test_exact_amount_matches_its_own_invoice():
    inv, reason = pick_invoice(tx("29.06"), BUSY_GRID)
    assert reason == "exact"
    assert inv.expected_amount == Decimal("29.06")


def test_tiny_rounding_still_matches():
    """Округлення при конвертації units -> Decimal не має ламати збіг."""
    inv, reason = pick_invoice(tx("29.0599"), BUSY_GRID)
    assert reason == "exact"
    assert inv.expected_amount == Decimal("29.06")


def test_overpay_is_not_given_to_a_stranger():
    """Переплата не має зараховуватись сусідньому інвойсу."""
    inv, reason = pick_invoice(tx("29.10"), [i for i in BUSY_GRID if i.expected_amount != Decimal("29.10")])
    assert inv is None
    assert reason == "none"


def test_rounded_payment_goes_to_manual_review():
    """Людина округлила до 30.00 — це не привід дарувати підписку іншому."""
    inv, reason = pick_invoice(tx("30.00"), BUSY_GRID)
    assert inv is None
    assert reason == "none"


def test_big_overpay_goes_to_manual_review():
    inv, reason = pick_invoice(tx("34.14"), BUSY_GRID)
    assert inv is None
    assert reason == "none"


def test_underpay_goes_to_manual_review():
    """Недоплату автосканер не вгадує — це робота перевірки за хешем."""
    inv, reason = pick_invoice(tx("29.05"), BUSY_GRID)
    assert inv is None
    assert reason == "none"


def test_duplicate_amounts_are_ambiguous():
    """Якщо в БД лишились інвойси зі старими сумами й вони збіглися — ручний розбір."""
    dupes = [
        Invoice(id=1, expected_amount=Decimal("29.4626")),
        Invoice(id=2, expected_amount=Decimal("29.4626")),
    ]
    inv, reason = pick_invoice(tx("29.4626"), dupes)
    assert inv is None
    assert reason == "ambiguous"


def test_no_candidates():
    assert pick_invoice(tx("29.06"), []) == (None, "none")
