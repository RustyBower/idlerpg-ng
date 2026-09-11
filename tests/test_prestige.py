"""Tests for prestige: the fresh start, the points, the perks and their effects."""

from __future__ import annotations

import random

import pytest
from sqlalchemy import create_engine as sa_engine
from sqlalchemy.orm import Session

from idlerpg import events, prestige, quests
from idlerpg.engine import Engine
from idlerpg.models import Base, Platform
from idlerpg.rules import Curve, Penalty, ttl


@pytest.fixture
def engine():
    db = sa_engine("sqlite://")
    Base.metadata.create_all(db)
    with Session(db) as session:
        yield Engine(session, Curve(), rng=random.Random(3))


def veteran(engine, name="vet", level=60):
    p = engine.register(name, "pw", "Old Hand", Platform.IRC, name)
    p.level = level
    for i, item in enumerate(p.items):
        item.value = 100 + i
    engine.session.commit()
    return p


def do(engine, player, line):
    verb, *args = line.split()
    return prestige.command(engine, player, verb, args)


class TestStartingOver:
    def test_it_opens_at_level_60(self, engine):
        p = veteran(engine, level=59)
        assert "opens at level 60" in do(engine, p, "PRESTIGE confirm")
        assert p.level == 59 and not p.prestige

    def test_without_confirm_it_only_explains(self, engine):
        p = veteran(engine, level=62)
        reply = do(engine, p, "PRESTIGE")
        assert "12 points" in reply and p.level == 62

    def test_confirm_resets_and_pays(self, engine):
        p = veteran(engine, level=60)
        engine.tick(1)
        do(engine, p, "PRESTIGE confirm")
        assert (p.level, p.prestige, p.points) == (0, 1, 10)
        assert all(i.value == 0 for i in p.items)
        assert p.next_ttl == int(ttl(0, engine.curve))
        assert any(o.kind == "prestige" for o in engine.tick(1))

    def test_heirloom_keeps_the_best_items(self, engine):
        p = veteran(engine)
        p.set_perk_rank("heirloom", 2)
        best = sorted((i.value for i in p.items), reverse=True)[:2]
        do(engine, p, "PRESTIGE confirm")
        assert sorted((i.value for i in p.items if i.value), reverse=True) == best

    def test_a_quester_who_prestiges_fails_the_quest(self, engine):
        party = [veteran(engine, f"q{i}") for i in range(4)]
        quests.start(engine.session, party, engine.rng, 500, 500)
        do(engine, party[0], "PRESTIGE confirm")
        assert quests.active_quest(engine.session) is None

    def test_standings_rank_prestige_first(self, engine):
        p = veteran(engine, "star")
        veteran(engine, "plain", level=59)
        do(engine, p, "PRESTIGE confirm")
        assert [q.name for q in engine.top_players(2)] == ["star", "plain"]


class TestPerks:
    def test_buying_spends_a_point_and_stops_at_the_most(self, engine):
        p = veteran(engine)
        p.points = 7
        for _ in range(5):
            do(engine, p, "PERK swiftness")
        assert p.perk_rank("swiftness") == 5 and p.points == 2
        assert "at its most" in do(engine, p, "PERK swiftness")

    def test_no_points_no_perk(self, engine):
        p = veteran(engine)
        assert "no points" in do(engine, p, "PERK fortune")

    def test_unknown_perks_are_refused(self, engine):
        p = veteran(engine)
        p.points = 1
        assert "No such perk" in do(engine, p, "PERK flight")

    def test_perks_lists_them_all(self, engine):
        reply = do(engine, veteran(engine), "PERKS")
        assert all(name in reply for name in prestige.PERKS)


class TestEffects:
    def test_swiftness_shortens_each_level(self, engine):
        p = veteran(engine, level=0)
        p.set_perk_rank("swiftness", 5)
        p.next_ttl = 1
        engine.session.commit()
        engine.tick(1)
        assert p.level == 1
        assert p.next_ttl == int(ttl(1, engine.curve) * 0.90) - 0   # 10% less

    def test_composure_and_lawful_together_cut_no_more_than_15_percent(self, engine):
        calm, plain = veteran(engine, "calm", 20), veteran(engine, "plain", 20)
        engine.set_alignment(calm, "lawful")
        calm.set_perk_rank("composure", 5)          # 0.9 * 0.9 = 0.81, floored
        assert engine.penalise(calm, Penalty.PART) == int(
            engine.penalise(plain, Penalty.PART) * events.PENALTY_FLOOR)

    def test_fortune_and_warding(self, engine):
        lucky, plain = veteran(engine, "lucky", 10), veteran(engine, "plain", 10)
        lucky.set_perk_rank("fortune", 5)
        lucky.set_perk_rank("warding", 5)
        def swing(p, fire):
            rng, total = random.Random(8), 0
            for _ in range(100):
                p.next_ttl = 100000
                fire(p, rng)
                total += abs(p.next_ttl - 100000)
            return total
        assert swing(lucky, events.godsend) > swing(plain, events.godsend)
        assert swing(lucky, events.calamity) < swing(plain, events.calamity)

    def test_champion_strengthens_battle_rolls(self, engine):
        a, b = veteran(engine, "a", 30), veteran(engine, "b", 30)
        a.set_perk_rank("champion", 5)
        out = events.battle(a, b, random.Random(1))
        mine = int(out[0].message.split("/")[1].split("]")[0])
        assert mine == int(events.item_sum(a) * 1.10)

    def test_one_strider_walks_the_whole_party_faster(self, engine):
        party = [veteran(engine, f"s{i}", 45) for i in range(4)]
        for p in party:
            p.x, p.y = 200, 200
        quests.start(engine.session, party, engine.rng, 500, 500)
        quest = quests.active_quest(engine.session)
        quest.kind, quest.stage, quest.x1, quest.y1 = 2, 1, 0, 200
        party[0].set_perk_rank("stride", 5)              # twice the pace
        engine.session.commit()
        quests.steer(engine.session, quests.JOURNEY_PACE * 10, engine.rng)
        assert all(p.x == 180 for p in party)


class TestOnThePlatforms:
    def test_irc(self):
        from idlerpg.adapters.irc import IRCAdapter, parse
        from idlerpg.config import Config
        from tests.test_irc_adapter import FakeWriter
        db = sa_engine("sqlite://")
        Base.metadata.create_all(db)
        with Session(db) as session:
            e = Engine(session, Curve())
            irc = IRCAdapter(e, Config())
            irc.writer = FakeWriter()
            irc.handle(parse(":vet!u@h PRIVMSG idlerpg :REGISTER vet pw Old"))
            e.find_player("vet").level = 61
            e.session.commit()
            irc.writer.lines.clear()
            irc.handle(parse(":vet!u@h PRIVMSG idlerpg :PRESTIGE"))
            assert "11 points" in "\n".join(irc.writer.lines)
            irc.handle(parse(":vet!u@h PRIVMSG idlerpg :PERKS"))
            assert all(len(line) < 512 for line in irc.writer.lines)
