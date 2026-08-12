import sys
from decimal import Decimal
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import AMOUNT_STEP, AUTO_MATCH_WINDOW, MAX_UNDERPAY_USD  # noqa: E402
from app.services.amounts import (  # noqa: E402
    AmountAllocationError,
    allocate_unique_amount,
    from_units,
    is_amount_acceptable,
    match_score,
    matches_exactly,
    to_units,
)


def test_conversion():
    assert to_units(Decimal("1.23"), 6) == 1230000
    assert from_units(1230000, 6) == Decimal("1.23")

    assert to_units(Decimal("0.0001"), 18) == 100000000000000
    assert from_units(100000000000000, 18) == Decimal("0.0001")


def test_underpay_within_fee_is_accepted():
    """Біржі знімають комісію з суми виводу — таку недоплату прощаємо."""
    expected = Decimal("29.06")
    assert is_amount_acceptable(expected, expected)
    assert is_amount_acceptable(expected - Decimal("0.29"), expected)   # комісія BSC
    assert is_amount_acceptable(expected - MAX_UNDERPAY_USD, expected)


def test_underpay_beyond_limit_is_rejected():
    expected = Decimal("29.06")
    assert not is_amount_acceptable(expected - MAX_UNDERPAY_USD - Decimal("0.01"), expected)
    assert not is_amount_acceptable(Decimal("20"), expected)


def test_overpay_is_always_accepted():
    """Переплата доступ не блокує — різницю за потреби повертає підтримка."""
    expected = Decimal("29.06")
    assert is_amount_acceptable(expected + Decimal("0.5"), expected)
    assert is_amount_acceptable(expected + Decimal("100"), expected)


def test_auto_match_window_is_narrower_than_grid():
    """Інваріант, на якому тримається вся ідентифікація за сумою."""
    assert AUTO_MATCH_WINDOW * 2 < AMOUNT_STEP


def test_matches_exactly():
    assert matches_exactly(Decimal("29.06"), Decimal("29.06"))
    assert matches_exactly(Decimal("29.0599"), Decimal("29.06"))
    assert not matches_exactly(Decimal("29.08"), Decimal("29.06"))
    assert not matches_exactly(Decimal("29.04"), Decimal("29.06"))


def test_match_score():
    expected = Decimal("10.0000")
    assert match_score(Decimal("10.0000"), expected) == 0
    assert match_score(Decimal("10.0500"), expected) == Decimal("0.05")


# ---------- видача унікальних сум ----------

class FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return [(r,) for r in self._rows]


class FakeSession:
    def __init__(self, active):
        self.active = active

    async def execute(self, _stmt):
        return FakeResult(self.active)


@pytest.mark.asyncio
async def test_allocated_amounts_are_spaced_apart():
    """Кожна нова сума має стояти щонайменше на крок від усіх активних."""
    active = []
    session = FakeSession(active)
    for _ in range(20):
        amount = await allocate_unique_amount(session, "bsc", Decimal("29"))
        for other in active:
            assert abs(amount - other) >= AMOUNT_STEP, f"{amount} надто близько до {other}"
        active.append(amount)


@pytest.mark.asyncio
async def test_allocation_avoids_legacy_random_amounts():
    """Старі «випадкові» суми в БД теж мають блокувати сусідні слоти."""
    legacy = Decimal("29.4626")
    session = FakeSession([legacy])
    for _ in range(10):
        amount = await allocate_unique_amount(session, "bsc", Decimal("29"))
        assert abs(amount - legacy) >= AMOUNT_STEP


@pytest.mark.asyncio
async def test_allocation_fails_loudly_when_grid_is_full():
    """Краще явна помилка, ніж мовчазна видача дубля."""
    full = [Decimal("29") + AMOUNT_STEP * i for i in range(1, int(1 / AMOUNT_STEP))]
    session = FakeSession(full)
    with pytest.raises(AmountAllocationError):
        await allocate_unique_amount(session, "bsc", Decimal("29"))
