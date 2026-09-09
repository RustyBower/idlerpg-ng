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
import secrets
import string
from datetime import timedelta
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from .auth import hash_password, verify_password
from datetime import timezone

from .models import (
    Alignment,
    LinkCode,
    Setting,
    EventLog,
    Item,
    PenaltyRecord,
    Platform,
    PlatformIdentity,
    Player,
    Presence,
    utcnow,
)
from . import events
from .events import Outcome
from .rules import Curve, Penalty, penalty_seconds, ttl

log = logging.getLogger(__name__)

MAP_X = int(os.environ.get("MAP_X", "500"))
MAP_Y = int(os.environ.get("MAP_Y", "500"))


def events_map_x() -> int:
    return MAP_X


def events_map_y() -> int:
    return MAP_Y

ITEM_SLOTS = (
    "amulet", "charm", "helm", "boots", "gloves",
    "ring", "leggings", "shield", "tunic", "weapon",
)


@dataclass
class LevelUp:
    player: str
    level: int
    next_ttl: int


BATTLE_CHECK_SECONDS = 3600  # the original challenges someone once an hour


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

    def tick(self, elapsed_seconds: float) -> list[Outcome]:
        """Advance the world by ``elapsed_seconds``.

        Every player idling on at least one platform spends that many seconds
        off their timer - once, no matter how many platforms they are on - and
        then the world gets its turn: items found, blessings, calamities,
        battles and drifting across the map.
        """
        if elapsed_seconds <= 0:
            return []

        players = self.session.scalars(
            select(Player)
            .options(selectinload(Player.identities), selectinload(Player.items))
        ).all()

        announcements: list[Outcome] = []
        online = [p for p in players if p.is_idling]

        for player in online:
            remaining = player.next_ttl - elapsed_seconds
            while remaining <= 0:
                player.level += 1
                remaining += int(ttl(player.level, self.curve))
                announcements.append(Outcome(
                    f"{player.name}, the {player.character_class or 'nameless'}, "
                    f"has attained level {player.level}!",
                    kind="levelup",
                ))
                # Levelling is when the original hands out loot.
                found = events.find_item(player, self.rng)
                if found:
                    announcements.append(found)
            player.next_ttl = int(remaining)

        announcements.extend(self._world_events(online, elapsed_seconds))

        for player in online:
            events.move_player(player, events_map_x(), events_map_y(), self.rng)

        for outcome in announcements:
            self.log_event(outcome.kind, outcome.message, commit=False)
        self.session.commit()
        return announcements

    def _world_events(self, online: list[Player],
                      elapsed: float) -> list[Outcome]:
        """Roll the periodic events, weighted by how many people are around."""
        out: list[Outcome] = []
        if not online:
            return out
        count = len(online)

        if events.should_fire(events.HOG_INTERVAL, elapsed, count, self.rng):
            out.append(events.hand_of_god(self.rng.choice(online), self.rng))
        if events.should_fire(events.CALAMITY_INTERVAL, elapsed, count, self.rng):
            out.append(events.calamity(self.rng.choice(online), self.rng))
        if events.should_fire(events.GODSEND_INTERVAL, elapsed, count, self.rng):
            out.append(events.godsend(self.rng.choice(online), self.rng))

        # Battles are checked on the hour in the original rather than rolled
        # continuously, so scale a once-an-hour chance by the tick length.
        if len(online) > 1 and self.rng.random() < elapsed / BATTLE_CHECK_SECONDS:
            challenger = self.rng.choice(online)
            # Below level 25 most challenges are declined, as in bot.pl.
            if challenger.level >= 25 or self.rng.randrange(4) < 1:
                opponent = self.rng.choice([p for p in online if p is not challenger])
                out.extend(events.battle(challenger, opponent, self.rng))
        return out

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

    # ------------------------------------------------------------ linking

    LINK_CODE_TTL = timedelta(minutes=15)

    def issue_link_code(self, player: Player) -> str:
        """Mint a code the player can redeem on another platform."""
        alphabet = string.ascii_uppercase + string.digits
        code = "".join(secrets.choice(alphabet) for _ in range(8))
        self.session.query(LinkCode).filter(
            LinkCode.player_id == player.id
        ).delete()
        self.session.add(
            LinkCode(
                code=code,
                player_id=player.id,
                expires=utcnow() + self.LINK_CODE_TTL,
            )
        )
        self.session.commit()
        return code

    def redeem_link_code(self, code: str, platform: Platform,
                         external_id: str, display_name: str = "") -> Player:
        entry = self.session.scalar(
            select(LinkCode).where(LinkCode.code == code.strip().upper())
        )
        if entry is None:
            raise RegistrationError("that code is not valid")
        expires = entry.expires
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)
        if expires < utcnow():
            self.session.delete(entry)
            self.session.commit()
            raise RegistrationError("that code has expired")
        player = self.session.get(Player, entry.player_id)
        self.link(player, platform, external_id, display_name)
        self.session.delete(entry)
        self.session.commit()
        return player

    # --------------------------------------------------------------- settings

    def get_setting(self, key: str) -> str | None:
        row = self.session.get(Setting, key)
        return row.value if row else None

    def set_setting(self, key: str, value: str) -> None:
        row = self.session.get(Setting, key)
        if row is None:
            self.session.add(Setting(key=key, value=value))
        else:
            row.value = value
        self.session.commit()

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
