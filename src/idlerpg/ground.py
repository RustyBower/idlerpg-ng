"""Items left lying on the map.

The original throws away the item you replace. Here the few worth keeping are
left where you were standing, and whoever wanders close enough picks one up -
if it beats what they carry in that slot.

Only what is worth a story is left: a unique always, and otherwise gear better
than the dropper's own average. Everything else is discarded exactly as the
original discards it. That is deliberate, and it is the whole design: picking
things up perturbs the battles, and the cost to the realm's pace scales with
how often it happens, so the realm trades a great many forgettable pickups for
a few worth crossing a map for. Nothing is ever created - a dropped item was
already in play - so this is never a second source of loot on top of level-ups.

Ground items rot after LIES_FOR. Without that the map would slowly accumulate
every item ever replaced, and a long walk would turn into a shopping trip.
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy import delete, select

from .events import SLOTS, Outcome
from .models import GroundItem, Player, utcnow

# Measured over 20-day simulations of a twelve-character realm: 6.9 items a
# week are worth leaving, 46% of them are found, 3.1 pickups a week, and the
# map carries nought to three at a time. Leaving everything instead put fifty
# a week on the ground and found 7% of them.
#
# It costs the realm nothing measurable, and favours nobody. Paired over
# eighteen seeds against a pickups-off control, with three characters on each
# of the nine alignments all starting level 30: the realm's pace moves
# +0.1% +/- 0.7%, and the spread between the best and worst alignment moves
# -0.4% +/- 2.4%. Neither clears zero, and with pickups on no alignment
# clears its own error bar.
#
# An earlier four-seed run suggested a 1.4% drag. It did not survive four more
# seeds - one swung +4.3% the other way - and a null experiment, the same
# rules in both arms differing only by one burned random number, moved a
# single seed by 1.3%. That is the noise all of this sits inside, and why the
# numbers above are paired and come from eighteen seeds rather than four.
#
# Two traps, both of which produced confident nonsense before they were
# spotted. GroundItem.dropped defaults to wall-clock time while a simulated
# month passes in two minutes, so nothing ever rots unless the rows are
# stamped from the simulated clock and sweep() is handed that same clock.
# And rng.randrange consumes a variable number of bits, so the instant a
# pickup changes an item sum the two arms' streams part company and a "paired"
# comparison stops being paired - only many seeds recover a signal.
LIES_FOR = dt.timedelta(days=3)   # rot keeps ahead of it; the map stays clean

# Squares, as a Chebyshev distance. An exact-tile rule would almost never
# fire: the realm is 500x500, and characters drift one square a tick, so
# landing on the very tile an item was dropped on is a once-a-year event.
# Generous on purpose: little is dropped now, so what is dropped has to be
# findable, and the pace cost rides on how many pickups happen rather than on
# how far anyone can see.
REACH = 50

# Times the dropper's own average item. Below this it is junk by the standards
# of the character who replaced it, and almost certainly junk to everyone else.
WORTH_LEAVING = 1.25


def worth_leaving(player: Player, value: int, tag: str = "") -> bool:
    """Is this worth leaving for somebody to cross a map for?

    A unique always is. Otherwise it has to beat the dropper's own average:
    the good sword they have outgrown, not the boots they never wore.
    """
    if value <= 0:
        return False
    if tag:
        return True
    mine = [i.value for i in player.items]
    return bool(mine) and value >= WORTH_LEAVING * (sum(mine) / len(mine))


def drop(session, player: Player, slot: str, value: int, tag: str = "") -> None:
    """Leave the item ``player`` just replaced where they are standing, if it
    is worth leaving at all; otherwise it is discarded as it always was."""
    if not worth_leaving(player, value, tag):
        return
    session.add(GroundItem(
        slot=slot, value=value, tag=tag or "",
        x=player.x or 0, y=player.y or 0, left_by=player.name,
    ))


def sweep(session, now: dt.datetime | None = None) -> int:
    """Clear away what has rotted; returns how many went."""
    cutoff = (now or utcnow()) - LIES_FOR
    result = session.execute(delete(GroundItem).where(GroundItem.dropped < cutoff))
    return result.rowcount or 0


def _near(item: GroundItem, player: Player) -> bool:
    return (abs((item.x or 0) - (player.x or 0)) <= REACH
            and abs((item.y or 0) - (player.y or 0)) <= REACH)


def pickups(session, online: list[Player]) -> list[Outcome]:
    """Whoever is standing near something better than what they carry takes it.

    One item each per turn, the best within reach, so nobody hoovers up a
    whole hoard in a single step. An item that beats nothing is left where it
    lies for someone it does suit.
    """
    lying = list(session.scalars(select(GroundItem)))
    if not lying:
        return []
    taken: set[int] = set()
    out: list[Outcome] = []
    for player in online:
        mine = {i.slot: i for i in player.items}
        best = None
        for item in lying:
            # A slot the player has no row for is skipped here rather than
            # after claiming it, or the item would be marked taken by someone
            # who never picks it up and nobody else could have it either.
            held = mine.get(item.slot)
            if held is None or item.id in taken or not _near(item, player):
                continue
            if item.value <= held.value:
                continue
            if best is None or item.value > best.value:
                best = item
        if best is None:
            continue
        taken.add(best.id)
        held = mine[best.slot]
        # Read before the swap: afterwards these hold the new item's own
        # value and tag, and what goes back on the ground would be wrong.
        old, old_tag = held.value, held.tag
        held.value, held.tag = best.value, best.tag or ""
        session.delete(best)
        whose = f", left behind by {best.left_by}" if best.left_by else ""
        out.append(Outcome(
            f"{player.name} comes across a level {best.value} {SLOTS[best.slot]} "
            f"lying at [{best.x},{best.y}]{whose}, and takes it. Their old level "
            f"{old} {SLOTS[best.slot]} is left where it lay.",
            kind="pickup", player_id=player.id,
        ))
        # What they were carrying takes its place on the ground, so the swap
        # neither creates nor destroys anything.
        drop(session, player, best.slot, old, old_tag)
    return out


def lying(session, limit: int = 50) -> list[GroundItem]:
    """What is on the ground, best first - for the website."""
    return list(session.scalars(
        select(GroundItem).order_by(GroundItem.value.desc()).limit(limit)))
