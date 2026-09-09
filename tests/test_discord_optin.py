"""Tests for the Discord click-to-opt-in flow.

No network: the adapter is constructed offline and driven with stand-ins for
the payloads discord.py would deliver.
"""

from __future__ import annotations

import random

import pytest
from sqlalchemy import create_engine as sa_engine
from sqlalchemy.orm import Session

from idlerpg.adapters.discord_adapter import OPTIN_MESSAGE_KEY, DiscordAdapter
from idlerpg.engine import Engine
from idlerpg.models import Base
from idlerpg.rules import Curve

ROLE_ID, CHAN_ID, MSG_ID, USER_ID = 700, 800, 900, 1000
EMOJI = "\N{GAME DIE}"


class FakeRole:
    def __init__(self, rid=ROLE_ID):
        self.id = rid

    def __str__(self):
        return "optin"


class FakeMember:
    def __init__(self, bot=False):
        self.id = USER_ID
        self.bot = bot
        self.added, self.removed = [], []

    async def add_roles(self, role, reason=None):
        self.added.append(role.id)

    async def remove_roles(self, role, reason=None):
        self.removed.append(role.id)

    def __str__(self):
        return "someone"


class FakeGuild:
    def __init__(self, member):
        self._member = member
        self._role = FakeRole()

    def get_role(self, rid):
        return self._role if rid == ROLE_ID else None

    def get_member(self, uid):
        return self._member


class Payload:
    def __init__(self, message_id=MSG_ID, emoji=EMOJI, member=None, guild_id=1):
        self.message_id = message_id
        self.emoji = emoji
        self.member = member
        self.user_id = USER_ID
        self.guild_id = guild_id


@pytest.fixture
def adapter():
    db = sa_engine("sqlite://")
    Base.metadata.create_all(db)
    with Session(db) as session:
        engine = Engine(session, Curve(), rng=random.Random(1))
        a = DiscordAdapter(
            engine, channel_id=1,
            optin_channel_id=CHAN_ID, optin_role_id=ROLE_ID, optin_emoji=EMOJI,
        )
        engine.set_setting(OPTIN_MESSAGE_KEY, str(MSG_ID))
        yield a


class TestMatching:
    def test_matches_the_stored_message_and_emoji(self, adapter):
        assert adapter._is_optin_reaction(Payload())

    def test_ignores_other_messages(self, adapter):
        assert not adapter._is_optin_reaction(Payload(message_id=12345))

    def test_ignores_other_emoji(self, adapter):
        assert not adapter._is_optin_reaction(Payload(emoji="\N{PILE OF POO}"))

    def test_ignores_direct_messages(self, adapter):
        assert not adapter._is_optin_reaction(Payload(guild_id=None))

    def test_disabled_when_unconfigured(self, adapter):
        adapter.optin_role_id = 0
        assert not adapter.optin_enabled
        assert not adapter._is_optin_reaction(Payload())


class TestGranting:
    @pytest.mark.asyncio
    async def test_reacting_grants_the_role(self, adapter, monkeypatch):
        member = FakeMember()
        monkeypatch.setattr(adapter, "get_guild", lambda _id: FakeGuild(member))
        await adapter.on_raw_reaction_add(Payload(member=member))
        assert member.added == [ROLE_ID]

    @pytest.mark.asyncio
    async def test_unreacting_removes_the_role(self, adapter, monkeypatch):
        member = FakeMember()
        monkeypatch.setattr(adapter, "get_guild", lambda _id: FakeGuild(member))
        await adapter.on_raw_reaction_remove(Payload())
        assert member.removed == [ROLE_ID]

    @pytest.mark.asyncio
    async def test_bots_are_ignored(self, adapter, monkeypatch):
        """The bot reacts first to seed the message; it must not grant itself."""
        member = FakeMember(bot=True)
        monkeypatch.setattr(adapter, "get_guild", lambda _id: FakeGuild(member))
        await adapter.on_raw_reaction_add(Payload(member=member))
        assert member.added == []

    @pytest.mark.asyncio
    async def test_wrong_emoji_grants_nothing(self, adapter, monkeypatch):
        member = FakeMember()
        monkeypatch.setattr(adapter, "get_guild", lambda _id: FakeGuild(member))
        await adapter.on_raw_reaction_add(Payload(emoji="x", member=member))
        assert member.added == []


class TestPersistence:
    def test_message_id_survives_a_restart(self, adapter):
        assert adapter.engine.get_setting(OPTIN_MESSAGE_KEY) == str(MSG_ID)
        adapter.engine.set_setting(OPTIN_MESSAGE_KEY, "42")
        assert adapter.engine.get_setting(OPTIN_MESSAGE_KEY) == "42"


class TestNoDuplicateMessages:
    """Posting rights without read-history must not cause a repost per restart."""

    @pytest.mark.asyncio
    async def test_forbidden_fetch_keeps_the_existing_message(self, adapter, monkeypatch):
        import discord

        sent = []

        class Chan:
            async def fetch_message(self, _id):
                raise discord.Forbidden(_Resp(), "no history")

            async def send(self, text):
                sent.append(text)
                raise AssertionError("must not repost when merely unreadable")

        class _Resp:
            status = 403
            reason = "Forbidden"

        monkeypatch.setattr(adapter, "get_channel", lambda _id: Chan())
        await adapter.ensure_optin_message()
        assert sent == []
        assert adapter.engine.get_setting(OPTIN_MESSAGE_KEY) == str(MSG_ID)

    @pytest.mark.asyncio
    async def test_deleted_message_is_replaced(self, adapter, monkeypatch):
        import discord

        class Msg:
            id = 4242

            async def add_reaction(self, _e):
                return None

        class _Resp:
            status = 404
            reason = "Not Found"

        class Chan:
            async def fetch_message(self, _id):
                raise discord.NotFound(_Resp(), "gone")

            async def send(self, text):
                return Msg()

        monkeypatch.setattr(adapter, "get_channel", lambda _id: Chan())
        await adapter.ensure_optin_message()
        assert adapter.engine.get_setting(OPTIN_MESSAGE_KEY) == "4242"
