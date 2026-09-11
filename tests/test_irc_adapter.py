"""Tests for the IRC adapter: parsing, presence and penalties."""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine as sa_engine
from sqlalchemy.orm import Session

from idlerpg.adapters.irc import IRCAdapter, parse
from idlerpg.config import Config
from idlerpg.engine import Engine
from idlerpg.models import Base, Platform, Presence
from idlerpg.rules import Curve


class FakeWriter:
    def __init__(self):
        self.lines = []

    def write(self, data):
        self.lines.append(data.decode().rstrip("\r\n"))


@pytest.fixture
def adapter():
    db = sa_engine("sqlite://")
    Base.metadata.create_all(db)
    with Session(db) as session:
        engine = Engine(session, Curve())
        a = IRCAdapter(engine, Config())
        a.writer = FakeWriter()
        yield a


def feed(adapter, line):
    adapter.handle(parse(line))


def sent(adapter):
    return "\n".join(adapter.writer.lines)


class TestParsing:
    @pytest.mark.parametrize(
        "line,command",
        [
            (":n!u@h PRIVMSG #idlerpg :hi", "PRIVMSG"),
            ("PING :xyz", "PING"),
            (":n!u@h QUIT :bye", "QUIT"),
        ],
    )
    def test_commands_parse(self, line, command):
        assert parse(line).command == command

    def test_empty_line_is_ignored(self):
        assert parse("") is None


class TestProtocol:
    def test_ping_is_answered(self, adapter):
        feed(adapter, "PING :abc123")
        assert "PONG :abc123" in sent(adapter)

    def test_joins_channel_on_welcome(self, adapter):
        feed(adapter, ":server 001 idlerpg :Welcome")
        assert f"JOIN {adapter.cfg.channel}" in sent(adapter)


class TestRegistrationFlow:
    def test_register_then_whoami(self, adapter):
        feed(adapter, ":rusty!u@h PRIVMSG idlerpg :REGISTER rusty hunter2 Sysadmin")
        assert adapter.engine.find_player("rusty") is not None
        adapter.writer.lines.clear()
        feed(adapter, ":rusty!u@h PRIVMSG idlerpg :WHOAMI")
        assert "level 0" in sent(adapter)

    def test_bad_password_does_not_log_in(self, adapter):
        feed(adapter, ":rusty!u@h PRIVMSG idlerpg :REGISTER rusty hunter2 Sysadmin")
        adapter.bound.clear()
        feed(adapter, ":other!u@h PRIVMSG idlerpg :LOGIN rusty wrong")
        assert adapter.character_for_nick("other") is None

    def test_register_is_not_echoed_to_the_channel(self, adapter):
        """The password must never reach a public channel."""
        feed(adapter, ":rusty!u@h PRIVMSG idlerpg :REGISTER rusty hunter2 Sysadmin")
        assert "hunter2" not in sent(adapter)


class TestPenaltiesAndPresence:
    def _registered(self, adapter):
        feed(adapter, ":rusty!u@h JOIN #idlerpg")
        feed(adapter, ":rusty!u@h PRIVMSG idlerpg :REGISTER rusty hunter2 Sysadmin")
        return adapter.engine.find_player("rusty")

    def test_talking_costs_time(self, adapter):
        p = self._registered(adapter)
        before = p.next_ttl
        feed(adapter, ":rusty!u@h PRIVMSG #idlerpg :hello everyone")
        assert p.next_ttl > before

    def test_a_longer_message_costs_more(self, adapter):
        p = self._registered(adapter)
        start = p.next_ttl
        feed(adapter, ":rusty!u@h PRIVMSG #idlerpg :hi")
        short = p.next_ttl - start
        mid = p.next_ttl
        feed(adapter, ":rusty!u@h PRIVMSG #idlerpg :" + "x" * 200)
        assert (p.next_ttl - mid) > short

    def test_parting_penalises_and_goes_offline(self, adapter):
        p = self._registered(adapter)
        before = p.next_ttl
        feed(adapter, ":rusty!u@h PART #idlerpg")
        assert p.next_ttl > before
        assert not p.is_idling

    def test_quitting_goes_offline(self, adapter):
        p = self._registered(adapter)
        feed(adapter, ":rusty!u@h QUIT :bye")
        assert p.identities[0].presence is Presence.OFFLINE

    def test_kick_penalises_the_victim(self, adapter):
        p = self._registered(adapter)
        before = p.next_ttl
        feed(adapter, ":op!u@h KICK #idlerpg rusty :out")
        assert p.next_ttl > before

    def test_nick_change_penalises_but_stays_online(self, adapter):
        p = self._registered(adapter)
        before = p.next_ttl
        feed(adapter, ":rusty!u@h NICK :rusty_afk")
        assert p.next_ttl > before
        assert p.is_idling
        assert adapter.character_for_nick("rusty_afk") is not None

    def test_strangers_are_ignored(self, adapter):
        """Someone talking without a character must not raise."""
        feed(adapter, ":nobody!u@h PRIVMSG #idlerpg :hello")
        feed(adapter, ":nobody!u@h QUIT :bye")


class TestNickServSelfRegistration:
    """Anope here runs db_sql, which overwrites rows it did not write itself,
    so registering through NickServ is the only thing that sticks."""

    def _configured(self, adapter, email="ops@129irc.com", password="s3cret"):
        adapter.cfg.nickserv_email = email
        adapter.cfg.nickserv_password = password
        return adapter

    def test_registers_when_services_say_unregistered(self, adapter):
        self._configured(adapter)
        feed(adapter, ":NickServ!s@services PRIVMSG idlerpg :hi")  # not a NOTICE
        feed(adapter, ":NickServ!s@services NOTICE idlerpg :Nick idlerpg is not registered.")
        assert "PRIVMSG NickServ :REGISTER s3cret ops@129irc.com" in sent(adapter)

    def test_only_registers_once_per_connection(self, adapter):
        self._configured(adapter)
        for _ in range(3):
            feed(adapter, ":NickServ!s@services NOTICE idlerpg :is not registered.")
        assert sent(adapter).count("REGISTER") == 1

    def test_ignores_notices_from_impostors(self, adapter):
        self._configured(adapter)
        feed(adapter, ":evil!u@h NOTICE idlerpg :your nick is not registered.")
        assert "REGISTER" not in sent(adapter)

    def test_does_nothing_without_an_email(self, adapter):
        self._configured(adapter, email="")
        feed(adapter, ":NickServ!s@services NOTICE idlerpg :is not registered.")
        assert "REGISTER" not in sent(adapter)

    def test_identifies_on_connect(self, adapter):
        self._configured(adapter)
        feed(adapter, ":server 001 idlerpg :Welcome")
        assert "PRIVMSG NickServ :IDENTIFY s3cret" in sent(adapter)

    def test_nickserv_password_never_reaches_the_channel(self, adapter):
        self._configured(adapter)
        feed(adapter, ":server 001 idlerpg :Welcome")
        feed(adapter, ":NickServ!s@services NOTICE idlerpg :is not registered.")
        channel_lines = [
            l for l in adapter.writer.lines
            if l.startswith(f"PRIVMSG {adapter.cfg.channel}")
        ]
        assert not any("s3cret" in l for l in channel_lines)


class TestMergeCommand:
    def _registered(self, adapter, nick="rusty", name="rusty"):
        feed(adapter, f":{nick}!u@h PRIVMSG idlerpg :REGISTER {name} pw Sysadmin")
        return adapter.engine.find_player(name)

    def test_merge_absorbs_the_named_character(self, adapter):
        self._registered(adapter, "rusty", "keeper")
        adapter.engine.register("other", "pw", "Wizard", Platform.DISCORD, "999")
        adapter.writer.lines.clear()
        feed(adapter, ":rusty!u@h PRIVMSG idlerpg :MERGE other pw")
        assert "folded into keeper" in sent(adapter)
        assert adapter.engine.find_player("other") is None
        assert adapter.engine.player_for(Platform.DISCORD, "999").name == "keeper"

    def test_merge_requires_being_logged_in(self, adapter):
        feed(adapter, ":stranger!u@h PRIVMSG idlerpg :MERGE other pw")
        assert "Log in as the character to keep" in sent(adapter)

    def test_a_wrong_password_is_reported_not_raised(self, adapter):
        self._registered(adapter)
        adapter.engine.register("other", "pw", "Wizard", Platform.DISCORD, "999")
        adapter.writer.lines.clear()
        feed(adapter, ":rusty!u@h PRIVMSG idlerpg :MERGE other nope")
        assert "Cannot merge" in sent(adapter)
        assert adapter.engine.find_player("other") is not None


class TestLoginReachesIRC:
    """Logging in is linking: a character from Discord must earn on IRC too,
    and stop earning when it leaves."""

    def _discord_character(self, adapter, name="rusty"):
        e = adapter.engine
        p = e.register(name, "pw", "Sysadmin", Platform.DISCORD, "999")
        e.set_presence(Platform.DISCORD, "999", Presence.OFFLINE)
        for nick in ("rusty", "other", "old"):  # all sitting in the channel
            feed(adapter, f":{nick}!u@h JOIN #idlerpg")
        return p

    def _irc(self, p):
        return [i for i in p.identities if i.platform is Platform.IRC]

    def test_login_gives_a_discord_character_an_irc_identity(self, adapter):
        p = self._discord_character(adapter)
        feed(adapter, ":rusty!u@h PRIVMSG idlerpg :LOGIN rusty pw")
        assert len(self._irc(p)) == 1
        assert p.is_idling

    def test_quitting_irc_stops_it_earning(self, adapter):
        p = self._discord_character(adapter)
        feed(adapter, ":rusty!u@h PRIVMSG idlerpg :LOGIN rusty pw")
        feed(adapter, ":rusty!u@h QUIT :bye")
        assert not p.is_idling

    def test_logging_in_again_reuses_the_identity(self, adapter):
        p = self._discord_character(adapter)
        feed(adapter, ":rusty!u@h PRIVMSG idlerpg :LOGIN rusty pw")
        feed(adapter, ":rusty!u@h QUIT :bye")
        feed(adapter, ":other!u@h PRIVMSG idlerpg :LOGIN rusty pw")
        assert len(self._irc(p)) == 1
        assert p.is_idling

    def test_a_merge_from_discord_does_not_leave_irc_earning(self, adapter):
        """The absorbed IRC identity keeps its old name; the nick bound to it
        must still take it offline when it quits."""
        e = adapter.engine
        feed(adapter, ":old!u@h PRIVMSG idlerpg :REGISTER oldirc pw Sysadmin")
        keeper = self._discord_character(adapter, "keeper")
        e.merge_by_password(keeper, "oldirc", "pw")  # as !merge on Discord does
        assert keeper.is_idling  # still sitting on IRC as "old"
        feed(adapter, ":old!u@h QUIT :bye")
        assert not keeper.is_idling

    def test_logging_in_after_a_merge_uses_the_absorbed_identity(self, adapter):
        e = adapter.engine
        e.register("oldirc", "pw", "Sysadmin", Platform.IRC, "oldirc")
        keeper = self._discord_character(adapter, "keeper")
        e.merge_by_password(keeper, "oldirc", "pw")
        e.reset_presence(Platform.IRC)
        feed(adapter, ":rusty!u@h PRIVMSG idlerpg :LOGIN keeper pw")
        assert len(self._irc(keeper)) == 1
        assert keeper.is_idling
        feed(adapter, ":rusty!u@h QUIT :bye")
        assert not keeper.is_idling

    def test_a_new_connection_clears_stale_irc_presence_until_resumed(self, adapter):
        """Recorded before a restart, bound to no nick: must not keep earning."""
        p = adapter.engine.register("rusty", "pw", "Sysadmin", Platform.IRC, "rusty")
        assert p.is_idling
        feed(adapter, ":server 001 idlerpg :Welcome")
        assert not p.is_idling


MASK = "rusty!ident@cloak.example"


class TestLoginsSurviveRestarts:
    """The original bot's autologin: a connection the bot never saw leave is
    logged back in by nick!user@host when the bot returns."""

    def _logged_in(self, adapter, name="rusty", mask=MASK):
        feed(adapter, f":{mask} PRIVMSG idlerpg :REGISTER {name} pw Sysadmin")
        return adapter.engine.find_player(name)

    def _restart(self, adapter):
        fresh = IRCAdapter(adapter.engine, Config())
        fresh.writer = FakeWriter()
        feed(fresh, ":server 001 idlerpg :Welcome")
        feed(fresh, ":idlerpg!bot@bot.host JOIN #idlerpg")
        return fresh

    def _who(self, adapter, nick="rusty", user="ident", host="cloak.example"):
        feed(adapter, f":server 352 idlerpg #idlerpg {user} {host} irc.server "
                      f"{nick} H :0 Real Name")
        feed(adapter, ":server 315 idlerpg #idlerpg :End of /WHO list.")

    def test_the_bot_asks_who_is_here_once_it_has_joined(self, adapter):
        fresh = self._restart(adapter)
        assert "WHO #idlerpg" in sent(fresh)

    def test_a_login_is_resumed_after_a_restart(self, adapter):
        p = self._logged_in(adapter)
        fresh = self._restart(adapter)
        assert not p.is_idling  # nobody is anybody until WHO answers
        self._who(fresh)
        assert p.is_idling
        feed(fresh, f":{MASK} PRIVMSG idlerpg :WHOAMI")
        assert "rusty, level 0" in sent(fresh)

    def test_the_same_nick_from_elsewhere_is_not(self, adapter):
        p = self._logged_in(adapter)
        fresh = self._restart(adapter)
        self._who(fresh, host="somewhere.else")
        assert not p.is_idling

    def test_turning_up_later_resumes_too(self, adapter):
        p = self._logged_in(adapter)
        fresh = self._restart(adapter)
        self._who(fresh, nick="someone_else", user="x", host="y")
        feed(fresh, f":{MASK} JOIN #idlerpg")
        assert p.is_idling

    def test_quitting_ends_the_login(self, adapter):
        p = self._logged_in(adapter)
        feed(adapter, f":{MASK} QUIT :Quit: bye")
        self._who(self._restart(adapter))
        assert not p.is_idling

    def test_logging_out_ends_the_login(self, adapter):
        p = self._logged_in(adapter)
        feed(adapter, f":{MASK} PRIVMSG idlerpg :LOGOUT")
        self._who(self._restart(adapter))
        assert not p.is_idling

    def test_parting_ends_the_login(self, adapter):
        p = self._logged_in(adapter)
        feed(adapter, f":{MASK} PART #idlerpg")
        fresh = self._restart(adapter)
        feed(fresh, f":{MASK} JOIN #idlerpg")
        assert not p.is_idling

    def test_a_nick_change_carries_the_login(self, adapter):
        p = self._logged_in(adapter)
        feed(adapter, f":{MASK} NICK :rusty_away")
        self._who(self._restart(adapter), nick="rusty_away")
        assert p.is_idling

    def test_the_connection_follows_the_latest_login(self, adapter):
        first = self._logged_in(adapter, "first")
        second = self._logged_in(adapter, "second")  # same connection
        self._who(self._restart(adapter))
        assert second.is_idling
        assert not first.is_idling

    def test_a_netsplit_costs_nothing_and_resumes_on_rejoin(self, adapter):
        p = self._logged_in(adapter)
        before = p.next_ttl
        feed(adapter, f":{MASK} QUIT :irc.east.example irc.west.example")
        assert p.next_ttl == before
        assert not p.is_idling
        feed(adapter, f":{MASK} JOIN #idlerpg")
        assert p.is_idling

    def test_an_ordinary_quit_is_not_mistaken_for_a_split(self, adapter):
        p = self._logged_in(adapter)
        before = p.next_ttl
        feed(adapter, f":{MASK} QUIT :Quit: irc.east.example irc.west.example")
        assert p.next_ttl > before

    def test_joins_to_other_channels_are_ignored(self, adapter):
        p = self._logged_in(adapter)
        fresh = self._restart(adapter)
        feed(fresh, f":{MASK} JOIN #elsewhere")
        assert not p.is_idling


class TestAlignCommand:
    def _registered(self, adapter):
        feed(adapter, ":rusty!u@h PRIVMSG idlerpg :REGISTER rusty pw Sysadmin")
        adapter.writer.lines.clear()
        return adapter.engine.find_player("rusty")

    def test_align_changes_it(self, adapter):
        from idlerpg.models import Alignment
        p = self._registered(adapter)
        feed(adapter, ":rusty!u@h PRIVMSG idlerpg :ALIGN evil")
        assert p.alignment is Alignment.EVIL
        assert "You are now neutral evil" in sent(adapter)

    def test_without_an_argument_it_explains(self, adapter):
        self._registered(adapter)
        feed(adapter, ":rusty!u@h PRIVMSG idlerpg :ALIGN")
        assert "You are true neutral" in sent(adapter)
        assert "critical hits" in sent(adapter)

    def test_nonsense_is_reported(self, adapter):
        self._registered(adapter)
        feed(adapter, ":rusty!u@h PRIVMSG idlerpg :ALIGN sideways")
        assert "Cannot align" in sent(adapter)

    def test_it_needs_a_login(self, adapter):
        feed(adapter, ":stranger!u@h PRIVMSG idlerpg :ALIGN good")
        assert "Log in first" in sent(adapter)

    def test_help_mentions_it(self, adapter):
        feed(adapter, ":rusty!u@h PRIVMSG idlerpg :HELP")
        assert "ALIGN" in sent(adapter)


class TestYouEarnOnlyInTheChannel:
    """The game is sitting in the channel. Logged in from anywhere else, a
    character is logged in but earns nothing until the nick joins."""

    def test_logging_in_from_outside_earns_nothing_until_you_join(self, adapter):
        feed(adapter, ":rusty!u@h PRIVMSG idlerpg :REGISTER rusty pw Sysadmin")
        p = adapter.engine.find_player("rusty")
        assert not p.is_idling
        assert "Join #idlerpg to start idling" in sent(adapter)
        feed(adapter, ":rusty!u@h JOIN #idlerpg")
        assert p.is_idling

    def test_the_names_list_counts_as_being_here(self, adapter):
        feed(adapter, ":server 353 idlerpg = #idlerpg :@op +rusty other")
        feed(adapter, ":rusty!u@h PRIVMSG idlerpg :REGISTER rusty pw Sysadmin")
        assert adapter.engine.find_player("rusty").is_idling
        assert "Join #idlerpg" not in sent(adapter)

    def test_a_second_login_ends_the_first(self, adapter):
        feed(adapter, ":rusty!u@h JOIN #idlerpg")
        feed(adapter, ":rusty!u@h PRIVMSG idlerpg :REGISTER first pw Sysadmin")
        feed(adapter, ":rusty!u@h PRIVMSG idlerpg :REGISTER second pw Sysadmin")
        assert adapter.engine.find_player("second").is_idling
        assert not adapter.engine.find_player("first").is_idling

    def test_one_of_two_nicks_leaving_keeps_the_character_earning(self, adapter):
        feed(adapter, ":a!u@h JOIN #idlerpg")
        feed(adapter, ":b!u@h JOIN #idlerpg")
        feed(adapter, ":a!u@h PRIVMSG idlerpg :REGISTER rusty pw Sysadmin")
        feed(adapter, ":b!u@h PRIVMSG idlerpg :LOGIN rusty pw")
        p = adapter.engine.find_player("rusty")
        feed(adapter, ":a!u@h QUIT :Quit: bye")
        assert p.is_idling
        feed(adapter, ":b!u@h QUIT :Quit: bye")
        assert not p.is_idling

    def test_parting_another_channel_is_ignored(self, adapter):
        feed(adapter, ":rusty!u@h JOIN #idlerpg")
        feed(adapter, ":rusty!u@h PRIVMSG idlerpg :REGISTER rusty pw Sysadmin")
        p = adapter.engine.find_player("rusty")
        before = p.next_ttl
        feed(adapter, ":rusty!u@h PART #elsewhere")
        assert p.is_idling
        assert p.next_ttl == before


class TestNickInUse:
    """Usually our own connection from before a restart, not yet timed out.
    Without handling, the new connection never finishes registering."""

    def _stand_in(self, adapter, password=None):
        adapter.cfg.nickserv_password = password
        feed(adapter, ":server 433 * idlerpg :Nickname is already in use")
        feed(adapter, ":server 001 idlerpg_ :Welcome")

    def test_a_taken_nick_gets_a_stand_in(self, adapter):
        feed(adapter, ":server 433 * idlerpg :Nickname is already in use")
        assert "NICK idlerpg_" in sent(adapter)

    def test_services_are_asked_to_remove_the_ghost(self, adapter):
        self._stand_in(adapter, "s3cret")
        assert "PRIVMSG NickServ :GHOST idlerpg s3cret" in sent(adapter)
        assert "IDENTIFY" not in sent(adapter)

    def test_the_nick_is_taken_back_and_identified(self, adapter):
        self._stand_in(adapter, "s3cret")
        adapter.writer.lines.clear()
        feed(adapter, ":NickServ!s@services NOTICE idlerpg_ :Ghost with your nick has been killed.")
        assert "NICK idlerpg" in adapter.writer.lines
        feed(adapter, ":idlerpg_!bot@bot.host NICK :idlerpg")
        assert adapter.nick == "idlerpg"
        assert "PRIVMSG NickServ :IDENTIFY s3cret" in sent(adapter)

    def test_without_services_the_ghost_quitting_frees_it(self, adapter):
        self._stand_in(adapter)
        adapter.writer.lines.clear()
        feed(adapter, ":idlerpg!old@bot.host QUIT :Ping timeout: 240 seconds")
        assert "NICK idlerpg" in adapter.writer.lines

    def test_commands_reach_the_stand_in(self, adapter):
        self._stand_in(adapter)
        adapter.writer.lines.clear()
        feed(adapter, ":rusty!u@h PRIVMSG idlerpg_ :HELP")
        assert "REGISTER" in sent(adapter)

    def test_joining_as_the_stand_in_still_asks_who(self, adapter):
        self._stand_in(adapter)
        feed(adapter, ":idlerpg_!bot@bot.host JOIN #idlerpg")
        assert "WHO #idlerpg" in sent(adapter)


class TestNothingGarblesTheChannel:
    def test_announcements_drop_bidi_and_colour(self, adapter):
        adapter.say("\u202eprofit-on-irc \x0304,02\x02won")
        assert adapter.writer.lines[-1] == "PRIVMSG #idlerpg :profit-on-irc won"

    def test_notices_too(self, adapter):
        adapter.notice("rusty", "hi \u202eflip")
        assert adapter.writer.lines[-1] == "NOTICE rusty :hi flip"

    def test_a_garbled_name_is_refused_at_registration(self, adapter):
        feed(adapter, ":profit!u@h PRIVMSG idlerpg :REGISTER \u202eprofit pw Rogue")
        assert "Cannot register" in sent(adapter)
        assert adapter.engine.find_player("\u202eprofit") is None


class TestCtcp:
    """Clients send VERSION on their own; it used to get the HELP text."""

    def test_version_is_answered_and_help_is_not(self, adapter):
        from idlerpg import __version__
        feed(adapter, ":rusty!u@h PRIVMSG idlerpg :\x01VERSION\x01")
        assert f"\x01VERSION idlerpg-ng {__version__}" in sent(adapter)
        assert "REGISTER" not in sent(adapter)

    def test_ping_is_echoed(self, adapter):
        feed(adapter, ":rusty!u@h PRIVMSG idlerpg :\x01PING 12345\x01")
        assert "NOTICE rusty :\x01PING 12345\x01" in adapter.writer.lines

    def test_other_ctcp_is_ignored(self, adapter):
        feed(adapter, ":rusty!u@h PRIVMSG idlerpg :\x01TIME\x01")
        assert adapter.writer.lines == []

    def test_whoami_reads_like_a_person_wrote_it(self, adapter):
        feed(adapter, ":rusty!u@h PRIVMSG idlerpg :REGISTER rusty pw Sysadmin")
        adapter.writer.lines.clear()
        feed(adapter, ":rusty!u@h PRIVMSG idlerpg :WHOAMI")
        assert "next level in 10m" in sent(adapter)


class TestAccountCommands:
    def _here(self, adapter):
        feed(adapter, ":rusty!u@h JOIN #idlerpg")
        feed(adapter, ":rusty!u@h PRIVMSG idlerpg :REGISTER rusty pw Sysadmin")
        adapter.writer.lines.clear()
        return adapter.engine.find_player("rusty")

    def test_newpass(self, adapter):
        self._here(adapter)
        feed(adapter, ":rusty!u@h PRIVMSG idlerpg :NEWPASS wrong better")
        assert "Cannot change it" in sent(adapter)
        feed(adapter, ":rusty!u@h PRIVMSG idlerpg :NEWPASS pw better")
        assert "Password changed" in sent(adapter)
        assert adapter.engine.authenticate("rusty", "better") is not None

    def test_removeme(self, adapter):
        self._here(adapter)
        feed(adapter, ":rusty!u@h PRIVMSG idlerpg :REMOVEME wrong")
        assert adapter.engine.find_player("rusty") is not None
        feed(adapter, ":rusty!u@h PRIVMSG idlerpg :REMOVEME pw")
        assert adapter.engine.find_player("rusty") is None
        assert adapter.character_for_nick("rusty") is None
        assert "rusty is gone" in sent(adapter)

    def test_talking_says_what_it_cost(self, adapter):
        self._here(adapter)
        feed(adapter, ":rusty!u@h PRIVMSG #idlerpg :hello there")
        assert "NOTICE rusty :That cost you" in sent(adapter)

    def test_logging_out_says_what_it_cost(self, adapter):
        self._here(adapter)
        feed(adapter, ":rusty!u@h PRIVMSG idlerpg :LOGOUT")
        assert "Logged out. That cost you" in sent(adapter)

    def test_login_is_announced_and_resuming_is_not(self, adapter):
        self._here(adapter)
        adapter.engine.tick(1)
        feed(adapter, ":rusty!u@h PRIVMSG idlerpg :LOGIN rusty pw")
        assert any(o.kind == "login" for o in adapter.engine.tick(1))
        fresh = IRCAdapter(adapter.engine, Config())
        fresh.writer = FakeWriter()
        feed(fresh, ":server 001 idlerpg :Welcome")
        feed(fresh, ":server 352 idlerpg #idlerpg u h irc.server rusty H :0 x")
        assert not any(o.kind == "login" for o in adapter.engine.tick(1))
