"""FIGHT: once a day, a duel for a share of the loser's clock.

Built to the rules the fairness harness held up (fairness.py, the rule set
"transfer-even"), after trying stakes, transfers, bonuses and alignment
twists against bullies, underdogs and prestiged veterans:

- The winner takes 5% of the loser's remaining time - but never more than
  5% of what the winner's own level costs, and exactly what the loser gives
  up. Sized by the loser, beating a newcomer's short clock wins next to
  nothing, so bullying does not pay. Capped by the winner, nobody at the
  level-60 wall wins days from one fight. Even, fights move time between
  players and make none: an underdog bonus did, and at the wall it became
  a way over it.
- Once a day each; both fighters online; from level 10; nobody more than 5
  levels below you. Anyone challenged is shielded for a day, which is what
  stops one player being piled on - up to 44 challenges a week without it.

Strength is the battle's: item sum, with the Champion perk. There are no
alignment twists; every one tried gave some alignment a real edge.
"""

from __future__ import annotations

import time

from . import events, quests
from .events import Outcome
from .models import Player
from .text import duration

STAKE = 0.05
MIN_LEVEL = 10
MAX_BELOW = 5
COOLDOWN = 24 * 3600
SHIELD = 24 * 3600
REACH_SHOWN = 8
# Characters who land on one tile fight too, as in the original - on these
# same terms, but chosen by the map, not by anyone. Walkers keep meeting the
# same neighbours, so a pair fights at most once in this long.
MEETING_COOLDOWN = 24 * 3600

VERBS = frozenset({"FIGHT"})


def now() -> int:
    return int(time.time())


def strength(player: Player) -> int:
    return max(1, int(events.item_sum(player) * events.champion(player)))


def why_not(engine, me: Player, them: Player | None, at: int) -> str | None:
    """Why ``me`` cannot fight ``them`` now, or None if they can."""
    if them is None:
        return "No such character."
    if them.id == me.id:
        return "You cannot fight yourself."
    if engine.paused:
        return "The realm is paused."
    if me.level < MIN_LEVEL:
        return f"Fights open at level {MIN_LEVEL}; you are level {me.level}."
    if (me.fight_ready_at or 0) > at:
        return f"You have fought today. Your next fight is in {duration(me.fight_ready_at - at)}."
    if not me.is_idling:
        return "You can only fight while you are idling in the game."
    if them.level < MIN_LEVEL:
        return f"{them.name} is under level {MIN_LEVEL}, too new to fight."
    if them.level < me.level - MAX_BELOW:
        return f"{them.name} is more than {MAX_BELOW} levels below you."
    if not them.is_idling:
        return f"{them.name} is not in the game right now."
    if (them.shield_until or 0) > at:
        return (f"{them.name} was challenged recently and is safe for another "
                f"{duration(them.shield_until - at)}.")
    return None


def in_reach(engine, me: Player, at: int) -> list[Player]:
    return sorted((p for p in engine.all_players() if why_not(engine, me, p, at) is None),
                  key=lambda p: (-p.level, p.name.lower()))


def status(engine, me: Player, at: int) -> str:
    if me.level < MIN_LEVEL:
        return f"Fights open at level {MIN_LEVEL}; you are level {me.level}."
    if (me.fight_ready_at or 0) > at:
        return f"You have fought today. Your next fight is in {duration(me.fight_ready_at - at)}."
    reach = in_reach(engine, me, at)
    if not reach:
        return "You may fight today, but nobody is in reach right now."
    names = ", ".join(f"{p.name} ({p.level})" for p in reach[:REACH_SHOWN])
    more = f" and {len(reach) - REACH_SHOWN} more" if len(reach) > REACH_SHOWN else ""
    return (f"You may fight today: FIGHT <name>. In reach: {names}{more}. The "
            f"winner takes 5% of the loser's time, at most 5% of their own level.")


def fight(engine, me: Player, them: Player, at: int) -> str:
    """The duel itself: roll, move the time, start the cooldown and shield."""
    mine, theirs = strength(me), strength(them)
    my_roll, their_roll = engine.rng.randrange(mine), engine.rng.randrange(theirs)
    won = my_roll >= their_roll
    winner, loser = (me, them) if won else (them, me)
    amount = _take(engine, winner, loser)
    me.fight_ready_at = at + COOLDOWN
    them.shield_until = at + SHIELD
    engine.announce([Outcome(
        f"{me.name} [{my_roll}/{mine}] challenged {them.name} [{their_roll}/{theirs}] "
        f"and {'won' if won else 'lost'}! {winner.name} takes {duration(amount)} "
        f"from {loser.name}'s clock.",
        kind="fight")])
    if won:
        return f"You won: {duration(amount)} taken from {them.name}'s clock and off yours."
    return f"You lost: {them.name} took {duration(amount)} from your clock."


def _take(engine, winner: Player, loser: Player) -> int:
    """Move 5% of the loser's clock to the winner, at most 5% of the winner's
    own level cost; returns how much."""
    cap = events.level_cost(winner, winner.level, engine.curve) * STAKE
    amount = max(0, int(min(loser.next_ttl * STAKE, cap)))
    winner.next_ttl -= amount       # past zero, the next tick levels them up
    loser.next_ttl += amount
    return amount


def meetings(engine, online: list[Player], at: int) -> list[Outcome]:
    """Duels between those who share a tile: from level 10, a pair at most
    once a day, and never two questers on the same quest - a journey's
    party walks as one."""
    tiles: dict[tuple[int, int], list[Player]] = {}
    for p in online:
        if p.level >= MIN_LEVEL:
            tiles.setdefault((p.x, p.y), []).append(p)
    crowds = [group for group in tiles.values() if len(group) > 1]
    if not crowds:
        return []
    quest = quests.active_quest(engine.session)
    questers = {q.player_id for q in quest.participants} if quest else set()
    out = []
    for group in crowds:
        for i, a in enumerate(group):
            for b in group[i + 1:]:
                pair = (min(a.id, b.id), max(a.id, b.id))
                if (a.id in questers and b.id in questers) or engine.met.get(pair, 0) > at:
                    continue
                engine.met[pair] = at + MEETING_COOLDOWN
                out.append(_clash(engine, a, b))
    return out


def _clash(engine, a: Player, b: Player) -> Outcome:
    sa, sb = strength(a), strength(b)
    ra, rb = engine.rng.randrange(sa), engine.rng.randrange(sb)
    winner, loser = (a, b) if ra >= rb else (b, a)
    amount = _take(engine, winner, loser)
    return Outcome(f"{a.name} [{ra}/{sa}] and {b.name} [{rb}/{sb}] crossed paths and "
                   f"fought! {winner.name} takes {duration(amount)} from "
                   f"{loser.name}'s clock.", kind="fight")


def command(engine, player: Player | None, verb: str, args: list[str]) -> str:
    """FIGHT alone shows who is in reach; FIGHT <name> challenges them."""
    if player is None:
        return "Log in first."
    at = now()
    if not args:
        return status(engine, player, at)
    them = engine.find_player(args[0])
    reason = why_not(engine, player, them, at)
    if reason is not None:
        return reason
    return fight(engine, player, them, at)
