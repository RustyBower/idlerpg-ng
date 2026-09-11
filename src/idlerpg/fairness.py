"""A harness for testing FIGHT's fairness before it is built.

FIGHT is proposed, not written: once a day a player may challenge another,
and both stake a share of their clocks. The worry is bullying - the strong
farming the weak - so this runs the real engine over a realm shaped like the
live one, bolts candidate rules and player strategies on from outside, and
compares every player's pace with the same seed played without any fights.

    python -m idlerpg.fairness
    python -m idlerpg.fairness --rules open,proposed --scenario mixed --seeds 24

Two questions decide a rule set: do the weak lose ground, and does bullying
pay better than fighting fairly? Nothing here changes the game; FIGHT gets
built to whichever rules hold up.
"""

from __future__ import annotations

import argparse
import math
import random
import statistics
from collections import Counter, defaultdict
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass

from . import events, simulate
from .npcs import PROFILES
from .rules import Curve

DAY = 86400
WEEK = 7 * DAY
EAGER_SECONDS = 2 * 3600   # a fight is used within a couple of hours online


@dataclass(frozen=True)
class Rules:
    """One candidate rule set for FIGHT."""

    name: str
    stake: float = 0.05             # each fighter stakes this share of their clock
    min_level: int = 0              # nobody below this fights or is fought
    max_below: int | None = None    # a target at most this many levels below you
    shield_hours: float = 0         # once challenged, safe from challenges this long
    underdog_bonus: float = 1.0     # the winner's gain, beating a higher level


RULES = {
    "open": Rules("open"),
    "proposed": Rules("proposed", min_level=10, max_below=5, shield_hours=24,
                      underdog_bonus=1.5),
    "no-shield": Rules("no-shield", min_level=10, max_below=5, underdog_bonus=1.5),
    "tight": Rules("tight", min_level=10, max_below=2, shield_hours=24,
                   underdog_bonus=1.5),
}

# The live realm on 2026-09-11: nine people and five fresh NPCs, by level.
REALM = [30, 30, 30, 21, 19, 19, 11, 3, 2, 0, 0, 0, 0, 0]


def tier(level: int) -> str:
    if level >= 30:
        return "top (30)"
    if level >= 15:
        return "mid (19-21)"
    if level >= 10:
        return "low (11)"
    return "new (0-3)"


TIERS = ["new (0-3)", "low (11)", "mid (19-21)", "top (30)"]


# Who a challenger picks from those the rules allow.
def _weakest(me, targets, rng):
    return min(targets, key=lambda t: (events.item_sum(t), t.level))


def _anyone(me, targets, rng):
    return rng.choice(targets)


def _highest(me, targets, rng):
    return max(targets, key=lambda t: (t.level, events.item_sum(t)))


STRATEGIES = {"bully": _weakest, "fair": _anyone, "underdog": _highest, "abstain": None}


def assign(scenario: str, levels: list[int], seed: int) -> list[str]:
    """Each player's strategy.

    bullies: everyone from level 19 bullies, the rest fight fairly.
    everyone: all bully - the worst case.
    mixed: the four strategies rotate through the roster with the seed, so
    each is played at every level and can be compared head to head.
    """
    if scenario == "bullies":
        return ["bully" if level >= 19 else "fair" for level in levels]
    if scenario == "everyone":
        return ["bully"] * len(levels)
    if scenario == "mixed":
        order = list(STRATEGIES)
        return [order[(i + seed) % len(order)] for i in range(len(levels))]
    raise ValueError(f"unknown scenario {scenario!r}")


class Fights:
    """The simulator hook: FIGHT under ``rules``, played by ``strategies``."""

    def __init__(self, rules: Rules, strategies: list[str], seed: int):
        self.rules = rules
        self.strategies = strategies
        self.rng = random.Random(seed + 7)   # apart from the realm's own luck
        self.ready_at: dict[int, float] = {}
        self.shielded_until: dict[int, float] = {}
        self.stats: dict[str, Counter] = defaultdict(Counter)
        self.weekly: Counter = Counter()

    def allowed(self, me, them, elapsed: float) -> bool:
        r = self.rules
        if them is me or not them.is_idling:
            return False
        if me.level < r.min_level or them.level < r.min_level:
            return False
        if r.max_below is not None and them.level < me.level - r.max_below:
            return False
        return elapsed >= self.shielded_until.get(them.id, 0)

    def __call__(self, realm, players, elapsed: float, step: int) -> None:
        order = list(range(len(players)))
        self.rng.shuffle(order)
        for i in order:
            me = players[i][0]
            pick = STRATEGIES[self.strategies[i]]
            if pick is None or not me.is_idling:
                continue
            if elapsed < self.ready_at.get(me.id, 0):
                continue
            if self.rng.random() >= step / EAGER_SECONDS:
                continue
            targets = [p for p, _ in players if self.allowed(me, p, elapsed)]
            if targets:
                self.fight(me, pick(me, targets, self.rng), elapsed)
                self.ready_at[me.id] = elapsed + DAY

    def fight(self, me, them, elapsed: float) -> None:
        mine = max(1, int(events.item_sum(me) * events.champion(me)))
        theirs = max(1, int(events.item_sum(them) * events.champion(them)))
        won = self.rng.randrange(mine) >= self.rng.randrange(theirs)
        winner, loser = (me, them) if won else (them, me)
        bonus = self.rules.underdog_bonus if winner.level < loser.level else 1.0
        gain = int(winner.next_ttl * self.rules.stake * bonus)
        loss = int(loser.next_ttl * self.rules.stake)
        winner.next_ttl = max(1, winner.next_ttl - gain)
        loser.next_ttl += loss
        self.stats[me.name]["made"] += 1
        self.stats[them.name]["received"] += 1
        self.stats[winner.name]["won"] += 1
        self.stats[winner.name]["gained"] += gain
        self.stats[loser.name]["lost"] += loss
        self.weekly[(them.name, int(elapsed // WEEK))] += 1
        if self.rules.shield_hours:
            self.shielded_until[them.id] = elapsed + self.rules.shield_hours * 3600


def _one(job: tuple) -> dict:
    """One seeded realm, in a worker: with FIGHT under a rule set, or none."""
    scenario, rules_name, seed, days, step = job
    strategies = assign(scenario, REALM, seed)
    fights = Fights(RULES[rules_name], strategies, seed) if rules_name else None
    # Alignments rotate with the seed, so none is tied to a level.
    roster = [simulate.NINE[(i + seed) % len(simulate.NINE)] for i in range(len(REALM))]
    results = simulate.run(roster, days=days, step=step, seed=seed,
                           habits=PROFILES["average"], curve=Curve(),
                           levels=REALM, hook=fights)
    players = []
    for i, r in enumerate(results):
        s = fights.stats[r.name] if fights else Counter()
        worst = max((n for (name, _), n in fights.weekly.items() if name == r.name),
                    default=0) if fights else 0
        players.append({"i": i, "start": r.start, "pace": r.pace,
                        "strategy": strategies[i], "worst_week": worst, **s})
    return {"scenario": scenario, "rules": rules_name, "seed": seed, "players": players}


def _mean_ci(values: list[float]) -> tuple[float, float]:
    mean = statistics.mean(values)
    ci = 1.96 * statistics.stdev(values) / math.sqrt(len(values)) if len(values) > 1 else 0.0
    return mean, ci


def report(runs: list[dict], days: float) -> str:
    control = {(r["seed"], p["i"]): p["pace"] for r in runs if not r["rules"]
               for p in r["players"]}
    weeks = days / 7
    lines = []
    cases = sorted({(r["scenario"], r["rules"]) for r in runs if r["rules"]})
    for scenario, rules_name in cases:
        rows = [(r["seed"], p) for r in runs
                if (r["scenario"], r["rules"]) == (scenario, rules_name) for p in r["players"]]
        lines += ["", f"{scenario}, {rules_name}: pace against the same realm without fights"]
        head = (f"{'':14}{'n':>4}{'change':>9}{'±95%':>7}{'made/wk':>9}{'taken/wk':>10}"
                f"{'won':>6}{'net h/wk':>10}{'worst wk':>10}")
        lines += [head, "-" * len(head)]
        groups = [(t, lambda p, t=t: tier(p["start"]) == t) for t in TIERS]
        if scenario == "mixed":
            groups += [(s, lambda p, s=s: p["strategy"] == s) for s in STRATEGIES]
        for label, member in groups:
            group = [(seed, p) for seed, p in rows if member(p)]
            if not group:
                continue
            change, ci = _mean_ci([p["pace"] - control[(seed, p["i"])] for seed, p in group])
            made = sum(p.get("made", 0) for _, p in group) / len(group) / weeks
            taken = sum(p.get("received", 0) for _, p in group) / len(group) / weeks
            bouts = sum(p.get("made", 0) + p.get("received", 0) for _, p in group)
            won = sum(p.get("won", 0) for _, p in group) / bouts if bouts else 0.0
            net = sum(p.get("gained", 0) - p.get("lost", 0) for _, p in group)
            net = net / len(group) / 3600 / weeks
            worst = max(p["worst_week"] for _, p in group)
            mark = "*" if abs(change) > ci else " "
            lines.append(f"{label:14}{len(group):>4}{change:>+9.3f}{ci:>7.3f}{mark}"
                         f"{made:>8.1f}{taken:>10.1f}{won:>6.0%}{net:>+10.1f}{worst:>10}")
    lines += ["", "change: pace (seconds earned per second) with fights, less without, "
              "same seed.", "* the interval clears zero: a real effect, not luck."]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m idlerpg.fairness",
                                     description="Test FIGHT's fairness in simulation.")
    parser.add_argument("--rules", default=",".join(RULES))
    parser.add_argument("--scenario", default="bullies,mixed")
    parser.add_argument("--seeds", type=int, default=24)
    parser.add_argument("--days", type=float, default=21)
    parser.add_argument("--step", type=int, default=1800)
    parser.add_argument("--jobs", type=int, default=1)
    args = parser.parse_args(argv)

    rules = [r.strip() for r in args.rules.split(",") if r.strip()]
    unknown = [r for r in rules if r not in RULES]
    if unknown:
        parser.error(f"unknown rules {unknown}; one of {sorted(RULES)}")
    scenarios = [s.strip() for s in args.scenario.split(",") if s.strip()]
    seeds = range(1, args.seeds + 1)
    # The control - no fights - is the same whatever the scenario.
    work = [("mixed", "", seed, args.days, args.step) for seed in seeds]
    work += [(s, r, seed, args.days, args.step)
             for s in scenarios for r in rules for seed in seeds]
    if args.jobs <= 1:
        runs = [_one(job) for job in work]
    else:
        with ProcessPoolExecutor(max_workers=args.jobs) as pool:
            runs = list(pool.map(_one, work))
    print(f"{len(REALM)} players at levels {REALM}, {args.days:g} days, "
          f"{args.seeds} seeds, {args.step // 60}m ticks.")
    print(report(runs, args.days))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
