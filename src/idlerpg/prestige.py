"""Prestige: an opt-in fresh start, for points to spend on perks.

From level 60 a character may start over. Past 60 each level costs a quarter
more than the last - 70 is half a year on, 80 years - so the levels stop being
the way forward. Starting over trades the level - never taken, only
offered - for a star beside the name, standings that rank prestige first, and
points to spend on perks that make the next climb a little different.

The reset is to level 0 with fresh items, bar any the Heirloom perk keeps:
otherwise a reborn character with its old gear would flatten every newcomer.
Name, alignment, logins and perks all stay.

Points are few: two for reaching 60 and one more for every five levels past
it, so waiting is a real choice - five levels past 60 take about seven weeks,
as long as climbing back. Most perks cost a point a rank. The deepest,
Endurance, softens the wall; it opens only to a character with ranks in other
perks already, and costs more, so reaching it takes many fresh starts. Perks
are capped, and
their effects live in events.py beside the alignment tuning, where the
simulator can reach them.
"""

from __future__ import annotations

from dataclasses import dataclass

from . import achievements, events, quests, seasonal
from .events import Outcome
from .models import Player

MIN_LEVEL = 60
POINTS_AT_MIN = 2     # for reaching MIN_LEVEL
POINTS_EVERY = 5      # and one more for every this many levels past it


@dataclass(frozen=True)
class Perk:
    most: int
    text: str
    cost: int = 1       # points per rank
    requires: int = 0   # ranks needed in other perks before the first


PERKS = {
    "swiftness": Perk(5, "each level takes 2% less time per rank"),
    "composure": Perk(5, "penalties 2% smaller per rank (with lawful, 15% at most)"),
    "fortune": Perk(5, "godsends 5% stronger per rank"),
    "warding": Perk(5, "calamities 5% weaker per rank"),
    "heirloom": Perk(3, "keep your best item through a prestige, one per rank"),
    "stride": Perk(5, "your quest party walks 20% faster per rank"),
    "champion": Perk(5, "2% more battle strength per rank"),
    "endurance": Perk(5, "the wall past 60 grows 20% less steeply per rank; "
                         "five bring it back to the ordinary curve",
                      cost=2, requires=10),
}


def points_for(level: int) -> int:
    if level < MIN_LEVEL:
        return 0
    return POINTS_AT_MIN + (level - MIN_LEVEL) // POINTS_EVERY


def _preview(player: Player) -> str:
    if player.level < MIN_LEVEL:
        return (f"Prestige opens at level {MIN_LEVEL}; you are level {player.level}.")
    kept = player.perk_rank("heirloom")
    items = f"fresh items, but for your best {kept}" if kept else "fresh items"
    return (f"PRESTIGE confirm starts {player.name} over at level 0 with {items}, "
            f"for {points_for(player.level)} points to spend on perks "
            f"(you have {player.points} unspent). You keep your name, alignment "
            f"and perks, and gain a star. Every {POINTS_EVERY} levels you wait "
            f"past {MIN_LEVEL} earns another point.")


def start_over(engine, player: Player) -> str:
    """Prestige ``player``: the reset, the points, and the announcement."""
    if player.level < MIN_LEVEL:
        return _preview(player)
    # Dropping to level 0 leaves any quest, which fails it as leaving would.
    engine._pending.extend(quests.fail(engine.session, player, engine.curve))
    # What they made of a season so far is theirs to keep through the reset.
    seasonal.carry(engine, player)
    earned = points_for(player.level)
    kept = sorted(player.items, key=lambda i: -i.value)[:player.perk_rank("heirloom")]
    for item in player.items:
        if item not in kept:
            item.value, item.tag = 0, ""
    player.level = 0
    player.next_ttl = int(events.level_cost(player, 0, engine.curve))
    player.prestige = (player.prestige or 0) + 1
    player.points = (player.points or 0) + earned
    engine.session.commit()
    engine.announce([Outcome(
        f"{player.name} has prestiged, and begins again at level 0 with "
        f"★{player.prestige} beside their name.", kind="prestige"),
        *achievements.on_prestige(player)])
    return (f"Reborn at level 0, ★{player.prestige}. You have {player.points} "
            f"points: PERKS shows what they buy.")


def _terms(perk: Perk) -> str:
    terms = []
    if perk.cost > 1:
        terms.append(f"{perk.cost} points a rank")
    if perk.requires:
        terms.append(f"opens after {perk.requires} ranks in other perks")
    return f" ({', '.join(terms)})" if terms else ""


def _elsewhere(player: Player, name: str) -> int:
    return sum(player.perk_rank(other) for other in PERKS if other != name)


def perks_text(player: Player) -> str:
    ranks = " | ".join(
        f"{name} {player.perk_rank(name)}/{perk.most}: {perk.text}{_terms(perk)}"
        for name, perk in PERKS.items())
    return f"{player.points or 0} points to spend (PERK <name>). | {ranks}"


def buy(engine, player: Player, name: str) -> str:
    perk = PERKS.get(name.lower())
    if perk is None:
        return "No such perk. PERKS lists them."
    name = name.lower()
    have = player.perk_rank(name)
    if have >= perk.most:
        return f"{name} is already at its most, {perk.most} ranks."
    elsewhere = _elsewhere(player, name)
    if elsewhere < perk.requires:
        return (f"{name} opens once you have {perk.requires} ranks in other perks; "
                f"you have {elsewhere}.")
    points = player.points or 0
    if points < 1:
        return "You have no points to spend. Prestige earns them, from level 60."
    if points < perk.cost:
        return f"{name} costs {perk.cost} points a rank; you have {points}."
    player.set_perk_rank(name, have + 1)
    player.points = points - perk.cost
    engine.session.commit()
    return f"{name} is now rank {have + 1} of {perk.most}. {player.points} points left."


VERBS = frozenset({"PRESTIGE", "PERKS", "PERK"})


def command(engine, player: Player | None, verb: str, args: list[str]) -> str:
    """PRESTIGE [confirm], PERKS and PERK <name>, for either platform."""
    if player is None:
        return "Log in first."
    verb = verb.upper()
    if verb == "PRESTIGE":
        if args and args[0].lower() == "confirm":
            return start_over(engine, player)
        return _preview(player)
    if verb == "PERKS":
        return perks_text(player)
    if not args:
        return "PERK <name> - PERKS lists them."
    return buy(engine, player, args[0])
