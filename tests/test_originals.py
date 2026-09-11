"""Tests for the last of the original bot's features: a battle on levelling,
the eight named uniques and their mount, and the good-and-evil modifier -
plus the rival and the trophy that came with them."""

from __future__ import annotations

import random
from functools import partial

import pytest
from sqlalchemy import create_engine as sa_engine
from sqlalchemy.orm import Session

from idlerpg import achievements as ach
from idlerpg import auth, events, fairness, quests
from idlerpg import engine as engine_module
from idlerpg.engine import Engine
from idlerpg.models import Base, Platform
from idlerpg.rules import Curve


@pytest.fixture
def engine(monkeypatch):
    monkeypatch.setattr(engine_module, "hash_password",
                        partial(auth.hash_password, iterations=1))
    db = sa_engine("sqlite://")
    Base.metadata.create_all(db)
    with Session(db) as session:
        yield Engine(session, Curve(), rng=random.Random(4))


def player(engine, name, level=30, items=20, next_ttl=100_000):
    p = engine.register(name, "pw", "x", Platform.IRC, name)
    p.level, p.next_ttl = level, next_ttl
    for item in p.items:
        item.value = items
    engine.session.commit()
    return p


class TestABattleOnLevelling:
    def test_it_happens_and_its_result_is_kept(self, engine):
        me = player(engine, "me", level=30, items=10**5, next_ttl=1)
        them = player(engine, "them", level=30, items=0)
        with fairness.calm():                      # no other battles to confuse it
            said = [o.message for o in engine.tick(2)]
        assert any("reaches level 31" in m for m in said)
        assert any("fought them" in m and "won" in m for m in said)
        # the win took time off the clock the level-up had just set
        assert me.next_ttl < int(events.level_cost(me, 31, engine.curve))

    def test_not_below_the_level_where_challenges_are_taken(self, engine):
        me = player(engine, "me", level=10, next_ttl=1)
        player(engine, "them", level=10)
        with fairness.calm():
            said = [o.message for o in engine.tick(2)]
        assert any("reaches level 11" in m for m in said)
        assert not any("fought" in m for m in said)

    def test_it_can_be_turned_off(self, engine, monkeypatch):
        monkeypatch.setattr(events, "LEVELUP_BATTLE_FROM", 999)
        me = player(engine, "me", next_ttl=1)
        player(engine, "them")
        with fairness.calm():
            said = [o.message for o in engine.tick(2)]
        assert any("reaches level" in m for m in said)
        assert not any("fought" in m for m in said)

    def test_alone_in_the_realm_nobody_is_challenged(self, engine):
        me = player(engine, "me", next_ttl=1)
        with fairness.calm():
            said = [o.message for o in engine.tick(2)]
        assert any("reaches level" in m for m in said) and not any("fought" in m for m in said)


class TestTheNamedUniques:
    def find(self, engine, p, tries=400):
        found = []
        for _ in range(tries):
            out = events.find_item(p, engine.rng)
            if out is not None and "found the" in out.message:
                found.append(out.message)
        return found

    def test_they_turn_up_named_and_only_deep_enough(self, engine):
        deep = player(engine, "deep", level=60, items=0)
        assert self.find(engine, deep), "a deep character turns up named uniques"
        shallow = player(engine, "shallow", level=26, items=0)
        names = " ".join(self.find(engine, shallow))
        assert names, "level 26 can find the one unique it qualifies for"
        assert "the Crown of Minor Kings" not in names        # level 50 and up
        assert "the Lantern of Small Mercies" in names        # the only one so far

    def test_each_has_its_own_slot_and_they_beat_the_curve(self):
        assert len(events.UNIQUES) == 8
        assert len({u.slot for u in events.UNIQUES}) == 8
        # Worth more than the curve can roll, and more the deeper they are.
        assert all(u.value >= 75 for u in events.UNIQUES)
        gates = [(u.from_level, u.value) for u in sorted(events.UNIQUES,
                                                         key=lambda u: u.from_level)]
        assert [v for _, v in gates] == sorted(v for _, v in gates)

    def test_below_the_gate_no_uniques_at_all(self, engine):
        young = player(engine, "young", level=20, items=0)
        assert self.find(engine, young) == []

    def test_a_roll_that_finds_nothing_named_is_an_ordinary_find(self, engine):
        """No nameless uniques any more: they out-numbered the eight and made
        their worth meaningless."""
        p = player(engine, "mid", level=26, items=0)
        for _ in range(2000):
            events.find_item(p, engine.rng)
        tagged = {i.slot: i.tag for i in p.items if i.tag}
        assert set(tagged) <= {"charm"}          # only the Lantern is his yet
        assert all(i.value < 75 for i in p.items if not i.tag)


class TestTheMount:
    def party(self, engine, mounted: bool):
        party = [player(engine, f"q{i}", level=45) for i in range(4)]
        for p in party:
            p.x, p.y = 200, 200
        if mounted:
            party[0].items[0].tag = events.MOUNT_TAG
        quests.start(engine.session, party, engine.rng, 500, 500)
        quest = quests.active_quest(engine.session)
        quest.kind, quest.stage, quest.x1, quest.y1 = 2, 1, 0, 200
        engine.session.commit()
        quests.steer(engine.session, quests.JOURNEY_PACE * 10, engine.rng)
        return party

    def test_one_mount_carries_the_whole_party(self, engine):
        walked = self.party(engine, mounted=True)
        assert all(p.x == 200 - 10 * (1 + events.STRIDE_PER_RANK * events.MOUNT_STRIDE)
                   for p in walked)

    def test_without_one_they_walk(self, engine):
        walked = self.party(engine, mounted=False)
        assert all(p.x == 190 for p in walked)


class TestGoodAndEvil:
    def test_off_by_default_and_a_tenth_either_way_when_on(self, engine, monkeypatch):
        good = player(engine, "good", items=10)
        engine.set_alignment(good, "good")
        assert events.battle_strength(good) == 100
        monkeypatch.setitem(events.MORAL_BATTLE, "good", 1.1)
        monkeypatch.setitem(events.MORAL_BATTLE, "evil", 0.9)
        assert events.battle_strength(good) == 110
        evil = player(engine, "evil", items=10)
        engine.set_alignment(evil, "evil")
        assert events.battle_strength(evil) == 90


class TestRivalsAndTrophies:
    def test_a_rival_is_whoever_beats_you_most(self, engine):
        me, bully, other = (player(engine, "me"), player(engine, "bully"),
                            player(engine, "other"))
        ach.on_fight(bully, me)
        ach.on_fight(bully, me)
        ach.on_fight(other, me)
        engine.session.commit()
        assert ach.rival(engine, me) == "bully" and ach.rival(engine, bully) is None

    def test_a_trophy_for_beating_someone_far_above(self, engine):
        small, giant = player(engine, "small", level=20), player(engine, "giant", level=45)
        said = ach.on_fight(small, giant)
        assert any("trophy" in o.message for o in said)
        assert [k.name for k in small.keepsakes] == ["a trophy taken from giant"]
        ach.on_fight(small, giant)                       # not twice off the same head
        assert len(small.keepsakes) == 1

    def test_no_trophy_for_a_near_match(self, engine):
        a, b = player(engine, "a", level=20), player(engine, "b", level=25)
        ach.on_fight(a, b)
        assert a.keepsakes == []

    def test_the_rival_is_shown_where_a_character_is(self, engine):
        from idlerpg import web
        me, bully = player(engine, "me"), player(engine, "bully")
        ach.on_fight(bully, me)
        engine.session.commit()
        assert ach.rival_line(engine, me) == " Rival: bully."
        assert ach.rival_line(engine, bully) == ""
        # and on the site, from the tallies already loaded
        names = {me.id: "me", bully.id: "bully"}
        assert web.rival_name(me, names) == "bully"
        assert web.rival_name(bully, names) == ""
