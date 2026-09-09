"""World events, ported from the Perl bot.

The original rolls these once per self_clock tick, so its probabilities are
entangled with how often it wakes up. Here they are expressed as rates - "once
per N days per online player" - and scaled by however long the tick actually
was, which gives the same feel without tying the odds to the tick length.

Numbers and formulas are taken from bot.pl, not invented.
"""

from __future__ import annotations

import random
from dataclasses import dataclass

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
            f"blessed hand of God carried {player.name} {amount}s toward "
            f"level {player.level + 1}."
        )
    else:
        player.next_ttl += amount
        text = (
            f"Thereupon He stretched out His little finger among them and "
            f"consumed {player.name} with fire, slowing the heathen {amount}s "
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
        f"{player.name} suffered a calamity! {amount}s is added to their clock.",
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
        f"{player.name} received a godsend! {amount}s is removed from their clock.",
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
            f"{gain}s is removed from {challenger.name}'s clock.",
            kind="battle",
        ))
        factor = CRITICAL_FACTOR.get(challenger.alignment.value, 35)
        if rng.randrange(factor) < 1:
            hit = int(((5 + rng.randrange(20)) / 100) * opponent.next_ttl)
            opponent.next_ttl += hit
            out.append(Outcome(
                f"{challenger.name} has dealt {opponent.name} a Critical "
                f"Strike! {hit}s is added to {opponent.name}'s clock.",
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
            f"{gain}s is added to {challenger.name}'s clock.",
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
