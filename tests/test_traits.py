"""Tests for what the named uniques grant beyond their value."""

from __future__ import annotations

import random
from functools import partial

import pytest
from sqlalchemy import create_engine as sa_engine
from sqlalchemy.orm import Session

from idlerpg import auth, events, quests
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
        yield Engine(session, Curve(), rng=random.Random(1))


def player(engine, name="rusty", level=50, items=20):
    p = engine.register(name, "pw", "Wanderer", Platform.IRC, name)
    p.level = level
    for item in p.items:
        item.value = items
    engine.session.commit()
    return p


def give(player, slot, tag):
    next(i for i in player.items if i.slot == slot).tag = tag


class TestTheRegistry:
    def test_every_unique_is_accounted_for(self):
        """A unique with no entry would be a big number and nothing else."""
        for unique in events.UNIQUES:
            assert unique.tag in events.TRAITS, unique.name

    def test_tags_tell_the_uniques_apart(self):
        tags = [u.tag for u in events.UNIQUES]
        assert len(set(tags)) == len(tags)

    def test_each_trait_can_be_put_into_words(self):
        for tag in events.TRAITS:
            assert events.trait_text(tag)

    def test_an_ordinary_item_grants_nothing(self):
        assert events.trait_text("") == ""
        assert events.trait_text("a") == ""


class AlwaysTheFirstUnique(random.Random):
    """Rolls so that the next find is a unique, and the first one available."""

    def randrange(self, *args, **kwargs):
        return 0

    def random(self):
        return 0.0

    def choice(self, seq):
        return seq[0]


class TestFindingOne:
    def test_the_announcement_says_what_it_does(self, engine):
        p = player(engine, level=80, items=1)
        found = events.find_item(p, AlwaysTheFirstUnique())
        assert found is not None
        assert "the Lantern of Small Mercies" in found.message
        assert "calamities 5% weaker" in found.message

    def test_an_ordinary_find_says_nothing_extra(self, engine):
        p = player(engine, level=5, items=0)
        found = events.find_item(p, random.Random(2))
        assert found is not None and "(" not in found.message


class TestCarryingOne:
    def test_a_unique_grants_its_ranks(self, engine):
        p = player(engine)
        assert events.rank(p, "warding") == 0
        give(p, "charm", "lantern")
        assert events.rank(p, "warding") == 1

    def test_it_adds_to_the_ranks_you_bought(self, engine):
        p = player(engine)
        p.set_perk_rank("champion", 3)
        give(p, "leggings", "crown")          # champion 2
        assert events.rank(p, "champion") == 5

    def test_two_uniques_stack(self, engine):
        p = player(engine)
        give(p, "leggings", "crown")          # champion 2
        give(p, "shield", "door")             # champion 1
        assert events.rank(p, "champion") == 3

    def test_it_reaches_the_rules_that_read_ranks(self, engine):
        p = player(engine)
        plain = events.swiftness(p)
        give(p, "amulet", "kumquat")          # swiftness 1
        assert events.swiftness(p) < plain

    def test_it_reaches_battle_strength(self, engine):
        p = player(engine)
        plain = events.battle_strength(p)
        give(p, "leggings", "crown")
        assert events.battle_strength(p) > plain

    def test_losing_the_item_loses_the_rank(self, engine):
        p = player(engine)
        give(p, "charm", "lantern")
        assert events.rank(p, "warding") == 1
        next(i for i in p.items if i.slot == "charm").tag = ""
        assert events.rank(p, "warding") == 0


class TestTheMountStillCarriesTheParty:
    """The Courser's stride now comes from the registry rather than a special
    case in quests.py, so the party must still move at its pace."""

    def walk(self, engine, party):
        """Put the party back on its mark and walk the same journey again."""
        for p in party:
            p.x, p.y = 200, 200
        engine.session.commit()
        quests.steer(engine.session, quests.JOURNEY_PACE * 10, engine.rng)
        return 200 - party[1].x                    # someone who has no mount

    def test_one_mount_carries_everyone(self, engine):
        party = [player(engine, f"q{i}", level=45) for i in range(4)]
        quests.start(engine.session, party, engine.rng, 500, 500)
        quest = quests.active_quest(engine.session)
        quest.kind, quest.stage, quest.x1, quest.y1 = 2, 1, 0, 200
        engine.session.commit()

        # The same party and the same quest walked twice, so nothing but the
        # mount differs - re-registering a second party would collide on the
        # platform identities the first one holds.
        plain = self.walk(engine, party)
        give(party[0], "boots", events.MOUNT_TAG)
        mounted = self.walk(engine, party)
        assert plain > 0 and mounted > plain
