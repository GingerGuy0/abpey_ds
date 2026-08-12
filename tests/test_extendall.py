"""Продовження підписки всім, хто в каналі."""

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.db.models import Subscription, User  # noqa: E402
from app.handlers.admin import cb_extendall_confirm  # noqa: E402
from app.keyboards import AdminCB  # noqa: E402
from app.services import repo  # noqa: E402

CHANNEL = -1001234567890


async def make_sub(session, tg_id: int, days_left: int, active: bool = True) -> Subscription:
    user = User(tg_id=tg_id, username=f"u{tg_id}", first_name="U")
    session.add(user)
    await session.flush()

    now = datetime.now(timezone.utc)
    sub = Subscription(
        user_id=user.id, plan="1m", source="payment",
        starts_at=now - timedelta(days=1),
        expires_at=now + timedelta(days=days_left),
        is_active=active,
        notified_3d=True, notified_1d=True,
    )
    session.add(sub)
    await session.commit()
    return sub


@pytest.mark.asyncio
async def test_extend_adds_days_and_resets_reminders(session):
    sub = await make_sub(session, 1, days_left=2)
    before = sub.expires_at

    new_date = await repo.extend_subscription(session, sub.id, 7)

    assert new_date == before + timedelta(days=7)
    # Дата змінилась -> нагадування треба показати заново.
    assert sub.notified_3d is False
    assert sub.notified_1d is False


@pytest.mark.asyncio
async def test_active_list_skips_expired_and_disabled(session):
    await make_sub(session, 1, days_left=5)
    await make_sub(session, 2, days_left=-1)              # прострочена
    await make_sub(session, 3, days_left=5, active=False)  # вимкнена

    subs = await repo.list_active_subscriptions(session)

    assert [s.user.tg_id for s in subs] == [1]


class FakeBot:
    def __init__(self, members: set[int], blocked: set[int] = frozenset()):
        self.members = members
        self.blocked = blocked
        self.sent = []

    async def get_chat_member(self, chat_id, user_id):
        if user_id in self.members:
            return SimpleNamespace(status="member")
        return SimpleNamespace(status="left")

    async def send_message(self, tg_id, text, **kw):
        if tg_id in self.blocked:
            raise RuntimeError("bot was blocked by the user")
        self.sent.append((tg_id, text))


def make_query(admin_id=777):
    replies = []

    async def answer(text=None, **kw):
        replies.append(text)

    message = SimpleNamespace(
        answer=answer,
        edit_reply_markup=lambda *a, **kw: _noop(),
        replies=replies,
    )
    return SimpleNamespace(from_user=SimpleNamespace(id=admin_id), message=message, answer=answer)


async def _noop():
    return None


@pytest.fixture(autouse=True)
def channel(monkeypatch):
    monkeypatch.setattr("app.handlers.admin.settings",
                        SimpleNamespace(channel_id=CHANNEL, owner_id=777, admin_ids=""))
    monkeypatch.setattr("app.handlers.admin.asyncio.sleep", lambda *_: _noop())


@pytest.mark.asyncio
async def test_only_channel_members_get_extension(session):
    in_channel = await make_sub(session, 111, days_left=5)
    left_channel = await make_sub(session, 222, days_left=5)
    before_left = left_channel.expires_at

    bot = FakeBot(members={111})
    query = make_query()

    await cb_extendall_confirm(query, AdminCB(action="extendall_confirm", arg="7"), session, bot)

    await session.refresh(in_channel)
    await session.refresh(left_channel)

    assert in_channel.expires_at.date() == (datetime.now(timezone.utc) + timedelta(days=12)).date()
    # Той, хто вийшов з каналу, днів не отримує.
    assert left_channel.expires_at == before_left
    assert [tg for tg, _ in bot.sent] == [111]
    assert "Продовжено: 1" in query.message.replies[-1]


@pytest.mark.asyncio
async def test_extension_survives_blocked_bot(session):
    sub = await make_sub(session, 111, days_left=5)
    before = sub.expires_at

    bot = FakeBot(members={111}, blocked={111})
    query = make_query()

    await cb_extendall_confirm(query, AdminCB(action="extendall_confirm", arg="7"), session, bot)

    await session.refresh(sub)
    # Дні нараховані, навіть якщо повідомлення не дійшло.
    assert sub.expires_at == before + timedelta(days=7)
    report = query.message.replies[-1]
    assert "Продовжено: 1" in report
    assert "Не вдалося повідомити: 1" in report


@pytest.mark.asyncio
async def test_nobody_in_channel(session):
    sub = await make_sub(session, 111, days_left=5)
    before = sub.expires_at

    bot = FakeBot(members=set())
    query = make_query()

    await cb_extendall_confirm(query, AdminCB(action="extendall_confirm", arg="7"), session, bot)

    await session.refresh(sub)
    assert sub.expires_at == before
    assert "Продовжено: 0" in query.message.replies[-1]
