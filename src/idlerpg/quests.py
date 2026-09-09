"""Quests.

Four players above level 40 are chosen by the gods. A timed quest just needs
them to stay put; a journey sends them to two waypoints in turn. Finishing one
removes a quarter of everyone's remaining burden. Talking, parting or quitting
fails it for the whole party, and the realm pays for it - which is the point:
it makes idling a shared discipline rather than a solitary one.

Quest texts are original: the fork this is descended from ships an events file
with no Q lines in it.
"""

from __future__ import annotations

import random
from datetime import timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from .events import Outcome
from .models import Player, Quest, QuestParticipant, utcnow

MIN_LEVEL = 40
PARTY_SIZE = 4
COOLDOWN = timedelta(hours=6)
COMPLETION_BONUS = 0.75      # a quarter of the remaining burden is removed
FAILURE_PENALTY = 0.15       # and failing costs the realm

TIMED_QUESTS = [
    "sit vigil at the Fountain of Unspoken Things until the moon sets",
    "hold their tongues in the Library of Whispers for a full watch",
    "guard the sleeping wyrm of Kettleridge without waking it",
    "keep the beacon lit through the long dark of Harrowmere",
]

JOURNEY_QUESTS = [
    "carry the Lantern of Small Mercies to the Ashen Gate, then onward to the Drowned Steps",
    "bear the Kumquat of Ages to the Cairn of Regrets, and thence to the Weeping Bridge",
    "escort the Last Cartographer to the Edge of the Map, and back to the Inn of Lost Causes",
]


def _aware(value):
    """SQLite gives naive datetimes back; compare like for like."""
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def active_quest(session: Session) -> Quest | None:
    return session.scalar(
        select(Quest).options(
            selectinload(Quest.participants).selectinload(QuestParticipant.player)
        ).order_by(Quest.id.desc()).limit(1)
    )


def eligible(players: list[Player]) -> list[Player]:
    return [p for p in players if p.is_idling and p.level >= MIN_LEVEL]


def start(session: Session, players: list[Player], rng: random.Random,
          map_x: int, map_y: int) -> Outcome | None:
    """Begin a quest if enough senior players are around."""
    candidates = eligible(players)
    if len(candidates) < PARTY_SIZE:
        return None
    party = rng.sample(candidates, PARTY_SIZE)

    if rng.randrange(2):
        quest = Quest(
            text=rng.choice(TIMED_QUESTS), kind=1,
            # The original waits 12 to 24 hours.
            expires=utcnow() + timedelta(seconds=43200 + rng.randrange(43201)),
        )
    else:
        quest = Quest(
            text=rng.choice(JOURNEY_QUESTS), kind=2, stage=1,
            x1=rng.randrange(map_x), y1=rng.randrange(map_y),
            x2=rng.randrange(map_x), y2=rng.randrange(map_y),
        )
    quest.participants = [QuestParticipant(player_id=p.id) for p in party]
    session.add(quest)
    session.commit()

    names = ", ".join(p.name for p in party[:-1]) + f" and {party[-1].name}"
    return Outcome(
        f"{names} have been chosen by the gods to {quest.text}. "
        f"Participants must remain silent.",
        kind="quest",
    )


def advance(session: Session, quest: Quest, rng: random.Random) -> list[Outcome]:
    """Move a quest along; returns anything worth announcing."""
    if quest is None:
        return []
    party = [p.player for p in quest.participants]
    if not party:
        return []

    if quest.kind == 1:
        if quest.expires and utcnow() >= _aware(quest.expires):
            return _complete(session, quest, party)
        return []

    # A journey: everyone must stand on the current waypoint together.
    target = (quest.x1, quest.y1) if quest.stage == 1 else (quest.x2, quest.y2)
    if not all((p.x, p.y) == target for p in party):
        return []
    if quest.stage == 1:
        quest.stage = 2
        session.commit()
        return [Outcome(
            f"The party has reached the first waypoint at {target}. "
            f"Their journey continues.",
            kind="quest",
        )]
    return _complete(session, quest, party)


def _complete(session: Session, quest: Quest, party: list[Player]) -> list[Outcome]:
    for p in party:
        p.next_ttl = int(p.next_ttl * COMPLETION_BONUS)
    names = ", ".join(p.name for p in party[:-1]) + f" and {party[-1].name}"
    session.delete(quest)
    session.commit()
    return [Outcome(
        f"{names} have blessed the realm by completing their quest! "
        f"25% of their burden is eliminated.",
        kind="quest",
    )]


def fail(session: Session, player: Player) -> list[Outcome]:
    """Called when a quester misbehaves. Everyone pays, not just them."""
    quest = active_quest(session)
    if quest is None:
        return []
    if not any(p.player_id == player.id for p in quest.participants):
        return []

    everyone = session.scalars(select(Player)).all()
    for other in everyone:
        other.next_ttl = int(other.next_ttl * (1 + FAILURE_PENALTY))
    session.delete(quest)
    session.commit()
    return [Outcome(
        f"{player.name}'s prudence and self-regard has brought the wrath of "
        f"the gods upon the realm. All are slowed by 15%.",
        kind="quest",
    )]
