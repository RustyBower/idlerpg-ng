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

from .lore import LORE, heartland, near
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
# Hallowtide's trick or treat, per online player, and at most this share of
# a level's cost either way: sized by the level, not the clock, so it stays
# small at the level-60 wall.
TRICK_INTERVAL = 3 * DAY
TRICK_SHARE = 0.05

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

# The original gives the good a tenth more item strength in a battle and the
# evil a tenth less. Off here (all 1.0) until the harness says what it costs:
# it is the one alignment rule that changes who wins.
MORAL_BATTLE = {"good": 1.0, "neutral": 1.0, "evil": 1.0}

# The original challenges someone on every level-up. Ours starts at the level
# where challenges stop being declined; 999 turns it off.
LEVELUP_BATTLE_FROM = 25

# A mount carries a quest party like this many ranks of Stride.
MOUNT_STRIDE = 2
# Each unique carries its own name in Item.tag, which is what TRAITS reads to
# decide what holding it does. The original marked every unique "a"; that
# cannot tell the eight apart, so it is not used here.
MOUNT_TAG = "mount"
# How uniques turn up: from this level, on about this many item finds.
UNIQUE_FROM = 25
UNIQUE_ODDS = 40


@dataclass(frozen=True)
class Unique:
    """One of the realm's eight named items: better than the curve allows,
    each in its own slot and behind its own level. The Courser is the mount,
    which carries a quest party; the rest are strength and a story."""

    name: str
    slot: str
    from_level: int
    value: int          # the least it is worth
    spread: int         # and how much above that it can roll
    tag: str            # its own, and the key into TRAITS - no default, so a
                        # ninth unique cannot quietly arrive granting nothing


UNIQUES = [
    Unique("the Lantern of Small Mercies", "charm", 25, 75, 20, tag="lantern"),
    Unique("the Kumquat of Ages", "amulet", 30, 80, 20, tag="kumquat"),
    Unique("the Seven-League Courser", "boots", 30, 80, 20, tag=MOUNT_TAG),
    Unique("the Last Honest Ledger", "tunic", 35, 85, 20, tag="ledger"),
    Unique("the Bell That Must Not Ring", "helm", 40, 90, 20, tag="bell"),
    Unique("the Moon-Rake", "weapon", 45, 95, 25, tag="moonrake"),
    Unique("the Crown of Minor Kings", "leggings", 50, 100, 25, tag="crown"),
    Unique("the Door That Was a Mimic", "shield", 55, 105, 25, tag="door"),
]

# What carrying one does, beyond being worth a great deal. Written as ranks of
# the prestige perks so that an item and a perk compose through one code path -
# rank() adds them together - rather than each growing its own arithmetic.
#
# A rank is what a prestige point buys, so one rank is real without being
# lavish, and a unique turns up about once in a climb from 1 to 80. These are
# too small and too rare for the simulator to resolve: it cannot separate 2%
# from its own noise without many more seeds than the effect deserves, so they
# are set by judgement and said so, not blessed by a measurement that could
# not have failed. The one worth watching is champion, which feeds FIGHT and
# the level-up battle, where the Crown and the Door together with five perk
# ranks reach +16%.
TRAITS: dict[str, dict[str, int]] = {
    "lantern": {"warding": 1},      # small mercies: calamities land softer
    "kumquat": {"swiftness": 1},    # of ages: the years weigh less
    MOUNT_TAG: {"stride": MOUNT_STRIDE},   # carries a whole quest party
    "ledger": {"composure": 1},     # honest books, fewer fines
    "bell": {"composure": 1},       # the one you must not ring
    "moonrake": {"fortune": 1},     # rakes in what the moon spills
    "crown": {"champion": 2},       # minor kings still command
    "door": {"champion": 1},        # it bites back
}

# For telling a player what their item does, on the site and when it is found.
TRAIT_TEXT = {
    "warding": "calamities {}% weaker",
    "fortune": "godsends {}% stronger",
    "swiftness": "levels {}% faster",
    "composure": "penalties {}% smaller",
    "champion": "{}% more battle strength",
    "stride": "carries a quest party {}% faster",
}
TRAIT_STEP = {"warding": 5, "fortune": 5, "swiftness": 2, "composure": 2,
              "champion": 2, "stride": 20}


def trait_text(tag: str) -> str:
    """What the item tagged ``tag`` grants, in words, or nothing."""
    return ", ".join(
        TRAIT_TEXT[perk].format(TRAIT_STEP[perk] * ranks)
        for perk, ranks in TRAITS.get(tag, {}).items()
        if perk in TRAIT_TEXT
    )

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
    """A player's ranks in a perk: those they bought, plus those the uniques
    they carry grant.

    Every modifier in the game reads its strength from here, so widening this
    one function is what lets a carried item ease the wall, soften a calamity
    or quicken a journey without any of those places learning about items.
    """
    reader = getattr(player, "perk_rank", None)
    bought = reader(perk) if reader else 0
    # Tags are read defensively: the fairness harness and the simulator hand
    # this function stand-in players whose items carry a value and nothing
    # else, and neither needs to grow a field to ask what a perk is worth.
    return bought + sum(
        TRAITS.get(getattr(item, "tag", "") or "", {}).get(perk, 0)
        for item in getattr(player, "items", None) or ()
    )


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
    player_id: int | None = None    # who it was about, for the history
    level: int | None = None        # the level reached, for a level-up
    # (slot, value, tag) of the item this one replaced, for the ground. The
    # engine owns the session, so find_item reports the drop rather than
    # writing it.
    dropped: tuple[str, int, str] | None = None


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
    named = ""

    # From level 25 a find can turn up one of the realm's eight named
    # uniques, far above the normal curve - whichever the character is deep
    # enough for, and in that item's own slot. The roll comes first and the
    # slot follows it: needing a random slot to match as well would put each
    # unique a few hundred level-ups away, which is nobody's lifetime. A
    # nameless unique besides would only out-number the eight and swamp
    # their worth, so there is none.
    if player.level >= UNIQUE_FROM and rng.randrange(UNIQUE_ODDS) < 1:
        choices = [u for u in UNIQUES if player.level >= u.from_level]
        if choices:
            unique = rng.choice(choices)
            slot = unique.slot
            level = unique.value + rng.randrange(unique.spread)
            tag, named = unique.tag, unique.name

    current = next((i for i in player.items if i.slot == slot), None)
    if current is None or level <= current.value:
        return None
    old, old_tag = current.value, current.tag
    current.value = level
    current.tag = tag
    left = (slot, old, old_tag)
    if named:
        grants = trait_text(tag)
        does = f" ({grants})" if grants else ""
        return Outcome(
            f"{player.name} found {named}, a level {level} {SLOTS[slot]}"
            f"{does}! Their old level {old} {SLOTS[slot]} is left where "
            f"they stood.",
            kind="item", player_id=player.id, dropped=left,
        )
    return Outcome(
        f"{player.name} found a level {level} {SLOTS[slot]}! "
        f"Their old level {old} {SLOTS[slot]} is left where they stood.",
        kind="item", player_id=player.id, dropped=left,
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
    return Outcome(text, kind="hog", player_id=player.id)


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
                kind="calamity", player_id=player.id,
            )
    amount = int(int(5 + rng.randrange(8)) / 100 * player.next_ttl
                 * LUCK[ethos(player)]
                 * (1 - WARDING_PER_RANK * rank(player, "warding")))
    player.next_ttl += amount
    return Outcome(
        f"{player.name} {LORE.calamity(rng)}. That costs them {duration(amount)} "
        f"on the road to level {player.level + 1}.",
        kind="calamity", player_id=player.id,
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
                    kind="godsend", player_id=player.id,
                )
    amount = int(int(5 + rng.randrange(8)) / 100 * player.next_ttl
                 * LUCK[ethos(player)]
                 * (1 + FORTUNE_PER_RANK * rank(player, "fortune")))
    player.next_ttl = max(1, player.next_ttl - amount)
    return Outcome(
        f"{player.name} {LORE.godsend(rng)}! That brings them {duration(amount)} "
        f"closer to level {player.level + 1}.",
        kind="godsend", player_id=player.id,
    )


def trick_or_treat(player, rng: random.Random, curve) -> Outcome:
    """Hallowtide: a knock on a door, and even odds of a treat or a trick."""
    amount = 1 + int(level_cost(player, player.level, curve) * TRICK_SHARE * rng.random())
    where = near(rng)
    if rng.randrange(2):
        player.next_ttl = max(1, player.next_ttl - amount)
        return Outcome(f"{player.name} knocked on a door {where} and was given a treat: "
                       f"{duration(amount)} off their clock.", kind="treat",
                       player_id=player.id)
    player.next_ttl += amount
    return Outcome(f"{player.name} knocked on a door {where} and was played a trick: "
                   f"{duration(amount)} on their clock.", kind="trick",
                   player_id=player.id)


def item_sum(player) -> int:
    return sum(i.value for i in player.items)


def battle_strength(player) -> int:
    """What a player brings to a battle: their items, the Champion perk, and
    the good-and-evil modifier if it is switched on."""
    moral = MORAL_BATTLE.get(player.alignment.value, 1.0)
    return max(1, int(item_sum(player) * champion(player) * moral))


def battle(challenger, opponent, rng: random.Random) -> list[Outcome]:
    """Pit two players against each other, rolling against their item sums."""
    out: list[Outcome] = []
    my_sum = battle_strength(challenger)
    opp_sum = battle_strength(opponent)
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


# Out in the wilds - where an admin's MOVE or an old position can leave
# someone - a drifting step heads back toward the heartland this often.
HOMEWARD = 0.75


def spawn_point(map_x: int, map_y: int, rng: random.Random) -> tuple[int, int]:
    """Somewhere in the heartland, for a new character."""
    return rng.randint(*heartland(map_x)), rng.randint(*heartland(map_y))


def move_player(player, map_x: int, map_y: int, rng: random.Random) -> None:
    """Drift one step: at random in the heartland, turning back at its edge,
    and mostly homeward from the wilds. The map used to wrap, which strung
    characters along its rim and flung them from one side to the other."""
    if rng.randrange(2):
        player.x = _drift(player.x, map_x, rng)
    else:
        player.y = _drift(player.y, map_y, rng)


def _drift(at: int, size: int, rng: random.Random) -> int:
    low, high = heartland(size)
    if at < low:
        step = 1 if rng.random() < HOMEWARD else -1
    elif at > high:
        step = -1 if rng.random() < HOMEWARD else 1
    else:
        step = rng.choice((-1, 1))
        if not low <= at + step <= high:
            step = -step
    return min(size - 1, max(0, at + step))


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
    return Outcome(f"Chaos stirs. {outcome.message}", kind="chaos",
                   player_id=player.id)


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
    return [Outcome(text, kind="balance", player_id=player.id)]
