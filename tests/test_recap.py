"""Tests for the weekly recap: when it is posted, and what it counts."""

from __future__ import annotations

import datetime as dt
import random
from functools import partial

import pytest
from sqlalchemy import create_engine as sa_engine
from sqlalchemy.orm import Session

from idlerpg import auth, recap
from idlerpg import engine as engine_module
from idlerpg.engine import Engine
from idlerpg.models import Base, Platform
from idlerpg.rules import Curve

# A Tuesday, so the next Sunday is a few days off rather than today.
NOW = 1_700_000_000


@pytest.fixture
def engine(monkeypatch):
    monkeypatch.setattr(engine_module, "hash_password",
                        partial(auth.hash_password, iterations=1))
    monkeypatch.setattr(recap, "now", lambda: NOW)
    db = sa_engine("sqlite://")
    Base.metadata.create_all(db)
    with Session(db) as session:
        yield Engine(session, Curve(), rng=random.Random(1))


def player(engine, name, level=10):
    p = engine.register(name, "pw", "Wanderer", Platform.IRC, name)
    p.level = level
    engine.session.commit()
    return p


def arm(engine, at=NOW):
    """Get past the first call, which only starts the clock."""
    assert recap.maybe(engine, at) == []


class TestWhen:
    def test_the_next_one_is_a_sunday_evening(self):
        when = dt.datetime.fromtimestamp(recap.next_due(NOW), tz=dt.timezone.utc)
        assert when.weekday() == recap.WEEKDAY
        assert (when.hour, when.minute, when.second) == (recap.HOUR, 0, 0)
        assert when.timestamp() > NOW

    def test_they_are_a_week_apart(self):
        first = recap.next_due(NOW)
        assert recap.next_due(first) == first + recap.PERIOD

    def test_the_boundary_itself_belongs_to_the_week_gone(self):
        """Asking on the hour gives the next one, never the same instant."""
        first = recap.next_due(NOW)
        assert recap.next_due(first - 1) == first


class TestArming:
    def test_the_first_call_only_starts_the_clock(self, engine):
        rusty = player(engine, "rusty")
        engine.log_event("levelup", "rusty reaches level 11", player_id=rusty.id)
        arm(engine)
        assert engine.get_setting(recap.DUE_KEY) == str(recap.next_due(NOW))
        # Whatever the log already held is behind the mark, so the first real
        # recap is about the week that follows and not about all of history.
        assert recap.lines(engine, recap._seen(engine)) == []

    def test_nothing_is_posted_before_it_is_due(self, engine):
        arm(engine)
        due = int(engine.get_setting(recap.DUE_KEY))
        player(engine, "rusty")
        assert recap.maybe(engine, due - 1) == []


class TestTheWeek:
    def seed(self, engine):
        a, b = player(engine, "alice"), player(engine, "bob")
        arm(engine)
        for level in (11, 12, 13):
            engine.log_event("levelup", f"alice reaches level {level}",
                             player_id=a.id, level=level)
        engine.log_event("levelup", "bob reaches level 11", player_id=b.id, level=11)
        engine.log_event("fight", "bob won", player_id=b.id)
        engine.log_event("fight", "bob won again", player_id=b.id)
        engine.log_event("battle", "alice won", player_id=a.id)
        engine.log_event("questdone", "the quest is complete")
        engine.log_event("item", "alice found a level 9 shield", player_id=a.id)
        engine.log_event("achievement", "bob earned something", player_id=b.id)
        engine.log_event("register", "carol joins the realm!",
                         player_id=player(engine, "carol").id)
        return a, b

    def test_it_counts_the_week(self, engine):
        self.seed(engine)
        said = " ".join(recap.lines(engine, recap._seen(engine)))
        assert "4 levels gained" in said
        assert "3 fights fought" in said      # two duels and a battle
        assert "1 quest finished" in said
        assert "1 item found" in said

    def test_it_names_who_did_it(self, engine):
        self.seed(engine)
        said = " ".join(recap.lines(engine, recap._seen(engine)))
        assert "Climbing hardest: alice (3 levels), bob (1 level)." in said
        assert "Most fights won: bob (2)." in said
        assert "New to the realm: carol." in said
        assert "1 feat earned" in said

    def test_a_quiet_week_says_nothing(self, engine):
        player(engine, "rusty")
        arm(engine)
        due = int(engine.get_setting(recap.DUE_KEY))
        assert recap.maybe(engine, due) == []
        # Still armed for the following week, though.
        assert engine.get_setting(recap.DUE_KEY) == str(recap.next_due(due))

    def test_a_week_is_counted_once(self, engine):
        self.seed(engine)
        due = int(engine.get_setting(recap.DUE_KEY))
        first = recap.maybe(engine, due)
        assert first and all(o.kind == "recap" for o in first)
        assert "4 levels gained" in first[0].message
        # The window has moved on: the same events are not reported again.
        assert recap.maybe(engine, due + recap.PERIOD) == []

    def test_someone_who_has_left_is_not_named(self, engine):
        a, _ = self.seed(engine)
        engine.session.delete(a)
        engine.session.commit()
        said = " ".join(recap.lines(engine, recap._seen(engine)))
        assert "alice" not in said
        assert "Most fights won: bob (2)." in said


class TestCommand:
    def test_a_quiet_week(self, engine):
        arm(engine)
        said = recap.command(engine, None, "RECAP", [])
        assert "Nothing has happened yet" in said
        assert "next recap is in" in said

    def test_the_week_so_far(self, engine):
        rusty = player(engine, "rusty")
        arm(engine)
        engine.log_event("levelup", "rusty reaches level 11", player_id=rusty.id)
        said = recap.command(engine, rusty, "RECAP", [])
        assert "1 level gained" in said
        assert "next recap is in" in said

    def test_asking_does_not_use_the_week_up(self, engine):
        rusty = player(engine, "rusty")
        arm(engine)
        engine.log_event("levelup", "rusty reaches level 11", player_id=rusty.id)
        recap.command(engine, rusty, "RECAP", [])
        due = int(engine.get_setting(recap.DUE_KEY))
        assert "1 level gained" in recap.maybe(engine, due)[0].message


class TestThroughTheEngine:
    def test_a_tick_posts_it_when_it_is_due(self, engine):
        player(engine, "alice")
        player(engine, "bob")
        engine.tick(1)                       # arms, and logs the two arrivals
        engine.set_setting(recap.DUE_KEY, "1")
        said = [o for o in engine.tick(1) if o.kind == "recap"]
        assert said
        assert "New to the realm: alice and bob." in " ".join(o.message for o in said)

    def test_the_recap_is_not_news_for_the_next_one(self, engine):
        player(engine, "alice")
        engine.tick(1)
        engine.set_setting(recap.DUE_KEY, "1")
        assert [o for o in engine.tick(1) if o.kind == "recap"]
        engine.set_setting(recap.DUE_KEY, "1")
        assert [o for o in engine.tick(1) if o.kind == "recap"] == []
