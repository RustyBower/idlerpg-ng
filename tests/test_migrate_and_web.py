"""Tests for the database migration and the website's data layer."""

from __future__ import annotations

import random

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from idlerpg import web
from idlerpg.engine import Engine
from idlerpg.migrate import migrate
from idlerpg.models import Base, Platform, Player, Presence
from idlerpg.rules import Curve, Penalty


def seeded(url: str) -> None:
    db = create_engine(url)
    Base.metadata.create_all(db)
    with Session(db) as s:
        e = Engine(s, Curve(), rng=random.Random(7))
        a = e.register("alice", "pw", "Bard", Platform.IRC, "alice")
        b = e.register("bob", "pw", "Wizard", Platform.DISCORD, "999")
        e.link(a, Platform.DISCORD, "111")
        e.penalise(b, Penalty.PART)
        a.items[0].value = 42
        e.tick(10)
        s.commit()


@pytest.fixture
def source(tmp_path):
    url = f"sqlite:///{tmp_path}/src.db"
    seeded(url)
    return url


class TestMigration:
    def test_copies_every_table(self, source, tmp_path):
        dest = f"sqlite:///{tmp_path}/dest.db"
        counts = migrate(source, dest)
        assert counts["player"] == 2
        assert counts["platform_identity"] == 3      # alice has two
        assert counts["item"] == 20
        assert counts["penalty"] == 1

    def test_data_survives_intact(self, source, tmp_path):
        dest = f"sqlite:///{tmp_path}/dest.db"
        migrate(source, dest)
        with Session(create_engine(dest)) as s:
            alice = s.scalar(select(Player).where(Player.name == "alice"))
            assert {str(i.platform).split(".")[-1].lower() for i in alice.identities} == {
                "irc", "discord"
            }
            assert max(i.value for i in alice.items) == 42
            # Credentials must survive, or everyone is locked out.
            assert alice.password_hash.startswith("pbkdf2_sha256$")

    def test_refuses_to_merge_into_a_populated_realm(self, source, tmp_path):
        dest = f"sqlite:///{tmp_path}/dest.db"
        migrate(source, dest)
        with pytest.raises(SystemExit):
            migrate(source, dest)

    def test_force_allows_a_rerun(self, source, tmp_path):
        dest = f"sqlite:///{tmp_path}/dest.db"
        migrate(source, dest)
        counts = migrate(source, dest, force=True)
        assert counts["player"] == 2
        with Session(create_engine(dest)) as s:
            assert len(s.scalars(select(Player)).all()) == 2   # merged, not doubled


class TestWebDataLayer:
    @pytest.fixture(autouse=True)
    def _point_web_at(self, source, monkeypatch):
        monkeypatch.setattr(web, "DATABASE_URL", source)
        monkeypatch.setattr(web, "_engine", None)

    def test_players_load_with_platforms_and_items(self):
        players = web.load_players()
        assert {p["username"] for p in players} == {"alice", "bob"}
        alice = next(p for p in players if p["username"] == "alice")
        assert set(alice["platforms"]) == {"irc", "discord"}
        assert alice["itemsum"] == 42

    def test_password_hashes_are_never_loaded(self):
        blob = repr(web.load_players())
        assert "pbkdf2" not in blob
        assert "password" not in blob

    def test_penalties_are_totalled_per_kind(self):
        bob = next(p for p in web.load_players() if p["username"] == "bob")
        assert bob["penalties"].get("part", 0) > 0

    def test_pages_render(self):
        players = web.load_players()
        for html in (web.page_index(players), web.page_map(players, None),
                     web.page_quest(None, players), web.page_game(),
                     web.page_player(players[0])):
            assert html.startswith("<!doctype html>")

    def test_a_missing_database_degrades_rather_than_raising(self, monkeypatch):
        monkeypatch.setattr(web, "DATABASE_URL", "sqlite:////nonexistent/x.db")
        monkeypatch.setattr(web, "_engine", None)
        assert web.load_players() == []
        assert web.load_quest() is None
