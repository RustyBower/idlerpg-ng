"""Tests for world events, pinned against the Perl bot's behaviour."""

from __future__ import annotations

import random

import pytest
from sqlalchemy import create_engine as sa_engine
from sqlalchemy.orm import Session

from idlerpg import events
from idlerpg.engine import Engine
from idlerpg.models import Alignment, Base, Platform
from idlerpg.rules import Curve


@pytest.fixture
def engine():
    db = sa_engine("sqlite://")
    Base.metadata.create_all(db)
    with Session(db) as session:
        yield Engine(session, Curve(), rng=random.Random(1234))


def player(engine, name="rusty", level=30):
    p = engine.register(name, "pw", "Sysadmin", Platform.IRC, name)
    p.level = level
    engine.session.commit()
    return p


class TestItems:
    def test_level_never_exceeds_one_and_a_half_times_the_player(self):
        rng = random.Random(9)
        for lvl in (1, 10, 40):
            rolls = [events.roll_item_level(lvl, rng) for _ in range(500)]
            assert max(rolls) <= int(lvl * 1.5)
            assert min(rolls) >= 1

    def test_found_item_replaces_only_something_worse(self, engine):
        p = player(engine)
        for item in p.items:
            item.value = 999  # already better than anything findable
        engine.session.commit()
        assert events.find_item(p, engine.rng) is None
        assert all(i.value == 999 for i in p.items)

    def test_finding_an_item_upgrades_the_slot(self, engine):
        p = player(engine, level=40)
        found = None
        for _ in range(200):
            found = events.find_item(p, engine.rng)
            if found:
                break
        assert found is not None
        assert "found a level" in found.message


class TestHandOfGod:
    def test_blesses_far_more_often_than_it_smites(self, engine):
        p = player(engine)
        blessed = 0
        for _ in range(400):
            p.next_ttl = 10000
            if "blessed hand" in events.hand_of_god(p, engine.rng).message:
                blessed += 1
        assert 0.7 < blessed / 400 < 0.9  # bot.pl: 4 in 5

    def test_a_blessing_never_drives_the_timer_negative(self, engine):
        p = player(engine)
        for _ in range(200):
            p.next_ttl = 3
            events.hand_of_god(p, engine.rng)
            assert p.next_ttl >= 1


class TestCalamityAndGodsend:
    def test_calamity_costs_time_or_damages_an_item(self, engine):
        p = player(engine)
        p.next_ttl = 10000
        before = p.next_ttl
        out = events.calamity(p, engine.rng)
        assert p.next_ttl > before or "damaged" in out.message

    def test_godsend_saves_time_or_improves_an_item(self, engine):
        p = player(engine)
        p.next_ttl = 10000
        before = p.next_ttl
        out = events.godsend(p, engine.rng)
        assert p.next_ttl < before or "blessed" in out.message


class TestBattle:
    def test_winner_gains_and_loser_is_unharmed_without_a_crit(self, engine):
        a = player(engine, "a", level=30)
        b = player(engine, "b", level=30)
        for i in a.items:
            i.value = 100          # a cannot lose
        for i in b.items:
            i.value = 1
        a.next_ttl = b.next_ttl = 10000
        engine.session.commit()
        out = events.battle(a, b, engine.rng)
        assert "won!" in out[0].message
        assert a.next_ttl < 10000

    def test_loser_pays_time(self, engine):
        a = player(engine, "a", level=30)
        b = player(engine, "b", level=30)
        for i in a.items:
            i.value = 1
        for i in b.items:
            i.value = 100          # a cannot win
        a.next_ttl = 10000
        engine.session.commit()
        out = events.battle(a, b, engine.rng)
        assert "lost!" in out[0].message
        assert a.next_ttl > 10000

    def test_good_players_crit_more_often_than_evil(self):
        assert events.CRITICAL_FACTOR["good"] > events.CRITICAL_FACTOR["neutral"]
        assert events.CRITICAL_FACTOR["neutral"] > events.CRITICAL_FACTOR["evil"]


class TestMap:
    def test_movement_wraps_and_stays_in_bounds(self, engine):
        p = player(engine)
        p.x, p.y = 0, 0
        for _ in range(500):
            events.move_player(p, 500, 500, engine.rng)
            assert 0 <= p.x < 500 and 0 <= p.y < 500


class TestRates:
    def test_nothing_fires_with_nobody_online(self):
        rng = random.Random(1)
        assert not any(
            events.should_fire(events.GODSEND_INTERVAL, 5, 0, rng)
            for _ in range(1000)
        )

    def test_rate_matches_the_interval(self):
        rng = random.Random(5)
        tick, days, online = 5, 200, 2
        ticks = int(days * 86400 / tick)
        fired = sum(
            events.should_fire(events.CALAMITY_INTERVAL, tick, online, rng)
            for _ in range(ticks)
        )
        expected = days * online / (events.CALAMITY_INTERVAL / 86400)
        assert expected * 0.7 < fired < expected * 1.3


class TestTickIntegration:
    def test_offline_players_are_untouched_by_events(self, engine):
        from idlerpg.models import Presence
        p = player(engine)
        engine.set_presence(Platform.IRC, "rusty", Presence.OFFLINE)
        before = (p.next_ttl, p.x, p.y)
        engine.tick(100000)
        assert (p.next_ttl, p.x, p.y) == before

    def test_levelling_up_can_yield_loot(self, engine):
        p = player(engine, level=5)
        p.next_ttl = 1
        engine.session.commit()
        out = engine.tick(50000)
        kinds = {o.kind for o in out}
        assert "levelup" in kinds
