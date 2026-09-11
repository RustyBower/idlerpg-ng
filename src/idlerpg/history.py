"""Recovering the level history the realm already announced.

Level-ups have been logged since the first day, but the event log only began
recording which character each one was about in 0.21.0. The wording has
always named the character and the level - "Rusty the Sysadmin reaches level
12!" - so the old rows can be read back and linked, and a player's chart
shows their whole climb rather than starting the day the column was added.

Once, at startup: it touches only rows with no player, matches names as they
were written, leaves anything it cannot match alone, and marks itself done.
"""

from __future__ import annotations

import logging
import re

from sqlalchemy import select, update

from .models import EventLog, Player

log = logging.getLogger(__name__)

DONE_KEY = "levels_backfilled"
# "Rusty the Sysadmin reaches level 12! Next level in 3d 4h." - and the
# wordings before it, which also opened with the name and said the level.
LEVELUP = re.compile(r"^(\S+)\b.*?\breaches level (\d+)\b")


def backfill(engine) -> int:
    """Link old level-ups to their characters; how many were linked."""
    if engine.get_setting(DONE_KEY):
        return 0
    by_name = {p.name.lower(): p.id for p in engine.session.scalars(select(Player))}
    rows = engine.session.execute(
        select(EventLog.id, EventLog.message)
        .where(EventLog.kind == "levelup", EventLog.player_id.is_(None))
    ).all()
    found = []
    for row_id, message in rows:
        match = LEVELUP.match(message or "")
        if match is None:
            continue
        player_id = by_name.get(match.group(1).lower())
        if player_id is not None:
            found.append({"id": row_id, "player_id": player_id,
                          "level": int(match.group(2))})
    if found:
        engine.session.execute(update(EventLog), found)
    engine.set_setting(DONE_KEY, "1")
    engine.session.commit()
    log.info("level history: linked %d of %d old level-ups", len(found), len(rows))
    return len(found)
