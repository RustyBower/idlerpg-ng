"""Tests for FIGHT: who may fight whom, when, and what moves."""

from __future__ import annotations

import random
from functools import partial

import pytest
from sqlalchemy import create_engine as sa_engine
from sqlalchemy.orm import Session

from idlerpg import auth, fights
from idlerpg import engine as engine_module
from idlerpg.engine import Engine
from idlerpg.models import Base, Platform, Presence
from idlerpg.rules import Curve, ttl

DAY = 86400


@pytest.fixture
def engine(monkeypatch):
    monkeypatch.setattr(engine_module, "hash_password",
                        partial(auth.hash_password, iterations=1))
    monkeypatch.setattr(fights, "now", lambda: 1_000_000)
    db = sa_engine("sqlite://")
    Base.metadata.create_all(db)
    with Session(db) as session:
        yield Engine(session, Curve(), rng=random.Random(1))


def fighter(engine, name, level=20, items=10, next_ttl=100_000):
    p = engine.register(name, "pw", "Brawler", Platform.IRC, name)
    p.level, p.next_ttl = level, next_ttl
    for item in p.items:
        item.value = items
    engine.session.commit()
    return p


def later(monkeypatch, seconds):
    monkeypatch.setattr(fights, "now", lambda: 1_000_000 + seconds)


def fight(engine, me, line=""):
    return fights.command(engine, me, "FIGHT", line.split())


class TestWhoMayFight:
    def test_not_yourself_and_not_nobody(self, engine):
        me = fighter(engine, "me")
        assert "yourself" in fight(engine, me, "me")
        assert "No such" in fight(engine, me, "ghost")

    def test_from_level_10(self, engine):
        me, them = fighter(engine, "me", level=9), fighter(engine, "them", level=12)
        assert "open at level 10" in fight(engine, me, "them")
        me.level, them.level = 12, 9
        assert "too new" in fight(engine, me, "them")

    def test_no_more_than_five_below_but_any_height_above(self, engine):
        me = fighter(engine, "me", level=30)
        fighter(engine, "low", level=24)
        fighter(engine, "near", level=25)
        fighter(engine, "high", level=50)
        assert "more than 5 levels below" in fight(engine, me, "low")
        assert fight(engine, me, "near").startswith(("You won", "You lost"))
        me.fight_ready_at = 0                            # a fresh day
        assert fight(engine, me, "high").startswith(("You won", "You lost"))

    def test_both_must_be_in_the_game(self, engine):
        me, them = fighter(engine, "me"), fighter(engine, "them")
        engine.set_presence(Platform.IRC, "them", Presence.OFFLINE)
        assert "not in the game" in fight(engine, me, "them")
        engine.set_presence(Platform.IRC, "me", Presence.OFFLINE)
        assert "while you are idling" in fight(engine, me, "them")

    def test_not_while_paused(self, engine, monkeypatch):
        me, _ = fighter(engine, "me"), fighter(engine, "them")
        monkeypatch.setattr(Engine, "paused", property(lambda self: True))
        assert "paused" in fight(engine, me, "them")


class TestOnceADayAndTheShield:
    def test_once_a_day(self, engine, monkeypatch):
        me = fighter(engine, "me")
        fighter(engine, "a")
        fighter(engine, "b")
        fight(engine, me, "a")
        assert "fought today" in fight(engine, me, "b")
        later(monkeypatch, DAY)
        assert "fought today" not in fight(engine, me, "b")

    def test_the_challenged_are_shielded_for_a_day(self, engine, monkeypatch):
        me, other = fighter(engine, "me"), fighter(engine, "other")
        fighter(engine, "target")
        fight(engine, me, "target")
        assert "safe for another" in fight(engine, other, "target")
        later(monkeypatch, DAY)
        assert "safe for another" not in fight(engine, other, "target")


class TestWhatMoves:
    def test_the_winner_takes_what_the_loser_loses_capped_by_their_own_level(self, engine):
        me = fighter(engine, "me", items=10**6, next_ttl=50_000)        # surely wins
        them = fighter(engine, "them", items=0, next_ttl=100_000)
        reply = fight(engine, me, "them")
        cap = int(ttl(20, engine.curve) * 0.05)          # 5% of a level-20 cost
        assert cap < 5_000                                # less than 5% of theirs
        assert "You won" in reply
        assert me.next_ttl == 50_000 - cap and them.next_ttl == 100_000 + cap

    def test_beating_a_newcomer_wins_next_to_nothing(self, engine):
        me = fighter(engine, "me", level=15, items=10**6)
        them = fighter(engine, "them", level=12, items=0, next_ttl=2_000)
        fight(engine, me, "them")
        assert 100_000 - me.next_ttl == int(2_000 * 0.05)    # 5% of their short clock

    def test_a_loss_and_the_realm_hears_of_it(self, engine):
        me = fighter(engine, "me", items=0)
        fighter(engine, "them", items=10**6)
        assert "You lost" in fight(engine, me, "them")
        said = [o.message for o in engine.tick(1)]
        assert any("challenged them" in m and "lost" in m for m in said)

    def test_a_win_past_the_end_of_the_clock_is_a_level(self, engine):
        me = fighter(engine, "me", items=10**6, next_ttl=10)
        fighter(engine, "them", items=0, next_ttl=10**7)
        fight(engine, me, "them")
        assert me.next_ttl < 0
        engine.tick(1)
        assert me.level == 21


class TestReach:
    def test_fight_alone_lists_who_is_in_reach(self, engine):
        me = fighter(engine, "me", level=30)
        fighter(engine, "peer", level=29)
        fighter(engine, "far", level=20)
        reply = fight(engine, me)
        assert "peer (29)" in reply and "far" not in reply

    def test_npcs_can_be_fought(self, engine):
        engine.npc_max, engine.npc_realm = 1, 12
        me = fighter(engine, "me", level=12)
        engine.tick(1)
        npc = next(p for p in engine.all_players() if p.npc)
        npc.level = 12
        engine.session.commit()
        assert fight(engine, me, npc.name).startswith(("You won", "You lost"))
        assert npc.shield_until             # and it is shielded, like anyone


class TestMeetings:
    def met(self, engine):
        return fights.meetings(engine, engine.online_players(), fights.now())

    def together(self, *players):
        for p in players:
            p.x, p.y = 200, 200

    def test_two_on_one_tile_fight_once_a_day(self, engine, monkeypatch):
        a = fighter(engine, "a", items=10**6, next_ttl=50_000)
        b = fighter(engine, "b", items=0, next_ttl=100_000)
        self.together(a, b)
        said = self.met(engine)                          # the fight, and First Blood
        assert "crossed paths" in said[0].message
        cap = int(ttl(20, engine.curve) * 0.05)
        assert a.next_ttl == 50_000 - cap and b.next_ttl == 100_000 + cap
        assert self.met(engine) == []                    # the same pair, the same day
        later(monkeypatch, DAY)
        assert "crossed paths" in self.met(engine)[0].message

    def test_meetings_leave_the_daily_fight_alone(self, engine):
        a, b = fighter(engine, "a"), fighter(engine, "b")
        self.together(a, b)
        self.met(engine)
        assert not a.fight_ready_at and not b.shield_until

    def test_nobody_under_10_and_nobody_offline(self, engine):
        a, b = fighter(engine, "a"), fighter(engine, "b", level=9)
        c = fighter(engine, "c")
        engine.set_presence(Platform.IRC, "c", Presence.OFFLINE)
        self.together(a, b, c)
        assert self.met(engine) == []

    def test_questers_on_one_quest_walk_together_in_peace(self, engine):
        from idlerpg import quests
        party = [fighter(engine, f"q{i}", level=45) for i in range(4)]
        quests.start(engine.session, party, engine.rng, 500, 500)
        self.together(*party)
        assert self.met(engine) == []

    def test_the_tick_finds_them(self, engine, monkeypatch):
        from idlerpg import events
        monkeypatch.setattr(events, "move_player", lambda *a: None)   # stand still
        a, b = fighter(engine, "a"), fighter(engine, "b")
        self.together(a, b)
        said = [o.message for o in engine.tick(1)]
        assert any("crossed paths" in m for m in said)


class TestOnThePlatforms:
    def test_irc(self, engine):
        from idlerpg.adapters.irc import IRCAdapter, parse
        from idlerpg.config import Config
        from tests.test_irc_adapter import FakeWriter
        irc = IRCAdapter(engine, Config())
        irc.writer = FakeWriter()
        irc.handle(parse(":me!u@h PRIVMSG idlerpg :REGISTER me pw Brawler"))
        engine.find_player("me").level = 12
        engine.session.commit()
        irc.writer.lines.clear()
        irc.handle(parse(":me!u@h PRIVMSG idlerpg :FIGHT"))
        assert "nobody is in reach" in "\n".join(irc.writer.lines)

    def test_help_mentions_it_and_fits_a_line(self, engine):
        from idlerpg.adapters.irc import IRCAdapter, parse
        from idlerpg.config import Config
        from tests.test_irc_adapter import FakeWriter
        irc = IRCAdapter(engine, Config())
        irc.writer = FakeWriter()
        irc.handle(parse(":me!u@h PRIVMSG idlerpg :HELP"))
        assert "FIGHT" in "\n".join(irc.writer.lines)
        assert all(len(line) < 450 for line in irc.writer.lines)
