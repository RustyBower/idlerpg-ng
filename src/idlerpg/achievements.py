"""Achievements, titles and keepsakes: things to earn that change nothing.

An achievement is earned once, announced to the realm, listed by
ACHIEVEMENTS and on the character's page. Some wait on a tally - fights
won, doors knocked on, eggs found - kept in its own table. Each season has
a set of its own, after the holiday achievements of long-running online
games; earning all of a season's grants its meta achievement and a title
worn beside the name: the Lantern-Bearer, Keeper of the Long Night, the
Blossoming.

Keepsakes are the seasons' collectables - masks and costumes in
Hallowtide's treats, gifts under Midwinter's Great Tree, Springtide's eggs.
Like the achievements they are for show: none of it touches a clock, an
item or a fight, so none of it needs the fairness harness. NPCs are the
realm's own and earn none of it.
"""

from __future__ import annotations

import datetime as dt
import random
import time
from dataclasses import dataclass

from . import lore
from .events import Outcome
from .models import Achievement, Keepsake, Player, Tally


@dataclass(frozen=True)
class Feat:
    key: str
    name: str
    badge: str
    text: str                       # how it is earned
    season: str | None = None
    title: str | None = None        # granted with it: the seasons' metas


FEATS = [
    Feat("level-10", "Finding Your Feet", "🥾", "Reach level 10."),
    Feat("level-25", "Seasoned", "🧭", "Reach level 25."),
    Feat("level-40", "Worthy of Quests", "📜", "Reach level 40, when the gods start asking."),
    Feat("level-60", "At the Wall", "🧱", "Reach level 60."),
    Feat("prestige-1", "Born Again", "🔁", "Prestige for the first time."),
    Feat("prestige-3", "Thrice Reborn", "🌀", "Prestige three times."),
    Feat("taking-sides", "Taking Sides", "⚖", "Choose an alignment other than true neutral."),
    Feat("first-blood", "First Blood", "⚔", "Win a fight."),
    Feat("brawler", "Brawler", "🥊", "Win ten fights."),
    Feat("giant-slayer", "Giant Slayer", "🗡", "Win a fight against someone five or more levels above you."),
    Feat("called", "Called by the Gods", "🙏", "Complete a quest."),
    Feat("hero", "Hero of the Realm", "🛡", "Complete five quests."),
    Feat("loose-lips", "Loose Lips", "👄", "Break a quest's silence. The gods noticed."),
    Feat("silent-week", "Silent as the Grave", "🤫", "Idle a full week without a single penalty."),
    Feat("unique", "Something Special", "💎", "Find a unique item."),

    Feat("hallowtide-knock", "Trick or Treater", "🚪", "Knock on five doors in Hallowtide.", "Hallowtide"),
    Feat("hallowtide-sweet", "Sweet Tooth", "🍬", "Be given ten treats in Hallowtide.", "Hallowtide"),
    Feat("hallowtide-masks", "Masquerade", "🎭", "Collect every Hallowtide mask and costume.", "Hallowtide"),
    Feat("hallowtide-horseman", "Headless, Not Heartless", "🏇", "Find the Horseman's lantern in a treat.", "Hallowtide"),
    Feat("hallowtide-kept", "Kept Hallowtide", "🎃", "Idle through at least half of a Hallowtide.", "Hallowtide"),
    Feat("hallowtide-meta", "Lantern-Bearer", "🏮", "Earn every other Hallowtide achievement.",
         "Hallowtide", title="the Lantern-Bearer"),

    Feat("midwinter-gift", "Under the Great Tree", "🎁", "Open a gift on Midwinter Day, 25 December.", "Midwinter"),
    Feat("midwinter-longest", "The Longest Night", "🌑", "Be idling as the solstice, 21 December, begins.", "Midwinter"),
    Feat("midwinter-snowball", "Snowball Fight", "⛄", "Win a fight in Midwinter.", "Midwinter"),
    Feat("midwinter-stocking", "Stocking Stuffer", "🧦", "Collect every Midwinter gift.", "Midwinter"),
    Feat("midwinter-kept", "Kept Midwinter", "❄", "Idle through at least half of a Midwinter.", "Midwinter"),
    Feat("midwinter-meta", "Keeper of the Long Night", "🕯", "Earn every other Midwinter achievement.",
         "Midwinter", title="Keeper of the Long Night"),

    Feat("springtide-eggs", "Egg Hunter", "🥚", "Find ten painted eggs in Springtide.", "Springtide"),
    Feat("springtide-golden", "The Golden Egg", "🌟", "Find the golden egg.", "Springtide"),
    Feat("springtide-basket", "A Full Basket", "🧺", "Collect every Springtide keepsake.", "Springtide"),
    Feat("springtide-step", "Spring in Your Step", "🐇", "Level up five times in Springtide.", "Springtide"),
    Feat("springtide-kept", "Kept Springtide", "🌱", "Idle through at least half of a Springtide.", "Springtide"),
    Feat("springtide-meta", "The Blossoming", "🌸", "Earn every other Springtide achievement.",
         "Springtide", title="the Blossoming"),
]
BY_KEY = {f.key: f for f in FEATS}

# Each season's keepsakes: the common ones, and one rare.
KEEPSAKES = {
    "Hallowtide": (["a goblin mask", "a skeleton costume", "a witch's pointed hat",
                    "a pumpkin-head lantern", "a jar of captive fog"], "the Horseman's lantern"),
    "Midwinter": (["a red winter hat", "a snow globe of the realm", "a clockwork reindeer",
                   "a tin soldier", "a scarf in the realm's colours"], "a bell from the Great Tree"),
    "Springtide": (["a set of spring robes", "a blossoming branch", "a rabbit's-foot charm",
                    "a crown of daisies", "a hatchling that follows you about"], "the golden egg"),
}
COLLECTION = {"Hallowtide": "hallowtide-masks", "Midwinter": "midwinter-stocking",
              "Springtide": "springtide-basket"}
RARE = {"Hallowtide": "hallowtide-horseman", "Springtide": "springtide-golden"}

RARE_CHANCE = 0.05          # of keepsakes drawn, the season's rare one
TREAT_KEEPSAKE = 1 / 3      # of Hallowtide's treats, holding a mask or costume
GIFT_CHANCE = 0.10          # of Midwinter level-ups, finding a gift on the way
EGG_INTERVAL = 2 * 86400    # Springtide's egg hunt, per online player
EGG_KEEPSAKE = 0.25         # of eggs, holding a keepsake
QUIET_WEEK = 7 * 86400
LEVELS = {10: "level-10", 25: "level-25", 40: "level-40", 60: "level-60"}
VERBS = frozenset({"ACHIEVEMENTS"})


def now() -> int:
    return int(time.time())


def has(player: Player, key: str) -> bool:
    return any(a.key == key for a in player.achievements)


def award(player: Player, key: str, quiet: bool = False) -> list[Outcome]:
    """Give ``player`` the achievement ``key``, once; what to announce. A
    season's last one brings its meta achievement and title with it."""
    feat = BY_KEY[key]
    if player.npc or has(player, key):
        return []
    player.achievements.append(Achievement(key=key, badge=feat.badge, title=feat.name))
    out = []
    if feat.title:
        player.title = feat.title
        out.append(Outcome(f"{player.name} has earned {feat.badge} {feat.name}, and is now "
                           f"{player.name}, {feat.title}!", kind="achievement",
                           player_id=player.id))
    elif not quiet:
        out.append(Outcome(f"{player.name} has earned the achievement {feat.badge} "
                           f"{feat.name}.", kind="achievement", player_id=player.id))
    if feat.season and not feat.title:
        season = [f for f in FEATS if f.season == feat.season]
        if all(has(player, f.key) for f in season if not f.title):
            out.extend(award(player, next(f.key for f in season if f.title)))
    return out


def get_tally(player: Player, key: str) -> int | None:
    return next((t.value for t in player.tallies if t.key == key), None)


def set_tally(player: Player, key: str, value: int) -> int:
    row = next((t for t in player.tallies if t.key == key), None)
    if row is None:
        row = Tally(key=key, value=0)
        player.tallies.append(row)
    row.value = value
    return value


def tally(player: Player, key: str, n: int = 1) -> int:
    return set_tally(player, key, (get_tally(player, key) or 0) + n)


def keepsake(player: Player, season: str, rng: random.Random) -> tuple[str | None, list[Outcome]]:
    """Draw one of a season's keepsakes; None if they have it already."""
    commons, rare = KEEPSAKES[season]
    name = rare if rng.random() < RARE_CHANCE else rng.choice(commons)
    if player.npc or any(k.name == name for k in player.keepsakes):
        return None, []
    player.keepsakes.append(Keepsake(name=name, season=season))
    out = []
    owned = {k.name for k in player.keepsakes}
    if set(commons) <= owned:
        out.extend(award(player, COLLECTION[season]))
    if name == rare and season in RARE:
        out.extend(award(player, RARE[season]))
    return name, out


def _season_is(name: str) -> bool:
    season = lore.current_season()
    return season is not None and season.name == name


# ------------------------------------------------------------- the hooks

def on_level(player: Player, rng: random.Random) -> list[Outcome]:
    """After a level-up and its item find."""
    if player.npc:
        return []
    out = []
    for level, key in LEVELS.items():
        if player.level >= level:
            out.extend(award(player, key))
    if any(i.tag for i in player.items):
        out.extend(award(player, "unique"))
    if _season_is("Springtide") and tally(player, "springtide-levels") >= 5:
        out.extend(award(player, "springtide-step"))
    if _season_is("Midwinter") and rng.random() < GIFT_CHANCE:
        name, more = keepsake(player, "Midwinter", rng)
        if name:
            out.append(Outcome(f"{player.name} found a gift on the way up, and inside was "
                               f"{name}.", kind="keepsake", player_id=player.id))
            out.extend(more)
    return out


def on_prestige(player: Player) -> list[Outcome]:
    out = award(player, "prestige-1") if (player.prestige or 0) >= 1 else []
    if (player.prestige or 0) >= 3:
        out.extend(award(player, "prestige-3"))
    return out


def on_align(player: Player) -> list[Outcome]:
    return award(player, "taking-sides") if player.alignment_name != "true neutral" else []


def on_fight(winner: Player, loser: Player) -> list[Outcome]:
    """A FIGHT or a meeting on the map, won."""
    if winner.npc:
        return []
    wins = tally(winner, "fights-won")
    out = award(winner, "first-blood")
    if wins >= 10:
        out.extend(award(winner, "brawler"))
    if loser.level >= winner.level + 5:
        out.extend(award(winner, "giant-slayer"))
    if _season_is("Midwinter"):
        out.extend(award(winner, "midwinter-snowball"))
    return out


def on_quest(player: Player) -> list[Outcome]:
    if player.npc:
        return []
    done = tally(player, "quests-done")
    out = award(player, "called")
    if done >= 5:
        out.extend(award(player, "hero"))
    return out


def on_loose_lips(player: Player) -> list[Outcome]:
    return award(player, "loose-lips")


def quiet(player: Player, at: int) -> None:
    """A penalty: the week of silence starts again."""
    if not player.npc:
        set_tally(player, "quiet-since", at)


def on_knock(player: Player, treat: bool, rng: random.Random) -> list[Outcome]:
    """Hallowtide's trick or treat, just done."""
    if player.npc:
        return []
    out = []
    if tally(player, "hallowtide-knocks") >= 5:
        out.extend(award(player, "hallowtide-knock"))
    if treat:
        if tally(player, "hallowtide-treats") >= 10:
            out.extend(award(player, "hallowtide-sweet"))
        if rng.random() < TREAT_KEEPSAKE:
            name, more = keepsake(player, "Hallowtide", rng)
            if name:
                out.append(Outcome(f"Inside {player.name}'s treat: {name}!", kind="keepsake",
                                   player_id=player.id))
                out.extend(more)
    return out


def egg_hunt(player: Player, rng: random.Random) -> list[Outcome]:
    """Springtide: a painted egg in the grass, and sometimes something in it."""
    out = [Outcome(f"{player.name} found a painted egg {lore.near(rng)}.", kind="egg",
                   player_id=player.id)]
    if player.npc:
        return out
    if tally(player, "springtide-eggs") >= 10:
        out.extend(award(player, "springtide-eggs"))
    if rng.random() < EGG_KEEPSAKE:
        name, more = keepsake(player, "Springtide", rng)
        if name:
            out.append(Outcome(f"Inside it: {name}!", kind="keepsake", player_id=player.id))
            out.extend(more)
    return out


def hourly(engine, online: list[Player], at: int | None = None) -> list[Outcome]:
    """The slow ones: a week without a penalty, the solstice, Midwinter Day."""
    at = at or now()
    people = [p for p in online if not p.npc]
    out = []
    for p in people:
        since = get_tally(p, "quiet-since")
        if since is None:
            set_tally(p, "quiet-since", at)         # counting starts now
        elif at - since >= QUIET_WEEK:
            out.extend(award(p, "silent-week"))
    if _season_is("Midwinter"):
        day = dt.datetime.fromtimestamp(at, dt.timezone.utc)
        year = str(day.year)
        if (day.month, day.day) == (12, 21) and engine.get_setting("solstice_kept") != year:
            engine.set_setting("solstice_kept", year)
            out.append(Outcome("The longest night has begun. All who idle through it will "
                               "be remembered.", kind="season"))
            for p in people:
                out.extend(award(p, "midwinter-longest", quiet=True))
        if (day.month, day.day) == (12, 25) and engine.get_setting("gifts_given") != year:
            engine.set_setting("gifts_given", year)
            out.append(Outcome("Midwinter Day: gifts have appeared under the Great Tree for "
                               "everyone about.", kind="keepsake"))
            for p in people:
                name, more = keepsake(p, "Midwinter", engine.rng)
                out.extend(award(p, "midwinter-gift", quiet=True))
                if name:
                    out.append(Outcome(f"{p.name} unwraps {name}.", kind="keepsake",
                                       player_id=p.id))
                out.extend(more)
    return out


# ------------------------------------------------------------ for showing

def styled(player: Player) -> str:
    """"Rusty, the Lantern-Bearer" - or just the name."""
    return f"{player.name}, {player.title}" if player.title else player.name


def summary(player: Player) -> str:
    """For WHOAMI."""
    earned = sum(1 for a in player.achievements if a.key in BY_KEY)
    kept = len(player.keepsakes)
    return ((f" Achievements: {earned} (ACHIEVEMENTS lists them)." if earned else "")
            + (f" Keepsakes: {kept}." if kept else ""))


def command(engine, player: Player | None, verb: str, args: list[str]) -> str:
    """ACHIEVEMENTS: what a character has earned and collected."""
    if player is None:
        return "Log in first."
    feats = [BY_KEY[a.key] for a in player.achievements if a.key in BY_KEY]
    parts = [f"{styled(player)}: {len(feats)} of {len(FEATS)} achievements"]
    parts += [f"{f.badge} {f.name}" for f in feats]
    if player.keepsakes:
        parts.append("Keepsakes: " + ", ".join(k.name for k in player.keepsakes))
    parts.append("Every achievement, and how to earn it, is on your page on the website.")
    return " | ".join(parts)
