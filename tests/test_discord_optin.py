"""Tests for the Discord click-to-opt-in flow.

No network: the adapter is constructed offline and driven with stand-ins for
the payloads discord.py would deliver.
"""

from __future__ import annotations

import random

import pytest
from sqlalchemy import create_engine as sa_engine
from sqlalchemy.orm import Session

from idlerpg.adapters.discord_adapter import (
    OPTIN_CHANNEL_KEY, OPTIN_MESSAGE_KEY, DiscordAdapter,
)
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
    def __init__(self, bot=False, dms_closed=False):
        self.id = USER_ID
        self.bot = bot
        self.added, self.removed = [], []
        self.dms = []
        self.dms_closed = dms_closed

    async def send(self, text):
        if self.dms_closed:
            import discord

            class _R:
                status, reason = 403, "Forbidden"

            raise discord.Forbidden(_R(), "Cannot send messages to this user")
        self.dms.append(text)

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


class TestReactingExplainsHowToPlay:
    """Reacting gets you the channel but no character; the DM is the rest."""

    @pytest.mark.asyncio
    async def test_someone_without_a_character_is_told_how_to_register(
            self, adapter, monkeypatch):
        member = FakeMember()
        monkeypatch.setattr(adapter, "get_guild", lambda _id: FakeGuild(member))
        await adapter.on_raw_reaction_add(Payload(member=member))
        assert member.added == [ROLE_ID]
        assert len(member.dms) == 1
        assert "!register" in member.dms[0] and "!login" in member.dms[0]

    @pytest.mark.asyncio
    async def test_a_player_is_not_told_again(self, adapter, monkeypatch):
        from idlerpg.models import Platform
        adapter.engine.register("rusty", "pw", "Sysadmin", Platform.DISCORD, str(USER_ID))
        member = FakeMember()
        monkeypatch.setattr(adapter, "get_guild", lambda _id: FakeGuild(member))
        await adapter.on_raw_reaction_add(Payload(member=member))
        assert member.added == [ROLE_ID]
        assert member.dms == []

    @pytest.mark.asyncio
    async def test_closed_dms_still_get_the_role(self, adapter, monkeypatch):
        member = FakeMember(dms_closed=True)
        monkeypatch.setattr(adapter, "get_guild", lambda _id: FakeGuild(member))
        await adapter.on_raw_reaction_add(Payload(member=member))
        assert member.added == [ROLE_ID]

    @pytest.mark.asyncio
    async def test_other_reactions_send_nothing(self, adapter, monkeypatch):
        member = FakeMember()
        monkeypatch.setattr(adapter, "get_guild", lambda _id: FakeGuild(member))
        await adapter.on_raw_reaction_add(Payload(emoji="x", member=member))
        assert member.dms == []


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

        posted = Msg(id=4242)

        class _Resp:
            status = 404
            reason = "Not Found"

        class Chan:
            async def fetch_message(self, _id):
                raise discord.NotFound(_Resp(), "gone")

            async def send(self, text):
                return posted

        monkeypatch.setattr(adapter, "get_channel", lambda _id: Chan())
        await adapter.ensure_optin_message()
        assert adapter.engine.get_setting(OPTIN_MESSAGE_KEY) == "4242"
        assert posted.pinned


class _Resp:
    def __init__(self, status, reason):
        self.status, self.reason = status, reason


def forbidden():
    import discord
    return discord.Forbidden(_Resp(403, "Forbidden"), "missing permissions")


def not_found():
    import discord
    return discord.NotFound(_Resp(404, "Not Found"), "unknown message")


class Reaction:
    def __init__(self, emoji):
        self.emoji = emoji
        self.me = True


class Msg:
    """A stand-in for the bot's own opt-in post."""

    def __init__(self, id=MSG_ID, content="", pinned=False, can_pin=True,
                 can_react=True, reacted=False):
        self.id = id
        self.content = content
        self.pinned = pinned
        self.can_pin = can_pin
        self.can_react = can_react
        self.reactions = [Reaction(EMOJI)] if reacted else []
        self.edits = []

    async def add_reaction(self, emoji):
        if not self.can_react:
            raise forbidden()
        self.reactions.append(Reaction(emoji))

    async def edit(self, content):
        self.edits.append(content)
        self.content = content

    async def pin(self, reason=None):
        if not self.can_pin:
            raise forbidden()
        self.pinned = True


class NoteChannel:
    """Keeps whatever the bot posts, so a second start can find it again."""

    def __init__(self, readable=True, can_react=True):
        self.readable = readable
        self.can_react = can_react
        self.messages = {}
        self.sent = 0

    async def fetch_message(self, mid):
        if not self.readable:
            raise forbidden()
        if mid not in self.messages:
            raise not_found()
        return self.messages[mid]

    async def send(self, text):
        self.sent += 1
        message = Msg(id=5000 + self.sent, content=text, can_react=self.can_react)
        self.messages[message.id] = message
        return message


async def start(adapter, monkeypatch, channel):
    monkeypatch.setattr(adapter, "get_channel", lambda _id: channel)
    await adapter.ensure_optin_message()


class TestRestartsChangeNothing:
    @pytest.mark.asyncio
    async def test_two_starts_post_one_note(self, adapter, monkeypatch):
        chan = NoteChannel()
        await start(adapter, monkeypatch, chan)
        await start(adapter, monkeypatch, chan)
        assert chan.sent == 1
        note = next(iter(chan.messages.values()))
        assert note.pinned
        assert len(note.reactions) == 1
        assert note.edits == []

    @pytest.mark.asyncio
    async def test_a_failed_reaction_does_not_cause_a_repost(self, adapter, monkeypatch):
        chan = NoteChannel(can_react=False)
        await start(adapter, monkeypatch, chan)
        await start(adapter, monkeypatch, chan)
        assert chan.sent == 1
        assert adapter.engine.get_setting(OPTIN_MESSAGE_KEY) == "5001"
        assert chan.messages[5001].pinned

    @pytest.mark.asyncio
    async def test_the_reaction_is_added_once_it_is_allowed(self, adapter, monkeypatch):
        chan = NoteChannel(can_react=False)
        await start(adapter, monkeypatch, chan)
        assert chan.messages[5001].reactions == []
        chan.messages[5001].can_react = True  # permission granted since
        await start(adapter, monkeypatch, chan)
        assert len(chan.messages[5001].reactions) == 1
        assert chan.sent == 1


class TestMovingTheNote:
    @pytest.mark.asyncio
    async def test_a_new_channel_gets_a_note_even_if_unreadable(self, adapter, monkeypatch):
        """The failure that left #idlerpg with no note: the old id was trusted
        because the new channel's history could not be read."""
        adapter.engine.set_setting(OPTIN_CHANNEL_KEY, "111")
        chan = NoteChannel(readable=False)
        await start(adapter, monkeypatch, chan)
        assert chan.sent == 1
        assert adapter.engine.get_setting(OPTIN_MESSAGE_KEY) == "5001"
        assert adapter.engine.get_setting(OPTIN_CHANNEL_KEY) == str(CHAN_ID)

    @pytest.mark.asyncio
    async def test_an_unreadable_note_in_the_same_channel_is_kept(self, adapter, monkeypatch):
        adapter.engine.set_setting(OPTIN_CHANNEL_KEY, str(CHAN_ID))
        chan = NoteChannel(readable=False)
        await start(adapter, monkeypatch, chan)
        assert chan.sent == 0
        assert adapter.engine.get_setting(OPTIN_MESSAGE_KEY) == str(MSG_ID)

    @pytest.mark.asyncio
    async def test_a_note_found_before_channels_were_recorded_gets_one(
            self, adapter, monkeypatch):
        """Notes posted by 0.11.0 have no channel stored; finding one fills it in."""
        chan = NoteChannel()
        chan.messages[MSG_ID] = Msg(content=adapter.optin_text, pinned=True,
                                    reacted=True)
        await start(adapter, monkeypatch, chan)
        assert chan.sent == 0
        assert adapter.engine.get_setting(OPTIN_CHANNEL_KEY) == str(CHAN_ID)


class TestTheNoteStaysCurrent:
    """The note is how newcomers learn to register, so it is pinned, and a
    reused one is reworded to match rather than left saying something stale."""

    def _serve(self, adapter, monkeypatch, message):
        class Chan:
            async def fetch_message(self, _id):
                return message

            async def send(self, text):
                raise AssertionError("must not repost an existing note")

        monkeypatch.setattr(adapter, "get_channel", lambda _id: Chan())

    @pytest.mark.asyncio
    async def test_a_stale_note_is_reworded_and_pinned(self, adapter, monkeypatch):
        note = Msg(content="old wording")
        self._serve(adapter, monkeypatch, note)
        await adapter.ensure_optin_message()
        assert note.content == adapter.optin_text
        assert note.pinned

    @pytest.mark.asyncio
    async def test_a_current_note_is_left_alone(self, adapter, monkeypatch):
        note = Msg(content=adapter.optin_text, pinned=True, reacted=True)
        self._serve(adapter, monkeypatch, note)
        await adapter.ensure_optin_message()
        assert note.edits == []
        assert len(note.reactions) == 1

    @pytest.mark.asyncio
    async def test_no_pin_permission_is_not_fatal(self, adapter, monkeypatch):
        note = Msg(content="old wording", can_pin=False)
        self._serve(adapter, monkeypatch, note)
        await adapter.ensure_optin_message()
        assert note.content == adapter.optin_text
        assert not note.pinned
