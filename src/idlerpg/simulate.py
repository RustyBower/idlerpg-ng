"""Simulate the realm offline, to balance its numbers.

Runs the real engine - its tick, events, quests and penalties - against an
in-memory database, with simulated players whose alignments and habits you
choose, over weeks of game time in a minute or two of real time. Reports how
each alignment fared, so the tuning at the top of events.py can be checked
against data rather than guessed.

    python -m idlerpg.simulate
    python -m idlerpg.simulate --days 60 --per-alignment 5 --talk 4
    python -m idlerpg.simulate --players "lawful good:5,chaotic evil:5"
    python -m idlerpg.simulate --set LUCK.chaotic=1.25 --set LAWFUL_PENALTY=0.85

The number to watch is pace: progress earned over time elapsed. Idling alone
gives 1.0 less the time spent away; events push it up and penalties pull it
down. Balanced alignments land close together. Every run is seeded, so a
changed number can be compared against the same luck.
"""

from __future__ import annotations

import argparse
import json
import math
import random
import statistics
from collections import Counter, defaultdict
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, field
from functools import partial

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from . import auth, events, lore
from . import engine as engine_module
from .engine import Engine
from .models import Base, EventLog, PenaltyRecord, Platform, Presence
from .npcs import PROFILES, Habits  # the habits NPCs play by, and players here
from .rules import Curve, Penalty, seconds_to_reach, ttl
from .text import duration

DAY = 86400
WEEK = 7 * DAY

NINE = [
    "lawful good", "lawful neutral", "lawful evil",
    "neutral good", "true neutral", "neutral evil",
    "chaotic good", "chaotic neutral", "chaotic evil",
]

# Event kinds worth reporting; registrations and the like are not.
KINDS = ["hog", "calamity", "godsend", "battle", "goodness", "evilness",
         "chaos", "balance", "war", "quest", "item", "levelup"]


@dataclass
class Result:
    name: str
    alignment: str
    level: int
    pace: float
    penalties: dict = field(default_factory=dict)
    events: dict = field(default_factory=dict)
    seed: int = 0
    start: int = 0      # the level it began at


def parse_roster(spec: str | None, per_alignment: int) -> list[str]:
    """ "lawful good:5,chaotic evil:2" -> ten alignments; None -> all nine,
    per_alignment of each."""
    if not spec:
        return [a for a in NINE for _ in range(per_alignment)]
    roster = []
    for part in spec.split(","):
        alignment, _, count = part.strip().rpartition(":")
        if not alignment:
            alignment, count = count, "1"
        alignment = alignment.strip().lower()
        if alignment not in NINE:
            raise ValueError(f"unknown alignment {alignment!r}; one of: {', '.join(NINE)}")
        roster += [alignment] * int(count)
    return roster


def apply_overrides(pairs: list[str]):
    """Set tuning numbers in events.py from NAME=VALUE or NAME.key=VALUE.
    Returns a function that puts them back."""
    saved = []
    for pair in pairs:
        target, _, raw = pair.partition("=")
        name, _, key = target.strip().partition(".")
        if not hasattr(events, name):
            raise ValueError(f"events.py has no {name}")
        value = float(raw)
        current = getattr(events, name)
        if key:
            saved.append((name, key, current[key]))
            current[key] = value
        else:
            saved.append((name, None, current))
            setattr(events, name, type(current)(value) if isinstance(current, int) else value)

    def restore():
        for name, key, old in reversed(saved):
            if key:
                getattr(events, name)[key] = old
            else:
                setattr(events, name, old)
    return restore


def _code(alignment: str) -> str:
    return "".join(word[0] for word in alignment.split()).upper()


def run(roster: list[str], days: float = 30, step: int = 300, seed: int = 1,
        habits: Habits | None = None, start_level: int = 30,
        curve: Curve | None = None, levels: list[int] | None = None,
        hook=None, season: str | None = None) -> list[Result]:
    """Simulate ``days`` of the realm and return how each player fared.

    ``levels`` gives each player of the roster its own start level, for a
    realm of mixed levels. ``hook(realm, players, elapsed, step)`` is called
    every step before the tick, to try out rules the game does not have yet.
    """
    if len(roster) > 99 * len(NINE):
        raise ValueError("at most 99 players of each alignment")
    levels = list(levels) if levels is not None else [start_level] * len(roster)
    if len(levels) != len(roster):
        raise ValueError("one start level per player")
    habits = habits or Habits()
    curve = curve or Curve()
    rng = random.Random(seed)             # the players' habits
    kit = random.Random(seed + 2)         # the items found on the way up
    db = create_engine("sqlite://")
    Base.metadata.create_all(db)
    session = Session(db)
    realm = Engine(session, curve, rng=random.Random(seed + 1))

    # Real hashing is deliberately slow; nothing here is a real password.
    real_hash = engine_module.hash_password
    engine_module.hash_password = partial(auth.hash_password, iterations=1)
    # Seasons draw on the luck and bend a rule: off unless a run asks for one,
    # so it gives the same answer in October as in May.
    real_season, lore.SEASON_OVERRIDE = lore.SEASON_OVERRIDE, season or "off"
    try:
        players, seen = [], Counter()
        for alignment, start in zip(roster, levels):
            code = _code(alignment)
            name = f"{code}{seen[code]:02d}"
            seen[code] += 1
            p = realm.register(name, "pw", alignment.title(), Platform.IRC, name)
            realm.set_alignment(p, alignment)
            # Start as a player who climbed here would: with a find at every
            # level so far. Empty-handed, a realm starting high fights with
            # nothing, and battles - a share of the clock each - swamp it.
            for level in range(1, start + 1):
                p.level = level
                events.find_item(p, kit)
            p.level, p.next_ttl = start, int(ttl(start, curve))
            players.append((p, alignment))
        session.commit()
        session.query(EventLog).delete()   # registrations are not the game
        realm._pending.clear()

        away_until: dict[int, float] = {}
        elapsed = 0.0
        while elapsed < days * DAY:
            for p, _ in players:
                back = away_until.get(p.id)
                if back is not None:
                    if elapsed >= back:
                        realm.set_presence(Platform.IRC, p.name, Presence.ACTIVE)
                        del away_until[p.id]
                    continue
                if rng.random() < habits.talk_per_day * step / DAY:
                    realm.penalise(p, Penalty.MESSAGE,
                                   message_length=rng.randint(10, 80),
                                   platform=Platform.IRC)
                if rng.random() < habits.absences_per_week * step / WEEK:
                    realm.penalise(p, Penalty.QUIT, platform=Platform.IRC)
                    realm.set_presence(Platform.IRC, p.name, Presence.OFFLINE)
                    away_until[p.id] = elapsed + rng.expovariate(
                        1 / (habits.away_hours * 3600))
            if hook is not None:
                hook(realm, players, elapsed, step)
            realm.tick(step)
            elapsed += step

        counts: dict[str, Counter] = defaultdict(Counter)
        for kind, message in session.execute(select(EventLog.kind, EventLog.message)):
            for p, _ in players:
                if p.name in message:
                    counts[p.name][kind] += 1
        penalties: dict[str, Counter] = defaultdict(Counter)
        by_id = {p.id: p.name for p, _ in players}
        for pid, kind, seconds in session.execute(
                select(PenaltyRecord.player_id, PenaltyRecord.kind, PenaltyRecord.seconds)):
            penalties[by_id[pid]][kind] += seconds

        results = []
        for (p, alignment), start in zip(players, levels):
            base = seconds_to_reach(start, curve)
            earned = seconds_to_reach(p.level, curve) + ttl(p.level, curve) - p.next_ttl - base
            results.append(Result(
                name=p.name, alignment=alignment, level=p.level,
                pace=earned / (days * DAY),
                penalties=dict(penalties[p.name]), events=dict(counts[p.name]),
                seed=seed, start=start,
            ))
        return results
    finally:
        engine_module.hash_password = real_hash
        lore.SEASON_OVERRIDE = real_season
        session.close()


def _one(job: tuple) -> list[Result]:
    """One seeded run in a worker process, with its own overrides applied."""
    roster, days, step, seed, habits, start_level, overrides, curve = job
    restore = apply_overrides(overrides)
    try:
        return run(roster, days=days, step=step, seed=seed, habits=habits,
                   start_level=start_level, curve=curve)
    finally:
        restore()


def run_many(roster: list[str], seeds: list[int], jobs: int = 1,
             overrides: list[str] | None = None, **kwargs) -> list[Result]:
    """Several seeded runs, in parallel across ``jobs`` processes. Every
    worker applies the same overrides, so the runs differ only by luck."""
    work = [(roster, kwargs.get("days", 30), kwargs.get("step", 300), seed,
             kwargs.get("habits") or Habits(), kwargs.get("start_level", 30),
             list(overrides or []), kwargs.get("curve") or Curve()) for seed in seeds]
    if jobs <= 1:
        return [r for job in work for r in _one(job)]
    with ProcessPoolExecutor(max_workers=jobs) as pool:
        return [r for batch in pool.map(_one, work) for r in batch]


def summarise(results: list[Result], days: float) -> list[dict]:
    """One row per alignment, in the order of the nine.

    pace_ci is the half-width of a 95% confidence interval on the mean, over
    every player of that alignment in every seed. relative is the mean's
    distance from the whole realm's, which is what balance is about: how
    much everyone talks moves every alignment together.
    """
    weeks = days / 7
    overall = statistics.mean(r.pace for r in results)
    rows = []
    for alignment in NINE:
        group = [r for r in results if r.alignment == alignment]
        if not group:
            continue
        paces = [r.pace for r in group]
        mean = statistics.mean(paces)
        ci = 1.96 * statistics.stdev(paces) / math.sqrt(len(paces)) if len(paces) > 1 else 0.0
        rows.append({
            "alignment": alignment,
            "players": len(group),
            "level": statistics.mean(r.level for r in group),
            "pace": mean,
            "pace_ci": ci,
            "relative": mean / overall - 1,
            "penalty_seconds": statistics.mean(sum(r.penalties.values()) for r in group),
            "per_week": {k: sum(r.events.get(k, 0) for r in group) / len(group) / weeks
                         for k in KINDS},
        })
    return rows


def spread(rows: list[dict]) -> float:
    """How far apart the best and worst alignments are, relative to the realm."""
    return max(r["relative"] for r in rows) - min(r["relative"] for r in rows)


def report(rows: list[dict], days: float, step: int, seeds: list[int],
           count: int, label: str = "") -> str:
    shown = ["hog", "calamity", "godsend", "battle", "goodness", "evilness",
             "chaos", "balance", "quest"]
    head = (f"{'alignment':<16}{'n':>4}{'level':>7}{'pace':>7}{'±95%':>7}{'vs all':>8}"
            f"{'penalties':>11}" + "".join(f"{k[:6]:>8}" for k in shown))
    seeds_text = f"seed {seeds[0]}" if len(seeds) == 1 else f"seeds {seeds[0]}-{seeds[-1]}"
    lines = [f"{label + ': ' if label else ''}{days:g} days, {count} players, "
             f"{duration(step)} ticks, {seeds_text}. Events are per player per week.",
             "", head, "-" * len(head)]
    for r in rows:
        lines.append(
            f"{r['alignment']:<16}{r['players']:>4}{r['level']:>7.1f}{r['pace']:>7.3f}"
            f"{r['pace_ci']:>7.3f}{r['relative']:>+8.1%}"
            f"{duration(r['penalty_seconds']):>11}"
            + "".join(f"{r['per_week'][k]:>8.2f}" for k in shown))
    lines.append(f"Spread between the best and worst alignment: {spread(rows):.1%} of "
                 f"the realm's pace.")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m idlerpg.simulate",
        description="Simulate the realm offline to balance its numbers.")
    parser.add_argument("--days", type=float, default=30)
    parser.add_argument("--step", type=int, default=300, help="seconds per tick")
    parser.add_argument("--per-alignment", type=int, default=3)
    parser.add_argument("--players", help='e.g. "lawful good:5,chaotic evil:5"')
    parser.add_argument("--start-level", type=int, default=30)
    parser.add_argument("--profile", choices=sorted(PROFILES), default=None,
                        help="a kind of player: sets --talk, --absences and --away-hours")
    parser.add_argument("--talk", type=float, help="lines said per day (default 2)")
    parser.add_argument("--absences", type=float, help="quits per week (default 1)")
    parser.add_argument("--away-hours", type=float, help="mean absence (default 8)")
    parser.add_argument("--rp-step", type=float,
                        help="the level curve, as RP_STEP (default %s)" % Curve().step)
    parser.add_argument("--penalty-step", type=float,
                        help="how fast penalties grow, as RP_PENALTY_STEP "
                             "(default %s)" % Curve().penalty_step)
    parser.add_argument("--post-cap-step",
                        help="how much more each level past 60 costs than the last, "
                             'as RP_POST_CAP_STEP, or "linear" for the original\'s '
                             "day a level (default %s)" % Curve().post_cap_step)
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument("--seeds", type=int, default=1,
                        help="runs to average, seeded from --seed upward")
    parser.add_argument("--jobs", type=int, default=1, help="runs at once, one per process")
    parser.add_argument("--set", action="append", default=[], metavar="NAME=VALUE",
                        help="override a tuning number in events.py, "
                             "e.g. LAWFUL_PENALTY=0.85 or LUCK.chaotic=1.25")
    parser.add_argument("--json", help="also write every player's results here")
    args = parser.parse_args(argv)

    base = PROFILES[args.profile] if args.profile else Habits()
    habits = Habits(
        args.talk if args.talk is not None else base.talk_per_day,
        args.absences if args.absences is not None else base.absences_per_week,
        args.away_hours if args.away_hours is not None else base.away_hours,
    )
    roster = parse_roster(args.players, args.per_alignment)
    apply_overrides(args.set)()  # refuse unknown names before any work starts
    seeds = list(range(args.seed, args.seed + args.seeds))
    default = Curve()
    post = default.post_cap_step
    if args.post_cap_step is not None:
        post = None if args.post_cap_step.lower() == "linear" else float(args.post_cap_step)
    curve = Curve(step=args.rp_step or default.step,
                  penalty_step=args.penalty_step or default.penalty_step,
                  post_cap_step=post)
    results = run_many(roster, seeds, jobs=args.jobs, overrides=args.set,
                       days=args.days, step=args.step, habits=habits,
                       start_level=args.start_level, curve=curve)
    rows = summarise(results, args.days)
    label = ", ".join(filter(None, [
        args.profile,
        f"rpstep {curve.step}" if curve.step != default.step else "",
        f"penalty step {curve.penalty_step}"
        if curve.penalty_step != default.penalty_step else "",
        f"past 60 {curve.post_cap_step or 'linear'}"
        if curve.post_cap_step != default.post_cap_step else "",
        *args.set,
    ]))
    print(report(rows, args.days, args.step, seeds, len(roster), label=label))
    if args.json:
        with open(args.json, "w") as fh:
            json.dump({"settings": vars(args), "habits": habits.__dict__,
                       "spread": spread(rows), "alignments": rows,
                       "players": [r.__dict__ for r in results]}, fh, indent=2)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
