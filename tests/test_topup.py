"""Доплата залишку після недоплати.

Регресія: кожен переказ порівнювався з ПОВНОЮ ціною інвойса окремо, тому
друга транзакція («докинув решту») знову виглядала недоплатою, і людина
ніколи не могла добити суму.
"""

import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.chains.base import IncomingTx  # noqa: E402
from app.db.models import Invoice, User  # noqa: E402
from app.services import payments, repo  # noqa: E402

NETWORK = "bsc"
ADDRESS = "0xwallet"
MIN_CONF = 12


def make_tx(tx_hash: str, amount: str, confirmations: int = MIN_CONF) -> IncomingTx:
    return IncomingTx(
        tx_hash=tx_hash, network=NETWORK, from_address="0xpayer", to_address=ADDRESS,
        amount=Decimal(amount), units=0, confirmations=confirmations,
        timestamp=datetime.now(timezone.utc), is_success=True,
    )


class FakeClient:
    """Мінімальний клієнт мережі: віддає заздалегідь підготовлені транзакції."""

    def __init__(self, txs: dict[str, IncomingTx]):
        self.txs = txs

    def normalize_hash(self, raw: str) -> str:
        return raw.lower()

    async def get_tx_by_hash(self, tx_hash: str, address: str) -> IncomingTx:
        return self.txs[tx_hash]


@pytest.fixture
def patch_chain(monkeypatch):
    def _patch(txs: dict[str, IncomingTx]):
        monkeypatch.setattr(payments.registry, "get", lambda network: FakeClient(txs))
    return _patch


async def check(session, inv, tx_hash: str):
    """Кожна перевірка в бою — це окремий апдейт від Telegram зі свіжим станом
    інвойса. Відтворюємо це, а не працюємо з протухлими атрибутами в пам'яті."""
    await session.refresh(inv)
    return await payments.check_by_hash(session, inv, tx_hash)


async def make_invoice(session, expected: str = "10.06") -> Invoice:
    now = datetime.now(timezone.utc)
    user = User(tg_id=901, username="payer", first_name="Payer")
    session.add(user)
    await session.flush()

    inv = Invoice(
        user_id=user.id, plan="1m", network=NETWORK,
        base_amount_usd=Decimal("10"), expected_amount=Decimal(expected),
        expected_units=Decimal("10060000000000000000"),
        pay_address=ADDRESS, status="pending",
        created_at=now, expires_at=now + timedelta(minutes=30),
        attempts=0,
    )
    session.add(inv)
    await session.commit()
    await session.refresh(inv)
    return inv


@pytest.mark.asyncio
async def test_topup_completes_invoice(session, patch_chain):
    """Класика: скинув 6, потім докинув 4.06 — доступ має відкритись."""
    first, second = make_tx("0xaaa", "6.00"), make_tx("0xbbb", "4.06")
    patch_chain({"0xaaa": first, "0xbbb": second})

    inv = await make_invoice(session)

    res1 = await check(session, inv, "0xaaa")
    assert res1.status == "underpaid"
    assert res1.extra["credited"] == Decimal("6.00")
    assert res1.extra["missing"] == Decimal("4.06")
    assert res1.extra["topup"] is False

    res2 = await check(session, inv, "0xbbb")
    assert res2.status == "paid"
    await session.refresh(inv)
    assert inv.status == "paid"
    assert await repo.get_credited_amount(session, inv.id) == Decimal("10.06")


@pytest.mark.asyncio
async def test_received_amount_is_the_total(session, patch_chain):
    """У звітності має лишитись повна сума, а не тільки остання транзакція."""
    patch_chain({"0xaaa": make_tx("0xaaa", "6.00"), "0xbbb": make_tx("0xbbb", "4.06")})
    inv = await make_invoice(session)

    await check(session, inv, "0xaaa")
    await check(session, inv, "0xbbb")

    await session.refresh(inv)
    assert Decimal(str(inv.received_amount)) == Decimal("10.06")


@pytest.mark.asyncio
async def test_three_small_payments_accumulate(session, patch_chain):
    patch_chain({
        "0x1": make_tx("0x1", "3.00"),
        "0x2": make_tx("0x2", "3.00"),
        "0x3": make_tx("0x3", "4.06"),
    })
    inv = await make_invoice(session)

    assert (await check(session, inv, "0x1")).status == "underpaid"
    res2 = await check(session, inv, "0x2")
    assert res2.status == "underpaid"
    assert res2.extra["topup"] is True
    assert res2.extra["credited"] == Decimal("6.00")
    assert (await check(session, inv, "0x3")).status == "paid"


@pytest.mark.asyncio
async def test_same_hash_is_not_counted_twice(session, patch_chain):
    """Той самий хеш вдруге не має накручувати зараховану суму."""
    patch_chain({"0xaaa": make_tx("0xaaa", "6.00")})
    inv = await make_invoice(session)

    assert (await check(session, inv, "0xaaa")).status == "underpaid"
    assert (await check(session, inv, "0xAAA")).status == "already_used"
    assert await repo.get_credited_amount(session, inv.id) == Decimal("6.00")


@pytest.mark.asyncio
async def test_unconfirmed_partial_is_not_credited(session, patch_chain):
    """Непідтверджену частку не зараховуємо: реорг забрав би гроші назад."""
    patch_chain({"0xaaa": make_tx("0xaaa", "6.00", confirmations=1)})
    inv = await make_invoice(session)

    res = await check(session, inv, "0xaaa")

    assert res.status == "partial_unconfirmed"
    assert await repo.get_credited_amount(session, inv.id) == Decimal(0)

    # Ліміт спроб не з'їдено: чекати підтверджень — не те саме, що перебирати хеші.
    await session.refresh(inv)
    assert inv.attempts == 0

    # Після підтверджень той самий хеш нарешті зараховується (кулдаун минув).
    inv.last_attempt_at = datetime.now(timezone.utc) - timedelta(minutes=5)
    await session.commit()
    patch_chain({"0xaaa": make_tx("0xaaa", "6.00", confirmations=MIN_CONF)})
    res2 = await check(session, inv, "0xaaa")
    assert res2.status == "underpaid"
    assert await repo.get_credited_amount(session, inv.id) == Decimal("6.00")


@pytest.mark.asyncio
async def test_topup_overpay_is_reported(session, patch_chain):
    patch_chain({"0xaaa": make_tx("0xaaa", "6.00"), "0xbbb": make_tx("0xbbb", "6.00")})
    inv = await make_invoice(session)

    await check(session, inv, "0xaaa")
    res = await check(session, inv, "0xbbb")

    assert res.status == "paid"
    assert res.extra["overpay"] == Decimal("1.94")


@pytest.mark.asyncio
async def test_small_shortfall_is_still_forgiven(session, patch_chain):
    """Комісію біржі в межах MAX_UNDERPAY_USD прощаємо як і раніше."""
    patch_chain({"0xaaa": make_tx("0xaaa", "9.80")})
    inv = await make_invoice(session)

    assert (await check(session, inv, "0xaaa")).status == "paid"


@pytest.mark.asyncio
async def test_expired_invoice_with_credit_is_reported(session, patch_chain, monkeypatch):
    """Гроші не мають зникати разом із протухлим інвойсом."""
    from app import workers

    patch_chain({"0xaaa": make_tx("0xaaa", "6.00")})
    inv = await make_invoice(session)
    await check(session, inv, "0xaaa")

    inv.expires_at = datetime.now(timezone.utc) - timedelta(minutes=1)
    await session.commit()

    sent = []

    class FakeBot:
        async def send_message(self, tg_id, text, **kw):
            sent.append((tg_id, text))

    class FakeSessionCtx:
        async def __aenter__(self_inner):
            return session

        async def __aexit__(self_inner, *a):
            return False

    async def fake_notify(bot, text):
        sent.append(("admin", text))
        return 1

    monkeypatch.setattr(workers, "async_session", lambda: FakeSessionCtx())
    monkeypatch.setattr(workers, "notify_admins", fake_notify)

    await workers.expire_invoices(FakeBot())

    await session.refresh(inv)
    assert inv.status == "expired"
    assert any(tg == 901 for tg, _ in sent), "юзеру не сказали про завислу часткову оплату"
    assert any(tg == "admin" for tg, _ in sent), "адмін не отримав алерт"
