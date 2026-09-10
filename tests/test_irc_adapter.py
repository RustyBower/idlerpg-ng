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
