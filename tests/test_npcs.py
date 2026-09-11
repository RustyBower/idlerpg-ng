"""Tests for NPCs: the realm's own characters, making up the numbers."""

from __future__ import annotations

import random
from datetime import timedelta
from functools import partial

import pytest
from sqlalchemy import create_engine as sa_engine
from sqlalchemy.orm import Session

from idlerpg import admin, auth, npcs, quests, web
from idlerpg import engine as engine_module
from idlerpg.engine import Engine, RegistrationError
from idlerpg.models import Base, PenaltyRecord, Platform, Presence, utcnow
from idlerpg.rules import Curve
from idlerpg.text import check_class, check_name


@pytest.fixture
def engine(monkeypatch):
    monkeypatch.setattr(engine_module, "hash_password",
                        partial(auth.hash_password, iterations=1))
    db = sa_engine("sqlite://")
    Base.metadata.create_all(db)
    with Session(db) as session:
        e = Engine(session, Curve(), rng=random.Random(5))
        e.npc_max, e.npc_realm = 5, 12
        yield e


def people(engine, count, start=0):
    return [engine.register(f"p{i}", "pw", "Human", Platform.IRC, f"p{i}")
            for i in range(start, start + count)]


def the_npcs(engine):
    return [p for p in engine.all_players() if p.npc]


def check_now(engine):
    engine.npc_wait = 0
    return engine.tick(1)


def long_gone(engine, player):
    engine.set_presence(Platform.IRC, player.name, Presence.OFFLINE)
    old = utcnow() - timedelta(days=8)
    player.identities[0].presence_since = old
    player.last_login = player.created = old
    engine.session.commit()


class TestMakingUpTheNumbers:
    def test_none_unless_configured(self, engine):
        engine.npc_max = 0
        people(engine, 2)
        check_now(engine)
        assert the_npcs(engine) == []

    def test_a_small_realm_gets_the_most_allowed(self, engine):
        people(engine, 4)
        said = check_now(engine)
        found = the_npcs(engine)
        assert len(found) == 5                      # 12 wanted, 5 at most
        assert all(p.is_idling and not p.identities for p in found)
        assert all(len(p.items) == 10 for p in found)
        assert sum("(an NPC)" in o.message for o in said) == 5

    def test_a_busier_realm_gets_fewer(self, engine):
        people(engine, 9)
        check_now(engine)
        assert len(the_npcs(engine)) == 3

    def test_they_step_aside_for_people_and_come_back_as_they_were(self, engine):
        people(engine, 4)
        check_now(engine)
        levels = {p.name: p.level for p in the_npcs(engine)}
        for p in the_npcs(engine):
            p.level = 20
        crowd = people(engine, 12, start=4)         # 16 people: no room
        said = check_now(engine)
        assert all(p.npc == npcs.BENCHED for p in the_npcs(engine))
        assert sum("distant lands" in o.message for o in said) == 5
        for p in crowd:
            long_gone(engine, p)
        check_now(engine)
        back = the_npcs(engine)
        assert {p.name for p in back} == set(levels)   # the same five, no new ones
        assert all(p.npc == npcs.PRESENT and p.level == 20 for p in back)

    def test_one_person_coming_and_going_does_not_move_them(self, engine):
        people(engine, 8)
        check_now(engine)
        assert len(the_npcs(engine)) == 4
        people(engine, 2, start=8)                  # 14 about: within the slack
        check_now(engine)
        assert sum(p.npc != npcs.BENCHED for p in the_npcs(engine)) == 4

    def test_admins_names_are_never_taken(self, engine):
        engine.apply_owners([n for n in npcs.NAMES[:20]])
        people(engine, 1)
        check_now(engine)
        assert {p.name for p in the_npcs(engine)} <= set(npcs.NAMES[20:])


class TestPlayingLikePeople:
    def test_they_talk_leave_and_pay_for_it(self, engine):
        people(engine, 1)
        check_now(engine)
        for _ in range(24 * 21):                    # three weeks, an hour a tick
            engine.tick(3600)
        ids = {p.id for p in the_npcs(engine)}
        kinds = {r.kind for r in engine.session.query(PenaltyRecord)
                 if r.player_id in ids}
        assert {"message", "quit"} <= kinds
        assert any(p.level > 20 for p in the_npcs(engine))

    def test_on_a_quest_they_hold_their_tongues(self, engine, monkeypatch):
        people(engine, 1)
        check_now(engine)
        party = the_npcs(engine)[:4]
        for p in party:
            p.level = 45
        quests.start(engine.session, party, engine.rng, 500, 500)
        monkeypatch.setattr(npcs, "HABITS", npcs.Habits(1e6, 1e6, 8))
        engine.tick(60)
        assert quests.active_quest(engine.session) is not None
        spoke = {r.player_id for r in engine.session.query(PenaltyRecord)}
        assert not spoke & {p.id for p in party}


class TestTheyAreNotPeople:
    def test_nobody_can_log_in_as_one(self, engine):
        people(engine, 1)
        check_now(engine)
        npc = the_npcs(engine)[0]
        for guess in ("", "pw", npcs.UNUSABLE_PASSWORD):
            assert engine.authenticate(npc.name, guess) is None

    def test_their_names_are_taken(self, engine):
        people(engine, 1)
        check_now(engine)
        with pytest.raises(RegistrationError):
            engine.register(the_npcs(engine)[0].name, "pw", "x", Platform.IRC, "someone")

    def test_delold_leaves_them_alone(self, engine):
        people(engine, 1)
        check_now(engine)
        npc = the_npcs(engine)[0]
        npc.npc = npcs.BENCHED
        npc.last_login = npc.created = utcnow() - timedelta(days=30)
        engine.session.commit()
        assert npc.name not in admin._delold(engine, None, ["7"])

    def test_the_site_marks_them(self):
        assert ">NPC<" in web.platform_badges({"npc": True, "online": True})

    def test_names_and_classes_are_valid(self):
        assert all(check_name(n) == n for n in npcs.NAMES)
        assert len({n.casefold() for n in npcs.NAMES}) == len(npcs.NAMES)
        assert all(check_class(c) == c for c in npcs.CLASSES)
