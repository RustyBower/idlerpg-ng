"""Tests for quests, team battles, war and the alignment events."""

from __future__ import annotations

import random
from datetime import timedelta

import pytest
from sqlalchemy import create_engine as sa_engine
from sqlalchemy.orm import Session

from idlerpg import events, quests
from idlerpg.engine import Engine
from idlerpg.models import Alignment, Base, Platform, Quest, utcnow
from idlerpg.rules import Curve, Penalty


@pytest.fixture
def engine():
    db = sa_engine("sqlite://")
    Base.metadata.create_all(db)
    with Session(db) as session:
        yield Engine(session, Curve(), rng=random.Random(99))


_seq = [0]


def make(engine, n, level=45, alignment=Alignment.NEUTRAL):
    out = []
    for i in range(n):
        _seq[0] += 1
        name = f"p{_seq[0]}"
        p = engine.register(name, "pw", "Wanderer", Platform.IRC, name)
        p.level = level
        p.alignment = alignment
        for item in p.items:
            item.value = 10 + i
        out.append(p)
    engine.session.commit()
    return out


class TestTeamBattle:
    def test_needs_six_players(self, engine):
        five = make(engine, 5)
        assert events.team_battle(five, engine.rng, 500, 500) == []

    def test_six_players_produces_a_result(self, engine):
        six = make(engine, 6)
        out = events.team_battle(six, engine.rng, 500, 500)
        assert len(out) == 1
        assert "team battled" in out[0].message
        assert ("won!" in out[0].message) or ("lost!" in out[0].message)

    def test_stake_is_a_fifth_of_the_smallest_winner_clock(self, engine):
        six = make(engine, 6)
        for p in six:
            p.next_ttl = 1000
        engine.session.commit()
        events.team_battle(six, engine.rng, 500, 500)
        # Winners lose 200, losers gain it; either way somebody moved by 200.
        assert any(abs(p.next_ttl - 1000) == 200 for p in six)


class TestWar:
    def test_needs_players(self, engine):
        assert events.war(make(engine, 2), engine.rng, 500, 500) == []

    def test_names_a_winning_quadrant(self, engine):
        players = make(engine, 8)
        # Spread them into the four corners.
        for i, p in enumerate(players):
            p.x = 50 if i % 2 else 450
            p.y = 50 if i < 4 else 450
        engine.session.commit()
        out = events.war(players, engine.rng, 500, 500)
        assert out and "prevailed" in out[0].message

    def test_winners_move_closer_and_losers_fall_back(self, engine):
        class TopRoll:  # every army rolls its full strength
            def randrange(self, n):
                return n - 1

        ne, se, sw, nw = make(engine, 4)
        placed = {ne: (450, 50, 100), se: (450, 450, 50),
                  sw: (50, 450, 10), nw: (50, 50, 50)}
        for p, (x, y, value) in placed.items():
            p.x, p.y, p.next_ttl = x, y, 1000
            for item in p.items:
                item.value = value
        engine.session.commit()
        out = events.war([ne, se, sw, nw], TopRoll(), 500, 500)
        # NE (1000) beats both neighbours; SW (100) loses to both.
        assert ne.next_ttl == 850
        assert sw.next_ttl == 1150
        assert se.next_ttl == nw.next_ttl == 1000
        assert "set back 15%" in out[0].message


class TestAlignment:
    def test_goodness_needs_two_good_players(self, engine):
        one = make(engine, 1, alignment=Alignment.GOOD)
        assert events.goodness(one, engine.rng) == []

    def test_goodness_rewards_both(self, engine):
        good = make(engine, 2, alignment=Alignment.GOOD)
        for p in good:
            p.next_ttl = 10000
        engine.session.commit()
        out = events.goodness(good, engine.rng)
        assert out
        assert all(p.next_ttl < 10000 for p in good)

    def test_evilness_needs_an_evil_player(self, engine):
        assert events.evilness(make(engine, 2, alignment=Alignment.GOOD), engine.rng) == []

    def test_evilness_either_steals_or_costs(self, engine):
        evil = make(engine, 1, alignment=Alignment.EVIL)
        good = make(engine, 1, alignment=Alignment.GOOD)
        good[0].name = "saint"
        for i in good[0].items:
            i.value = 500
        evil[0].next_ttl = 10000
        engine.session.commit()
        out = events.evilness(evil + good, engine.rng)
        assert out
        assert "stole" in out[0].message or evil[0].next_ttl > 10000


class TestQuests:
    def test_needs_four_players_above_the_level_floor(self, engine):
        low = make(engine, 4, level=10)
        assert quests.start(engine.session, low, engine.rng, 500, 500) is None
        three = make(engine, 3, level=45)
        assert quests.start(engine.session, three, engine.rng, 500, 500) is None

    def test_starting_a_quest_picks_four(self, engine):
        party = make(engine, 6, level=45)
        out = quests.start(engine.session, party, engine.rng, 500, 500)
        assert out is not None
        quest = quests.active_quest(engine.session)
        assert len(quest.participants) == 4

    def test_timed_quest_completes_and_reduces_burden(self, engine):
        party = make(engine, 4, level=45)
        quests.start(engine.session, party, engine.rng, 500, 500)
        quest = quests.active_quest(engine.session)
        quest.kind = 1
        quest.expires = utcnow() - timedelta(seconds=1)
        for p in party:
            p.next_ttl = 1000
        engine.session.commit()
        out = quests.advance(engine.session, quest, engine.rng)
        assert out and "completing their quest" in out[0].message
        assert all(p.next_ttl == 750 for p in party)   # 25% removed
        assert quests.active_quest(engine.session) is None

    def test_journey_needs_both_waypoints(self, engine):
        party = make(engine, 4, level=45)
        quests.start(engine.session, party, engine.rng, 500, 500)
        quest = quests.active_quest(engine.session)
        quest.kind, quest.stage = 2, 1
        quest.x1, quest.y1, quest.x2, quest.y2 = 10, 10, 20, 20
        engine.session.commit()

        assert quests.advance(engine.session, quest, engine.rng) == []  # nobody there
        for p in party:
            p.x, p.y = 10, 10
        engine.session.commit()
        out = quests.advance(engine.session, quest, engine.rng)
        assert out and "first waypoint" in out[0].message
        assert quest.stage == 2

        for p in party:
            p.x, p.y = 20, 20
        engine.session.commit()
        out = quests.advance(engine.session, quest, engine.rng)
        assert out and "completing their quest" in out[0].message

    def test_a_quester_speaking_sets_the_party_back_and_nobody_else(self, engine):
        from idlerpg.models import PenaltyRecord
        from idlerpg.rules import penalty_seconds
        party = make(engine, 4, level=45)
        bystander = engine.register("watcher", "pw", "Bard", Platform.IRC, "watcher")
        bystander.next_ttl = 1000
        for p in party:
            p.next_ttl = 1000
        quests.start(engine.session, party, engine.rng, 500, 500)
        engine.session.commit()

        spoke = engine.penalise(party[0], Penalty.MESSAGE, message_length=20)
        assert quests.active_quest(engine.session) is None
        step = penalty_seconds(Penalty.QUEST, 45)
        assert party[0].next_ttl == 1000 + spoke + step   # their slip, and the vow
        assert all(p.next_ttl == 1000 + step for p in party[1:])
        assert bystander.next_ttl == 1000                  # not their quest
        records = engine.session.query(PenaltyRecord).filter_by(kind="quest").all()
        assert len(records) == 4
        assert any("wrath of the gods upon the quest" in o.message for o in engine._pending)

    def test_no_quest_for_twelve_hours_after_a_failure(self, engine):
        from idlerpg.models import Setting
        party = make(engine, 4, level=45)
        quests.start(engine.session, party, engine.rng, 500, 500)
        engine.session.commit()
        engine.penalise(party[0], Penalty.MESSAGE, message_length=5)
        assert quests.start(engine.session, party, engine.rng, 500, 500) is None
        rest = engine.session.get(Setting, quests.REST_KEY)
        rest.value = (utcnow() - timedelta(seconds=1)).isoformat()
        engine.session.commit()
        assert quests.start(engine.session, party, engine.rng, 500, 500) is not None

    def _journey(self, engine, party, target=(10, 10), then=(20, 20)):
        quests.start(engine.session, party, engine.rng, 500, 500)
        quest = quests.active_quest(engine.session)
        quest.kind, quest.stage = 2, 1
        quest.x1, quest.y1 = target
        quest.x2, quest.y2 = then
        quest.expires = utcnow() + quests.JOURNEY_TIMEOUT
        engine.session.commit()
        return quest

    def test_the_party_walks_to_the_waypoint(self, engine):
        party = make(engine, 4, level=45)
        for i, p in enumerate(party):
            p.x, p.y = 100 + i * 7, 300 - i * 11
        outsider = make(engine, 1, level=45)[0]
        outsider.x, outsider.y = 400, 400
        quest = self._journey(engine, party)
        walked = quests.steer(engine.session, quests.JOURNEY_PACE * 1000, engine.rng)
        assert walked == {p.id for p in party}
        assert all((p.x, p.y) == (10, 10) for p in party)
        assert (outsider.x, outsider.y) == (400, 400)
        out = quests.advance(engine.session, quest, engine.rng)
        assert out and "first waypoint" in out[0].message

    def test_the_pace_is_a_step_per_half_minute(self, engine):
        party = make(engine, 4, level=45)
        for p in party:
            p.x, p.y = 200, 200
        self._journey(engine, party, target=(0, 200))
        quests.steer(engine.session, quests.JOURNEY_PACE * 10, engine.rng)
        assert all(p.x == 190 for p in party)

    def test_the_tick_walks_questers_instead_of_drifting_them(self, engine):
        party = make(engine, 4, level=45)
        for p in party:
            p.x, p.y = 200, 200
        self._journey(engine, party, target=(0, 200))
        engine.tick(quests.JOURNEY_PACE * 10)
        assert all((p.x, p.y) == (190, 200) for p in party)

    def test_a_journey_out_of_time_is_abandoned_without_blame(self, engine):
        party = make(engine, 4, level=45)
        for p in party:
            p.next_ttl = 1000
        quest = self._journey(engine, party)
        quest.expires = utcnow() - timedelta(seconds=1)
        engine.session.commit()
        out = quests.advance(engine.session, quest, engine.rng)
        assert out and "abandoned" in out[0].message
        assert quests.active_quest(engine.session) is None
        assert all(p.next_ttl == 1000 for p in party)

    def test_a_journey_names_its_road(self, engine):
        party = make(engine, 4, level=45)
        for seed in range(20):
            engine.rng.seed(seed)
            out = quests.start(engine.session, party, engine.rng, 500, 500)
            quest = quests.active_quest(engine.session)
            if quest.kind == 2:
                assert "Their road runs to [" in out.message
                return
            engine.session.delete(quest)
            engine.session.commit()
        pytest.fail("no journey in 20 seeds")

    def test_a_non_quester_speaking_does_not_fail_it(self, engine):
        party = make(engine, 4, level=45)
        outsider = engine.register("nosy", "pw", "Bard", Platform.IRC, "nosy")
        quests.start(engine.session, party, engine.rng, 500, 500)
        engine.session.commit()
        engine.penalise(outsider, Penalty.MESSAGE, message_length=20)
        assert quests.active_quest(engine.session) is not None
