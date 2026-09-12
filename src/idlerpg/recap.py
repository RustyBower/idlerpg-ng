"""The week in review, posted to every platform.

Counted from the event log rather than from tallies of its own: the numbers
are then exactly what the realm was told at the time, and a restart cannot
lose a week of them.

The window is a range of event ids, not of clock time. Ids are exact across a
restart, need no index on a timestamp, and dodge the question of what a naive
datetime in SQLite means next to an aware one from Postgres - a comparison
that is right on one database and quietly wrong on the other.
"""

from __future__ import annotations

import datetime as dt
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field

from sqlalchemy import func, select

from . import lore
from .events import Outcome
from .models import EventLog, Player
from .text import duration

# Sunday evening UTC: the week just gone, at an hour that is not the middle
# of anyone's night in the halves of the world the realm is played from.
WEEKDAY = 6
HOUR = 18
PERIOD = 7 * 86400

DUE_KEY = "recap_due"     # unix time of the next recap
SEEN_KEY = "recap_seen"   # the last event id the last recap covered

TOP = 3                   # climbers named
NAMED = 5                 # newcomers named before it becomes a count

VERBS = frozenset({"RECAP"})


def now() -> int:
    return int(time.time())


def next_due(after: int) -> int:
    """The next Sunday 18:00 UTC strictly after ``after``."""
    when = dt.datetime.fromtimestamp(after, tz=dt.timezone.utc)
    ahead = (WEEKDAY - when.weekday()) % 7
    boundary = (when + dt.timedelta(days=ahead)).replace(
        hour=HOUR, minute=0, second=0, microsecond=0)
    if boundary.timestamp() <= after:
        boundary += dt.timedelta(days=7)
    return int(boundary.timestamp())


@dataclass
class Survey:
    """What happened in a window, by kind and by who it was about."""

    totals: Counter = field(default_factory=Counter)
    who: dict[str, Counter] = field(default_factory=lambda: defaultdict(Counter))


def latest_id(engine) -> int:
    return engine.session.scalar(select(func.max(EventLog.id))) or 0


def survey(engine, after: int) -> Survey:
    rows = engine.session.execute(
        select(EventLog.kind, EventLog.player_id).where(EventLog.id > after)
    ).all()
    out = Survey()
    for kind, player_id in rows:
        out.totals[kind] += 1
        if player_id:
            out.who[kind][player_id] += 1
    return out


def _names(engine, ids) -> dict[int, str]:
    """Names for the ids still in the realm; whoever has left is dropped."""
    ids = [i for i in set(ids) if i]
    if not ids:
        return {}
    return {i: n for i, n in engine.session.execute(
        select(Player.id, Player.name).where(Player.id.in_(ids)))}


def _count(n: int, noun: str) -> str:
    return f"{n} {noun}" if n == 1 else f"{n} {noun}s"


def _listed(names: list[str]) -> str:
    if len(names) == 1:
        return names[0]
    return ", ".join(names[:-1]) + f" and {names[-1]}"


def lines(engine, after: int) -> list[str]:
    """The recap for events since ``after``, or nothing if the week was empty."""
    s = survey(engine, after)
    levels = s.totals["levelup"]
    battles = s.totals["battle"] + s.totals["fight"]
    quests = s.totals["questdone"]
    joined = s.who["register"]
    feats = s.totals["achievement"]
    finds = s.totals["item"]
    if not (levels or battles or quests or joined):
        return []

    wanted = set(joined)
    for kind in ("levelup", "battle", "fight"):
        wanted.update(s.who[kind])
    names = _names(engine, wanted)

    head = f"The week in the realm: {_count(levels, 'level')} gained"
    if battles:
        head += f", {_count(battles, 'fight')} fought"
    if quests:
        head += f", {_count(quests, 'quest')} finished"
    if finds:
        head += f", {_count(finds, 'item')} found"
    out = [head + "."]

    climbers = [(names[i], n) for i, n in s.who["levelup"].most_common()
                if i in names][:TOP]
    won: Counter = Counter()
    for kind in ("battle", "fight"):
        won.update(s.who[kind])
    best = next(((names[i], n) for i, n in won.most_common() if i in names), None)
    second = []
    if climbers:
        second.append("Climbing hardest: " + ", ".join(
            f"{name} ({_count(n, 'level')})" for name, n in climbers) + ".")
    if best is not None:
        second.append(f"Most fights won: {best[0]} ({best[1]}).")
    if second:
        out.append(" ".join(second))

    third = []
    newcomers = [names[i] for i in joined if i in names]
    if newcomers:
        third.append("New to the realm: " + (
            _listed(newcomers) if len(newcomers) <= NAMED
            else f"{_listed(newcomers[:NAMED])} and {len(newcomers) - NAMED} more") + ".")
    if feats:
        third.append(f"{_count(feats, 'feat')} earned.")
    if third:
        out.append(" ".join(third))

    top = engine.top_players(TOP)
    if top:
        out.append("At the top: " + ", ".join(
            f"{p.name} (level {p.level})" for p in top) + ".")
    season = lore.current_season()
    if season is not None:
        out.append(f"{season.name} is still on.")
    return out


def maybe(engine, at: int | None = None) -> list[Outcome]:
    """The recap, if one is due. Called on every tick; cheap when it is not.

    The first run only arms the clock: the realm is told about the week that
    follows, not about however much history the log happens to hold.
    """
    at = now() if at is None else at
    due = engine.get_setting(DUE_KEY)
    if due is None:
        engine.set_setting(DUE_KEY, str(next_due(at)))
        engine.set_setting(SEEN_KEY, str(latest_id(engine)))
        return []
    try:
        when = int(due)
    except ValueError:
        when = next_due(at)
    if at < when:
        return []
    engine.set_setting(DUE_KEY, str(next_due(at)))
    return report(engine)


def report(engine) -> list[Outcome]:
    """Build the recap and move the window on, whether or not it says much."""
    after = _seen(engine)
    mark = latest_id(engine)
    said = lines(engine, after)
    engine.set_setting(SEEN_KEY, str(mark))
    return [Outcome(line, kind="recap") for line in said]


def _seen(engine) -> int:
    try:
        return int(engine.get_setting(SEEN_KEY) or 0)
    except ValueError:
        return 0


def command(engine, player: Player | None, verb: str, args: list[str]) -> str:
    """RECAP: the week so far, and when the next one is posted."""
    said = lines(engine, _seen(engine))
    due = engine.get_setting(DUE_KEY)
    when = int(due) if due and due.isdigit() else next_due(now())
    ahead = max(0, when - now())
    if not said:
        return f"Nothing has happened yet this week. The next recap is in {duration(ahead)}."
    return " ".join(said) + f" The next recap is in {duration(ahead)}."
