"""Tests for the realm's lore: enough variety to feel endless, and journeys
that go somewhere real."""

from __future__ import annotations

import random

from idlerpg import lore
from idlerpg.lore import LORE_MAP, PLACES, Lore, parse, place


def draws(kind: str, n: int = 500, seed: int = 7) -> list:
    rng = random.Random(seed)
    realm = Lore()
    return [getattr(realm, kind)(rng) for _ in range(n)]


class TestVariety:
    """A share of events come from the short hand-written lists and repeat by
    design; the composed rest should almost never repeat. At about one
    calamity a day, a given hand-written line comes round every month or two."""

    def test_calamities_rarely_repeat(self):
        assert len(set(draws("calamity"))) > 300          # of 500

    def test_godsends_rarely_repeat(self):
        assert len(set(draws("godsend"))) > 300

    def test_vigils_and_journeys_vary(self):
        assert len(set(draws("vigil", 200))) > 100
        assert len({j.text for j in draws("journey", 200)}) > 180

    def test_the_composed_share_is_nearly_unique(self):
        rng = random.Random(11)
        realm = Lore(calamities=[], godsends=[])       # composed only
        lines = [realm.calamity(rng) for _ in range(500)]
        assert len(set(lines)) > 480

    def test_every_line_is_a_plain_phrase(self):
        for line in draws("calamity", 200) + draws("godsend", 200):
            assert line and line == line.strip() and "{" not in line


class TestPlaces:
    def test_places_are_on_the_map_and_distinct(self):
        spots = [p.at for p in PLACES]
        assert len(set(spots)) == len(spots)
        assert all(0 <= x < LORE_MAP and 0 <= y < LORE_MAP for x, y in spots)
        assert len({p.name for p in PLACES}) == len(PLACES)

    def test_every_quadrant_has_somewhere_to_go(self):
        quadrants = {(x >= 250, y >= 250) for x, y in (p.at for p in PLACES)}
        assert len(quadrants) == 4

    def test_journeys_run_between_two_different_named_places(self):
        by_spot = {p.at: p.name for p in PLACES}
        for journey in draws("journey", 100):
            assert journey.first != journey.second
            assert by_spot[journey.first] in journey.text
            assert by_spot[journey.second] in journey.text

    def test_positions_scale_into_the_heartland(self):
        assert place((0, 0), 500, 500) == (50, 50)
        assert place((499, 499), 500, 500) == (449, 449)
        assert place((0, 499), 1000, 250) == (100, 224)
        assert place((250, 250), 100, 100) == (49, 49)

    def test_every_place_is_off_the_rim(self):
        for spot in PLACES:
            x, y = place(spot.at, 500, 500)
            assert 50 <= x <= 449 and 50 <= y <= 449


class TestEventsFile:
    SAMPLE = "\n".join([
        "C fell into a hole",
        "G caught a unicorn",
        "Q1 find the lost tomes",
        "Q2 225 315 280 360 lay waste to the towers",
        "Q2 not a journey",
        "junk line",
    ])

    def test_its_lines_join_the_realms_own(self):
        realm = parse(self.SAMPLE)
        assert "fell into a hole" in realm.calamities
        assert len(realm.calamities) == len(lore.CALAMITIES) + 1
        assert "caught a unicorn" in realm.godsends
        assert "find the lost tomes" in realm.vigils
        assert [j.first for j in realm.journeys] == [(225, 315)]

    def test_a_missing_file_falls_back_quietly(self, tmp_path):
        realm = lore.load(str(tmp_path / "nope.txt"))
        assert realm.calamities == lore.CALAMITIES

    def test_no_file_is_the_default(self):
        assert lore.load("").journeys == []
