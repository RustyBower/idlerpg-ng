"""Tests for the realm's seasons: when they fall, what they change, who is told."""

from __future__ import annotations

import datetime as dt
import random
import re
from functools import partial

import pytest
from sqlalchemy import create_engine as sa_engine
from sqlalchemy.orm import Session

from idlerpg import admin, auth, lore, web
from idlerpg import engine as engine_module
from idlerpg.engine import Engine
from idlerpg.models import Base, Platform
from idlerpg.rules import Curve

HALLOWTIDE, MIDWINTER, SPRINGTIDE = lore.SEASONS


@pytest.fixture
def engine(monkeypatch):
    monkeypatch.setattr(engine_module, "hash_password",
                        partial(auth.hash_password, iterations=1))
    db = sa_engine("sqlite://")
    Base.metadata.create_all(db)
    with Session(db) as session:
        yield Engine(session, Curve(), rng=random.Random(1))


def on(monkeypatch, season):
    monkeypatch.setattr(lore, "SEASON_OVERRIDE", season)


class TestTheCalendar:
    @pytest.mark.parametrize("day,season", [
        ((9, 30), None), ((10, 1), HALLOWTIDE), ((10, 31), HALLOWTIDE), ((11, 1), None),
        ((12, 14), None), ((12, 15), MIDWINTER), ((1, 1), MIDWINTER), ((1, 6), MIDWINTER),
        ((1, 7), None), ((3, 20), SPRINGTIDE), ((4, 21), None),
    ])
    def test_when_each_falls(self, monkeypatch, day, season):
        on(monkeypatch, None)
        assert lore.current_season(dt.date(2026, *day)) is season

    def test_off_and_forced(self, monkeypatch):
        october = dt.date(2026, 10, 12)
        on(monkeypatch, "off")
        assert lore.current_season(october) is None
        on(monkeypatch, "Springtide")
        assert lore.current_season(october) is SPRINGTIDE

    def test_the_next_one(self):
        season, begins = lore.next_season(dt.date(2026, 9, 11))
        assert season is HALLOWTIDE and begins == "1 October"
        season, begins = lore.next_season(dt.date(2026, 11, 2))
        assert season is MIDWINTER and begins == "15 December"

    def test_tests_run_without_one(self):
        assert lore.current_season() is None


class TestWhatChanges:
    def words(self, rng):
        return " ".join(
            [lore.LORE.calamity(rng) for _ in range(300)]
            + [lore.LORE.godsend(rng) for _ in range(300)]
            + [lore.LORE.vigil(rng) for _ in range(100)]
            + [lore.LORE.journey(rng).text for _ in range(100)])

    def test_a_season_colours_a_share_of_events(self, monkeypatch):
        on(monkeypatch, "Hallowtide")
        text = self.words(random.Random(2))
        assert "pumpkin" in text and "ghost" in text

    def test_without_one_nothing_seasonal_appears(self):
        text = self.words(random.Random(2))
        assert "pumpkin" not in text and "yule" not in text

    def test_and_outside_one_the_luck_is_drawn_exactly_as_before(self):
        a, b = random.Random(5), random.Random(5)
        lore.LORE.calamity(a)
        lore.LORE.calamity(b)
        assert a.random() == b.random()

    @pytest.mark.parametrize("season", lore.SEASONS, ids=lambda s: s.name)
    def test_no_words_run_together(self, season):
        lines = (season.creatures + season.helpers + season.treasures + season.cargo
                 + season.calamities + season.godsends + season.vigil_until + (season.arrives,))
        for line in lines:
            assert line.isascii() and not re.search(r"[a-z][A-Z]|\s\s", line), line


class TestTheRealmIsTold:
    def test_once_as_a_season_begins_and_once_as_it_ends(self, engine, monkeypatch):
        on(monkeypatch, "Hallowtide")
        said = [o.message for o in engine.tick(1)]
        assert HALLOWTIDE.arrives in said
        assert HALLOWTIDE.arrives not in [o.message for o in engine.tick(1)]
        on(monkeypatch, "off")
        assert any("Hallowtide is over" in o.message for o in engine.tick(1))

    def test_a_restart_does_not_tell_it_twice(self, engine, monkeypatch):
        on(monkeypatch, "Hallowtide")
        engine.tick(1)
        again = Engine(engine.session, Curve())
        assert HALLOWTIDE.arrives not in [o.message for o in again.tick(1)]


class TestTheAdminCommand:
    def admin_player(self, engine):
        p = engine.register("boss", "pw", "x", Platform.IRC, "boss")
        engine.set_admin(p, True)
        return p

    def test_force_follow_the_calendar_and_switch_off(self, engine):
        boss = self.admin_player(engine)
        assert "It is Midwinter (forced)" in admin.run(engine, boss, "SEASON", ["midwinter"])
        assert engine.get_setting(engine.SEASON_KEY) == "Midwinter"
        assert "switched off" in admin.run(engine, boss, "SEASON", ["off"])
        assert "by the calendar" in admin.run(engine, boss, "SEASON", ["auto"])
        assert "SEASON [" in admin.run(engine, boss, "SEASON", ["summer"])

    def test_a_forced_season_survives_a_restart(self, engine, monkeypatch):
        boss = self.admin_player(engine)
        admin.run(engine, boss, "SEASON", ["springtide"])
        on(monkeypatch, None)                        # as a fresh process would be
        Engine(engine.session, Curve()).tick(1)
        assert lore.SEASON_OVERRIDE == "Springtide"


class TestTheSite:
    def banner(self, monkeypatch, day, choice=""):
        monkeypatch.setattr(web, "load_season_choice", lambda: choice)
        monkeypatch.setattr(web, "today", lambda: dt.date(2026, *day))
        return web.season_banner()

    def test_a_banner_while_one_lasts(self, monkeypatch):
        banner = self.banner(monkeypatch, (10, 12))
        assert "pumpkins are watching" in banner and "Until 31 October" in banner
        assert "trick-or-treating goblins" in banner

    def test_two_weeks_ahead_it_says_what_is_coming(self, monkeypatch):
        soon = self.banner(monkeypatch, (9, 17))
        assert "Hallowtide begins on 1 October: until 31 October" in soon
        assert "headless horsemen" in soon
        assert self.banner(monkeypatch, (9, 16)) == ""        # fifteen days out
        assert "Midwinter begins on 15 December" in self.banner(monkeypatch, (12, 3))

    def test_the_how_to_play_page_lists_them(self):
        page = web.page_game()
        assert "<h2>Seasons</h2>" in page
        for season in lore.SEASONS:
            assert season.name in page and season.taste in page
        assert "1 October to 31 October" in page and "15 December to 6 January" in page

    def test_an_admins_choice_wins(self, monkeypatch):
        assert self.banner(monkeypatch, (10, 12), choice="off") == ""
        assert "thaw has come" in self.banner(monkeypatch, (9, 11), choice="Springtide")

    def test_upcoming(self):
        assert lore.upcoming(dt.date(2026, 9, 17)) == (HALLOWTIDE, "1 October")
        assert lore.upcoming(dt.date(2026, 9, 11)) is None
