"""Tests for the admin commands, the clock's handling of them, and their
wiring on both platforms."""

from __future__ import annotations

import asyncio
import random
from datetime import timedelta

import pytest
from sqlalchemy import create_engine as sa_engine
from sqlalchemy.orm import Session

from idlerpg import admin, quests
from idlerpg.__main__ import build_topic, tick_once
from idlerpg.engine import Engine, RegistrationError
from idlerpg.models import Base, EventLog, Platform, utcnow
from idlerpg.rules import Curve, Penalty


@pytest.fixture
def engine():
    db = sa_engine("sqlite://")
    Base.metadata.create_all(db)
    with Session(db) as session:
        yield Engine(session, Curve(), rng=random.Random(7))


def register(engine, name, level=10):
    p = engine.register(name, "pw", "Tester", Platform.IRC, name)
    p.level = level
    engine.session.commit()
    return p


@pytest.fixture
def boss(engine):
    p = register(engine, "Rusty")
    engine.apply_owners(["Rusty"])
    return p


def do(engine, actor, line):
    verb, *args = line.split()
    return admin.run(engine, actor, verb, args)


class TestWhoIsAnAdmin:
    def test_the_deployment_names_the_owners(self, engine, boss):
        assert boss.is_admin

    def test_everyone_else_is_refused(self, engine, boss):
        pleb = register(engine, "pleb")
        assert do(engine, pleb, "INFO") == "That is an admin command."
        assert do(engine, None, "INFO") == "That is an admin command."

    def test_mkadmin_and_deladmin(self, engine, boss):
        pleb = register(engine, "pleb")
        do(engine, boss, "MKADMIN pleb")
        assert pleb.is_admin
        do(engine, boss, "DELADMIN pleb")
        assert not pleb.is_admin

    def test_owners_cannot_be_demoted(self, engine, boss):
        assert "stays one" in do(engine, boss, "DELADMIN Rusty")
        assert boss.is_admin

    def test_an_owners_name_cannot_be_taken_once_free(self, engine):
        engine.apply_owners(["Ghost"])
        with pytest.raises(RegistrationError, match="reserved"):
            engine.register("ghost", "pw", "Tester", Platform.IRC, "ghost")


class TestRunningTheRealm:
    def test_info(self, engine, boss):
        from idlerpg import __version__
        reply = do(engine, boss, "INFO")
        assert f"idlerpg-ng {__version__}" in reply and "alignments:" in reply

    def test_pause_stops_the_clock_and_the_penalties(self, engine, boss):
        before = boss.next_ttl
        do(engine, boss, "PAUSE on")
        engine.tick(100)
        assert boss.next_ttl == before
        assert engine.penalise(boss, Penalty.PART) == 0
        do(engine, boss, "PAUSE off")
        engine.tick(100)
        assert boss.next_ttl < before

    def test_a_pause_outlives_a_restart(self, engine, boss):
        do(engine, boss, "PAUSE on")
        fresh = Engine(engine.session, Curve())
        assert fresh.paused

    def test_restart_and_topic_are_requests_for_the_clock(self, engine, boss):
        do(engine, boss, "RESTART")
        assert engine.restart_requested
        do(engine, boss, "TOPIC Welcome back")
        assert engine.topic_requested
        assert build_topic(engine, "https://site/").startswith("Welcome back | https://site/")
        do(engine, boss, "TOPIC clear")
        assert not build_topic(engine, "https://site/").startswith("Welcome")


class TestCharacters:
    def test_del_needs_confirm_and_not_yourself(self, engine, boss):
        register(engine, "victim")
        assert "confirm" in do(engine, boss, "DEL victim")
        assert engine.find_player("victim") is not None
        assert "deleted" in do(engine, boss, "DEL victim confirm")
        assert engine.find_player("victim") is None
        assert "REMOVEME" in do(engine, boss, "DEL Rusty confirm")

    def test_delold_lists_then_deletes(self, engine, boss):
        old = register(engine, "old")
        old.last_login = utcnow() - timedelta(days=40)
        engine.set_presence(Platform.IRC, "old", __import__("idlerpg").models.Presence.OFFLINE)
        register(engine, "recent")
        reply = do(engine, boss, "DELOLD 30")
        assert "old" in reply and "recent" not in reply
        assert engine.find_player("old") is not None
        do(engine, boss, "DELOLD 30 confirm")
        assert engine.find_player("old") is None
        assert engine.find_player("recent") is not None

    def test_chpass_chuser_chclass(self, engine, boss):
        p = register(engine, "who")
        do(engine, boss, "CHPASS who secret")
        assert engine.authenticate("who", "secret") is not None
        assert "Renamed to whom" in do(engine, boss, "CHUSER who whom")
        assert p.name == "whom"
        assert "taken" in do(engine, boss, "CHUSER whom Rusty")
        assert "letters" in do(engine, boss, "CHUSER whom bad‮name")
        do(engine, boss, "CHCLASS whom Grand Vizier")
        assert p.character_class == "Grand Vizier"

    def test_push_and_move(self, engine, boss):
        p = register(engine, "who")
        p.next_ttl = 1000
        do(engine, boss, "PUSH who 300")
        assert p.next_ttl == 700
        do(engine, boss, "PUSH who -100")
        assert p.next_ttl == 800
        assert "[5,6]" in do(engine, boss, "MOVE who 5 6")
        assert "realm is" in do(engine, boss, "MOVE who 9999 6")


class TestEvents:
    def test_hog_and_pit_are_announced_at_the_next_tick(self, engine, boss):
        register(engine, "a")
        register(engine, "b")
        engine.tick(1)
        assert do(engine, boss, "HOG a").startswith("Done:")
        assert do(engine, boss, "PIT a b").startswith("Done:")
        kinds = {o.kind for o in engine.tick(1)}
        assert {"hog", "battle"} <= kinds

    @pytest.mark.parametrize("kind", [k for k in admin.EVENT_KINDS if k != "quest"])
    def test_every_kind_can_be_fired(self, engine, boss, kind):
        for i in range(6):
            register(engine, f"p{i}", level=45)
        reply = do(engine, boss, f"EVENT {kind}")
        assert reply.startswith(("Done:", "Nothing happened"))

    def test_a_quest_can_be_forced_through_the_rest(self, engine, boss):
        for i in range(4):
            register(engine, f"q{i}", level=45)
        engine.set_setting(quests.REST_KEY, (utcnow() + timedelta(hours=5)).isoformat())
        assert do(engine, boss, "EVENT quest").startswith("Done:")
        assert quests.active_quest(engine.session) is not None
        assert "No quest" in do(engine, boss, "EVENT quest")   # one at a time

    def test_unknown_events_are_refused(self, engine, boss):
        assert do(engine, boss, "EVENT meteor").startswith("EVENT <")


class FakeAdapter:
    def __init__(self):
        self.said, self.topics = [], []

    async def announce(self, text):
        self.said.append(text)

    async def set_topic(self, text):
        self.topics.append(text)


class TestTheClock:
    def test_announcements_reach_every_adapter(self, engine, boss):
        a, b = FakeAdapter(), FakeAdapter()
        asyncio.run(tick_once(engine, [a, b], 1, "https://site/"))
        assert a.said and a.said == b.said

    def test_silence_still_logs_but_says_nothing(self, engine, boss):
        do(engine, boss, "SILENT on")
        a = FakeAdapter()
        asyncio.run(tick_once(engine, [a], 1, "https://site/"))
        assert a.said == []
        assert engine.session.query(EventLog).count() > 0

    def test_a_topic_request_sets_it_now(self, engine, boss):
        do(engine, boss, "TOPIC hello")
        a = FakeAdapter()
        asyncio.run(tick_once(engine, [a], 1, "https://site/"))
        assert a.topics and a.topics[0].startswith("hello | https://site/")

    def test_restart_exits_after_speaking(self, engine, boss):
        do(engine, boss, "RESTART")
        a = FakeAdapter()
        with pytest.raises(SystemExit):
            asyncio.run(tick_once(engine, [a], 1, "https://site/"))
        assert a.said   # what was queued still went out first


class TestOnThePlatforms:
    def test_irc_admin_by_private_message(self):
        from idlerpg.adapters.irc import IRCAdapter, parse
        from idlerpg.config import Config
        from tests.test_irc_adapter import FakeWriter
        db = sa_engine("sqlite://")
        Base.metadata.create_all(db)
        with Session(db) as session:
            e = Engine(session, Curve())
            irc = IRCAdapter(e, Config())
            irc.writer = FakeWriter()
            irc.handle(parse(":rusty!u@h PRIVMSG idlerpg :REGISTER Rusty pw Boss"))
            irc.handle(parse(":pleb!u@h PRIVMSG idlerpg :REGISTER pleb pw Nobody"))
            e.apply_owners(["Rusty"])
            irc.writer.lines.clear()
            irc.handle(parse(":pleb!u@h PRIVMSG idlerpg :INFO"))
            assert "admin command" in "\n".join(irc.writer.lines)
            irc.writer.lines.clear()
            irc.handle(parse(":rusty!u@h PRIVMSG idlerpg :ADMIN"))
            assert len(irc.writer.lines) >= 2          # split across notices
            assert all(len(l) < 512 for l in irc.writer.lines)
