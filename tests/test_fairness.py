"""Tests for the FIGHT fairness harness - the harness, not FIGHT itself."""

from __future__ import annotations

from types import SimpleNamespace

from idlerpg import fairness
from idlerpg.fairness import RULES, Fights


def who(id, level, online=True, value=10, next_ttl=100_000):
    items = [SimpleNamespace(value=value)]
    return SimpleNamespace(id=id, name=f"p{id}", level=level, is_idling=online,
                           items=items, perks="{}", next_ttl=next_ttl,
                           perk_rank=lambda name: 0)


class TestTheRules:
    def test_open_allows_anyone_online(self):
        f = Fights(RULES["open"], ["bully"], seed=1)
        me = who(1, 30)
        assert f.allowed(me, who(2, 0), 0)
        assert not f.allowed(me, who(3, 30, online=False), 0)
        assert not f.allowed(me, me, 0)

    def test_proposed_bars_the_weak_and_the_far_below(self):
        f = Fights(RULES["proposed"], ["bully"], seed=1)
        me = who(1, 30)
        assert not f.allowed(me, who(2, 9), 0)       # under level 10
        assert not f.allowed(me, who(3, 24), 0)      # six below
        assert f.allowed(me, who(4, 25), 0)          # five below
        assert f.allowed(me, who(5, 40), 0)          # above is always fair game
        assert not f.allowed(who(6, 9), who(7, 12), 0)   # nor can the weak start one

    def test_the_shield_lasts_a_day(self):
        f = Fights(RULES["proposed"], ["bully"], seed=1)
        me, them = who(1, 30, value=1000), who(2, 30)
        f.fight(me, them, elapsed=0)
        assert not f.allowed(who(3, 30), them, 23 * 3600)
        assert f.allowed(who(3, 30), them, 24 * 3600)

    def test_stakes_and_the_underdog_bonus(self):
        f = Fights(RULES["proposed"], ["bully"], seed=1)
        low, high = who(1, 20, value=10**6), who(2, 25, value=1)  # low surely wins
        f.fight(low, high, elapsed=0)
        assert low.next_ttl == 100_000 - int(100_000 * 0.05 * 1.5)
        assert high.next_ttl == 100_000 + 5_000


class TestTheHarness:
    def test_strategies_rotate_in_mixed(self):
        a = fairness.assign("mixed", fairness.REALM, seed=1)
        b = fairness.assign("mixed", fairness.REALM, seed=2)
        assert set(a) == set(fairness.STRATEGIES) and a != b

    def test_a_short_run_reports_every_player(self):
        run = fairness._one(("bullies", "proposed", 1, 0.25, 3600))
        assert len(run["players"]) == len(fairness.REALM)
        assert [p["start"] for p in run["players"]] == fairness.REALM
