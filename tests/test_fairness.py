"""Tests for the FIGHT fairness harness - the harness, not FIGHT itself."""

from __future__ import annotations

from types import SimpleNamespace

from idlerpg import fairness
from idlerpg.fairness import RULES, Fights


def who(id, level, online=True, value=10, next_ttl=100_000, moral="neutral",
        ethos="neutral"):
    items = [SimpleNamespace(value=value)]
    return SimpleNamespace(id=id, name=f"p{id}", level=level, is_idling=online,
                           items=items, perks="{}", next_ttl=next_ttl,
                           perk_rank=lambda name: 0,
                           alignment=SimpleNamespace(value=moral),
                           ethos=SimpleNamespace(value=ethos))


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


    def test_a_transfer_is_sized_by_the_loser_and_can_carry_over(self):
        f = Fights(RULES["transfer"], ["bully"], seed=1)
        low = who(1, 20, value=10**6, next_ttl=1_000)          # surely wins
        high = who(2, 25, value=1, next_ttl=100_000)
        f.fight(low, high, elapsed=0)
        assert high.next_ttl == 105_000                        # 5% of its own
        assert low.next_ttl == 1_000 - int(5_000 * 1.5)        # past zero: a level up


    def test_a_capped_transfer_is_no_more_than_the_winners_own_stake(self):
        from idlerpg.rules import Curve, ttl
        f = Fights(RULES["transfer-capped"], ["bully"], seed=1)
        low = who(1, 20, value=10**6, next_ttl=1_000)          # surely wins
        high = who(2, 25, value=1, next_ttl=10**7)             # a vast clock
        f.fight(low, high, elapsed=0)
        cap = int(ttl(20, Curve()) * 0.05)
        assert high.next_ttl == 10**7 + cap
        assert low.next_ttl == 1_000 - int(cap * 1.5)

    def test_meetings_are_even_capped_and_once_a_day_a_pair(self):
        from idlerpg.rules import Curve, ttl
        f = Fights(RULES["meetings"], [], seed=1)
        a = who(1, 20, value=10**6, next_ttl=50_000)           # surely wins
        b = who(2, 20, value=0, next_ttl=100_000)
        f.clash(a, b, 0)
        cap = int(ttl(20, Curve()) * 0.05)
        assert a.next_ttl == 50_000 - cap and b.next_ttl == 100_000 + cap
        f.clash(b, a, 3600)                                    # the same pair, same day
        assert b.next_ttl == 100_000 + cap
        f.clash(a, b, 86400)
        assert b.next_ttl == 100_000 + 2 * cap

    def test_nobody_under_10_meets_in_anger(self):
        f = Fights(RULES["meetings"], [], seed=1)
        a, b = who(1, 20, value=10**6), who(2, 9, value=0)
        f.clash(a, b, 0)
        assert b.next_ttl == 100_000

    def test_walking_takes_a_step_every_five_seconds(self, monkeypatch):
        steps = []
        monkeypatch.setattr(fairness.events, "move_player",
                            lambda p, mx, my, rng: steps.append(p.id))
        fairness.Walk(seed=1, fights=None)(None, [(who(1, 20), "x")], 0, 1800)
        assert len(steps) == 1800 // fairness.SUBSTEP

    def test_luck_scales_each_fighters_stake(self):
        f = Fights(RULES["luck"], ["bully"], seed=1)
        calm = who(1, 25, value=10**6, ethos="lawful")          # surely wins
        wild = who(2, 25, value=1, ethos="chaotic")
        f.fight(calm, wild, elapsed=0)
        assert calm.next_ttl == 100_000 - int(100_000 * 0.05 * 0.5)
        assert wild.next_ttl == 100_000 + int(100_000 * 0.05 * 1.5)

    def test_good_fights_stronger_and_evil_weaker(self):
        f = Fights(RULES["moral"], ["bully"], seed=1)
        assert f.strength(who(1, 20, value=100, moral="good")) == 110
        assert f.strength(who(2, 20, value=100, moral="evil")) == 90
        assert Fights(RULES["proposed"], [], 1).strength(who(3, 20, value=100,
                                                              moral="good")) == 100


class TestTheHarness:
    def test_strategies_rotate_in_mixed(self):
        a = fairness.assign("mixed", fairness.REALM, seed=1)
        b = fairness.assign("mixed", fairness.REALM, seed=2)
        assert set(a) == set(fairness.STRATEGIES) and a != b

    def test_a_short_run_reports_every_player(self):
        run = fairness._one(("bullies", "proposed", 1, 0.25, 3600))
        assert len(run["players"]) == len(fairness.REALM)
        assert [p["start"] for p in run["players"]] == fairness.REALM
