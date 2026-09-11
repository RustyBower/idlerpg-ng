"""Tests for achievements, titles, keepsakes and the level history."""

from __future__ import annotations

import datetime as dt
import random
from functools import partial

import pytest
from sqlalchemy import create_engine as sa_engine, select
from sqlalchemy.orm import Session

from idlerpg import achievements as ach
from idlerpg import auth, fairness, fights, lore, prestige, quests, web
from idlerpg import engine as engine_module
from idlerpg.engine import Engine
from idlerpg.models import Base, EventLog, Platform
from idlerpg.rules import Curve, Penalty


@pytest.fixture
def engine(monkeypatch):
    monkeypatch.setattr(engine_module, "hash_password",
                        partial(auth.hash_password, iterations=1))
    db = sa_engine("sqlite://")
    Base.metadata.create_all(db)
    with Session(db) as session:
        yield Engine(session, Curve(), rng=random.Random(1))


def player(engine, name="p", level=20):
    p = engine.register(name, "pw", "x", Platform.IRC, name)
    p.level, p.next_ttl = level, 100_000
    engine.session.commit()
    return p


def keys(p):
    return {a.key for a in p.achievements}


class TestMilestones:
    def test_levels_as_they_come_and_the_realm_hears(self, engine):
        p = player(engine, level=24)
        p.next_ttl = 1
        with fairness.calm():
            said = [o.message for o in engine.tick(2)]     # to 25, announced at once
        assert {"level-10", "level-25"} <= keys(p) and "level-40" not in keys(p)
        assert any("Seasoned" in m for m in said)

    def test_the_level_history_is_recorded(self, engine):
        p = player(engine, level=5)
        p.next_ttl = 1
        with fairness.calm():
            engine.tick(2)
        row = engine.session.scalars(select(EventLog).where(EventLog.kind == "levelup")).one()
        assert (row.player_id, row.level) == (p.id, 6)

    def test_prestige_alignment_and_a_quiet_week(self, engine):
        p = player(engine, level=60)
        prestige.start_over(engine, p)
        assert "prestige-1" in keys(p)
        engine.set_alignment(p, "chaotic evil")
        assert "taking-sides" in keys(p)
        ach.hourly(engine, [p], at=1_000_000)                  # counting starts
        ach.hourly(engine, [p], at=1_000_000 + 6 * 86400)
        assert "silent-week" not in keys(p)
        ach.hourly(engine, [p], at=1_000_000 + 7 * 86400)
        assert "silent-week" in keys(p)

    def test_a_penalty_starts_the_week_again(self, engine, monkeypatch):
        p = player(engine)
        ach.hourly(engine, [p], at=1_000_000)
        monkeypatch.setattr(ach, "now", lambda: 1_000_000 + 5 * 86400)
        engine.penalise(p, Penalty.MESSAGE, message_length=10)
        ach.hourly(engine, [p], at=1_000_000 + 8 * 86400)
        assert "silent-week" not in keys(p)

    def test_fights_quests_and_loose_lips(self, engine):
        a, b = player(engine, "a", 20), player(engine, "b", 26)
        assert ach.on_fight(a, b) and {"first-blood", "giant-slayer"} <= keys(a)
        for _ in range(9):
            ach.on_fight(a, b)
        assert "brawler" in keys(a)
        party = [player(engine, f"q{i}", 45) for i in range(4)]
        quests.start(engine.session, party, engine.rng, 500, 500)
        said = quests.fail(engine.session, party[0], engine.curve)
        assert "loose-lips" in keys(party[0]) and any("Loose Lips" in o.message for o in said)
        assert ach.on_quest(party[1]) and "called" in keys(party[1])

    def test_npcs_earn_nothing(self, engine):
        engine.npc_max, engine.npc_realm = 1, 12
        me = player(engine)
        engine.tick(1)
        npc = next(p for p in engine.all_players() if p.npc)
        assert ach.on_fight(npc, me) == [] and npc.achievements == []


class TestSeasons:
    def test_hallowtide_knocks_treats_masks_and_the_meta_title(self, engine, monkeypatch):
        p = player(engine)
        monkeypatch.setattr(ach, "TREAT_KEEPSAKE", 1.0)
        rng = random.Random(4)
        for _ in range(200):
            ach.on_knock(p, True, rng)
        assert {"hallowtide-knock", "hallowtide-sweet", "hallowtide-masks"} <= keys(p)
        commons, rare = ach.KEEPSAKES["Hallowtide"]
        assert set(commons) <= {k.name for k in p.keepsakes}
        # the lantern too, then keeping a Hallowtide: the meta and its title
        if "hallowtide-horseman" not in keys(p):
            ach.award(p, "hallowtide-horseman")
        said = ach.award(p, "hallowtide-kept", quiet=True)
        assert "hallowtide-meta" in keys(p) and p.title == "the Lantern-Bearer"
        assert any("now p, the Lantern-Bearer" in o.message for o in said)

    def test_a_keepsake_is_had_once(self, engine, monkeypatch):
        p = player(engine)
        monkeypatch.setattr(ach, "RARE_CHANCE", 0.0)
        rng = random.Random(1)
        names = [ach.keepsake(p, "Midwinter", rng)[0] for _ in range(40)]
        found = [n for n in names if n]
        assert len(found) == len(set(found)) == len(p.keepsakes)

    def test_springtide_eggs_the_golden_egg_and_steps(self, engine, monkeypatch):
        p = player(engine)
        monkeypatch.setattr(lore, "SEASON_OVERRIDE", "Springtide")
        monkeypatch.setattr(ach, "EGG_KEEPSAKE", 1.0)
        monkeypatch.setattr(ach, "RARE_CHANCE", 1.0)            # the golden egg at once
        said = ach.egg_hunt(p, random.Random(2))
        assert "painted egg" in said[0].message and "springtide-golden" in keys(p)
        for _ in range(9):
            ach.egg_hunt(p, random.Random(3))
        assert "springtide-eggs" in keys(p)
        for _ in range(5):
            ach.on_level(p, random.Random(1))
        assert "springtide-step" in keys(p)

    def test_the_egg_hunt_only_in_springtide(self, engine, monkeypatch):
        player(engine)
        monkeypatch.setattr(ach, "EGG_INTERVAL", 1)
        assert not any("painted egg" in o.message for o in engine.tick(60))
        monkeypatch.setattr(lore, "SEASON_OVERRIDE", "Springtide")
        assert any("painted egg" in o.message for o in engine.tick(60))

    def test_midwinter_day_and_the_longest_night(self, engine, monkeypatch):
        p = player(engine)
        monkeypatch.setattr(lore, "SEASON_OVERRIDE", "Midwinter")
        solstice = int(dt.datetime(2026, 12, 21, 0, 30, tzinfo=dt.timezone.utc).timestamp())
        christmas = int(dt.datetime(2026, 12, 25, 9, tzinfo=dt.timezone.utc).timestamp())
        ach.hourly(engine, [p], at=solstice)
        said = ach.hourly(engine, [p], at=christmas)
        assert {"midwinter-longest", "midwinter-gift"} <= keys(p)
        assert any("Great Tree" in o.message for o in said) and p.keepsakes
        assert ach.hourly(engine, [p], at=christmas + 3600) == []    # once a year

    def test_a_snowball_fight(self, engine, monkeypatch):
        a, b = player(engine, "a"), player(engine, "b")
        monkeypatch.setattr(lore, "SEASON_OVERRIDE", "Midwinter")
        ach.on_fight(a, b)
        assert "midwinter-snowball" in keys(a)


class TestShown:
    def test_the_command(self, engine):
        p = player(engine)
        ach.award(p, "first-blood")
        reply = ach.command(engine, p, "ACHIEVEMENTS", [])
        assert f"1 of {len(ach.FEATS)} achievements" in reply and "First Blood" in reply

    def test_whoami_wears_the_title(self, engine):
        p = player(engine)
        p.title = "the Blossoming"
        ach.award(p, "first-blood")
        assert ach.styled(p) == "p, the Blossoming"
        assert "Achievements: 1" in ach.summary(p)

    def test_the_player_page(self, monkeypatch):
        monkeypatch.setattr(web, "load_levels", lambda pid: [])
        page = web.page_player({
            "username": "p", "title": "the Lantern-Bearer", "level": 3, "class": "x",
            "next": 60, "itemsum": 0, "alignment": "True Neutral", "online": True,
            "platforms": {}, "x": 1, "y": 1, "created": None, "lastlogin": None,
            "admin": False, "items": {}, "item_tags": {}, "penalties": {}, "id": 1,
            "feats": {"first-blood": "2026-10-02"}, "keepsakes": [("a goblin mask", "Hallowtide")],
        })
        assert "the Lantern-Bearer" in page and "<h2>Achievements</h2>" in page
        assert "First Blood" in page and "a goblin mask" in page
        assert "Level history" in page

    def test_the_level_chart(self):
        t = dt.datetime(2026, 9, 12)
        chart = web.level_chart([(t, 30), (t + dt.timedelta(days=1), 31)])
        assert "<polyline" in chart and "From level 30" in chart

    def test_every_feat_is_well_formed(self):
        assert len({f.key for f in ach.FEATS}) == len(ach.FEATS)
        for season in ("Hallowtide", "Midwinter", "Springtide"):
            metas = [f for f in ach.FEATS if f.season == season and f.title]
            assert len(metas) == 1
