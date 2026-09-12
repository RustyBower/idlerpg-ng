"""Tests for items left lying on the map."""

from __future__ import annotations

import datetime as dt
import random
from functools import partial

import pytest
from sqlalchemy import create_engine as sa_engine, select
from sqlalchemy.orm import Session

from idlerpg import auth, events, ground
from idlerpg import engine as engine_module
from idlerpg.engine import Engine
from idlerpg.models import Base, GroundItem, Platform, utcnow
from idlerpg.rules import Curve


@pytest.fixture
def engine(monkeypatch):
    monkeypatch.setattr(engine_module, "hash_password",
                        partial(auth.hash_password, iterations=1))
    db = sa_engine("sqlite://")
    Base.metadata.create_all(db)
    with Session(db) as session:
        yield Engine(session, Curve(), rng=random.Random(1))


def player(engine, name, level=20, items=10, at=(100, 100)):
    p = engine.register(name, "pw", "Wanderer", Platform.IRC, name)
    p.level = level
    p.x, p.y = at
    for item in p.items:
        item.value = items
    engine.session.commit()
    return p


def lie(engine, slot="shield", value=50, at=(100, 100), tag="", by="someone"):
    item = GroundItem(slot=slot, value=value, x=at[0], y=at[1], tag=tag, left_by=by)
    engine.session.add(item)
    engine.session.commit()
    return item


def on_ground(engine):
    return list(engine.session.scalars(select(GroundItem)))


def held(player, slot):
    return next(i for i in player.items if i.slot == slot)


class TestDropping:
    def test_a_good_replaced_item_is_left_behind(self, engine):
        p = player(engine, "rusty", items=10, at=(7, 9))
        ground.drop(engine.session, p, "shield", 20, "")
        engine.session.commit()
        [item] = on_ground(engine)
        assert (item.slot, item.value) == ("shield", 20)
        assert (item.x, item.y) == (7, 9)
        assert item.left_by == "rusty"

    def test_an_empty_slot_leaves_nothing(self, engine):
        p = player(engine, "rusty")
        ground.drop(engine.session, p, "shield", 0, "")
        engine.session.commit()
        assert on_ground(engine) == []

    def test_junk_is_discarded_as_it_always_was(self, engine):
        p = player(engine, "rusty", items=10)
        ground.drop(engine.session, p, "shield", 11, "")   # under 1.25x their own
        engine.session.commit()
        assert on_ground(engine) == []

    def test_a_unique_is_always_worth_leaving(self, engine):
        p = player(engine, "rusty", items=40)
        assert ground.worth_leaving(p, 3, "courser") is True
        assert ground.worth_leaving(p, 3, "") is False

    def test_worth_leaving_measures_against_the_dropper(self, engine):
        poor = player(engine, "poor", items=10)
        rich = player(engine, "rich", items=100)
        # The same item is a find to one character and litter to the other.
        assert ground.worth_leaving(poor, 20) is True
        assert ground.worth_leaving(rich, 20) is False

    def test_a_find_reports_what_it_displaced(self, engine):
        p = player(engine, "rusty", level=40, items=5)
        rng = random.Random(3)
        found = None
        while found is None:
            found = events.find_item(p, rng)
        slot, value, _ = found.dropped
        assert value == 5                      # what they were carrying
        assert "left where they stood" in found.message


class TestRotting:
    def test_what_nobody_came_for_rots(self, engine):
        fresh = lie(engine, value=40)
        old = lie(engine, value=41)
        old.dropped = utcnow() - ground.LIES_FOR - dt.timedelta(hours=1)
        engine.session.commit()
        assert ground.sweep(engine.session) == 1
        engine.session.commit()
        assert [i.id for i in on_ground(engine)] == [fresh.id]


class TestPickingUp:
    def test_something_better_within_reach_is_taken(self, engine):
        p = player(engine, "rusty", items=10, at=(100, 100))
        lie(engine, slot="shield", value=50, at=(101, 102), by="alice")
        [said] = ground.pickups(engine.session, [p])
        assert held(p, "shield").value == 50
        assert "left behind by alice" in said.message
        assert said.kind == "pickup" and said.player_id == p.id

    def test_a_good_old_one_takes_its_place(self, engine):
        p = player(engine, "rusty", items=10)
        held(p, "shield").value = 30          # well above their own average
        engine.session.commit()
        lie(engine, slot="shield", value=50)
        ground.pickups(engine.session, [p])
        engine.session.commit()
        [left] = on_ground(engine)
        assert (left.slot, left.value, left.left_by) == ("shield", 30, "rusty")

    def test_a_poor_old_one_is_not_left_lying(self, engine):
        p = player(engine, "rusty", items=10)
        lie(engine, slot="shield", value=50)
        ground.pickups(engine.session, [p])
        engine.session.commit()
        # Their old level 10 was junk by their own standard, so it goes the
        # way the original throws things away rather than littering the map.
        assert on_ground(engine) == []

    def test_a_worse_item_is_left_alone(self, engine):
        p = player(engine, "rusty", items=10)
        lie(engine, slot="shield", value=4)
        assert ground.pickups(engine.session, [p]) == []
        assert held(p, "shield").value == 10
        assert len(on_ground(engine)) == 1

    def test_out_of_reach_is_out_of_reach(self, engine):
        p = player(engine, "rusty", items=10, at=(100, 100))
        lie(engine, slot="shield", value=99, at=(100 + ground.REACH + 1, 100))
        assert ground.pickups(engine.session, [p]) == []
        assert held(p, "shield").value == 10

    def test_one_each_a_turn(self, engine):
        p = player(engine, "rusty", items=10)
        lie(engine, slot="shield", value=50)
        lie(engine, slot="helm", value=60)
        assert len(ground.pickups(engine.session, [p])) == 1

    def test_the_best_within_reach_is_the_one_taken(self, engine):
        p = player(engine, "rusty", items=10)
        lie(engine, slot="shield", value=30)
        lie(engine, slot="helm", value=70)
        ground.pickups(engine.session, [p])
        assert held(p, "helm").value == 70
        assert held(p, "shield").value == 10

    def test_two_cannot_take_the_same_one(self, engine):
        a = player(engine, "alice", items=10, at=(100, 100))
        b = player(engine, "bob", items=10, at=(100, 100))
        lie(engine, slot="shield", value=50)
        said = ground.pickups(engine.session, [a, b])
        assert len(said) == 1
        assert (held(a, "shield").value, held(b, "shield").value) in {(50, 10), (10, 50)}

    def test_a_unique_keeps_its_tag_and_the_old_tag_goes_with_the_old_item(self, engine):
        p = player(engine, "rusty", items=10)
        held(p, "shield").tag = "old"
        engine.session.commit()
        lie(engine, slot="shield", value=80, tag="courser")
        ground.pickups(engine.session, [p])
        engine.session.commit()
        assert held(p, "shield").tag == "courser"
        [left] = on_ground(engine)
        assert (left.value, left.tag) == (10, "old")


class TestTheMap:
    """What is lying about has to be visible, or nobody goes looking for it."""

    def test_it_draws_what_is_lying_about(self):
        from idlerpg import web
        lost = [{"what": "shield", "value": 42, "x": 120, "y": 88, "left_by": "alice"}]
        players = [{"username": "rusty", "level": 32, "x": 100, "y": 100,
                    "online": True, "platforms": {"irc": True}}]
        html = web.page_map(players, None, lost)
        assert 'class="lost"' in html                       # the marker itself
        assert "level 42 shield" in html                    # and what it is
        assert "left by alice" in html                      # and whose it was
        assert "k-lost" in html                             # the legend swatch
        assert "lie where they were replaced" in html

    def test_a_clean_map_says_nothing_about_litter(self):
        from idlerpg import web
        html = web.page_map([], None, [])
        assert "lie where they were replaced" not in html
        assert 'class="lost"' not in html

    def test_the_map_keeps_itself_current(self):
        from idlerpg import web
        html = web.page_map([], None, [])
        assert "DOMParser" in html          # swaps the map in without a reload
        assert 'content="300"' in html      # a slow fallback without JavaScript


class TestThroughTheEngine:
    def test_a_tick_picks_things_up(self, engine):
        p = player(engine, "rusty", items=10, at=(100, 100))
        lie(engine, slot="shield", value=50, at=(100, 100))
        said = engine.tick(1)
        assert any(o.kind == "pickup" for o in said)
        assert held(p, "shield").value == 50
