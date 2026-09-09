"""Tests for the ported game rules.

These pin the numbers against the original Perl implementation. If one fails
after a change, the game has been retuned - which is a decision, not a bug to
paper over.
"""

from __future__ import annotations

import pytest

from idlerpg.rules import (
    LINEAR_STEP_SECONDS,
    Curve,
    Penalty,
    item_sum,
    penalty_seconds,
    penalty_ttl,
    seconds_to_reach,
    ttl,
)

UPSTREAM = Curve(step=1.16, penalty_step=1.14)


class TestCurve:
    def test_first_level_is_the_base(self):
        assert ttl(0, Curve()) == 600

    def test_exponential_below_the_cap(self):
        c = Curve(base_seconds=600, step=1.12)
        assert ttl(10, c) == pytest.approx(600 * 1.12**10)
        assert ttl(60, c) == pytest.approx(600 * 1.12**60)

    def test_linear_above_the_cap(self):
        c = Curve(base_seconds=600, step=1.12, cap_level=60)
        # Each level past the cap costs exactly one more day than the last.
        assert ttl(61, c) - ttl(60, c) == pytest.approx(LINEAR_STEP_SECONDS)
        assert ttl(70, c) - ttl(60, c) == pytest.approx(10 * LINEAR_STEP_SECONDS)

    def test_curve_is_continuous_at_the_cap(self):
        c = Curve(cap_level=60)
        assert ttl(60, c) == pytest.approx(600 * c.step**60)

    def test_negative_level_rejected(self):
        with pytest.raises(ValueError):
            ttl(-1)


class TestWhyNotUpstream:
    """The cap alone does not make high levels reachable; rpstep is the lever.

    These assertions are the evidence behind choosing 1.12 over 1.16.
    """

    def test_upstream_level_60_costs_about_51_days(self):
        assert ttl(60, UPSTREAM) / 86400 == pytest.approx(51.2, abs=0.5)

    def test_upstream_linear_term_is_negligible(self):
        # One extra day on top of a 51-day step is ~2%, so the cap only stops
        # runaway growth - it does not make the late game attainable.
        assert LINEAR_STEP_SECONDS / ttl(60, UPSTREAM) < 0.03

    def test_upstream_level_100_is_years_away(self):
        assert seconds_to_reach(100, UPSTREAM) / 86400 / 365 == pytest.approx(8.6, abs=0.3)

    def test_chosen_curve_is_reachable(self):
        chosen = Curve()
        assert seconds_to_reach(60, chosen) / 86400 == pytest.approx(51.9, abs=1.0)
        assert seconds_to_reach(100, chosen) / 86400 / 365 == pytest.approx(3.0, abs=0.2)


class TestPenalties:
    @pytest.mark.parametrize(
        "kind,multiplier",
        [
            (Penalty.QUIT, 20),
            (Penalty.NICK, 30),
            (Penalty.PART, 200),
            (Penalty.KICK, 250),
            (Penalty.LOGOUT, 20),
            (Penalty.QUEST, 15),
        ],
    )
    def test_multipliers_match_the_original(self, kind, multiplier):
        c = Curve(penalty_limit_seconds=0)  # disable the cap to see raw values
        level = 10
        expected = int(multiplier * penalty_ttl(level, c) / c.base_seconds)
        assert penalty_seconds(kind, level, c) == expected

    def test_message_penalty_scales_with_length(self):
        c = Curve(penalty_limit_seconds=0)
        short = penalty_seconds(Penalty.MESSAGE, 20, c, message_length=5)
        long = penalty_seconds(Penalty.MESSAGE, 20, c, message_length=50)
        assert long > short
        assert long == pytest.approx(short * 10, rel=0.05)

    def test_message_requires_a_length(self):
        with pytest.raises(ValueError):
            penalty_seconds(Penalty.MESSAGE, 10)

    def test_limit_caps_large_penalties(self):
        c = Curve()  # limitpen defaults to one week
        # A kick at a high level would otherwise be enormous.
        assert penalty_seconds(Penalty.KICK, 80, c) == c.penalty_limit_seconds

    def test_penalties_grow_with_level(self):
        c = Curve(penalty_limit_seconds=0)
        assert penalty_seconds(Penalty.QUIT, 30, c) > penalty_seconds(Penalty.QUIT, 10, c)


class TestMisc:
    def test_item_sum(self):
        assert item_sum({"weapon": 10, "shield": 5}) == 15
        assert item_sum({}) == 0

    def test_invalid_curves_rejected(self):
        with pytest.raises(ValueError):
            Curve(step=1.0)
        with pytest.raises(ValueError):
            Curve(base_seconds=0)
