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
from .rules import ttl
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

# One win in this many lands a critical strike, which adds time to the loser's
# clock. As in the original, the evil land them most often (1 in 20) and the
# good least (1 in 50). This comment once said the reverse, and the help text
# followed it; the numbers were always the original's.
CRITICAL_FACTOR = {"good": 50, "evil": 20, "neutral": 35}

# Alignment tuning, kept together so it can be adjusted as the realm is
# watched. Good and evil decide critical strikes and the goodness and evilness
# events; the law-chaos axis decides how hard luck lands.
LAWFUL_PENALTY = 0.9                                    # 10% smaller penalties
LUCK = {"lawful": 0.5, "neutral": 1.0, "chaotic": 1.5}  # calamities, godsends
QUEST_WEIGHT = {"lawful": 1.25, "neutral": 1.0, "chaotic": 1.0}
CHAOS_INTERVAL = 6 * DAY      # an odd event, per chaotic player online
BALANCE_INTERVAL = 20 * DAY   # a nudge to the middle, per true-neutral player
BALANCE_SHIFT = 0.05

# Prestige perks, per rank bought; the most ranks of each live in prestige.py.
SWIFTNESS_PER_RANK = 0.02   # each level takes this much less time
COMPOSURE_PER_RANK = 0.02   # penalties this much smaller
PENALTY_FLOOR = 0.85        # lawful and composure together cut at most 15%
FORTUNE_PER_RANK = 0.05     # godsends this much stronger
WARDING_PER_RANK = 0.05     # calamities this much weaker
STRIDE_PER_RANK = 0.20      # journeys walked this much faster
CHAMPION_PER_RANK = 0.02    # battle strength this much greater
ENDURANCE_PER_RANK = 0.20   # the wall past the cap, eased this much toward the curve


def rank(player, perk: str) -> int:
    """A player's ranks in a prestige perk, or none for anything without."""
    reader = getattr(player, "perk_rank", None)
    return reader(perk) if reader else 0


def swiftness(player) -> float:
    """What a level costs this player, as a share of the curve's time."""
    return 1 - SWIFTNESS_PER_RANK * rank(player, "swiftness")


def champion(player) -> float:
    return 1 + CHAMPION_PER_RANK * rank(player, "champion")


def level_cost(player, level: int, curve) -> float:
    """What reaching the level after ``level`` costs this player, in seconds.

    Past the cap each level costs post_cap_step times the last: the wall.
    Endurance eases that growth back toward the ordinary curve's step, all
    the way at five ranks, and Swiftness trims the whole. Under the
    original's linear cap there is no wall, and Endurance has nothing to do.
    """
    if level <= curve.cap_level or curve.post_cap_step is None:
        cost = ttl(level, curve)
    else:
        ease = min(1.0, ENDURANCE_PER_RANK * rank(player, "endurance"))
        growth = curve.post_cap_step - (curve.post_cap_step - curve.step) * ease
        cost = (curve.base_seconds * curve.step ** curve.cap_level
                * growth ** (level - curve.cap_level))
    return cost * swiftness(player)


def ethos(player) -> str:
    """A player's place on the law-chaos axis, as a word."""
    value = getattr(player, "ethos", None)
    return value.value if value is not None else "neutral"


def scale_penalty(player, seconds: int) -> int:
    """A penalty after lawful's cut and the composure perk, which together
    never take off more than PENALTY_FLOOR allows."""
    factor = (LAWFUL_PENALTY if ethos(player) == "lawful" else 1.0) \
        * (1 - COMPOSURE_PER_RANK * rank(player, "composure"))
    if factor == 1.0:
        return seconds
    return int(seconds * max(PENALTY_FLOOR, factor))


def will_fight(challenger, rng: random.Random) -> bool:
    """Below level 25 most challenges are declined, as in the original -
    unless the challenger is chaotic, and will fight anyone."""
    return (challenger.level >= 25 or ethos(challenger) == "chaotic"
            or rng.randrange(4) < 1)


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
            f"The clouds part and a great hand reaches down, carrying "
            f"{player.name} {duration(amount)} toward level {player.level + 1}."
        )
    else:
        player.next_ttl += amount
        text = (
            f"A single godly finger descends and flicks {player.name} into a "
            f"hedge, {duration(amount)} further from level {player.level + 1}."
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
    amount = int(int(5 + rng.randrange(8)) / 100 * player.next_ttl
                 * LUCK[ethos(player)]
                 * (1 - WARDING_PER_RANK * rank(player, "warding")))
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
    amount = int(int(5 + rng.randrange(8)) / 100 * player.next_ttl
                 * LUCK[ethos(player)]
                 * (1 + FORTUNE_PER_RANK * rank(player, "fortune")))
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
    my_sum = max(1, int(item_sum(challenger) * champion(challenger)))
    opp_sum = max(1, int(item_sum(opponent) * champion(opponent)))
    my_roll = rng.randrange(my_sum)
    opp_roll = rng.randrange(opp_sum)

    if my_roll >= opp_roll:
        percent = max(7, opponent.level // 4)
        gain = int((percent / 100) * challenger.next_ttl)
        challenger.next_ttl = max(1, challenger.next_ttl - gain)
        out.append(Outcome(
            f"{challenger.name} [{my_roll}/{my_sum}] fought "
            f"{opponent.name} [{opp_roll}/{opp_sum}] and won! "
            f"{duration(gain)} is taken off {challenger.name}'s clock.",
            kind="battle",
        ))
        factor = CRITICAL_FACTOR.get(challenger.alignment.value, 35)
        if rng.randrange(factor) < 1:
            hit = int(((5 + rng.randrange(20)) / 100) * opponent.next_ttl)
            opponent.next_ttl += hit
            out.append(Outcome(
                f"{challenger.name} landed a crushing blow on {opponent.name}! "
                f"{duration(hit)} is added to {opponent.name}'s clock.",
                kind="battle",
            ))
        elif rng.randrange(25) < 1 and challenger.level > 19:
            out.extend(_swap_item(challenger, opponent, rng))
    else:
        percent = max(7, opponent.level // 7)
        gain = int((percent / 100) * challenger.next_ttl)
        challenger.next_ttl += gain
        out.append(Outcome(
            f"{challenger.name} [{my_roll}/{my_sum}] fought "
            f"{opponent.name} [{opp_roll}/{opp_sum}] and lost! "
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
        f"In the scuffle {loser.name} lost hold of their level {mine.value} "
        f"{SLOTS[slot]}. {winner.name} pockets it and leaves their own level "
        f"{theirs.value} {SLOTS[slot]} behind.",
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
        verdict = f"won! {duration(gain)} is taken off their clocks"
    else:
        for p in team_a:
            p.next_ttl += gain
        verdict = f"lost! {duration(gain)} is added to their clocks"
    return [Outcome(
        f"{names_a} [{roll_a}/{sum_a}] met {names_b} [{roll_b}/{sum_b}] "
        f"in open battle at [{x},{y}] and {verdict}.",
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
        f"{a.name} and {b.name} prayed together in quiet company, and their "
        f"god answered: both are {percent}% closer to their next level.",
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
                    f"{me.name} crept into {target.name}'s camp by night and "
                    f"made off with their level {mine.value} {SLOTS[slot]}.",
                    kind="evilness",
                )]
    percent = 1 + rng.randrange(5)
    added = int(me.next_ttl * (percent / 100))
    me.next_ttl += added
    return [Outcome(
        f"{me.name}'s dark patron is displeased, and takes {duration(added)} "
        f"of their time as tribute.",
        kind="evilness",
    )]


def chaos(player, rng: random.Random) -> Outcome:
    """Something odd happens to a chaotic player: luck, either way."""
    outcome = godsend(player, rng) if rng.randrange(2) else calamity(player, rng)
    return Outcome(f"Chaos stirs. {outcome.message}", kind="chaos")


def balance(player, online: list, rng: random.Random) -> list[Outcome]:
    """The realm tugs a truly neutral player toward its middle level: time
    off if they are behind it, time on if they have pulled ahead."""
    levels = sorted(p.level for p in online)
    middle = levels[len(levels) // 2]
    shift = int(player.next_ttl * BALANCE_SHIFT)
    if player.level < middle:
        player.next_ttl = max(1, player.next_ttl - shift)
        text = (f"The scales of the realm tip toward {player.name}, who keeps "
                f"to the middle way: {duration(shift)} closer to level "
                f"{player.level + 1}.")
    elif player.level > middle:
        player.next_ttl += shift
        text = (f"The scales of the realm tip against {player.name}, who has "
                f"pulled ahead of it: {duration(shift)} further from level "
                f"{player.level + 1}.")
    else:
        return []
    return [Outcome(text, kind="balance")]
