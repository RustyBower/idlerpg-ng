"""Tests for the what's-new announcement."""

from __future__ import annotations

import random
from functools import partial

import pytest
from sqlalchemy import create_engine as sa_engine
from sqlalchemy.orm import Session

from idlerpg import __version__, auth, news
from idlerpg import engine as engine_module
from idlerpg.engine import Engine
from idlerpg.models import Base
from idlerpg.rules import Curve

CHANGELOG = f"""# Changelog

Newest first.

## {__version__} - 2026-09-12

- **Items left lying on the map.** The item you replace is no longer thrown
  away if it was worth keeping.
- **The world map updates as you watch it**, swapping in a fresh map every
  fifteen seconds.
- **Not shipped: a thing that was measured and rejected.** It was unfair.
- *Operators*: nothing to do.
- **A fourth thing** nobody needs to hear about.

## 0.1.0 - 2026-01-01

- **The realm began.** Long ago.
"""


@pytest.fixture
def engine(monkeypatch):
    monkeypatch.setattr(engine_module, "hash_password",
                        partial(auth.hash_password, iterations=1))
    monkeypatch.setattr(news, "changelog_text", lambda: CHANGELOG)
    db = sa_engine("sqlite://")
    Base.metadata.create_all(db)
    with Session(db) as session:
        yield Engine(session, Curve(), rng=random.Random(1))


class TestReadingTheChangelog:
    def test_it_takes_the_headline_of_each_entry(self):
        assert news.headlines(CHANGELOG, __version__, most=9) == [
            "Items left lying on the map",
            "The world map updates as you watch it",
            "A fourth thing",
        ]

    def test_operator_notes_are_not_for_the_realm(self):
        assert not any("perators" in h
                       for h in news.headlines(CHANGELOG, __version__, most=9))

    def test_a_rejected_change_is_not_announced_as_a_feature(self):
        """These entries are bold, so only SKIP keeps them out of the channel."""
        assert not any("Not shipped" in h
                       for h in news.headlines(CHANGELOG, __version__, most=9))

    def test_it_stops_at_the_version_it_was_asked_for(self):
        assert news.headlines(CHANGELOG, "0.1.0") == ["The realm began"]

    def test_a_version_with_no_entry_says_nothing(self):
        assert news.headlines(CHANGELOG, "9.9.9") == []

    def test_only_the_first_few_are_read_out(self):
        assert len(news.headlines(CHANGELOG, __version__)) == news.MOST


class TestAnnouncing:
    def test_it_says_what_the_version_brought(self, engine):
        [said] = news.announce(engine, "https://idlerpg.example.com/")
        assert said.kind == "news"
        assert f"now {__version__}" in said.message
        assert "Items left lying on the map" in said.message
        assert "https://idlerpg.example.com/changes" in said.message

    def test_it_reaches_both_platforms_on_the_next_tick(self, engine):
        news.announce(engine)
        assert any(o.kind == "news" for o in engine.tick(1))

    def test_it_says_it_once_and_not_on_every_restart(self, engine):
        assert news.announce(engine)
        assert news.announce(engine) == []

    def test_a_build_with_no_changelog_stays_quiet(self, engine, monkeypatch):
        monkeypatch.setattr(news, "changelog_text", lambda: "")
        assert news.announce(engine) == []
        # And it does not go looking again on every restart for the rest of
        # the release.
        assert engine.get_setting(news.SEEN_KEY) == __version__

    def test_a_new_version_is_news_again(self, engine):
        engine.set_setting(news.SEEN_KEY, "0.0.1")
        assert news.announce(engine)
