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
