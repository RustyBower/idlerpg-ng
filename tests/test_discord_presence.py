"""Tests for Discord presence and the account commands.

Presence is the game role: a registered player idles while they hold it, and
status is ignored. No network - the adapter is driven with stand-ins.
"""

from __future__ import annotations

import random

import discord
import pytest
from sqlalchemy import create_engine as sa_engine
from sqlalchemy.orm import Session

from idlerpg.adapters.discord_adapter import DiscordAdapter
from idlerpg.engine import Engine
from idlerpg.models import Base, Platform, Presence
from idlerpg.rules import Curve

ROLE_ID, CHAN_ID, GAME_CHANNEL, USER_ID = 700, 800, 1, 1000


class Role:
    def __init__(self, rid):
        self.id = rid


class Guild:
    def __init__(self, has_role=True):
        self.members = []
        self._has_role = has_role

    def get_role(self, rid):
        return Role(rid) if self._has_role and rid == ROLE_ID else None

    def get_member(self, uid):
        return next((m for m in self.members if m.id == uid), None)


class Forbidden403:
    status = 403
    reason = "Forbidden"


class Member:
    def __init__(self, guild, uid=USER_ID, role=False,
                 status=discord.Status.online, forbid=False):
        self.id = uid
        self.bot = False
        self.guild = guild
        self.roles = [Role(ROLE_ID)] if role else []
        self.status = status
        self.forbid = forbid
        self.added = []
        self.dms = []
        guild.members.append(self)

    async def add_roles(self, role, reason=None):
        if self.forbid:
            raise discord.Forbidden(Forbidden403(), "role above the bot's")
        self.added.append(role.id)

    async def send(self, text):
        self.dms.append(text)

    def __str__(self):
        return "someone#1"


class DM(discord.DMChannel):
    def __init__(self):  # the real one needs a live connection
        pass


class Channel:
    id = GAME_CHANNEL


class Message:
    def __init__(self, author, content, channel=None):
        self.author = author
        self.content = content
        self.channel = channel if channel is not None else DM()
        self.replies = []
        self.deleted = False

    async def reply(self, text, mention_author=False):
        self.replies.append(text)

    async def delete(self):
        self.deleted = True


def make_adapter(optin=True):
    db = sa_engine("sqlite://")
    Base.metadata.create_all(db)
    session = Session(db)
    engine = Engine(session, Curve(), rng=random.Random(1))
    if optin:
        return DiscordAdapter(engine, channel_id=GAME_CHANNEL,
                              optin_channel_id=CHAN_ID, optin_role_id=ROLE_ID)
    return DiscordAdapter(engine, channel_id=GAME_CHANNEL)


@pytest.fixture
def guild(monkeypatch):
    g = Guild()
    monkeypatch.setattr(DiscordAdapter, "guilds", property(lambda self: [g]))
    return g


@pytest.fixture
def adapter(guild):
    return make_adapter()


async def command(adapter, member, text, channel=None):
    message = Message(member, text, channel)
    await adapter.on_message(message)
    return message


def presence(adapter, uid=USER_ID):
    return adapter.engine.find_identity(Platform.DISCORD, str(uid)).presence


class TestRegistering:
    """Registering is joining: it hands out the game role, since the opt-in
    message may be somewhere a newcomer cannot see."""

    @pytest.mark.asyncio
    async def test_registering_gives_you_the_role(self, adapter, guild):
        member = Member(guild, role=False)
        m = await command(adapter, member, "!register rusty pw Sysadmin")
        assert member.added == [ROLE_ID]
        assert presence(adapter) is Presence.ACTIVE
        assert "not idling" not in m.replies[0]

    @pytest.mark.asyncio
    async def test_having_the_role_already_is_fine(self, adapter, guild):
        member = Member(guild, role=True)
        m = await command(adapter, member, "!register rusty pw Sysadmin")
        assert "Welcome" in m.replies[0]
        assert member.added == []
        assert presence(adapter) is Presence.ACTIVE

    @pytest.mark.asyncio
    async def test_if_the_role_cannot_be_given_you_are_told(self, adapter, guild):
        member = Member(guild, role=False, forbid=True)
        m = await command(adapter, member, "!register rusty pw Sysadmin")
        assert presence(adapter) is Presence.OFFLINE
        assert "ask an admin" in m.replies[0]

    @pytest.mark.asyncio
    async def test_outside_the_server_you_are_told_to_join(self, adapter, guild):
        stranger = Member(Guild(), role=False)  # shares no guild with the bot
        m = await command(adapter, stranger, "!register rusty pw Sysadmin")
        assert presence(adapter) is Presence.OFFLINE
        assert "join the server" in m.replies[0]

    @pytest.mark.asyncio
    async def test_offline_status_does_not_matter(self, adapter, guild):
        member = Member(guild, role=True, status=discord.Status.offline)
        await command(adapter, member, "!register rusty pw Sysadmin")
        assert presence(adapter) is Presence.ACTIVE


class TestTheRoleIsPresence:
    async def _registered(self, adapter, guild):
        member = Member(guild, role=True)
        await command(adapter, member, "!register rusty pw Sysadmin")
        return member, adapter.engine.find_player("rusty")

    @pytest.mark.asyncio
    async def test_status_changes_are_ignored(self, adapter, guild):
        member, _ = await self._registered(adapter, guild)
        offline = Member(Guild(), role=True, status=discord.Status.offline)
        await adapter.on_presence_update(member, offline)
        assert presence(adapter) is Presence.ACTIVE

    @pytest.mark.asyncio
    async def test_losing_the_role_is_parting(self, adapter, guild):
        member, player = await self._registered(adapter, guild)
        before = player.next_ttl
        await adapter.on_member_update(member, Member(Guild(), role=False))
        assert presence(adapter) is Presence.OFFLINE
        assert player.next_ttl > before

    @pytest.mark.asyncio
    async def test_gaining_the_role_is_joining(self, adapter, guild):
        member, player = await self._registered(adapter, guild)
        without = Member(Guild(), role=False)
        await adapter.on_member_update(member, without)
        assert not player.is_idling
        await adapter.on_member_update(without, Member(Guild(), role=True))
        assert presence(adapter) is Presence.ACTIVE
        assert player.is_idling

    @pytest.mark.asyncio
    async def test_other_role_changes_do_nothing(self, adapter, guild):
        member, player = await self._registered(adapter, guild)
        before = player.next_ttl
        await adapter.on_member_update(member, Member(Guild(), role=True))
        assert player.next_ttl == before

    @pytest.mark.asyncio
    async def test_leaving_the_server_is_quitting(self, adapter, guild):
        member, player = await self._registered(adapter, guild)
        before = player.next_ttl
        await adapter.on_member_remove(member)
        assert presence(adapter) is Presence.OFFLINE
        assert player.next_ttl > before

    @pytest.mark.asyncio
    async def test_startup_seeds_role_holders_and_clears_everyone_else(
            self, adapter, guild, monkeypatch):
        holder, _ = await self._registered(adapter, guild)
        gone = adapter.engine.register("gone", "pw", "Bard", Platform.DISCORD, "55")
        assert gone.is_idling  # recorded before a restart; no longer here
        monkeypatch.setattr(adapter, "ensure_optin_message", _noop)
        await adapter.on_ready()
        assert presence(adapter) is Presence.ACTIVE
        assert not gone.is_idling


class TestWithoutARoleStatusDecides:
    @pytest.mark.asyncio
    async def test_status_is_presence(self, guild):
        adapter = make_adapter(optin=False)
        member = Member(guild)
        await command(adapter, member, "!register rusty pw Sysadmin")
        assert presence(adapter) is Presence.ACTIVE
        await adapter.on_presence_update(
            member, Member(Guild(), status=discord.Status.offline))
        assert presence(adapter) is Presence.OFFLINE


class TestLoginAndMerge:
    @pytest.mark.asyncio
    async def test_login_links_an_irc_character(self, adapter, guild):
        adapter.engine.register("rusty", "pw", "Sysadmin", Platform.IRC, "rusty")
        member = Member(guild, role=False)
        m = await command(adapter, member, "!login rusty pw")
        assert "Logged in as rusty" in m.replies[0]
        assert adapter.engine.player_for(Platform.DISCORD, str(USER_ID)).name == "rusty"
        assert member.added == [ROLE_ID]  # logging in joins, like registering
        assert presence(adapter) is Presence.ACTIVE

    @pytest.mark.asyncio
    async def test_login_as_a_second_character_points_at_merge(self, adapter, guild):
        member = Member(guild, role=True)
        await command(adapter, member, "!register mine pw Sysadmin")
        adapter.engine.register("theirs", "pw", "Bard", Platform.IRC, "theirs")
        m = await command(adapter, member, "!login theirs pw")
        assert "!merge theirs" in m.replies[0]
        assert adapter.engine.player_for(Platform.DISCORD, str(USER_ID)).name == "mine"

    @pytest.mark.asyncio
    async def test_merge_folds_the_named_character_in(self, adapter, guild):
        member = Member(guild, role=True)
        await command(adapter, member, "!register mine pw Sysadmin")
        adapter.engine.register("theirs", "pw", "Bard", Platform.IRC, "theirs")
        m = await command(adapter, member, "!merge theirs pw")
        assert "folded into mine" in m.replies[0]
        assert adapter.engine.find_player("theirs") is None

    @pytest.mark.asyncio
    async def test_merge_in_a_channel_is_refused_and_deleted(self, adapter, guild):
        member = Member(guild, role=True)
        await command(adapter, member, "!register mine pw Sysadmin")
        adapter.engine.register("theirs", "pw", "Bard", Platform.IRC, "theirs")
        m = await command(adapter, member, "!merge theirs pw", channel=Channel())
        assert m.deleted
        assert "DM" in member.dms[0]
        assert adapter.engine.find_player("theirs") is not None


async def _noop():
    return None


class TestRestarts:
    """Discord logins need nothing remembered: the account id is durable and
    the role, re-read on every start, says who is playing."""

    @pytest.mark.asyncio
    async def test_a_restart_keeps_role_holders_playing(self, adapter, guild, monkeypatch):
        await command(adapter, Member(guild, role=True), "!register rusty pw Sysadmin")
        fresh = DiscordAdapter(adapter.engine, channel_id=GAME_CHANNEL,
                               optin_channel_id=CHAN_ID, optin_role_id=ROLE_ID)
        monkeypatch.setattr(fresh, "ensure_optin_message", _noop)
        await fresh.on_ready()
        assert presence(fresh) is Presence.ACTIVE
