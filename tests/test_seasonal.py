"""Tests for what seasons do beyond their words: twists and honours."""

from __future__ import annotations

import datetime as dt
import random
from functools import partial

import pytest
from sqlalchemy import create_engine as sa_engine
from sqlalchemy.orm import Session

from idlerpg import achievements, auth, events, fairness, lore, prestige, seasonal, web
from idlerpg import engine as engine_module
from idlerpg.engine import Engine
from idlerpg.models import Base, Platform
from idlerpg.rules import Curve

DAY = 86400
START = 1_790_000_000           # a moment in 2026


@pytest.fixture
def engine(monkeypatch):
    monkeypatch.setattr(engine_module, "hash_password",
                        partial(auth.hash_password, iterations=1))
    monkeypatch.setattr(seasonal, "now", lambda: START)
    db = sa_engine("sqlite://")
    Base.metadata.create_all(db)
    with Session(db) as session:
        yield Engine(session, Curve(), rng=random.Random(1))


def player(engine, name, level=20):
    p = engine.register(name, "pw", "x", Platform.IRC, name)
    p.level, p.next_ttl = level, 100_000
    engine.session.commit()
    return p


def by_the_calendar(monkeypatch, day=(10, 1)):
    monkeypatch.setattr(lore, "SEASON_OVERRIDE", None)
    monkeypatch.setattr(lore, "today", lambda: dt.date(2026, *day))


def later(monkeypatch, days):
    monkeypatch.setattr(seasonal, "now", lambda: START + int(days * DAY))


class TestTwists:
    # calm(): no battles, godsends or the like to move a clock under the test.
    def test_midwinter_nights_are_long_for_everyone(self, engine, monkeypatch):
        a, b = player(engine, "a", 5), player(engine, "b", 40)
        monkeypatch.setattr(lore, "SEASON_OVERRIDE", "Midwinter")
        with fairness.calm():
            engine.tick(1000)
        assert a.next_ttl == b.next_ttl == 100_000 - 1050

    def test_springtide_speeds_only_those_below_the_middle(self, engine, monkeypatch):
        low, mid, high = player(engine, "low", 5), player(engine, "mid", 20), player(engine, "high", 40)
        monkeypatch.setattr(lore, "SEASON_OVERRIDE", "Springtide")
        with fairness.calm():
            engine.tick(1000)
        assert low.next_ttl == 100_000 - 1100
        assert mid.next_ttl == high.next_ttl == 100_000 - 1000

    def test_no_season_no_twist(self, engine):
        a = player(engine, "a")
        with fairness.calm():
            engine.tick(1000)
        assert a.next_ttl == 100_000 - 1000

    def test_trick_or_treat_is_even_odds_and_small(self, engine):
        p = player(engine, "p", 30)
        rng, cost = random.Random(3), events.level_cost(p, 30, engine.curve)
        treats = tricks = 0
        for _ in range(400):
            p.next_ttl = 10**7
            out = events.trick_or_treat(p, rng, engine.curve)
            moved = abs(p.next_ttl - 10**7)
            assert 0 < moved <= cost * events.TRICK_SHARE + 1
            treats += "treat" in out.message
            tricks += "trick:" in out.message
        assert treats + tricks == 400 and 160 < treats < 240

    def test_trick_or_treat_only_in_hallowtide(self, engine, monkeypatch):
        player(engine, "a")
        monkeypatch.setattr(events, "TRICK_INTERVAL", 1)       # every tick, if at all
        assert not any("knocked on a door" in o.message for o in engine.tick(60))
        monkeypatch.setattr(lore, "SEASON_OVERRIDE", "Hallowtide")
        assert any("knocked on a door" in o.message for o in engine.tick(60))


class TestHonours:
    def season(self, engine, monkeypatch, *gains, days=31):
        """Run Hallowtide by the calendar; gains[i] is how far each climbs."""
        people = [player(engine, f"p{i}") for i in range(len(gains))]
        by_the_calendar(monkeypatch, (10, 1))
        engine.tick(1)                                          # it begins
        for p, gained in zip(people, gains):
            p.next_ttl -= gained
        engine.session.commit()
        later(monkeypatch, days)
        by_the_calendar(monkeypatch, (11, 1))
        said = [o.message for o in engine.tick(1)]              # it ends
        return people, said

    def test_the_three_most_devoted_are_honoured_and_named(self, engine, monkeypatch):
        (a, b, c, d), said = self.season(engine, monkeypatch, 90_000, 50_000, 70_000, 10)
        assert any("most devoted idlers: p0, p2 and p1" in m for m in said)
        assert [x.title for x in seasonal.best(a.achievements)] == [
            "the most devoted idler of Hallowtide 2026"]
        assert "third most" in seasonal.best(b.achievements)[0].title
        assert d.achievements == []

    def test_keeping_half_the_season_earns_the_badge(self, engine, monkeypatch):
        # Five people, so the fifth is no placegetter: they keep it by idling.
        people, _ = self.season(engine, monkeypatch, 9_000_000, 8_000_000, 7_000_000,
                                31 * DAY * 0.6, 31 * DAY * 0.4)
        kept, idle = people[3], people[4]
        honours = seasonal.best(kept.achievements)
        assert [a.title for a in honours] == ["kept Hallowtide 2026"] and honours[0].badge == "🎃"
        assert achievements.has(kept, "hallowtide-kept")      # and the achievement
        assert seasonal.best(idle.achievements) == []

    def test_a_forced_or_short_season_honours_nobody(self, engine, monkeypatch):
        p = player(engine, "p")
        monkeypatch.setattr(lore, "SEASON_OVERRIDE", "Hallowtide")
        engine.tick(1)
        p.next_ttl -= 90_000
        later(monkeypatch, 31)
        monkeypatch.setattr(lore, "SEASON_OVERRIDE", "off")
        said = [o.message for o in engine.tick(1)]
        assert p.achievements == [] and not any("honours" in m for m in said)

    def test_npcs_are_not_honoured(self, engine, monkeypatch):
        engine.npc_max, engine.npc_realm = 1, 12
        (p,), _ = self.season(engine, monkeypatch, 90_000)
        npc = next(x for x in engine.all_players() if x.npc)
        assert npc.achievements == [] and p.achievements

    def test_a_prestige_keeps_what_the_season_gave(self, engine, monkeypatch):
        p = player(engine, "p", 60)
        by_the_calendar(monkeypatch, (10, 1))
        engine.tick(1)
        before = seasonal.progress(p, engine.curve)
        p.next_ttl -= 50_000                      # climbed in the season...
        prestige.start_over(engine, p)            # ...then started over
        later(monkeypatch, 31)
        by_the_calendar(monkeypatch, (11, 1))
        engine.tick(1)
        assert before > 0 and any("most devoted" in a.title for a in p.achievements)


class TestShown:
    def test_whoami_names_the_honours(self, engine):
        p = player(engine, "p")
        seasonal._award(engine, p, "Hallowtide 2026:kept", "🎃", "kept Hallowtide 2026")
        seasonal._award(engine, p, "Hallowtide 2026:2", "🎃",
                        "the second most devoted idler of Hallowtide 2026")
        assert seasonal.honours_text(p) == (
            " Honours: the second most devoted idler of Hallowtide 2026.")

    def test_the_standings_wear_the_badge(self):
        cell = web.honours({"honours": [{"badge": "🎃", "title": "kept Hallowtide 2026",
                                         "won": False}]})
        assert "🎃" in cell and 'title="kept Hallowtide 2026"' in cell

    def test_the_seasons_table_has_each_twist(self):
        page = web.page_game()
        assert all(web.E(s.twist) in page for s in lore.SEASONS)    # "realm's", escaped
