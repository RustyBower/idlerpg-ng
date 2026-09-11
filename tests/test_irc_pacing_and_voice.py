"""Tests for the IRC adapter's paced output and its voicing of the logged in."""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine as sa_engine
from sqlalchemy.orm import Session

from idlerpg.adapters.irc import IRCAdapter, parse
from idlerpg.config import Config
from idlerpg.engine import Engine
from idlerpg.models import Base
from idlerpg.rules import Curve
from tests.test_irc_adapter import FakeWriter

CHANNEL = "#idlerpg"


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


@pytest.fixture
def irc():
    db = sa_engine("sqlite://")
    Base.metadata.create_all(db)
    with Session(db) as session:
        a = IRCAdapter(Engine(session, Curve()), Config())
        a.writer = FakeWriter()
        yield a


def feed(a, line):
    a.handle(parse(line))


def paced(a):
    clock = Clock()
    a.clock, a.refilled, a.paced = clock, clock(), True
    return clock


def modes(a):
    return [line for line in a.writer.lines if line.startswith("MODE ")]


class TestPacing:
    def test_unpaced_everything_goes_at_once(self, irc):
        for i in range(10):
            irc.say(f"news {i}")
        assert len(irc.writer.lines) == 10

    def test_a_burst_then_one_every_interval(self, irc):
        clock = paced(irc)
        for i in range(10):
            irc.say(f"news {i}")
        assert len(irc.writer.lines) == irc.SEND_BURST
        clock.now += irc.SEND_INTERVAL
        irc.flush()
        assert len(irc.writer.lines) == irc.SEND_BURST + 1
        for _ in range(10):                     # never more than a burst at once
            clock.now += irc.SEND_INTERVAL
            irc.flush()
        assert [line.split(":", 1)[1] for line in irc.writer.lines] == [
            f"news {i}" for i in range(10)]

    def test_a_long_quiet_spell_earns_only_one_burst(self, irc):
        clock = paced(irc)
        for i in range(12):
            irc.say(f"news {i}")
        clock.now += 3600
        irc.flush()
        assert len(irc.writer.lines) == 2 * irc.SEND_BURST

    def test_replies_jump_ahead_of_channel_news(self, irc):
        clock = paced(irc)
        for i in range(8):
            irc.say(f"news {i}")
        irc.notice("rusty", "your reply")
        clock.now += irc.SEND_INTERVAL
        irc.flush()
        assert irc.writer.lines[-1] == "NOTICE rusty :your reply"

    def test_the_protocol_never_waits(self, irc):
        paced(irc)
        for i in range(10):
            irc.say(f"news {i}")
        feed(irc, "PING :abc")
        assert irc.writer.lines[-1] == "PONG :abc"


class TestVoice:
    def opped(self, irc, *others):
        feed(irc, f":server 353 idlerpg = {CHANNEL} :@idlerpg {' '.join(others)}")

    def test_logging_in_voices_when_we_hold_ops(self, irc):
        self.opped(irc, "rusty")
        feed(irc, ":rusty!u@h PRIVMSG idlerpg :REGISTER rusty pw Sysadmin")
        assert modes(irc) == [f"MODE {CHANNEL} +v rusty"]

    def test_without_ops_nothing_is_asked(self, irc):
        feed(irc, f":server 353 idlerpg = {CHANNEL} :idlerpg rusty")
        feed(irc, ":rusty!u@h PRIVMSG idlerpg :REGISTER rusty pw Sysadmin")
        assert modes(irc) == []

    def test_logging_out_takes_it_away(self, irc):
        self.opped(irc, "rusty")
        feed(irc, ":rusty!u@h PRIVMSG idlerpg :REGISTER rusty pw Sysadmin")
        feed(irc, ":rusty!u@h PRIVMSG idlerpg :LOGOUT")
        assert modes(irc)[-1] == f"MODE {CHANNEL} -v rusty"

    def test_leaving_needs_no_devoice(self, irc):
        self.opped(irc, "rusty")
        feed(irc, ":rusty!u@h PRIVMSG idlerpg :REGISTER rusty pw Sysadmin")
        feed(irc, f":rusty!u@h PART {CHANNEL}")
        assert modes(irc) == [f"MODE {CHANNEL} +v rusty"]
        assert "rusty" not in irc.voiced

    def test_joining_after_logging_in_voices(self, irc):
        self.opped(irc)
        feed(irc, ":rusty!u@h PRIVMSG idlerpg :REGISTER rusty pw Sysadmin")
        assert modes(irc) == []                      # not in the channel yet
        feed(irc, f":rusty!u@h JOIN {CHANNEL}")
        assert modes(irc) == [f"MODE {CHANNEL} +v rusty"]

    def test_gaining_ops_voices_everyone_logged_in(self, irc):
        feed(irc, f":server 353 idlerpg = {CHANNEL} :idlerpg a b c d e")
        for n in "abcde":
            feed(irc, f":{n}!u@h PRIVMSG idlerpg :REGISTER {n}{n}{n} pw X")
        feed(irc, f":ChanServ!s@services MODE {CHANNEL} +o idlerpg")
        assert modes(irc) == [f"MODE {CHANNEL} +vvvv a b c d",
                              f"MODE {CHANNEL} +v e"]

    def test_a_voice_follows_a_nick_change(self, irc):
        self.opped(irc, "rusty")
        feed(irc, ":rusty!u@h PRIVMSG idlerpg :REGISTER rusty pw Sysadmin")
        feed(irc, ":rusty!u@h NICK :rusty2")
        assert modes(irc) == [f"MODE {CHANNEL} +v rusty"]   # none asked again
        assert "rusty2" in irc.voiced

    def test_losing_ops_stops_it(self, irc):
        self.opped(irc, "rusty")
        feed(irc, f":ChanServ!s@services MODE {CHANNEL} -o idlerpg")
        feed(irc, ":rusty!u@h PRIVMSG idlerpg :REGISTER rusty pw Sysadmin")
        assert modes(irc) == []

    def test_a_mode_line_with_other_parameters_is_read_right(self, irc):
        self.opped(irc, "rusty", "bob")
        feed(irc, f":op!u@h MODE {CHANNEL} +lbv 20 *!*@spam bob")
        assert "bob" in irc.voiced and "20" not in irc.voiced

    def test_who_flags_carry_voice_and_our_ops(self, irc):
        feed(irc, f":server 352 idlerpg {CHANNEL} bot host srv idlerpg Hr@ :0 IdleRPG")
        feed(irc, f":server 352 idlerpg {CHANNEL} u h srv rusty Hr+ :0 Rusty")
        assert irc.ranks == {"o"} and "rusty" in irc.voiced

    def test_it_can_be_turned_off(self, irc):
        irc.cfg.voice = False
        self.opped(irc, "rusty")
        feed(irc, ":rusty!u@h PRIVMSG idlerpg :REGISTER rusty pw Sysadmin")
        assert modes(irc) == []
