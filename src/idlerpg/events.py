"""World events, ported from the Perl bot.

The original rolls these once per self_clock tick, so its probabilities are
entangled with how often it wakes up. Here they are expressed as rates - "once
per N days per online player" - and scaled by however long the tick actually
was, which gives the same feel without tying the odds to the tick length.

Numbers and formulas are taken from bot.pl, not invented.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass

from .lore import LORE
from .text import duration

DAY = 86400

# Mean interval per online player, in seconds. From rpcheck().
HOG_INTERVAL = 20 * DAY
TEAM_BATTLE_INTERVAL = 24 * DAY
CALAMITY_INTERVAL = 8 * DAY
GODSEND_INTERVAL = 4 * DAY
# Not per-player: the realm gets one of these on its own schedule.
WAR_INTERVAL = 10 * DAY

# Item slots, with the wording the original announces them by.
SLOTS = {
    "amulet": "amulet", "charm": "charm", "helm": "helm",
    "boots": "pair of boots", "gloves": "pair of gloves", "ring": "ring",
    "leggings": "set of leggings", "shield": "shield", "tunic": "tunic",
    "weapon": "weapon",
}

# Chance of a critical strike, by the winner's alignment: the good are more
# likely to land one, the evil less.
CRITICAL_FACTOR = {"good": 50, "evil": 20, "neutral": 35}


@dataclass
class Outcome:
    """What an event did, so the caller can announce it."""

    message: str
    kind: str = "event"


def roll_item_level(player_level: int, rng: random.Random) -> int:
    """Item level found by a player.

    Walks candidate levels up to 1.5x the player's own, keeping the highest one
    that passes an increasingly unlikely roll, so good items get rarer sharply
    rather than linearly.
    """
    level = 1
    for num in range(1, int(player_level * 1.5) + 1):
        if rng.random() * (1.4 ** (num / 4)) < 1:
            level = num
    return level


def find_item(player, rng: random.Random) -> Outcome | None:
    """Award an item if it beats what the player already has in that slot."""
    slot = rng.choice(list(SLOTS))
    level = roll_item_level(player.level, rng)
    tag = ""

    # From level 25 a player can turn up a unique, far above the normal curve.
    if player.level >= 25 and rng.randrange(40) < 1:
        level = 50 + rng.randrange(25)
        tag = "a"

    current = next((i for i in player.items if i.slot == slot), None)
    if current is None or level <= current.value:
        return None
    old = current.value
    current.value = level
    current.tag = tag
    return Outcome(
        f"{player.name} found a level {level} {SLOTS[slot]}! "
        f"Their old level {old} {SLOTS[slot]} is discarded.",
        kind="item",
    )


def hand_of_god(player, rng: random.Random) -> Outcome:
    """A blessing four times in five, a smiting otherwise."""
    blessed = rng.randrange(5) > 0
    amount = int(((5 + rng.randrange(71)) / 100) * player.next_ttl)
    if blessed:
        player.next_ttl = max(1, player.next_ttl - amount)
        text = (
            f"Verily I say unto thee, the Heavens have burst forth, and the "
            f"blessed hand of God carried {player.name} {duration(amount)} toward"
            f"level {player.level + 1}."
        )
    else:
        player.next_ttl += amount
        text = (
            f"Thereupon He stretched out His little finger among them and "
            f"consumed {player.name} with fire, slowing the heathen {duration(amount)}"
            f"from level {player.level + 1}."
        )
    return Outcome(text, kind="hog")


def calamity(player, rng: random.Random) -> Outcome:
    """Damage an item one time in ten, otherwise cost the player time."""
    if rng.randrange(10) < 1:
        item = rng.choice(player.items) if player.items else None
        if item is not None and item.value > 0:
            before = item.value
            item.value = int(item.value * 0.9)
            return Outcome(
                f"{player.name}'s {SLOTS.get(item.slot, item.slot)} was "
                f"damaged! It drops from level {before} to {item.value}.",
                kind="calamity",
            )
    amount = int(int(5 + rng.randrange(8)) / 100 * player.next_ttl)
    player.next_ttl += amount
    return Outcome(
        f"{player.name} {LORE.calamity(rng)}. That costs them {duration(amount)} "
        f"on the road to level {player.level + 1}.",
        kind="calamity",
    )


def godsend(player, rng: random.Random) -> Outcome:
    """The mirror of a calamity."""
    if rng.randrange(10) < 1:
        item = rng.choice(player.items) if player.items else None
        if item is not None and item.value > 0:
            before = item.value
            item.value = int(item.value * 1.1)
            if item.value > before:
                return Outcome(
                    f"{player.name}'s {SLOTS.get(item.slot, item.slot)} was "
                    f"blessed! It rises from level {before} to {item.value}.",
                    kind="godsend",
                )
    amount = int(int(5 + rng.randrange(8)) / 100 * player.next_ttl)
    player.next_ttl = max(1, player.next_ttl - amount)
    return Outcome(
        f"{player.name} {LORE.godsend(rng)}! That brings them {duration(amount)} "
        f"closer to level {player.level + 1}.",
        kind="godsend",
    )


def item_sum(player) -> int:
    return sum(i.value for i in player.items)


def battle(challenger, opponent, rng: random.Random) -> list[Outcome]:
    """Pit two players against each other, rolling against their item sums."""
    out: list[Outcome] = []
    my_sum = max(1, item_sum(challenger))
    opp_sum = max(1, item_sum(opponent))
    my_roll = rng.randrange(my_sum)
    opp_roll = rng.randrange(opp_sum)

    if my_roll >= opp_roll:
        percent = max(7, opponent.level // 4)
        gain = int((percent / 100) * challenger.next_ttl)
        challenger.next_ttl = max(1, challenger.next_ttl - gain)
        out.append(Outcome(
            f"{challenger.name} [{my_roll}/{my_sum}] has challenged "
            f"{opponent.name} [{opp_roll}/{opp_sum}] in combat and won! "
            f"{duration(gain)} is removed from {challenger.name}'s clock.",
            kind="battle",
        ))
        factor = CRITICAL_FACTOR.get(challenger.alignment.value, 35)
        if rng.randrange(factor) < 1:
            hit = int(((5 + rng.randrange(20)) / 100) * opponent.next_ttl)
            opponent.next_ttl += hit
            out.append(Outcome(
                f"{challenger.name} has dealt {opponent.name} a Critical "
                f"Strike! {duration(hit)} is added to {opponent.name}'s clock.",
                kind="battle",
            ))
        elif rng.randrange(25) < 1 and challenger.level > 19:
            out.extend(_swap_item(challenger, opponent, rng))
    else:
        percent = max(7, opponent.level // 7)
        gain = int((percent / 100) * challenger.next_ttl)
        challenger.next_ttl += gain
        out.append(Outcome(
            f"{challenger.name} [{my_roll}/{my_sum}] has challenged "
            f"{opponent.name} [{opp_roll}/{opp_sum}] in combat and lost! "
            f"{duration(gain)} is added to {challenger.name}'s clock.",
            kind="battle",
        ))
    return out


def _swap_item(winner, loser, rng: random.Random) -> list[Outcome]:
    """The winner takes a better item off the loser, leaving their own."""
    slot = rng.choice(list(SLOTS))
    mine = next((i for i in winner.items if i.slot == slot), None)
    theirs = next((i for i in loser.items if i.slot == slot), None)
    if mine is None or theirs is None or theirs.value <= mine.value:
        return []
    mine.value, theirs.value = theirs.value, mine.value
    mine.tag, theirs.tag = theirs.tag, mine.tag
    return [Outcome(
        f"In the fierce battle, {loser.name} dropped their level "
        f"{mine.value} {SLOTS[slot]}! {winner.name} picks it up, tossing "
        f"their old level {theirs.value} {SLOTS[slot]} to {loser.name}.",
        kind="battle",
    )]


def move_player(player, map_x: int, map_y: int, rng: random.Random) -> None:
    """Drift one step in a random direction, wrapping at the edges."""
    if rng.randrange(2):
        player.x = (player.x + rng.choice((-1, 1))) % map_x
    else:
        player.y = (player.y + rng.choice((-1, 1))) % map_y


def step_toward(player, x: int, y: int, steps: int = 1) -> None:
    """Walk up to ``steps`` squares toward (x, y), diagonals allowed.

    For a journey's party, which the original walks to each waypoint rather
    than leaving to stumble onto it by chance.
    """
    for _ in range(max(0, steps)):
        if (player.x, player.y) == (x, y):
            return
        player.x += (x > player.x) - (x < player.x)
        player.y += (y > player.y) - (y < player.y)


def should_fire(interval_seconds: float, elapsed: float, weight: float,
                rng: random.Random) -> bool:
    """Rate-based roll: on average once per interval, per unit of weight.

    weight is usually the number of online players, matching the original's
    `rand(interval / clock) < online`, but expressed so the odds do not shift
    when the tick length changes.
    """
    if weight <= 0 or elapsed <= 0:
        return False
    return rng.random() < (elapsed * weight) / interval_seconds


GOODNESS_INTERVAL = 12 * DAY
EVILNESS_INTERVAL = 8 * DAY


def team_battle(online: list, rng: random.Random, map_x: int,
                map_y: int) -> list[Outcome]:
    """Six players nearest a random point, split into two teams of three.

    The teams are formed geometrically rather than at random: sort the six by
    angle around the chosen point and cut the ring in a random place, so
    neighbours on the map fight side by side.
    """
    if len(online) < 6:
        return []
    x, y = rng.randrange(map_x), rng.randrange(map_y)

    def distance(p):
        return math.hypot(p.x - x, p.y - y)

    nearest = sorted(online, key=distance)[:6]
    nearest.sort(key=lambda p: math.atan2(p.y - y, p.x - x))
    rot = rng.randrange(6)
    ring = nearest[rot:] + nearest[:rot]
    team_a, team_b = ring[:3], ring[3:]

    sum_a = max(1, sum(item_sum(p) for p in team_a))
    sum_b = max(1, sum(item_sum(p) for p in team_b))
    # The stake is a fifth of whichever winner has least left to do, so a
    # veteran cannot farm newcomers for enormous gains.
    gain = int(min(p.next_ttl for p in team_a) * 0.20)
    roll_a, roll_b = rng.randrange(sum_a), rng.randrange(sum_b)

    names_a = ", ".join(p.name for p in team_a)
    names_b = ", ".join(p.name for p in team_b)
    if roll_a >= roll_b:
        for p in team_a:
            p.next_ttl = max(1, p.next_ttl - gain)
        verdict = f"won! {duration(gain)} is removed from their clocks"
    else:
        for p in team_a:
            p.next_ttl += gain
        verdict = f"lost! {duration(gain)} is added to their clocks"
    return [Outcome(
        f"{names_a} [{roll_a}/{sum_a}] have team battled {names_b} "
        f"[{roll_b}/{sum_b}] at [{x},{y}] and {verdict}.",
        kind="battle",
    )]


QUADRANTS = ("Northeast", "Southeast", "Southwest", "Northwest")


# How far a war moves clocks. The original halves the winners' remaining time
# and doubles the losers'; in a realm of a handful of players one roll of that
# outweighs days of idling, so the stakes here are gentler.
WAR_SHIFT = 0.15


def quadrant(player, map_x: int, map_y: int) -> int | None:
    """The quadrant a player stands in, or None exactly on a meridian."""
    if 2 * player.y + 1 < map_y:
        return 3 if 2 * player.x + 1 < map_x else 0
    if 2 * player.y + 1 > map_y:
        return 2 if 2 * player.x + 1 < map_x else 1
    return None


def war(online: list, rng: random.Random, map_x: int, map_y: int) -> list[Outcome]:
    """The four quadrants of the map fight.

    A quadrant that beats both of its neighbours prevails and its players move
    WAR_SHIFT closer to their next level; one that loses to both is routed and
    set back by as much. Empty quadrants neither win nor lose.
    """
    if len(online) < 4:
        return []
    armies: dict[int, list] = {0: [], 1: [], 2: [], 3: []}
    for p in online:
        q = quadrant(p, map_x, map_y)
        if q is not None:
            armies[q].append(p)
    sums = [sum(item_sum(p) for p in armies[q]) for q in range(4)]

    if not any(sums):
        return []
    rolls = [rng.randrange(s) if s else 0 for s in sums]
    neighbours = [((i + 1) % 4, (i + 3) % 4) for i in range(4)]
    winners = [i for i in range(4) if sums[i]
               and all(rolls[i] >= rolls[n] for n in neighbours[i])]
    losers = [i for i in range(4) if sums[i]
              and all(rolls[i] < rolls[n] for n in neighbours[i])]
    if not winners:
        return []

    for i in winners:
        for p in armies[i]:
            p.next_ttl = max(1, int(p.next_ttl * (1 - WAR_SHIFT)))
    for i in losers:
        for p in armies[i]:
            p.next_ttl = int(p.next_ttl * (1 + WAR_SHIFT))

    percent = round(WAR_SHIFT * 100)
    named = " and ".join(QUADRANTS[i] for i in winners)
    detail = ", ".join(f"{QUADRANTS[i]} [{rolls[i]}/{sums[i]}]" for i in range(4))
    message = (f"The quadrants went to war: {detail}. {named} prevailed, and "
               f"their people are {percent}% closer to their next level.")
    if losers:
        routed = " and ".join(QUADRANTS[i] for i in losers)
        message += f" {routed} fell, and are set back {percent}%."
    return [Outcome(message, kind="war")]


def goodness(online: list, rng: random.Random) -> list[Outcome]:
    """Two good players help each other along."""
    good = [p for p in online if p.alignment.value == "good"]
    if len(good) < 2:
        return []
    a, b = rng.sample(good, 2)
    percent = 5 + rng.randrange(8)
    for p in (a, b):
        p.next_ttl = max(1, int(p.next_ttl * (1 - percent / 100)))
    return [Outcome(
        f"{a.name} and {b.name} have not let the iniquities of evil men "
        f"poison them. Together they have prayed to their god, and are "
        f"rewarded {percent}% of their time toward the next level.",
        kind="goodness",
    )]


def evilness(online: list, rng: random.Random) -> list[Outcome]:
    """Evil pays about half the time; otherwise it costs."""
    evil = [p for p in online if p.alignment.value == "evil"]
    if not evil:
        return []
    me = rng.choice(evil)

    if rng.randrange(2) < 1:
        good = [p for p in online if p.alignment.value == "good"]
        if good:
            target = rng.choice(good)
            slot = rng.choice(list(SLOTS))
            mine = next((i for i in me.items if i.slot == slot), None)
            theirs = next((i for i in target.items if i.slot == slot), None)
            if mine is not None and theirs is not None and theirs.value > mine.value:
                mine.value, theirs.value = theirs.value, mine.value
                mine.tag, theirs.tag = theirs.tag, mine.tag
                return [Outcome(
                    f"{me.name} stole {target.name}'s level {mine.value} "
                    f"{SLOTS[slot]} while they were sleeping!",
                    kind="evilness",
                )]
    percent = 1 + rng.randrange(5)
    added = int(me.next_ttl * (percent / 100))
    me.next_ttl += added
    return [Outcome(
        f"{me.name} is forsaken by their evil god. {duration(added)} is added to"
        f"their clock.",
        kind="evilness",
    )]
