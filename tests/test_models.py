"""Tests for the identity model and the cross-platform idle rule."""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from idlerpg.models import (
    Base,
    Platform,
    PlatformIdentity,
    Player,
    Presence,
)


@pytest.fixture
def session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


def make_player(session, name="rusty", **identities) -> Player:
    player = Player(name=name, password_hash="x")
    for platform, presence in identities.items():
        player.identities.append(
            PlatformIdentity(
                platform=Platform(platform),
                external_id=f"{name}-{platform}",
                presence=presence,
            )
        )
    session.add(player)
    session.commit()
    return player


class TestOneCharacterManyPlatforms:
    def test_a_player_can_hold_identities_on_both_platforms(self, session):
        player = make_player(
            session, irc=Presence.ACTIVE, discord=Presence.OFFLINE
        )
        assert {i.platform for i in player.identities} == {
            Platform.IRC,
            Platform.DISCORD,
        }

    def test_an_external_account_cannot_belong_to_two_characters(self, session):
        make_player(session, name="one", irc=Presence.ACTIVE)
        clash = Player(name="two", password_hash="x")
        clash.identities.append(
            PlatformIdentity(
                platform=Platform.IRC,
                external_id="one-irc",  # already claimed
                presence=Presence.ACTIVE,
            )
        )
        session.add(clash)
        with pytest.raises(IntegrityError):
            session.commit()


class TestIdleRule:
    """Present and silent on at least one linked platform."""

    def test_active_on_one_platform_is_enough(self, session):
        player = make_player(
            session, irc=Presence.ACTIVE, discord=Presence.OFFLINE
        )
        assert player.is_idling

    def test_away_still_counts(self, session):
        # A Discord user whose presence is "idle", or an IRC user marked away,
        # is still connected and still earning.
        player = make_player(session, discord=Presence.AWAY)
        assert player.is_idling

    def test_offline_everywhere_earns_nothing(self, session):
        player = make_player(
            session, irc=Presence.OFFLINE, discord=Presence.OFFLINE
        )
        assert not player.is_idling

    def test_no_linked_platforms_earns_nothing(self, session):
        player = make_player(session)
        assert not player.is_idling

    def test_being_on_both_is_not_double_credit(self, session):
        """The engine credits the character, not each connection."""
        one = make_player(session, name="single", irc=Presence.ACTIVE)
        both = make_player(
            session, name="dual", irc=Presence.ACTIVE, discord=Presence.ACTIVE
        )
        # Both are simply "idling" - there is no per-connection multiplier to
        # accumulate, which is what stops two-platform players farming time.
        assert one.is_idling is both.is_idling is True
