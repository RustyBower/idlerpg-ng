"""Tests for recovering the level history from what the realm already said."""

from __future__ import annotations

import random
from functools import partial

import pytest
from sqlalchemy import create_engine as sa_engine, select
from sqlalchemy.orm import Session

from idlerpg import auth, history
from idlerpg import engine as engine_module
from idlerpg.engine import Engine
from idlerpg.models import Base, EventLog, Platform
from idlerpg.rules import Curve


@pytest.fixture
def engine(monkeypatch):
    monkeypatch.setattr(engine_module, "hash_password",
                        partial(auth.hash_password, iterations=1))
    db = sa_engine("sqlite://")
    Base.metadata.create_all(db)
    with Session(db) as session:
        yield Engine(session, Curve(), rng=random.Random(1))


def old(engine, message, kind="levelup"):
    engine.log_event(kind, message)


def linked(engine):
    """Level-ups that know whose they are - what backfill fills in and what
    the site's chart reads. Finds, arrivals and fights carry an id too, but
    neither of those ever looks at them."""
    return {(row.player_id, row.level) for row in engine.session.scalars(
        select(EventLog).where(EventLog.player_id.is_not(None),
                               EventLog.kind == "levelup"))}


class TestBackfill:
    def test_it_reads_the_old_announcements(self, engine):
        rusty = engine.register("rusty", "pw", "Sysadmin", Platform.IRC, "rusty")
        old(engine, "rusty the Sysadmin reaches level 12! Next level in 3d 4h.")
        old(engine, "rusty, the Sysadmin, reaches level 13!")      # an older wording
        assert history.backfill(engine) == 2
        assert linked(engine) == {(rusty.id, 12), (rusty.id, 13)}

    def test_it_leaves_what_it_cannot_match(self, engine):
        engine.register("rusty", "pw", "x", Platform.IRC, "rusty")
        old(engine, "someone-else the wanderer reaches level 4!")
        old(engine, "rusty found a level 9 pair of gloves!", kind="item")
        assert history.backfill(engine) == 0 and linked(engine) == set()

    def test_it_runs_once(self, engine):
        engine.register("rusty", "pw", "x", Platform.IRC, "rusty")
        old(engine, "rusty the x reaches level 2!")
        assert history.backfill(engine) == 1
        old(engine, "rusty the x reaches level 3!")
        assert history.backfill(engine) == 0          # marked done
        assert len(linked(engine)) == 1

    def test_new_level_ups_need_no_backfill(self, engine):
        p = engine.register("rusty", "pw", "x", Platform.IRC, "rusty")
        p.next_ttl = 1
        engine.session.commit()
        engine.tick(2)
        assert linked(engine) == {(p.id, 1)}
        assert history.backfill(engine) == 0
