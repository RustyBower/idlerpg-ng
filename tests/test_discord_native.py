"""Tests for Discord's slash commands, the Play button and its forms, and the
/whoami card. No network: interactions are stand-ins."""

from __future__ import annotations

import pytest

from idlerpg.adapters.discord_adapter import LoginModal, PlayView, RegisterModal
from idlerpg.models import Platform, Presence
from tests.test_discord_optin import NoteChannel, start
from tests.test_discord_presence import ROLE_ID, USER_ID, Member, guild, adapter  # noqa: F401


class Response:
    def __init__(self):
        self.sent, self.modal, self._done = [], None, False

    def is_done(self):
        return self._done

    async def send_message(self, content=None, *, embed=None, ephemeral=False):
        self.sent.append((content, embed, ephemeral))
        self._done = True

    async def send_modal(self, modal):
        self.modal = modal
        self._done = True


class Followup:
    def __init__(self):
        self.sent = []

    async def send(self, content=None, *, ephemeral=False):
        self.sent.append((content, ephemeral))


class Interaction:
    def __init__(self, user):
        self.user = user
        self.response = Response()
        self.followup = Followup()


class TestPrivateReplies:
    @pytest.mark.asyncio
    async def test_every_reply_is_ephemeral_however_many(self, adapter, guild):
        i = Interaction(Member(guild))
        reply = adapter._private_reply(i)
        await reply("one")
        await reply("two")
        assert i.response.sent == [("one", None, True)]
        assert i.followup.sent == [("two", True)]


class TestThePlayButton:
    @pytest.mark.asyncio
    async def test_someone_new_gets_the_form_and_it_registers_them(self, adapter, guild):
        member = Member(guild, role=False)
        i = Interaction(member)
        await adapter.play(i)
        assert isinstance(i.response.modal, RegisterModal)
        form = Interaction(member)
        await i.response.modal.submit(form, "rusty", "hunter2", "")
        assert adapter.engine.find_player("rusty").character_class == "adventurer"
        assert member.added == [ROLE_ID]
        assert "Welcome" in form.response.sent[0][0] and form.response.sent[0][2]
        assert "hunter2" not in form.response.sent[0][0]

    @pytest.mark.asyncio
    async def test_a_player_is_seated_at_once(self, adapter, guild):
        member = Member(guild, role=False)
        await adapter.run_command(member, "register", ["rusty", "pw", "x"],
                                  adapter._private_reply(Interaction(member)))
        member.roles = []                         # lost the role since
        i = Interaction(member)
        await adapter.play(i)
        assert i.response.modal is None
        assert "playing as rusty" in i.response.sent[0][0]
        assert adapter.engine.find_identity(Platform.DISCORD, str(USER_ID)).presence \
            is Presence.ACTIVE

    @pytest.mark.asyncio
    async def test_the_irc_form_logs_in_and_links(self, adapter, guild):
        adapter.engine.register("rusty", "pw", "x", Platform.IRC, "rusty")
        member = Member(guild)
        i = Interaction(member)
        await LoginModal(adapter).submit(i, "rusty", "pw")
        assert "Logged in as rusty" in i.response.sent[0][0]
        assert adapter.engine.player_for(Platform.DISCORD, str(USER_ID)).name == "rusty"

    @pytest.mark.asyncio
    async def test_the_note_carries_the_buttons_once(self, adapter, monkeypatch):
        chan = NoteChannel()
        await start(adapter, monkeypatch, chan)
        await start(adapter, monkeypatch, chan)
        note = next(iter(chan.messages.values()))
        assert chan.sent == 1 and isinstance(note.components[0], PlayView)
        ids = {c.custom_id for c in note.components[0].children}
        assert ids == {"idlerpg:play", "idlerpg:login"}


class TestSlashCommands:
    def test_every_command_is_there(self, adapter):
        names = {c.name for c in adapter.tree.get_commands()}
        assert {"register", "login", "merge", "newpass", "removeme", "align", "whoami",
                "fight", "achievements", "prestige", "perks", "perk", "help", "admin"} <= names

    @pytest.mark.asyncio
    async def test_a_command_runs_and_answers_privately(self, adapter, guild):
        member = Member(guild)
        i = Interaction(member)
        await adapter.run_command(member, "register", ["rusty", "pw", "x"],
                                  adapter._private_reply(i))
        assert i.response.sent[0][2] is True

    @pytest.mark.asyncio
    async def test_without_the_scope_the_bang_commands_carry_on(self, adapter):
        assert await adapter.sync_commands() is False      # no application, no server


class TestTheCard:
    @pytest.mark.asyncio
    async def test_whoami_is_a_card(self, adapter, guild):
        member = Member(guild)
        await adapter.run_command(member, "register", ["rusty", "pw", "Sysadmin"],
                                  adapter._private_reply(Interaction(member)))
        p = adapter.engine.find_player("rusty")
        p.title = "the Blossoming"
        embed = adapter.card(p)
        fields = {f.name: f.value for f in embed.fields}
        assert embed.title == "rusty, the Blossoming"
        assert {"Next level", "Alignment", "Items", "Achievements"} <= set(fields)
        assert fields["Alignment"] == "true neutral"
