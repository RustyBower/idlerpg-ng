"""What a season adds to its words: honours for those who kept it.

When a season that ran its course ends, the realm honours its three most
devoted idlers - the three who made the most progress toward their next
levels during it - and everyone who idled through at least half of it wears
the season's badge. Progress is time toward levels rather than levels
counted, so a newcomer can take a title from a veteran: a level is worth
what it costs, not one apiece. NPCs are the realm's own and are not
honoured, and a season an admin forced to try out, or one shorter than a
week, honours nobody.

Each season's twist on the rules - trick or treat, the long nights, fresh
starts - lives with its words in lore.py and acts in the engine and events.
"""

from __future__ import annotations

import datetime as dt
import time

from sqlalchemy import delete, select

from . import lore
from .events import Outcome
from .models import Achievement, Player, SeasonMark
from .rules import seconds_to_reach, ttl

MIN_DAYS = 7          # shorter than this - forced to try out - honours nobody
KEPT_SHARE = 0.5      # idled through at least this share of the season
PLACES = ("most", "second most", "third most")
KEY, BEGAN, FORCED = "season_key", "season_began", "season_forced"


def now() -> int:
    return int(time.time())


def progress(player: Player, curve) -> int:
    """Seconds of the curve a character has climbed: what a season measures."""
    return int(seconds_to_reach(player.level, curve) + ttl(player.level, curve)
               - player.next_ttl)


def _aware(value: dt.datetime) -> dt.datetime:
    return value if value.tzinfo else value.replace(tzinfo=dt.timezone.utc)


def _mark(engine, key: str, player: Player) -> SeasonMark | None:
    return engine.session.scalar(select(SeasonMark).where(
        SeasonMark.season == key, SeasonMark.player_id == player.id))


def begin(engine, season: lore.Season) -> None:
    """A season has come: note where everyone stands."""
    started = now()
    key = f"{season.name} {dt.datetime.fromtimestamp(started, dt.timezone.utc).year}"
    engine.set_setting(KEY, key)
    engine.set_setting(BEGAN, str(started))
    engine.set_setting(FORCED, "1" if lore.SEASON_OVERRIDE else "")
    engine.session.execute(delete(SeasonMark).where(SeasonMark.season == key))
    for p in engine.all_players():
        if not p.npc:
            engine.session.add(SeasonMark(season=key, player_id=p.id,
                                          progress=progress(p, engine.curve)))
    engine.session.commit()


def carry(engine, player: Player) -> None:
    """Before a prestige sets a character back to level 0: keep what they
    have made of the season so far."""
    key = engine.get_setting(KEY)
    if not key or player.npc:
        return
    mark = _mark(engine, key, player)
    if mark is None:            # joined during the season, from nothing
        mark = SeasonMark(season=key, player_id=player.id, progress=0)
        engine.session.add(mark)
    mark.progress -= progress(player, engine.curve)


def end(engine, name: str) -> list[Outcome]:
    """A season is over: honour those who kept it, if it ran its course."""
    key, began = engine.get_setting(KEY), int(engine.get_setting(BEGAN) or 0)
    forced = bool(engine.get_setting(FORCED))
    for setting in (KEY, BEGAN, FORCED):
        engine.set_setting(setting, "")
    if not key:
        return []
    marks = {m.player_id: m.progress for m in engine.session.scalars(
        select(SeasonMark).where(SeasonMark.season == key))}
    engine.session.execute(delete(SeasonMark).where(SeasonMark.season == key))
    engine.session.commit()
    season, length = lore.season_named(name), now() - began
    if forced or season is None or length < MIN_DAYS * 86400:
        return []

    scored = []
    for p in engine.all_players():
        if p.npc:
            continue
        gained = progress(p, engine.curve) - marks.get(p.id, 0)
        joined = int(_aware(p.created).timestamp()) if p.created else began
        present = length if p.id in marks else max(0, min(length, now() - joined))
        scored.append((gained, p, present))
    scored.sort(key=lambda s: (-s[0], s[1].name.lower()))
    top = [p for gained, p, _ in scored if gained > 0][:len(PLACES)]
    for place, p in enumerate(top):
        _award(engine, p, f"{key}:{place + 1}", season.badge,
               f"the {PLACES[place]} devoted idler of {key}")
    kept = [p for gained, p, present in scored if present and gained >= KEPT_SHARE * present]
    for p in kept:
        _award(engine, p, f"{key}:kept", season.badge, f"kept {key}")
    engine.session.commit()
    if not top:
        return []
    names = [p.name for p in top]
    listed = names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]
    return [Outcome(f"The realm honours {key}'s most devoted idlers: {listed}. "
                    f"{len(kept)} kept the season, and wear {season.badge} beside "
                    f"their names.", kind="season")]


def _award(engine, player: Player, key: str, badge: str, title: str) -> None:
    if not any(a.key == key for a in player.achievements):
        player.achievements.append(Achievement(key=key, badge=badge, title=title))


def best(achievements) -> list[Achievement]:
    """A character's honours, one per season: a place beats having kept it."""
    chosen: dict[str, Achievement] = {}
    for a in sorted(achievements, key=lambda a: a.earned or dt.datetime.min):
        season, _, what = a.key.rpartition(":")
        if season not in chosen or chosen[season].key.endswith(":kept"):
            chosen[season] = a
    return list(chosen.values())


def honours_text(player: Player) -> str:
    """For WHOAMI: " Honours: kept Hallowtide 2026." or nothing."""
    titles = [a.title for a in best(player.achievements)]
    return f" Honours: {', '.join(titles)}." if titles else ""
