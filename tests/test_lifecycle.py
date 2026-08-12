"""Життєвий цикл підписки: нагадування, видалення з каналу, статистика."""

import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest
from aiogram.exceptions import TelegramForbiddenError

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import workers  # noqa: E402
from app.db.models import Invoice, Subscription, User  # noqa: E402
from app.services import repo  # noqa: E402

CHANNEL = -1001234567890


async def make_sub(session, tg_id: int, days_left: float, **kw) -> Subscription:
    user = User(tg_id=tg_id, username=f"u{tg_id}", first_name="U")
    session.add(user)
    await session.flush()

    now = datetime.now(timezone.utc)
    sub = Subscription(
        user_id=user.id, plan="1m", source="payment",
        starts_at=now - timedelta(days=30),
        expires_at=now + timedelta(days=days_left),
        is_active=kw.pop("is_active", True),
        notified_3d=kw.pop("notified_3d", False),
        notified_1d=kw.pop("notified_1d", False),
        **kw,
    )
    session.add(sub)
    await session.commit()
    return sub


class FakeBot:
    def __init__(self, blocked: set[int] = frozenset(), kick_fails: set[int] = frozenset()):
        self.blocked = blocked
        self.kick_fails = kick_fails
        self.sent = []
        self.banned = []

    async def send_message(self, tg_id, text, **kw):
        if tg_id in self.blocked:
            raise TelegramForbiddenError(method=None, message="bot was blocked by the user")
        self.sent.append((tg_id, text))

    async def ban_chat_member(self, chat_id, user_id, **kw):
        if user_id in self.kick_fails:
            raise RuntimeError("not enough rights to restrict")
        self.banned.append(user_id)

    async def unban_chat_member(self, chat_id, user_id, **kw):
        pass


@pytest.fixture
def patched(session, monkeypatch):
    """Воркери ходять у БД через власну сесію — підміняємо на тестову."""
    class Ctx:
        async def __aenter__(self):
            return session

        async def __aexit__(self, *a):
            return False

    monkeypatch.setattr(workers, "async_session", lambda: Ctx())
    monkeypatch.setattr(workers, "settings", SimpleNamespace(channel_id=CHANNEL, owner_id=1, admin_ids=""))
    monkeypatch.setattr(workers.asyncio, "sleep", _noop)

    alerts = []

    async def fake_notify(_bot, text):
        alerts.append(text)

    monkeypatch.setattr(workers, "notify_admins", fake_notify)
    return alerts


async def _noop(*a, **kw):
    return None


# ---------- нагадування ----------

@pytest.mark.asyncio
async def test_reminders_are_actually_sent(session, patched):
    sub3 = await make_sub(session, 111, days_left=3)
    sub1 = await make_sub(session, 222, days_left=1)
    bot = FakeBot()

    await workers.subscription_reminders(bot)

    recipients = [tg for tg, _ in bot.sent]
    assert recipients == [111, 222]
    await session.refresh(sub3)
    await session.refresh(sub1)
    assert sub3.notified_3d is True
    assert sub1.notified_1d is True


@pytest.mark.asyncio
async def test_reminder_not_sent_twice(session, patched):
    await make_sub(session, 111, days_left=3, notified_3d=True)
    bot = FakeBot()

    await workers.subscription_reminders(bot)

    assert bot.sent == []


@pytest.mark.asyncio
async def test_failed_reminder_is_retried_next_time(session, patched, monkeypatch):
    """Тимчасовий збій не має «спалювати» нагадування в базі."""
    sub = await make_sub(session, 111, days_left=3)

    class FlakyBot(FakeBot):
        async def send_message(self, tg_id, text, **kw):
            raise RuntimeError("timeout")

    await workers.subscription_reminders(FlakyBot())

    await session.refresh(sub)
    assert sub.notified_3d is False   # спробуємо ще раз наступної години

    bot = FakeBot()
    await workers.subscription_reminders(bot)
    await session.refresh(sub)
    assert sub.notified_3d is True
    assert [tg for tg, _ in bot.sent] == [111]


@pytest.mark.asyncio
async def test_blocked_user_is_marked_and_not_retried(session, patched):
    sub = await make_sub(session, 111, days_left=3)
    bot = FakeBot(blocked={111})

    await workers.subscription_reminders(bot)

    await session.refresh(sub)
    assert sub.notified_3d is True    # повторювати немає сенсу
    user = await repo.get_user_by_tg_id(session, 111)
    assert user.is_blocked_bot is True


# ---------- видалення з каналу ----------

@pytest.mark.asyncio
async def test_expired_user_is_kicked_and_notified(session, patched):
    sub = await make_sub(session, 111, days_left=-1)
    bot = FakeBot()

    await workers.kick_expired(bot)

    assert bot.banned == [111]
    assert [tg for tg, _ in bot.sent] == [111]
    await session.refresh(sub)
    assert sub.is_active is False
    assert sub.kicked_at is not None


@pytest.mark.asyncio
async def test_failed_kick_does_not_mark_as_kicked(session, patched):
    """Головний фікс: якщо видалити не вдалося — не брехати в базу."""
    sub = await make_sub(session, 111, days_left=-1)
    alerts = patched
    bot = FakeBot(kick_fails={111})

    await workers.kick_expired(bot)

    await session.refresh(sub)
    assert sub.kicked_at is None      # спробуємо ще раз наступної години
    assert sub.is_active is True
    assert alerts and "Не вдалося видалити" in alerts[0]


@pytest.mark.asyncio
async def test_active_subscription_is_not_kicked(session, patched):
    await make_sub(session, 111, days_left=5)
    bot = FakeBot()

    await workers.kick_expired(bot)

    assert bot.banned == []


# ---------- статистика ----------

@pytest.mark.asyncio
async def test_stats_counts(session):
    await make_sub(session, 111, days_left=5)
    await make_sub(session, 222, days_left=2)     # закінчується за 3 дні
    await make_sub(session, 333, days_left=-1)    # прострочена

    now = datetime.now(timezone.utc)
    session.add(Invoice(
        user_id=1, plan="1m", network="bsc", base_amount_usd=Decimal("29"),
        expected_amount=Decimal("29.06"), expected_units=Decimal("29060000000000000000"),
        pay_address="0xabc", status="paid", expires_at=now,
        received_amount=Decimal("29.06"), paid_at=now - timedelta(hours=2),
    ))
    session.add(Invoice(
        user_id=1, plan="1m", network="bsc", base_amount_usd=Decimal("29"),
        expected_amount=Decimal("29.10"), expected_units=Decimal("29100000000000000000"),
        pay_address="0xabc", status="pending", expires_at=now + timedelta(minutes=20),
    ))
    await session.commit()

    stats = await repo.get_stats(session)

    assert stats["users"] == 3
    assert stats["subs_active"] == 2
    assert stats["subs_expiring_3d"] == 1
    assert stats["paid_day"] == 1
    assert stats["revenue_day"] == Decimal("29.06")
    assert stats["invoices_pending"] == 1
    assert stats["orphans"] == 0
