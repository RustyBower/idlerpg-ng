"""Shared test setup."""

from __future__ import annotations

import pytest

from idlerpg import lore


@pytest.fixture(autouse=True)
def no_season(monkeypatch):
    """Seasons stay off unless a test turns one on, so no test depends on
    the date it happens to run."""
    monkeypatch.setattr(lore, "SEASON_OVERRIDE", "off")
