"""Quests.

Four players at level 40 or above are chosen by the gods. A timed quest needs
them to stay put for 12 to 24 hours; a journey walks them to two waypoints in
turn. Finishing one removes a quarter of each quester's remaining time.

Talking, parting or quitting fails a quest, and the party pays for it: every
quester takes the original's fifteen-step quest penalty, and the gods offer no
quest for twelve hours. The original set back everyone online instead. Here the
stake is the party's own, since a quest is a vow its members made, not their
neighbours.

The original walks a journey's party a step a second, so its journeys end in
minutes while vigils last a day. Here the party walks a step every half-minute,
which makes a journey a few hours - something the realm can watch on the map -
and gives up after a day, blaming no one.

Quest texts come from lore.py: vigils at, and journeys between, the realm's
named places.
"""

from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from . import events
from .events import Outcome
from .lore import LORE, place
from . import achievements
from .models import PenaltyRecord, Player, Quest, QuestParticipant, Setting, utcnow
from .rules import Curve, Penalty, penalty_seconds
from .text import duration

MIN_LEVEL = 40
PARTY_SIZE = 4
COOLDOWN = timedelta(hours=6)          # mean wait between attempts to start one
REST = timedelta(hours=6)              # after a quest ends, as the original
FAILURE_REST = timedelta(hours=12)     # after one fails, as the original
JOURNEY_TIMEOUT = timedelta(hours=24)
JOURNEY_PACE = 30                      # seconds per step on a journey
COMPLETION_BONUS = 0.75                # a quarter of the remaining burden is removed
REST_KEY = "quest_rest_until"


def _aware(value):
    """SQLite gives naive datetimes back; compare like for like."""
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _names(party: list[Player]) -> str:
    return ", ".join(p.name for p in party[:-1]) + f" and {party[-1].name}"


def _target(quest: Quest) -> tuple[int, int]:
    return (quest.x1, quest.y1) if quest.stage == 1 else (quest.x2, quest.y2)


def _rest(session: Session, length: timedelta) -> None:
    """No quest is offered until ``length`` from now."""
    until = (utcnow() + length).isoformat()
    row = session.get(Setting, REST_KEY)
    if row is None:
        session.add(Setting(key=REST_KEY, value=until))
    else:
        row.value = until


def resting(session: Session) -> bool:
    row = session.get(Setting, REST_KEY)
    return row is not None and utcnow() < _aware(datetime.fromisoformat(row.value))


def active_quest(session: Session) -> Quest | None:
    return session.scalar(
        select(Quest).options(
            selectinload(Quest.participants).selectinload(QuestParticipant.player)
        ).order_by(Quest.id.desc()).limit(1)
    )


def eligible(players: list[Player]) -> list[Player]:
    return [p for p in players if p.is_idling and p.level >= MIN_LEVEL]


def choose_party(candidates: list[Player], rng: random.Random) -> list[Player]:
    """PARTY_SIZE of the candidates, the lawful a little likelier to be
    chosen."""
    pool, party = list(candidates), []
    while len(party) < PARTY_SIZE:
        weights = [events.QUEST_WEIGHT[events.ethos(p)] for p in pool]
        chosen = rng.choices(pool, weights=weights)[0]
        pool.remove(chosen)
        party.append(chosen)
    return party


def start(session: Session, players: list[Player], rng: random.Random,
          map_x: int, map_y: int, force: bool = False) -> Outcome | None:
    """Begin a quest if enough senior players are around and the gods are
    not resting. ``force`` - an admin's EVENT quest - ignores the rest, but
    never starts a second quest alongside one already running."""
    if active_quest(session) is not None:
        return None
    if resting(session) and not force:
        return None
    candidates = eligible(players)
    if len(candidates) < PARTY_SIZE:
        return None
    party = choose_party(candidates, rng)

    if rng.randrange(2):
        # The original waits 12 to 24 hours.
        seconds = 43200 + rng.randrange(43201)
        quest = Quest(
            text=LORE.vigil(rng), kind=1,
            expires=utcnow() + timedelta(seconds=seconds),
        )
        route = f" The vigil lasts {duration(seconds)}."
    else:
        journey = LORE.journey(rng)
        (x1, y1), (x2, y2) = (place(journey.first, map_x, map_y),
                              place(journey.second, map_x, map_y))
        quest = Quest(
            text=journey.text, kind=2, stage=1, x1=x1, y1=y1, x2=x2, y2=y2,
            expires=utcnow() + JOURNEY_TIMEOUT,
        )
        route = f" Their road runs to [{x1},{y1}], then [{x2},{y2}]."
    quest.participants = [QuestParticipant(player_id=p.id) for p in party]
    session.add(quest)
    session.commit()

    return Outcome(
        f"The gods have chosen {_names(party)} to {quest.text}.{route} "
        f"They must keep silent until it is done.",
        kind="quest",
    )


def steer(session: Session, elapsed: float, rng: random.Random) -> set[int]:
    """Walk a journey's party toward its waypoint; returns who was walked.

    A step every JOURNEY_PACE seconds, with the remainder of a tick rolled as
    a chance, so the pace holds whatever the tick length. Everyone else keeps
    drifting at random.
    """
    quest = active_quest(session)
    if quest is None or quest.kind != 2:
        return set()
    # The party walks at its fastest strider's pace: one carries the rest -
    # and a mount among them carries everyone, like ranks of Stride.
    stride = max((events.rank(m.player, "stride") for m in quest.participants), default=0)
    if any(item.tag == events.MOUNT_TAG
           for m in quest.participants for item in m.player.items):
        stride = max(stride, events.MOUNT_STRIDE)
    pace = JOURNEY_PACE / (1 + events.STRIDE_PER_RANK * stride)
    whole, part = divmod(elapsed, pace)
    steps = int(whole) + (1 if rng.random() < part / pace else 0)
    x, y = _target(quest)
    walked = set()
    for member in quest.participants:
        events.step_toward(member.player, x, y, steps)
        walked.add(member.player_id)
    return walked


def advance(session: Session, quest: Quest, rng: random.Random) -> list[Outcome]:
    """Move a quest along; returns anything worth announcing."""
    if quest is None:
        return []
    party = [p.player for p in quest.participants]
    if not party:
        return []

    expired = quest.expires is not None and utcnow() >= _aware(quest.expires)
    if quest.kind == 1:
        return _complete(session, quest, party) if expired else []

    if expired:
        session.delete(quest)
        _rest(session, REST)
        session.commit()
        return [Outcome(
            f"{_names(party)} did not reach the end of their road in time. "
            f"The quest is abandoned, and no one is blamed.",
            kind="quest",
        )]

    # A journey: everyone must stand on the current waypoint together.
    target = _target(quest)
    if not all((p.x, p.y) == target for p in party):
        return []
    if quest.stage == 1:
        quest.stage = 2
        session.commit()
        return [Outcome(
            f"The party has reached the first waypoint at [{target[0]},{target[1]}]. "
            f"Their journey continues to [{quest.x2},{quest.y2}].",
            kind="quest",
        )]
    return _complete(session, quest, party)


def _complete(session: Session, quest: Quest, party: list[Player]) -> list[Outcome]:
    for p in party:
        p.next_ttl = int(p.next_ttl * COMPLETION_BONUS)
    session.delete(quest)
    _rest(session, REST)
    session.commit()
    done = [Outcome(
        f"{_names(party)} have done it: the quest is complete, and each of "
        f"them is 25% closer to their next level.",
        # Its own kind, so a finished quest can be counted apart from the
        # ones that were merely offered, walked or abandoned.
        kind="questdone",
    )]
    for p in party:
        done.extend(achievements.on_quest(p))
    return done


def fail(session: Session, player: Player,
         curve: Curve | None = None) -> list[Outcome]:
    """Called when a quester misbehaves. The whole party pays; nobody else."""
    quest = active_quest(session)
    if quest is None:
        return []
    if not any(p.player_id == player.id for p in quest.participants):
        return []

    party = [p.player for p in quest.participants]
    costs = []
    for member in party:
        seconds = penalty_seconds(Penalty.QUEST, member.level, curve)
        member.next_ttl += seconds
        session.add(PenaltyRecord(
            player_id=member.id, kind=Penalty.QUEST.value, seconds=seconds,
            platform=None,
        ))
        costs.append(f"{member.name} +{duration(seconds)}")
    session.delete(quest)
    _rest(session, FAILURE_REST)
    session.commit()
    return [Outcome(
        f"{player.name} broke the party's silence, and the gods noticed. The "
        f"party is set back fifteen steps each "
        f"({', '.join(costs)}), and the gods will offer no quest for 12 hours.",
        kind="quest",
    )] + achievements.on_loose_lips(player)
