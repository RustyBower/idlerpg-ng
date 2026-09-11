"""NPCs: the realm's own characters, to keep a small realm lively.

A realm of four people cannot fill a quest party or a team battle, and leaves
nobody fair to fight. NPCs make up the numbers: while fewer than NPC_REALM
characters have been about in the last week, NPCs join - NPC_MAX at most -
and when people come back they set off for distant lands, keeping their level
for when they are next needed. NPC_MAX is 0 unless the deployment sets it.

They play like people rather than perfect idlers: an average player's habits,
the simulator's, so they talk now and then, leave for hours, and pay the same
penalties, and a dedicated idler passes them. They never prestige, and on a
quest they hold their tongues, so no party ever loses a quest to a bot.

They live only in the engine - no IRC or Discord connection - and the website
marks them. Their password can never match, so nobody can log in as one.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from . import quests
from .events import SLOTS, Outcome, spawn_point
from .models import Alignment, Ethos, Item, Player, utcnow
from .rules import Penalty, ttl

DAY = 86400
WEEK = 7 * DAY


@dataclass
class Habits:
    talk_per_day: float = 2.0       # lines said in the game channel
    absences_per_week: float = 1.0  # quits, each followed by time away
    away_hours: float = 8.0         # mean length of an absence


# Kinds of player, since an alignment's worth depends on how you play: a
# penalty cut is worth far more to someone who talks than to someone who idles.
PROFILES = {
    "quiet": Habits(talk_per_day=0.2, absences_per_week=0.5, away_hours=8),
    "average": Habits(talk_per_day=1.0, absences_per_week=1.0, away_hours=8),
    "chatty": Habits(talk_per_day=4.0, absences_per_week=2.0, away_hours=6),
}
HABITS = PROFILES["average"]

# Player.npc: None for a person; for an NPC, where it is.
PRESENT, AWAY, BENCHED = "present", "away", "benched"

# A person counts toward the realm while playing, or if seen within this long.
ACTIVE_WINDOW = timedelta(days=7)
# NPCs join below NPC_REALM but leave only once the realm is this many past
# it, so one person coming and going does not send an NPC back and forth.
SLACK = 3
CHECK_SECONDS = 3600
UNUSABLE_PASSWORD = "!npc"   # not a hash: verify_password never matches it

NAMES = [
    "Bramble", "Hobb", "Wenna", "Tamsin", "Osric", "Merrow", "Quill", "Fennick",
    "Isolde", "Crispin", "Tobiah", "Rook", "Sable", "Nettle", "Perrin", "Elspeth",
    "Barnaby", "Corwin", "Hester", "Jory", "Linnet", "Aldous", "Wilhelmina", "Mote",
]
CLASSES = [
    "hedge knight", "wandering tinker", "retired bard", "lapsed monk",
    "goose herder", "mushroom forager", "bell ringer", "apprentice lich",
    "tax collector", "cartographer", "reformed bandit", "rat catcher",
]


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _about(player: Player, now: datetime) -> bool:
    """A person playing now, or seen within ACTIVE_WINDOW."""
    if player.is_idling:
        return True
    seen = [i.presence_since for i in player.identities]
    seen += [player.last_login, player.created]
    seen = [_aware(s) for s in seen if s is not None]
    return bool(seen) and now - max(seen) < ACTIVE_WINDOW


def _questers(session) -> set[int]:
    quest = quests.active_quest(session)
    return {q.player_id for q in quest.participants} if quest else set()


def tend(engine, players: list[Player], elapsed: float) -> list[Outcome]:
    """Each tick, before anyone earns: the NPCs' comings, goings and chatter."""
    if engine.npc_max <= 0 and not any(p.npc for p in players):
        return []
    out: list[Outcome] = []
    engine.npc_wait -= elapsed
    if engine.npc_wait <= 0:
        engine.npc_wait = CHECK_SECONDS
        out.extend(_make_up_numbers(engine, players))
    questing = _questers(engine.session)
    for p in players:
        if p.npc in (PRESENT, AWAY) and p.id not in questing:
            _habits(engine, p, elapsed)
    return out


def _habits(engine, npc: Player, elapsed: float) -> None:
    rng = engine.rng
    if npc.npc == AWAY:
        if rng.random() < elapsed / (HABITS.away_hours * 3600):
            npc.npc = PRESENT
        return
    if rng.random() < HABITS.talk_per_day * elapsed / DAY:
        engine.penalise(npc, Penalty.MESSAGE, message_length=rng.randint(10, 80))
    if rng.random() < HABITS.absences_per_week * elapsed / WEEK:
        engine.penalise(npc, Penalty.QUIT)
        npc.npc = AWAY


def _make_up_numbers(engine, players: list[Player]) -> list[Outcome]:
    """Bring NPCs in, or send them off, to keep the realm near NPC_REALM."""
    people = sum(1 for p in players if not p.npc and _about(p, utcnow()))
    active = [p for p in players if p.npc in (PRESENT, AWAY)]
    benched = [p for p in players if p.npc == BENCHED]
    most = max(0, engine.npc_max)
    want = max(0, min(most, engine.npc_realm - people))
    allow = max(0, min(most, engine.npc_realm + SLACK - people))
    out: list[Outcome] = []

    rows = len(active) + len(benched)
    taken = {p.name.casefold() for p in players} | set(engine.owners)
    for _ in range(want - len(active)):
        if benched:
            npc = benched.pop(engine.rng.randrange(len(benched)))
            npc.npc = PRESENT
            out.append(Outcome(f"{npc.name} is back from distant lands.", kind="npc"))
        elif rows < most and (npc := _create(engine, taken)) is not None:
            rows += 1
            out.append(Outcome(
                f"{npc.name}, the {npc.character_class}, wanders into the realm "
                f"to make up the numbers (an NPC).", kind="register"))
        else:
            break

    if len(active) > allow:
        questing = _questers(engine.session)
        # Those already away go first, then the least advanced.
        spare = sorted((p for p in active if p.id not in questing),
                       key=lambda p: (p.npc != AWAY, p.level, p.id))
        for npc in spare[:len(active) - allow]:
            npc.npc = BENCHED
            out.append(Outcome(f"{npc.name} sets off for distant lands.", kind="npc"))
    engine.session.commit()
    return out


def _create(engine, taken: set[str]) -> Player | None:
    from .engine import events_map_x, events_map_y   # engine imports this module

    free = [n for n in NAMES if n.casefold() not in taken]
    if not free:
        return None
    rng = engine.rng
    x, y = spawn_point(events_map_x(), events_map_y(), rng)
    npc = Player(
        name=rng.choice(free),
        password_hash=UNUSABLE_PASSWORD,
        character_class=rng.choice(CLASSES),
        level=0,
        next_ttl=int(ttl(0, engine.curve)),
        alignment=rng.choice(list(Alignment)),
        ethos=rng.choice(list(Ethos)),
        last_login=utcnow(),
        x=x,
        y=y,
        npc=PRESENT,
    )
    for slot in SLOTS:
        npc.items.append(Item(slot=slot, value=0))
    engine.session.add(npc)
    taken.add(npc.name.casefold())
    return npc
