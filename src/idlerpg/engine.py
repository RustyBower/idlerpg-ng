"""The game engine.

Owns the clock, the rules and the events. Knows nothing about IRC or Discord:
adapters report presence and relay commands, and the engine decides what that
means. Keeping the clock here is what stops two adapters crediting the same
wall-clock second twice.
"""

from __future__ import annotations

import logging
import os
import random
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from .auth import hash_password, verify_password
from .models import (
    Alignment,
    EventLog,
    Item,
    PenaltyRecord,
    Platform,
    PlatformIdentity,
    Player,
    Presence,
    utcnow,
)
from .rules import Curve, Penalty, penalty_seconds, ttl

log = logging.getLogger(__name__)

MAP_X = int(os.environ.get("MAP_X", "500"))
MAP_Y = int(os.environ.get("MAP_Y", "500"))

ITEM_SLOTS = (
    "amulet", "charm", "helm", "boots", "gloves",
    "ring", "leggings", "shield", "tunic", "weapon",
)


@dataclass
class LevelUp:
    player: str
    level: int
    next_ttl: int


class RegistrationError(Exception):
    pass


class Engine:
    def __init__(self, session: Session, curve: Curve | None = None,
                 rng: random.Random | None = None):
        self.session = session
        self.curve = curve or Curve()
        self.rng = rng or random.Random()

    # ---------------------------------------------------------------- players

    def register(self, name: str, password: str, character_class: str,
                 platform: Platform, external_id: str) -> Player:
        name = name.strip()
        if not name:
            raise RegistrationError("a character needs a name")
        if len(name) > 64:
            raise RegistrationError("that name is too long")
        if self.find_player(name) is not None:
            raise RegistrationError(f"{name} is already taken")
        if self.find_identity(platform, external_id) is not None:
            raise RegistrationError("that account already has a character")

        player = Player(
            name=name,
            password_hash=hash_password(password),
            character_class=character_class.strip()[:64],
            level=0,
            next_ttl=int(ttl(0, self.curve)),
            alignment=Alignment.NEUTRAL,
            last_login=utcnow(),
            x=self.rng.randrange(MAP_X),
            y=self.rng.randrange(MAP_Y),
        )
        player.identities.append(
            PlatformIdentity(
                platform=platform,
                external_id=external_id,
                display_name=external_id,
                presence=Presence.ACTIVE,
            )
        )
        for slot in ITEM_SLOTS:
            player.items.append(Item(slot=slot, value=0))
        self.session.add(player)
        self.session.commit()
        self.log_event("register", f"{name} joins the realm as a {player.character_class}")
        return player

    def find_player(self, name: str) -> Player | None:
        return self.session.scalar(
            select(Player)
            .options(selectinload(Player.identities), selectinload(Player.items))
            .where(Player.name.ilike(name))
        )

    def find_identity(self, platform: Platform, external_id: str) -> PlatformIdentity | None:
        return self.session.scalar(
            select(PlatformIdentity).where(
                PlatformIdentity.platform == platform,
                PlatformIdentity.external_id == external_id,
            )
        )

    def player_for(self, platform: Platform, external_id: str) -> Player | None:
        identity = self.find_identity(platform, external_id)
        return identity.player if identity else None

    def authenticate(self, name: str, password: str) -> Player | None:
        player = self.find_player(name)
        if player and verify_password(password, player.password_hash):
            return player
        return None

    def link(self, player: Player, platform: Platform, external_id: str,
             display_name: str = "") -> PlatformIdentity:
        """Attach another platform account to an existing character."""
        existing = self.find_identity(platform, external_id)
        if existing is not None:
            if existing.player_id != player.id:
                raise RegistrationError("that account is linked to another character")
            return existing
        identity = PlatformIdentity(
            player_id=player.id,
            platform=platform,
            external_id=external_id,
            display_name=display_name or external_id,
            presence=Presence.ACTIVE,
        )
        self.session.add(identity)
        self.session.commit()
        return identity

    # --------------------------------------------------------------- presence

    def set_presence(self, platform: Platform, external_id: str,
                     presence: Presence) -> PlatformIdentity | None:
        identity = self.find_identity(platform, external_id)
        if identity is None:
            return None
        if identity.presence is not presence:
            identity.presence = presence
            identity.presence_since = utcnow()
            self.session.commit()
        return identity

    # ------------------------------------------------------------------ clock

    def tick(self, elapsed_seconds: float) -> list[LevelUp]:
        """Advance the world by ``elapsed_seconds``.

        Every player idling on at least one platform spends that many seconds
        off their timer - once, no matter how many platforms they are on.
        """
        if elapsed_seconds <= 0:
            return []

        players = self.session.scalars(
            select(Player).options(selectinload(Player.identities))
        ).all()

        levelled: list[LevelUp] = []
        for player in players:
            if not player.is_idling:
                continue
            remaining = player.next_ttl - elapsed_seconds
            while remaining <= 0:
                player.level += 1
                cost = int(ttl(player.level, self.curve))
                remaining += cost
                levelled.append(LevelUp(player.name, player.level, cost))
                self.log_event(
                    "levelup",
                    f"{player.name}, the {player.character_class or 'nameless'}, "
                    f"has attained level {player.level}",
                    commit=False,
                )
            player.next_ttl = int(remaining)

        self.session.commit()
        return levelled

    # -------------------------------------------------------------- penalties

    def penalise(self, player: Player, kind: Penalty, *,
                 message_length: int | None = None,
                 platform: Platform | None = None) -> int:
        seconds = penalty_seconds(
            kind, player.level, self.curve, message_length=message_length
        )
        player.next_ttl += seconds
        self.session.add(
            PenaltyRecord(
                player_id=player.id,
                kind=kind.value,
                seconds=seconds,
                platform=platform,
            )
        )
        self.session.commit()
        return seconds

    # ----------------------------------------------------------------- events

    def log_event(self, kind: str, message: str, commit: bool = True) -> None:
        self.session.add(EventLog(kind=kind, message=message[:1024]))
        if commit:
            self.session.commit()

    def online_players(self) -> list[Player]:
        players = self.session.scalars(
            select(Player).options(selectinload(Player.identities))
        ).all()
        return [p for p in players if p.is_idling]
