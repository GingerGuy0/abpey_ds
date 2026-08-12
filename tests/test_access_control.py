"""Тести доступу: персональні інвайти та список адмінів."""

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import Settings  # noqa: E402
from app.handlers.join import handle_join_request  # noqa: E402

CHANNEL = -1001234567890
PAYER_TG = 111
STRANGER_TG = 222
PAYER_LINK = "https://t.me/+payer_link"
OTHER_LINK = "https://t.me/+other_link"


# ---------- адміни ----------

def make_settings(**kw) -> Settings:
    # _env_file=None — щоб тест не залежав від реального .env розробника.
    kw.setdefault("admin_ids", "")
    return Settings(_env_file=None, **kw)


def test_admin_list_includes_owner_and_extras():
    s = make_settings(owner_id=100, admin_ids="200, 300")
    assert s.admin_id_list == [100, 200, 300]
    assert s.is_admin(100) and s.is_admin(300)
    assert not s.is_admin(999)
    assert not s.is_admin(None)


def test_admin_list_survives_garbage():
    s = make_settings(owner_id=100, admin_ids="200,,   ,abc,100,300")
    assert s.admin_id_list == [100, 200, 300]   # без дублів і без сміття


def test_admin_list_without_extras():
    assert make_settings(owner_id=100).admin_id_list == [100]


# ---------- заявки на вступ ----------

class FakeBot:
    def __init__(self):
        self.approved = []
        self.declined = []
        self.revoked = []
        self.messages = []

    async def approve_chat_join_request(self, chat_id, user_id):
        self.approved.append(user_id)

    async def decline_chat_join_request(self, chat_id, user_id):
        self.declined.append(user_id)

    async def revoke_chat_invite_link(self, chat_id, invite_link):
        self.revoked.append(invite_link)

    async def send_message(self, tg_id, text, **kw):
        self.messages.append((tg_id, text))


def make_event(tg_id: int, link: str | None):
    return SimpleNamespace(
        from_user=SimpleNamespace(id=tg_id, full_name="U", username="u"),
        chat=SimpleNamespace(id=CHANNEL),
        invite_link=SimpleNamespace(invite_link=link) if link else None,
    )


@pytest.fixture
def patched(monkeypatch):
    """Підміняємо repo/settings: тест про логіку рішення, не про БД."""
    state = {"sub": None, "cleared": []}

    async def get_user_by_tg_id(_s, tg_id):
        return SimpleNamespace(id=1, tg_id=tg_id) if tg_id == PAYER_TG else None

    async def get_active_subscription(_s, _uid):
        return state["sub"]

    async def clear_invite_link(_s, sub_id):
        state["cleared"].append(sub_id)

    monkeypatch.setattr("app.handlers.join.repo.get_user_by_tg_id", get_user_by_tg_id)
    monkeypatch.setattr("app.handlers.join.repo.get_active_subscription", get_active_subscription)
    monkeypatch.setattr("app.handlers.join.repo.clear_invite_link", clear_invite_link)
    monkeypatch.setattr("app.handlers.join.settings", make_settings(owner_id=1, channel_id=CHANNEL))
    return state


def active_sub(link=PAYER_LINK):
    return SimpleNamespace(
        id=7, invite_link=link,
        expires_at=datetime.now(timezone.utc) + timedelta(days=30),
    )


@pytest.mark.asyncio
async def test_payer_is_approved_and_link_burned(patched):
    patched["sub"] = active_sub()
    bot = FakeBot()

    await handle_join_request(make_event(PAYER_TG, PAYER_LINK), bot, session=None)

    assert bot.approved == [PAYER_TG]
    assert bot.declined == []
    # Посилання одноразове: після використання гаситься і в Telegram, і в БД.
    assert bot.revoked == [PAYER_LINK]
    assert patched["cleared"] == [7]


@pytest.mark.asyncio
async def test_forwarded_link_stranger_is_declined(patched):
    """Той, кому переслали посилання, не має підписки -> відмова."""
    patched["sub"] = active_sub()
    bot = FakeBot()

    await handle_join_request(make_event(STRANGER_TG, PAYER_LINK), bot, session=None)

    assert bot.declined == [STRANGER_TG]
    assert bot.approved == []
    assert bot.revoked == []   # чуже посилання не гасимо, воно ще потрібне платнику


@pytest.mark.asyncio
async def test_subscriber_with_foreign_link_is_declined(patched):
    """Підписка є, але постукав чужим посиланням — теж відмова."""
    patched["sub"] = active_sub(link=PAYER_LINK)
    bot = FakeBot()

    await handle_join_request(make_event(PAYER_TG, OTHER_LINK), bot, session=None)

    assert bot.declined == [PAYER_TG]
    assert bot.approved == []


@pytest.mark.asyncio
async def test_request_to_foreign_chat_ignored(patched):
    patched["sub"] = active_sub()
    bot = FakeBot()
    event = make_event(PAYER_TG, PAYER_LINK)
    event.chat.id = -100999

    await handle_join_request(event, bot, session=None)

    assert bot.approved == [] and bot.declined == []


@pytest.mark.asyncio
async def test_blocked_bot_does_not_break_approval(patched):
    """Якщо адресат не стартував бота — заявку все одно обробляємо."""
    patched["sub"] = active_sub()
    bot = FakeBot()

    async def boom(*a, **kw):
        raise RuntimeError("bot was blocked")

    bot.send_message = boom
    await handle_join_request(make_event(PAYER_TG, PAYER_LINK), bot, session=None)

    assert bot.approved == [PAYER_TG]
