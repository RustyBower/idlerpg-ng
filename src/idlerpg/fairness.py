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
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import timedelta

from . import events, quests, simulate
from .npcs import PROFILES
from .rules import Curve

DAY = 86400
WEEK = 7 * DAY
EAGER_SECONDS = 2 * 3600   # a fight is used within a couple of hours online
SUBSTEP = 5                # the live tick: a step on the map every 5 seconds


@dataclass(frozen=True)
class Rules:
    """One candidate rule set for FIGHT."""

    name: str
    stake: float = 0.05             # each fighter stakes this share of their clock
    min_level: int = 0              # nobody below this fights or is fought
    max_below: int | None = None    # a target at most this many levels below you
    shield_hours: float = 0         # once challenged, safe from challenges this long
    underdog_bonus: float = 1.0     # the winner's gain, beating a higher level
    # Staked, each fighter risks a share of their own clock. Transferred, the
    # winner takes a share of the loser's: what a fight is worth is set by
    # the loser, and a win past the end of your clock carries into the next
    # level, as any time does.
    transfer: bool = False
    # Each fighter's stake scaled by their place on the law-chaos axis, as
    # luck is: lawful risks half, chaotic half as much again.
    luck_stakes: bool = False
    # Good fights at +10% strength and evil at -10%, as in the original's
    # battles.
    moral_rolls: bool = False
    # A transfer takes no more than the stake of what the winner's own level
    # costs, so a win is worth as much to a low player as to a high one and
    # nobody at the wall wins days from one fight.
    cap_by_winner: bool = False
    # The daily FIGHT itself; and duels when two characters meet on a tile,
    # at most once a day for any pair, on FIGHT's even, capped transfer.
    # Meetings need --walk, so characters move at the live pace.
    daily: bool = True
    meetings: bool = False
    # A season kept throughout, to measure its twist against a realm
    # without one: the season rule sets have no fights.
    season: str | None = None


MORAL = {"good": 1.1, "neutral": 1.0, "evil": 0.9}


RULES = {
    "open": Rules("open"),
    "proposed": Rules("proposed", min_level=10, max_below=5, shield_hours=24,
                      underdog_bonus=1.5),
    "no-shield": Rules("no-shield", min_level=10, max_below=5, underdog_bonus=1.5),
    "tight": Rules("tight", min_level=10, max_below=2, shield_hours=24,
                   underdog_bonus=1.5),
    "transfer-open": Rules("transfer-open", transfer=True),
    "transfer": Rules("transfer", min_level=10, max_below=5, shield_hours=24,
                      underdog_bonus=1.5, transfer=True),
    "transfer-tight": Rules("transfer-tight", min_level=10, max_below=2,
                            shield_hours=24, underdog_bonus=1.5, transfer=True),
}
# The proposed limits with an alignment twist, staked and transferred.
_PROPOSED = dict(min_level=10, max_below=5, shield_hours=24, underdog_bonus=1.5)
RULES.update({
    "luck": Rules("luck", luck_stakes=True, **_PROPOSED),
    "moral": Rules("moral", moral_rolls=True, **_PROPOSED),
    "transfer-luck": Rules("transfer-luck", transfer=True, luck_stakes=True, **_PROPOSED),
    "transfer-moral": Rules("transfer-moral", transfer=True, moral_rolls=True, **_PROPOSED),
    "transfer-capped": Rules("transfer-capped", transfer=True, cap_by_winner=True,
                             **_PROPOSED),
    # As transfer-capped, but without the underdog bonus, which makes time
    # from nothing: the winner gets exactly what the loser loses.
    "transfer-even": Rules("transfer-even", transfer=True, cap_by_winner=True,
                           **{**_PROPOSED, "underdog_bonus": 1.0}),
    # Meeting on the map: alone, and beside the daily FIGHT as shipped.
    "meetings": Rules("meetings", daily=False, meetings=True, transfer=True,
                      cap_by_winner=True, min_level=10),
    "fight+meetings": Rules("fight+meetings", meetings=True, transfer=True,
                            cap_by_winner=True, **{**_PROPOSED, "underdog_bonus": 1.0}),
    # The seasons' twists, each against the same realm with no season.
    "hallowtide": Rules("hallowtide", daily=False, season="Hallowtide"),
    "midwinter": Rules("midwinter", daily=False, season="Midwinter"),
    "springtide": Rules("springtide", daily=False, season="Springtide"),
})

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


def bands(levels: list[int], count: int = 4) -> list[tuple[int, int]]:
    """Any other realm's start levels, split into about ``count`` bands."""
    distinct = sorted(set(levels))
    size = math.ceil(len(distinct) / count)
    return [(g[0], g[-1]) for g in
            (distinct[i:i + size] for i in range(0, len(distinct), size))]


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
    champions: as bullies, and the level-30 veterans have five ranks of the
    Champion perk - prestiged players among fresh ones.
    everyone: all bully - the worst case.
    mixed: the four strategies rotate through the roster with the seed, so
    each is played at every level and can be compared head to head.
    """
    if scenario in ("bullies", "champions"):
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
        self.curve = Curve()               # the realm's, once it is running
        self.pair_ready: dict[tuple[int, int], float] = {}

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
        self.curve = realm.curve
        if not self.rules.daily:
            return
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

    def clash(self, a, b, at: float) -> None:
        """Two characters met on a tile: a duel on FIGHT's even, capped
        transfer, if both are old enough and this pair has not met today."""
        r = self.rules
        if a.level < r.min_level or b.level < r.min_level:
            return
        pair = (min(a.id, b.id), max(a.id, b.id))
        if at < self.pair_ready.get(pair, 0):
            return
        self.pair_ready[pair] = at + DAY
        won = self.rng.randrange(self.strength(a)) >= self.rng.randrange(self.strength(b))
        winner, loser = (a, b) if won else (b, a)
        cap = events.level_cost(winner, winner.level, self.curve) * r.stake
        amount = max(0, int(min(loser.next_ttl * r.stake, cap)))
        winner.next_ttl -= amount
        loser.next_ttl += amount
        for p in (a, b):
            self.stats[p.name]["received"] += 1
            self.weekly[(p.name, int(at // WEEK))] += 1
        self.stats[winner.name]["won"] += 1
        self.stats[winner.name]["gained"] += amount
        self.stats[loser.name]["lost"] += amount

    def strength(self, p) -> int:
        s = events.item_sum(p) * events.champion(p)
        if self.rules.moral_rolls:
            s *= MORAL.get(p.alignment.value, 1.0)
        return max(1, int(s))

    def luck(self, p) -> float:
        return events.LUCK.get(events.ethos(p), 1.0) if self.rules.luck_stakes else 1.0

    def fight(self, me, them, elapsed: float) -> None:
        won = self.rng.randrange(self.strength(me)) >= self.rng.randrange(self.strength(them))
        winner, loser = (me, them) if won else (them, me)
        bonus = self.rules.underdog_bonus if winner.level < loser.level else 1.0
        loss = int(loser.next_ttl * self.rules.stake * self.luck(loser))
        if self.rules.transfer:
            if self.rules.cap_by_winner:
                own = events.level_cost(winner, winner.level, self.curve)
                loss = min(loss, int(own * self.rules.stake))
            gain = int(loss * bonus)
            winner.next_ttl -= gain       # past zero, the tick levels them up
        else:
            gain = int(winner.next_ttl * self.rules.stake * self.luck(winner) * bonus)
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


class Walk:
    """Characters stepping at the live pace - a step every SUBSTEP seconds -
    rather than once a simulated tick, so they meet as often as they would
    live, and keep meeting the same neighbours; with ``fights``, they clash
    when they do. Run in the control too, so only the duels differ."""

    def __init__(self, seed: int, fights: Fights | None):
        self.rng = random.Random(seed + 11)
        self.fights = fights

    def __call__(self, realm, players, elapsed: float, step: int) -> None:
        from .engine import events_map_x, events_map_y
        mx, my = events_map_x(), events_map_y()
        walkers = [p for p, _ in players if p.is_idling]
        for sub in range(max(1, int(step // SUBSTEP))):
            for p in walkers:
                events.move_player(p, mx, my, self.rng)
            if self.fights is None:
                continue
            tiles: dict[tuple[int, int], list] = {}
            for p in walkers:
                tiles.setdefault((p.x, p.y), []).append(p)
            for group in tiles.values():
                for i, a in enumerate(group):
                    for b in group[i + 1:]:
                        self.fights.clash(a, b, elapsed + sub * SUBSTEP)


# The realm's events that act on a share of the clock, silenced by --calm.
_CALM = ("HOG_INTERVAL", "CALAMITY_INTERVAL", "GODSEND_INTERVAL", "TEAM_BATTLE_INTERVAL",
         "WAR_INTERVAL", "GOODNESS_INTERVAL", "EVILNESS_INTERVAL", "CHAOS_INTERVAL",
         "BALANCE_INTERVAL")


@contextmanager
def calm():
    """A realm without the events that move a share of the clock - battles,
    godsends, calamities, the Hand of God, war, quests - so what fights do
    can be told apart from what those events then make of it."""
    saved = {name: getattr(events, name) for name in _CALM}
    will_fight, cooldown = events.will_fight, quests.COOLDOWN
    for name in _CALM:
        setattr(events, name, 1e18)
    events.will_fight = lambda challenger, rng: False
    quests.COOLDOWN = timedelta(days=10**6)
    try:
        yield
    finally:
        for name, value in saved.items():
            setattr(events, name, value)
        events.will_fight, quests.COOLDOWN = will_fight, cooldown


def _one(job: tuple) -> dict:
    if len(job) > 7 and job[7]:
        with calm():
            return _run(job)
    return _run(job)


def _run(job: tuple) -> dict:
    """One seeded realm, in a worker: with FIGHT under a rule set, or none."""
    scenario, rules_name, seed, days, step, *rest = job
    levels = rest[0] if rest else REALM
    walk = rest[1] if len(rest) > 1 else False     # rest[2], calm, is _one's
    strategies = assign(scenario, levels, seed)
    fights = Fights(RULES[rules_name], strategies, seed) if rules_name else None
    walker = (Walk(seed, fights if fights is not None and fights.rules.meetings else None)
              if walk else None)
    # Alignments rotate with the seed, so none is tied to a level.
    roster = [simulate.NINE[(i + seed) % len(simulate.NINE)] for i in range(len(levels))]
    started = []

    def hook(realm, players, elapsed, step):
        if not started:
            started.append(True)
            if scenario == "champions":     # in the control run too
                for (p, _), level in zip(players, levels):
                    if level >= 30:
                        p.set_perk_rank("champion", 5)
        if fights is not None:
            fights(realm, players, elapsed, step)
        if walker is not None:
            walker(realm, players, elapsed, step)

    results = simulate.run(roster, days=days, step=step, seed=seed,
                           habits=PROFILES["average"], curve=Curve(),
                           levels=levels, hook=hook,
                           season=RULES[rules_name].season if rules_name else None)
    players = []
    for i, r in enumerate(results):
        s = fights.stats[r.name] if fights else Counter()
        worst = max((n for (name, _), n in fights.weekly.items() if name == r.name),
                    default=0) if fights else 0
        players.append({"i": i, "start": r.start, "pace": r.pace,
                        "alignment": r.alignment,
                        "strategy": strategies[i], "worst_week": worst, **s})
    return {"scenario": scenario, "rules": rules_name, "seed": seed, "players": players}


def _mean_ci(values: list[float]) -> tuple[float, float]:
    mean = statistics.mean(values)
    ci = 1.96 * statistics.stdev(values) / math.sqrt(len(values)) if len(values) > 1 else 0.0
    return mean, ci


def _baseline(scenario: str) -> str:
    """Which control run a scenario is measured against."""
    return "champions" if scenario == "champions" else "mixed"


def _ethos(alignment: str) -> str:
    return "neutral" if alignment == "true neutral" else alignment.split()[0]


def _moral(alignment: str) -> str:
    return alignment.split()[-1]


def report(runs: list[dict], days: float, levels: list[int] = REALM) -> str:
    control = {(r["scenario"], r["seed"], p["i"]): p["pace"] for r in runs
               if not r["rules"] for p in r["players"]}
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
        if list(levels) == REALM:
            groups = [(t, lambda p, t=t: tier(p["start"]) == t) for t in TIERS]
        else:
            groups = [(f"levels {lo}-{hi}" if lo != hi else f"level {lo}",
                       lambda p, lo=lo, hi=hi: lo <= p["start"] <= hi)
                      for lo, hi in bands(levels)]
        if scenario == "mixed":
            groups += [(s, lambda p, s=s: p["strategy"] == s) for s in STRATEGIES]
        rules = RULES[rules_name]
        if rules.luck_stakes:
            groups += [(e, lambda p, e=e: _ethos(p["alignment"]) == e)
                       for e in ("lawful", "neutral", "chaotic")]
        if rules.moral_rolls:
            groups += [(m, lambda p, m=m: _moral(p["alignment"]) == m)
                       for m in ("good", "neutral", "evil")]
        base = _baseline(scenario)
        for label, member in groups:
            group = [(seed, p) for seed, p in rows if member(p)]
            if not group:
                continue
            change, ci = _mean_ci([p["pace"] - control[(base, seed, p["i"])]
                                   for seed, p in group])
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
    parser.add_argument("--levels", help="the realm's start levels, e.g. 62,60,58 "
                                         "(default: the live realm; the bullies and "
                                         "champions scenarios assume it)")
    parser.add_argument("--walk", action="store_true",
                        help="move characters a step every 5 seconds, as live, "
                             "rather than once a tick; meetings need it")
    parser.add_argument("--calm", action="store_true",
                        help="silence the events that move a share of the clock "
                             "(battles, godsends, calamities, war, quests...)")
    args = parser.parse_args(argv)

    rules = [r.strip() for r in args.rules.split(",") if r.strip()]
    unknown = [r for r in rules if r not in RULES]
    if unknown:
        parser.error(f"unknown rules {unknown}; one of {sorted(RULES)}")
    if any(RULES[r].meetings for r in rules) and not args.walk:
        parser.error("meetings need --walk: at one step a tick, nobody meets anyone")
    scenarios = [s.strip() for s in args.scenario.split(",") if s.strip()]
    levels = [int(v) for v in args.levels.split(",")] if args.levels else REALM
    seeds = range(1, args.seeds + 1)
    # The control - no fights - is the same whatever the strategies, but the
    # champions' perks change the realm itself, so they get their own.
    work = [(base, "", seed, args.days, args.step, levels, args.walk, args.calm)
            for base in sorted({_baseline(s) for s in scenarios}) for seed in seeds]
    work += [(s, r, seed, args.days, args.step, levels, args.walk, args.calm)
             for s in scenarios for r in rules for seed in seeds]
    if args.jobs <= 1:
        runs = [_one(job) for job in work]
    else:
        with ProcessPoolExecutor(max_workers=args.jobs) as pool:
            runs = list(pool.map(_one, work))
    print(f"{len(levels)} players at levels {levels}, {args.days:g} days, "
          f"{args.seeds} seeds, {args.step // 60}m ticks"
          + (", walking at the live pace" if args.walk else "")
          + (", with the clock-share events silenced." if args.calm else "."))
    print(report(runs, args.days, levels))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
