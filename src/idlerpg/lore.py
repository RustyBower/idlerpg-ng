"""The realm's flavour: calamities, godsends and quests.

A fixed list repeats within days in a channel that idles for months, so most
of what happens is composed: a gazetteer of named places with fixed spots on
the map, a cast of creatures and helpers, and small grammars that combine
them - tens of thousands of distinct events - mixed with hand-written
one-offs so it never reads as mechanical.

Written for this realm. The classic IdleRPG events.txt is not bundled: the
original's licence forbids redistributing it without its authors'
permission. A server with its own copy can point EVENTS_FILE at it, and its
lines join the hand-written ones (C calamities, G godsends, Q1 vigils, and
Q2 journeys as "Q2 x1 y1 x2 y2 text").

Calamities and godsends complete "<name> <line>". Quests complete "chosen by
the gods to <line>". Places sit on a 500x500 map, scaled to the realm's
actual size when it differs.
"""

from __future__ import annotations

import logging
import os
import random
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

# The size places and journeys are written for.
LORE_MAP = 500

# Share of events drawn from the hand-written lines rather than composed.
HANDWRITTEN_SHARE = 0.3


@dataclass(frozen=True)
class Place:
    name: str
    at: tuple[int, int]


# Spread across the four quadrants, so journeys cross the realm and wars
# have somewhere to be fought over.
PLACES = [
    # north-west
    Place("the Salt Flats of Mumble", (60, 40)),
    Place("Kettleridge", (180, 30)),
    Place("the Silent Tower", (140, 95)),
    Place("the Hermitage of Gallowmere", (35, 150)),
    Place("the Library of Whispers", (210, 170)),
    Place("the Stone That Listens", (110, 200)),
    Place("the Lighthouse of Nod", (15, 230)),
    Place("the Lantern Market", (225, 225)),
    # north-east
    Place("the Fountain of Unspoken Things", (300, 60)),
    Place("the Monastery of Saint Lurk", (360, 30)),
    Place("the Orchard of Echoes", (440, 40)),
    Place("the Cairn of Regrets", (390, 120)),
    Place("the Edge of the Map", (495, 180)),
    Place("the Weeping Bridge", (310, 212)),
    Place("the Glass Mines", (470, 230)),
    Place("the Idle Hearth", (260, 240)),
    # south-east
    Place("the Tollgate of Small Coins", (360, 280)),
    Place("the Inn of Lost Causes", (270, 300)),
    Place("the Laundry of the Gods", (470, 300)),
    Place("the Chapel of Late Arrivals", (410, 360)),
    Place("the Marsh of Forgotten Names", (300, 390)),
    Place("Harrowmere", (330, 440)),
    Place("the Sunken Archive", (440, 470)),
    # south-west
    Place("the Crooked Mill", (220, 290)),
    Place("the Bog of Mild Regret", (40, 330)),
    Place("the Greyfold Hills", (160, 330)),
    Place("the Ashen Gate", (88, 402)),
    Place("the Drowned Steps", (131, 455)),
    Place("the Tomb of the First Idler", (200, 470)),
    Place("the Fen of Slow Bells", (60, 480)),
]

NEAR = ["near", "outside", "on the road to", "somewhere past", "in the shadow of",
        "a day's walk from", "just short of"]

CREATURES = [
    "an indignant swan", "a goose of considerable standing",
    "three raccoons in a long coat", "a lich on holiday", "a bored hedge-witch",
    "a very polite bandit", "a troll with opinions", "a flock of unimpressed crows",
    "a dragon with a head cold", "a mimic pretending to be a chest",
    "a runaway cheese wheel", "a bard who knows only one song",
    "a basilisk with poor eyesight", "a tax collector", "a cursed accordion",
    "bees with a grievance", "an owl that gives wrong directions",
    "a manticore in a bad mood", "a gnome with a rake", "a ghost who wants to talk",
    "the Guild of Silence", "a knight errant, mostly errant",
    "a vampire who is not a morning person", "a sheep of unusual cunning",
]

MISHAPS = [
    "chased", "ambushed", "out-argued", "robbed", "sneezed on", "cursed", "sat on",
    "hexed", "fined", "misled", "lectured", "followed for miles", "bitten",
    "challenged to riddles and beaten", "mistaken for a scarecrow",
    "talked at for an afternoon", "sold a map of somewhere else",
]

BLUNDERS = [
    "got lost in the fog", "took a wrong turn", "fell into a ditch", "lost a boot",
    "missed the last ferry", "slept through a whole day", "walked in a circle",
    "stepped in something regrettable", "argued with a signpost and lost",
    "forgot which way was north", "dropped their pack down a well",
]

HELPERS = [
    "a friendly giant eagle", "a kind ferryman", "a wise and accurate owl",
    "a passing saint", "a merchant in a hurry", "a helpful ghost",
    "a festival procession", "a talking fox", "a monk with patience to spare",
    "an off-duty dragon", "a cart-tortoise that turned out to be fast",
    "a pleased Guild of Silence", "a retired hero", "a cartographer who owes them",
]

HELPS = [
    "carried a fair way", "given a lift", "shown a shortcut", "blessed",
    "given good directions", "lent a fast horse", "taught a quicker road",
    "waved through every gate", "handed a map with the good parts marked",
]

TREASURES = [
    "a pair of seven-league boots", "a lantern that lights the road ahead",
    "a coin that always lands the right way up", "a secret passage",
    "a spring that tastes of tomorrow", "a horseshoe, with the horse still attached",
    "an hourglass running backwards", "a cloak of fair winds",
    "a compass that points where they meant to go", "a scroll of mild haste",
    "a road that was not there yesterday",
]

CALAMITIES = [
    "tripped over a sleeping goat",
    "got their cloak caught in a windmill",
    "sat on a porcupine",
    "fell asleep in a haystack and woke three villages away",
    "was chased up a tree by an indignant swan",
    "lost a staring contest with a basilisk, briefly",
    "drank from the wrong end of a well",
    "was fined by the Guild of Silence for humming",
    "lent their boots to a troll and has not seen them since",
    "tried to open a door that turned out to be a mimic",
    "was caught out in the open by a netsplit",
    "tried to pet a manticore",
    "ate the mysterious soup at the Inn of Lost Causes",
    "stepped on a rake left out by a careless gnome",
    "was pickpocketed by a very polite raccoon",
]

GODSENDS = [
    "learned to sleep while walking",
    "was mistaken for royalty and waved through every gate",
    "fell in with a festival going their way",
    "rode a runaway cheese wheel downhill in the right direction",
    "sat out a netsplit with great dignity",
    "found a secret passage behind a suspiciously loose brick",
    "was granted a small, specific wish by a lamp",
    "caught a favourable wind",
    "drank from a spring that tasted of tomorrow",
]

VIGIL_ACTS = [
    "sit silent vigil", "keep watch", "hold their tongues", "stand honour guard",
    "keep the lamps lit", "wait without a word", "guard a sleeping wyrm",
]
VIGIL_UNTIL = [
    "until the moon sets", "for a full watch", "until the bells fall silent",
    "through the long dark", "until the tide turns", "until the candle burns out",
    "until the gods are satisfied", "for a day and a night",
]
VIGILS = [
    "sit vigil at the Fountain of Unspoken Things until the moon sets",
    "wait out the Long Hush at the Monastery of Saint Lurk",
    "stand silent honour guard at the Tomb of the First Idler",
]

CARGO = [
    "carry the Lantern of Small Mercies", "escort the Last Cartographer",
    "deliver an unopened letter", "bear the Kumquat of Ages",
    "return a borrowed moon-rake", "take a very old cheese",
    "bring the realm's lost socks", "escort a nervous prince",
    "carry a sealed jar of thunder", "deliver the Crown of Minor Kings",
    "bring a message nobody wants to read", "carry the last honest ledger",
    "escort a retired hero", "carry a sack of complaints",
    "deliver a map of somewhere else", "bear a bell that must not ring",
]
ONWARD = ["then onward to", "and thence to", "then home by way of",
          "and finally to", "then back to"]


@dataclass(frozen=True)
class Journey:
    text: str
    first: tuple[int, int]
    second: tuple[int, int]


def _near(rng: random.Random) -> str:
    return f"{rng.choice(NEAR)} {rng.choice(PLACES).name}"


@dataclass
class Lore:
    """Draws the realm's events. Hand-written lines can be extended from an
    EVENTS_FILE; composed ones are always in the mix."""

    calamities: list[str] = field(default_factory=lambda: list(CALAMITIES))
    godsends: list[str] = field(default_factory=lambda: list(GODSENDS))
    vigils: list[str] = field(default_factory=lambda: list(VIGILS))
    journeys: list[Journey] = field(default_factory=list)

    def _handwritten(self, pool: list, rng: random.Random) -> bool:
        return bool(pool) and rng.random() < HANDWRITTEN_SHARE

    def calamity(self, rng: random.Random) -> str:
        if self._handwritten(self.calamities, rng):
            return rng.choice(self.calamities)
        if rng.randrange(2):
            return f"was {rng.choice(MISHAPS)} by {rng.choice(CREATURES)} {_near(rng)}"
        return f"{rng.choice(BLUNDERS)} {_near(rng)}"

    def godsend(self, rng: random.Random) -> str:
        if self._handwritten(self.godsends, rng):
            return rng.choice(self.godsends)
        if rng.randrange(2):
            return f"was {rng.choice(HELPS)} by {rng.choice(HELPERS)} {_near(rng)}"
        return f"found {rng.choice(TREASURES)} {_near(rng)}"

    def vigil(self, rng: random.Random) -> str:
        if self._handwritten(self.vigils, rng):
            return rng.choice(self.vigils)
        return (f"{rng.choice(VIGIL_ACTS)} at {rng.choice(PLACES).name} "
                f"{rng.choice(VIGIL_UNTIL)}")

    def journey(self, rng: random.Random) -> Journey:
        if self._handwritten(self.journeys, rng):
            return rng.choice(self.journeys)
        start, end = rng.sample(PLACES, 2)
        return Journey(
            f"{rng.choice(CARGO)} to {start.name}, {rng.choice(ONWARD)} {end.name}",
            start.at, end.at,
        )


def parse(text: str) -> Lore:
    """Read lines in the classic events.txt format into a Lore whose
    hand-written lines include them. Malformed Q2 lines are skipped."""
    lore = Lore()
    pools = {"C": lore.calamities, "G": lore.godsends, "Q1": lore.vigils}
    for raw in text.splitlines():
        kind, _, rest = raw.strip().partition(" ")
        rest = rest.strip()
        if not rest:
            continue
        if kind in pools:
            pools[kind].append(rest)
        elif kind == "Q2":
            parts = rest.split(None, 4)
            try:
                x1, y1, x2, y2 = (int(v) for v in parts[:4])
                lore.journeys.append(Journey(parts[4], (x1, y1), (x2, y2)))
            except (ValueError, IndexError):
                log.warning("skipping malformed journey: %s", raw.strip())
    return lore


def load(path: str | None = None) -> Lore:
    """The realm's lore, extended by EVENTS_FILE where one is configured."""
    path = path if path is not None else os.environ.get("EVENTS_FILE", "")
    if not path:
        return Lore()
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            lore = parse(fh.read())
    except OSError as exc:
        log.warning("cannot read EVENTS_FILE %s (%s); using the realm's own lore",
                    path, exc)
        return Lore()
    log.info("lore extended from %s", path)
    return lore


LORE = load()


# The outer tenth of the map on every side is the wilds; the rest - the middle
# 80% on each axis - is the heartland, where the realm's life goes on. Its
# places lie inside it, and characters drift back to it: see move_player.
WILDS = 0.10


def heartland(size: int) -> tuple[int, int]:
    """The lowest and highest coordinate of the heartland on one axis."""
    margin = int(size * WILDS)
    return margin, size - 1 - margin


def place(point: tuple[int, int], map_x: int, map_y: int) -> tuple[int, int]:
    """A lore position on this realm's map: the lore's whole map scaled into
    the heartland, so journeys lead through the middle of the realm."""
    def scale(v: int, size: int) -> int:
        low, high = heartland(size)
        v = min(LORE_MAP - 1, max(0, v))
        return low + v * (high - low) // (LORE_MAP - 1)
    x, y = point
    return scale(x, map_x), scale(y, map_y)
