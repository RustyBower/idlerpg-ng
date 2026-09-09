"""Database schema.

The shape here is the whole reason for the rewrite. The original bot kept every
player in one flat file that it rewrote wholesale, keyed by IRC nick, which
makes a player and an IRC connection the same thing. That cannot represent
someone who plays from both IRC and Discord.

Here a Player is the character, and PlatformIdentity rows attach accounts on
each platform to it. One human is one character wherever they connect.
"""

from __future__ import annotations

import enum
from datetime import datetime, timezone

from sqlalchemy import (
    BigInteger,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class Platform(str, enum.Enum):
    IRC = "irc"
    DISCORD = "discord"


class Presence(str, enum.Enum):
    """How present a player is on one platform.

    ACTIVE means connected and quiet, which is what earns time. AWAY covers an
    IRC user marked away or a Discord user whose presence is idle: still
    connected, still counts. OFFLINE earns nothing.
    """

    ACTIVE = "active"
    AWAY = "away"
    OFFLINE = "offline"


class Alignment(str, enum.Enum):
    GOOD = "good"
    NEUTRAL = "neutral"
    EVIL = "evil"


class Player(Base):
    __tablename__ = "player"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(64), unique=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    character_class: Mapped[str] = mapped_column(String(64), default="")
    level: Mapped[int] = mapped_column(Integer, default=0)
    # Seconds of idling still owed before the next level.
    next_ttl: Mapped[int] = mapped_column(BigInteger, default=600)
    alignment: Mapped[Alignment] = mapped_column(
        Enum(Alignment), default=Alignment.NEUTRAL
    )
    is_admin: Mapped[bool] = mapped_column(default=False)
    # Position in the realm, for the map and journey quests.
    x: Mapped[int] = mapped_column(Integer, default=0)
    y: Mapped[int] = mapped_column(Integer, default=0)
    created: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_login: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    identities: Mapped[list["PlatformIdentity"]] = relationship(
        back_populates="player", cascade="all, delete-orphan"
    )
    items: Mapped[list["Item"]] = relationship(
        back_populates="player", cascade="all, delete-orphan"
    )

    @property
    def is_idling(self) -> bool:
        """True when the player earns time.

        The cross-platform rule: present and silent on **at least one** linked
        platform. Being on two platforms neither punishes the player nor pays
        them twice, because the engine credits the character, not a connection.
        """
        return any(
            identity.presence in (Presence.ACTIVE, Presence.AWAY)
            for identity in self.identities
        )


class PlatformIdentity(Base):
    """An account on one platform, linked to a character."""

    __tablename__ = "platform_identity"
    __table_args__ = (
        # One Discord account cannot be two characters, and vice versa.
        UniqueConstraint("platform", "external_id", name="uq_platform_external_id"),
        Index("ix_identity_player", "player_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    player_id: Mapped[int] = mapped_column(ForeignKey("player.id", ondelete="CASCADE"))
    platform: Mapped[Platform] = mapped_column(Enum(Platform))
    # IRC account name, or Discord snowflake. Opaque to the engine.
    external_id: Mapped[str] = mapped_column(String(128))
    display_name: Mapped[str] = mapped_column(String(128), default="")
    presence: Mapped[Presence] = mapped_column(Enum(Presence), default=Presence.OFFLINE)
    presence_since: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )

    player: Mapped[Player] = relationship(back_populates="identities")


class Item(Base):
    __tablename__ = "item"
    __table_args__ = (
        UniqueConstraint("player_id", "slot", name="uq_item_player_slot"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    player_id: Mapped[int] = mapped_column(ForeignKey("player.id", ondelete="CASCADE"))
    slot: Mapped[str] = mapped_column(String(32))
    value: Mapped[int] = mapped_column(Integer, default=0)
    # Unique items carry a tag in the original ("a" for Mattt's Omniscience...).
    tag: Mapped[str] = mapped_column(String(32), default="")

    player: Mapped[Player] = relationship(back_populates="items")


class PenaltyRecord(Base):
    """Individual penalties, rather than the original's running totals.

    Keeping each one lets the engine explain why a timer moved, which the flat
    file could never do.
    """

    __tablename__ = "penalty"
    __table_args__ = (Index("ix_penalty_player_at", "player_id", "at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    player_id: Mapped[int] = mapped_column(ForeignKey("player.id", ondelete="CASCADE"))
    kind: Mapped[str] = mapped_column(String(32))
    seconds: Mapped[int] = mapped_column(BigInteger)
    platform: Mapped[Platform | None] = mapped_column(Enum(Platform))
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class LinkCode(Base):
    """A short-lived code for attaching a second platform to a character.

    Issued on a platform where the player is already authenticated, then
    redeemed on the new one. This is what keeps linking safe: you must already
    control the character to mint a code for it.
    """

    __tablename__ = "link_code"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(16), unique=True)
    player_id: Mapped[int] = mapped_column(ForeignKey("player.id", ondelete="CASCADE"))
    expires: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class Quest(Base):
    """The realm runs one quest at a time; this is it.

    Type 1 is a wait: the party simply has to stay online until the clock runs
    out. Type 2 is a journey to two map waypoints in turn. Either way a quester
    who talks, parts or quits fails it for everyone.
    """

    __tablename__ = "quest"

    id: Mapped[int] = mapped_column(primary_key=True)
    text: Mapped[str] = mapped_column(String(512))
    kind: Mapped[int] = mapped_column(Integer, default=1)   # 1 timed, 2 journey
    stage: Mapped[int] = mapped_column(Integer, default=1)  # journey only
    expires: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    started: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    # Journey waypoints, only meaningful for kind 2.
    x1: Mapped[int] = mapped_column(Integer, default=0)
    y1: Mapped[int] = mapped_column(Integer, default=0)
    x2: Mapped[int] = mapped_column(Integer, default=0)
    y2: Mapped[int] = mapped_column(Integer, default=0)

    participants: Mapped[list["QuestParticipant"]] = relationship(
        back_populates="quest", cascade="all, delete-orphan"
    )


class QuestParticipant(Base):
    __tablename__ = "quest_participant"
    __table_args__ = (UniqueConstraint("quest_id", "player_id", name="uq_quest_player"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    quest_id: Mapped[int] = mapped_column(ForeignKey("quest.id", ondelete="CASCADE"))
    player_id: Mapped[int] = mapped_column(ForeignKey("player.id", ondelete="CASCADE"))

    quest: Mapped[Quest] = relationship(back_populates="participants")
    player: Mapped[Player] = relationship()


class Setting(Base):
    """Small key/value store for things the bot must remember across restarts,
    such as which message is the Discord opt-in post."""

    __tablename__ = "setting"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(String(255))


class EventLog(Base):
    """Quests, battles, godsends and calamities, for the site and for replay."""

    __tablename__ = "event_log"
    __table_args__ = (Index("ix_event_at", "at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(32))
    message: Mapped[str] = mapped_column(String(1024))
    at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, server_default=func.now()
    )
