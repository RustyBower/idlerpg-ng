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
import random
import statistics
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from functools import partial

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from . import auth, events
from . import engine as engine_module
from .engine import Engine
from .models import Base, EventLog, PenaltyRecord, Platform, Presence
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
class Habits:
    talk_per_day: float = 2.0       # lines said in the game channel
    absences_per_week: float = 1.0  # quits, each followed by time away
    away_hours: float = 8.0         # mean length of an absence


@dataclass
class Result:
    name: str
    alignment: str
    level: int
    pace: float
    penalties: dict = field(default_factory=dict)
    events: dict = field(default_factory=dict)


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
        curve: Curve | None = None) -> list[Result]:
    """Simulate ``days`` of the realm and return how each player fared."""
    if len(roster) > 99 * len(NINE):
        raise ValueError("at most 99 players of each alignment")
    habits = habits or Habits()
    curve = curve or Curve()
    rng = random.Random(seed)             # the players' habits
    db = create_engine("sqlite://")
    Base.metadata.create_all(db)
    session = Session(db)
    realm = Engine(session, curve, rng=random.Random(seed + 1))

    # Real hashing is deliberately slow; nothing here is a real password.
    real_hash = engine_module.hash_password
    engine_module.hash_password = partial(auth.hash_password, iterations=1)
    try:
        players, seen = [], Counter()
        for alignment in roster:
            code = _code(alignment)
            name = f"{code}{seen[code]:02d}"
            seen[code] += 1
            p = realm.register(name, "pw", alignment.title(), Platform.IRC, name)
            realm.set_alignment(p, alignment)
            p.level, p.next_ttl = start_level, int(ttl(start_level, curve))
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

        base = seconds_to_reach(start_level, curve)
        results = []
        for p, alignment in players:
            earned = seconds_to_reach(p.level, curve) + ttl(p.level, curve) - p.next_ttl - base
            results.append(Result(
                name=p.name, alignment=alignment, level=p.level,
                pace=earned / (days * DAY),
                penalties=dict(penalties[p.name]), events=dict(counts[p.name]),
            ))
        return results
    finally:
        engine_module.hash_password = real_hash
        session.close()


def summarise(results: list[Result], days: float) -> list[dict]:
    """One row per alignment, in the order of the nine."""
    weeks = days / 7
    rows = []
    for alignment in NINE:
        group = [r for r in results if r.alignment == alignment]
        if not group:
            continue
        paces = [r.pace for r in group]
        rows.append({
            "alignment": alignment,
            "players": len(group),
            "level": statistics.mean(r.level for r in group),
            "pace": statistics.mean(paces),
            "pace_spread": statistics.pstdev(paces),
            "penalty_seconds": statistics.mean(sum(r.penalties.values()) for r in group),
            "per_week": {k: sum(r.events.get(k, 0) for r in group) / len(group) / weeks
                         for k in KINDS},
        })
    return rows


def report(rows: list[dict], days: float, step: int, seed: int, count: int) -> str:
    shown = ["hog", "calamity", "godsend", "battle", "goodness", "evilness",
             "chaos", "balance", "quest"]
    head = (f"{'alignment':<16}{'n':>3}{'level':>7}{'pace':>7}{'±':>6}{'penalties':>11}"
            + "".join(f"{k[:6]:>8}" for k in shown))
    lines = [f"{days:g} days, {count} players, {duration(step)} ticks, seed {seed}. "
             f"Events are per player per week.", "", head, "-" * len(head)]
    for r in rows:
        lines.append(
            f"{r['alignment']:<16}{r['players']:>3}{r['level']:>7.1f}{r['pace']:>7.3f}"
            f"{r['pace_spread']:>6.3f}{duration(r['penalty_seconds']):>11}"
            + "".join(f"{r['per_week'][k]:>8.2f}" for k in shown))
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
    parser.add_argument("--talk", type=float, default=2.0, help="lines said per day")
    parser.add_argument("--absences", type=float, default=1.0, help="quits per week")
    parser.add_argument("--away-hours", type=float, default=8.0)
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument("--set", action="append", default=[], metavar="NAME=VALUE",
                        help="override a tuning number in events.py, "
                             "e.g. LAWFUL_PENALTY=0.85 or LUCK.chaotic=1.25")
    parser.add_argument("--json", help="also write every player's results here")
    args = parser.parse_args(argv)

    roster = parse_roster(args.players, args.per_alignment)
    restore = apply_overrides(args.set)
    try:
        results = run(roster, days=args.days, step=args.step, seed=args.seed,
                      habits=Habits(args.talk, args.absences, args.away_hours),
                      start_level=args.start_level)
    finally:
        restore()
    rows = summarise(results, args.days)
    print(report(rows, args.days, args.step, args.seed, len(roster)))
    if args.json:
        with open(args.json, "w") as fh:
            json.dump({"settings": vars(args), "alignments": rows,
                       "players": [r.__dict__ for r in results]}, fh, indent=2)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
