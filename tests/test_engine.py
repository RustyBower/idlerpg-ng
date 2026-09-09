"""Tests for the engine: registration, the clock, and penalties."""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine as sa_engine
from sqlalchemy.orm import Session

from idlerpg.engine import Engine, RegistrationError
from idlerpg.models import Base, Platform, Presence
from idlerpg.rules import Curve, Penalty


@pytest.fixture
def engine():
    db = sa_engine("sqlite://")
    Base.metadata.create_all(db)
    with Session(db) as session:
        yield Engine(session, Curve())


def register(engine, name="rusty", platform=Platform.IRC, external=None):
    return engine.register(
        name, "hunter2", "Sysadmin", platform, external or f"{name}!u@h"
    )


class TestRegistration:
    def test_creates_a_playable_character(self, engine):
        p = register(engine)
        assert p.level == 0
        assert p.next_ttl == 600
        assert len(p.items) == 10
        assert len(p.identities) == 1

    def test_password_is_hashed_not_stored(self, engine):
        p = register(engine)
        assert "hunter2" not in p.password_hash
        assert engine.authenticate("rusty", "hunter2") is not None
        assert engine.authenticate("rusty", "wrong") is None

    def test_names_are_unique_case_insensitively(self, engine):
        register(engine, "rusty")
        with pytest.raises(RegistrationError):
            register(engine, "RUSTY", external="other!u@h")

    def test_one_account_cannot_hold_two_characters(self, engine):
        register(engine, "one", external="same!u@h")
        with pytest.raises(RegistrationError):
            register(engine, "two", external="same!u@h")

    def test_empty_name_rejected(self, engine):
        with pytest.raises(RegistrationError):
            register(engine, "   ")


class TestClock:
    def test_idling_spends_the_timer(self, engine):
        p = register(engine)
        engine.tick(100)
        assert p.next_ttl == 500

    def test_offline_players_earn_nothing(self, engine):
        p = register(engine)
        engine.set_presence(Platform.IRC, p.identities[0].external_id, Presence.OFFLINE)
        engine.tick(100)
        assert p.next_ttl == 600

    def test_away_still_earns(self, engine):
        p = register(engine)
        engine.set_presence(Platform.IRC, p.identities[0].external_id, Presence.AWAY)
        engine.tick(100)
        assert p.next_ttl == 500

    def test_two_platforms_are_credited_once(self, engine):
        """The anti-farming property: the character is credited, not each link."""
        solo = register(engine, "solo", external="solo!u@h")
        dual = register(engine, "dual", external="dual!u@h")
        engine.link(dual, Platform.DISCORD, "1234567890")
        assert len(dual.identities) == 2

        engine.tick(100)
        assert solo.next_ttl == dual.next_ttl == 500

    def test_levelling_up_resets_the_timer(self, engine):
        p = register(engine)
        ups = engine.tick(600)
        levels = [o for o in ups if o.kind == "levelup"]
        assert len(levels) == 1
        assert "attained level 1" in levels[0].message
        assert p.level == 1
        assert p.next_ttl == pytest.approx(int(600 * 1.12), abs=1)

    def test_a_long_tick_can_grant_several_levels(self, engine):
        p = register(engine)
        ups = engine.tick(600 + 672 + 752)  # levels 1, 2 and 3
        levels = [o for o in ups if o.kind == "levelup"]
        assert len(levels) == 3
        assert p.level == 3

    def test_zero_or_negative_ticks_do_nothing(self, engine):
        p = register(engine)
        assert engine.tick(0) == []
        assert engine.tick(-5) == []
        assert p.next_ttl == 600


class TestPenalties:
    def test_penalty_adds_to_the_timer(self, engine):
        p = register(engine)
        before = p.next_ttl
        added = engine.penalise(p, Penalty.PART)
        assert added > 0
        assert p.next_ttl == before + added

    def test_message_penalty_needs_a_length(self, engine):
        p = register(engine)
        with pytest.raises(ValueError):
            engine.penalise(p, Penalty.MESSAGE)

    def test_penalties_are_recorded_individually(self, engine):
        p = register(engine)
        engine.penalise(p, Penalty.QUIT)
        engine.penalise(p, Penalty.NICK)
        from idlerpg.models import PenaltyRecord
        kinds = {r.kind for r in engine.session.query(PenaltyRecord).all()}
        assert kinds == {"quit", "nick"}


class TestLinking:
    def test_linking_a_second_platform(self, engine):
        p = register(engine)
        engine.link(p, Platform.DISCORD, "999")
        assert engine.player_for(Platform.DISCORD, "999").name == "rusty"

    def test_cannot_steal_a_linked_account(self, engine):
        a = register(engine, "a", external="a!u@h")
        b = register(engine, "b", external="b!u@h")
        engine.link(a, Platform.DISCORD, "shared")
        with pytest.raises(RegistrationError):
            engine.link(b, Platform.DISCORD, "shared")


class TestLinkCodes:
    """Linking requires already controlling the character, which is what keeps
    someone from attaching themselves to another player's progress."""

    def test_code_links_a_second_platform(self, engine):
        p = register(engine)
        code = engine.issue_link_code(p)
        linked = engine.redeem_link_code(code, Platform.DISCORD, "999", "rusty#1")
        assert linked.name == p.name
        assert engine.player_for(Platform.DISCORD, "999").name == "rusty"

    def test_codes_are_single_use(self, engine):
        p = register(engine)
        code = engine.issue_link_code(p)
        engine.redeem_link_code(code, Platform.DISCORD, "999")
        with pytest.raises(RegistrationError):
            engine.redeem_link_code(code, Platform.DISCORD, "888")

    def test_unknown_code_rejected(self, engine):
        with pytest.raises(RegistrationError):
            engine.redeem_link_code("NOPE1234", Platform.DISCORD, "999")

    def test_expired_code_rejected(self, engine):
        from datetime import timedelta
        from idlerpg.models import LinkCode, utcnow
        p = register(engine)
        code = engine.issue_link_code(p)
        entry = engine.session.query(LinkCode).filter_by(code=code).one()
        entry.expires = utcnow() - timedelta(minutes=1)
        engine.session.commit()
        with pytest.raises(RegistrationError):
            engine.redeem_link_code(code, Platform.DISCORD, "999")

    def test_issuing_a_new_code_invalidates_the_old_one(self, engine):
        p = register(engine)
        first = engine.issue_link_code(p)
        engine.issue_link_code(p)
        with pytest.raises(RegistrationError):
            engine.redeem_link_code(first, Platform.DISCORD, "999")

    def test_linked_character_is_credited_once(self, engine):
        """The whole point: two platforms, one character, one credit."""
        p = register(engine)
        code = engine.issue_link_code(p)
        engine.redeem_link_code(code, Platform.DISCORD, "999")
        engine.tick(100)
        assert p.next_ttl == 500


class TestTopPlayers:
    def test_ranks_by_level_then_closest_to_the_next(self, engine):
        a = register(engine, "a", external="a")
        b = register(engine, "b", external="b")
        c = register(engine, "c", external="c")
        a.level, a.next_ttl = 5, 900
        b.level, b.next_ttl = 5, 100      # same level, closer to levelling
        c.level, c.next_ttl = 9, 5000
        engine.session.commit()
        assert [p.name for p in engine.top_players(3)] == ["c", "b", "a"]

    def test_empty_realm_has_no_top_players(self, engine):
        assert engine.top_players() == []
