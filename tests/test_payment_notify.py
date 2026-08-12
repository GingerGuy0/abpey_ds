"""Сповіщення про зараховану оплату.

Регресія: інвойс переходив у `paid` у фонових джобах (скан гаманця та
дочекування підтверджень), підписка створювалась, але користувачу ніхто
нічого не писав — людина дізнавалась про доступ випадково.
"""

import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest
from aiogram.exceptions import TelegramForbiddenError

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.db.models import Invoice, Subscription, User  # noqa: E402
from app.services import delivery  # noqa: E402

CHANNEL = -1001234567890


def patch_env(monkeypatch, admins=()):
    """channel_id — звичайне поле, а admin_id_list — property, тому підміняємо
    не самі налаштування, а функцію сповіщення адмінів."""
    monkeypatch.setattr(delivery.settings, "channel_id", CHANNEL, raising=False)

    async def fake_notify(bot, text):
        for admin in admins:
            await bot.send_message(admin, text)
        return len(admins)

    monkeypatch.setattr(delivery, "notify_admins", fake_notify)


class FakeLink:
    def __init__(self, link: str):
        self.invite_link = link


class FakeBot:
    def __init__(self, blocked: set[int] = frozenset(), invite_fails: bool = False):
        self.blocked = blocked
        self.invite_fails = invite_fails
        self.sent = []

    async def send_message(self, tg_id, text, **kw):
        if tg_id in self.blocked:
            raise TelegramForbiddenError(method=None, message="bot was blocked by the user")
        self.sent.append((tg_id, text))

    async def create_chat_invite_link(self, chat_id, **kw):
        if self.invite_fails:
            raise RuntimeError("not enough rights to invite users")
        return FakeLink("https://t.me/+fake")

    async def revoke_chat_invite_link(self, chat_id, invite_link):
        pass


async def make_paid_invoice(session, tg_id: int = 555) -> Invoice:
    now = datetime.now(timezone.utc)
    user = User(tg_id=tg_id, username="payer", first_name="Payer")
    session.add(user)
    await session.flush()

    session.add(Subscription(
        user_id=user.id, plan="1m", source="payment",
        starts_at=now, expires_at=now + timedelta(days=30),
    ))
    inv = Invoice(
        user_id=user.id, plan="1m", network="bsc",
        base_amount_usd=Decimal("2.5"), expected_amount=Decimal("2.42"),
        expected_units=Decimal("2420000000000000000"),
        pay_address="0xabc", status="paid",
        expires_at=now + timedelta(minutes=30),
        received_amount=Decimal("2.42"),
    )
    session.add(inv)
    await session.commit()
    await session.refresh(inv)
    return inv


@pytest.mark.asyncio
async def test_deliver_access_sends_invite(session, monkeypatch):
    patch_env(monkeypatch)

    inv = await make_paid_invoice(session)
    bot = FakeBot()

    delivered = await delivery.deliver_access(bot, session, inv, source="тест")

    assert delivered is True
    assert len(bot.sent) == 1
    tg_id, text = bot.sent[0]
    assert tg_id == 555
    assert "https://t.me/+fake" in text
    assert "Оплату знайдено" in text


@pytest.mark.asyncio
async def test_overpay_is_not_mentioned_to_the_user(session, monkeypatch):
    """Юзеру про переплату не пишемо — тільки адмінам."""
    patch_env(monkeypatch, admins=[777])

    inv = await make_paid_invoice(session, tg_id=556)
    bot = FakeBot()

    await delivery.deliver_access(bot, session, inv, overpay=Decimal("1.5"), source="тест")

    user_text = next(text for tg_id, text in bot.sent if tg_id == 556)
    assert "більше" not in user_text
    assert "підтримку" not in user_text
    assert "Оплату знайдено" in user_text

    admin_text = next(text for tg_id, text in bot.sent if tg_id == 777)
    assert "Переплата" in admin_text


@pytest.mark.asyncio
async def test_blocked_user_marked_and_admins_alerted(session, monkeypatch):
    patch_env(monkeypatch, admins=[777])

    inv = await make_paid_invoice(session, tg_id=557)
    bot = FakeBot(blocked={557})

    delivered = await delivery.deliver_access(bot, session, inv, source="тест")

    assert delivered is False
    admin_msgs = [text for tg_id, text in bot.sent if tg_id == 777]
    assert any("заблокував бота" in m for m in admin_msgs)
    assert any("Нова оплата" in m for m in admin_msgs)


@pytest.mark.asyncio
async def test_user_notified_even_if_invite_fails(session, monkeypatch):
    """Посилання не видалось — людина все одно має знати, що оплату зараховано."""
    patch_env(monkeypatch, admins=[777])

    inv = await make_paid_invoice(session, tg_id=558)
    bot = FakeBot(invite_fails=True)

    delivered = await delivery.deliver_access(bot, session, inv, source="тест")

    assert delivered is True
    user_msgs = [text for tg_id, text in bot.sent if tg_id == 558]
    assert user_msgs and "Оплату знайдено" in user_msgs[0]
    assert any("посилання не видалось" in text for tg_id, text in bot.sent if tg_id == 777)


@pytest.mark.asyncio
async def test_finalize_confirming_notifies_user(session, monkeypatch):
    """Обіцянку «сповіщу автоматично» виконує саме ця джоба."""
    from app import workers
    from app.services import payments

    patch_env(monkeypatch)

    inv = await make_paid_invoice(session, tg_id=559)
    inv.status = "confirming"
    await session.commit()

    bot = FakeBot()
    calls = []

    class FakeSessionCtx:
        async def __aenter__(self_inner):
            return session

        async def __aexit__(self_inner, *a):
            return False

    async def fake_finalize(_session):
        inv.status = "paid"
        calls.append(True)
        return [payments.SettleResult(status="paid", invoice=inv, tx=None)]

    monkeypatch.setattr(workers, "async_session", lambda: FakeSessionCtx())
    monkeypatch.setattr(workers.payments, "finalize_confirming", fake_finalize)

    await workers.finalize_confirming(bot)

    assert calls, "finalize_confirming не викликано"
    assert [tg for tg, _ in bot.sent] == [559]
