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
import json
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
    inspect,
    text,
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


class Ethos(str, enum.Enum):
    """The law-chaos axis, beside Alignment's good-evil one. Together they
    make the nine alignments; neutral on both is true neutral."""

    LAWFUL = "lawful"
    NEUTRAL = "neutral"
    CHAOTIC = "chaotic"


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
    # Stored as text rather than a database enum, so upgrade() can add it to
    # a table that already exists with a plain ALTER.
    ethos: Mapped[Ethos] = mapped_column(
        Enum(Ethos, native_enum=False, length=16), default=Ethos.NEUTRAL
    )
    # Prestige: how many times the character has started over, the points
    # those fresh starts earned and not yet spent, and the perks bought with
    # them, as {"swiftness": 2, ...}. See prestige.py.
    prestige: Mapped[int] = mapped_column(Integer, default=0)
    points: Mapped[int] = mapped_column(Integer, default=0)
    perks: Mapped[str] = mapped_column(String(255), default="{}")
    is_admin: Mapped[bool] = mapped_column(default=False)
    # None for a person. For the realm's own characters, the NPCs, where they
    # are: present, away or benched. See npcs.py.
    npc: Mapped[str | None] = mapped_column(String(16), default=None)
    # FIGHT, in Unix seconds: when this character may next challenge
    # someone, and until when nobody may challenge them. See fights.py.
    fight_ready_at: Mapped[int] = mapped_column(BigInteger, default=0)
    shield_until: Mapped[int] = mapped_column(BigInteger, default=0)
    # A title earned with a season's meta achievement, worn beside the name.
    title: Mapped[str | None] = mapped_column(String(64), default=None)
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
    achievements: Mapped[list["Achievement"]] = relationship(cascade="all, delete-orphan")
    keepsakes: Mapped[list["Keepsake"]] = relationship(cascade="all, delete-orphan")
    tallies: Mapped[list["Tally"]] = relationship(cascade="all, delete-orphan")

    @property
    def is_idling(self) -> bool:
        """True when the player earns time.

        The cross-platform rule: present and silent on **at least one** linked
        platform. Being on two platforms neither punishes the player nor pays
        them twice, because the engine credits the character, not a connection.
        An NPC has no platforms; it earns while present.
        """
        if self.npc:
            return self.npc == "present"
        return any(
            identity.presence in (Presence.ACTIVE, Presence.AWAY)
            for identity in self.identities
        )

    @property
    def alignment_name(self) -> str:
        """'lawful good', 'chaotic neutral', 'true neutral' and so on."""
        ethos = (self.ethos or Ethos.NEUTRAL).value
        moral = self.alignment.value
        return "true neutral" if ethos == moral == "neutral" else f"{ethos} {moral}"

    def perk_rank(self, name: str) -> int:
        """How many ranks of a perk this character has bought."""
        return int(json.loads(self.perks or "{}").get(name, 0))

    def set_perk_rank(self, name: str, rank: int) -> None:
        ranks = json.loads(self.perks or "{}")
        ranks[name] = rank
        self.perks = json.dumps(ranks, sort_keys=True)


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
    # The connection this identity is logged in from, kept across restarts so
    # the login can be resumed: nick!user@host on IRC. Discord needs none - its
    # account id is already durable, and the role says who is playing.
    login_mask: Mapped[str | None] = mapped_column(String(255), default=None)

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


class GroundItem(Base):
    """An item lying on the map, waiting for someone to come across it.

    Not tied to a player: it is the item somebody replaced, left where they
    were standing. No unique constraint either - one tile can hold several,
    and the same slot can be lying about in a dozen places at once.
    """

    __tablename__ = "ground_item"

    id: Mapped[int] = mapped_column(primary_key=True)
    slot: Mapped[str] = mapped_column(String(32))
    value: Mapped[int] = mapped_column(Integer, default=0)
    tag: Mapped[str] = mapped_column(String(32), default="")
    x: Mapped[int] = mapped_column(Integer, default=0)
    y: Mapped[int] = mapped_column(Integer, default=0)
    # A name rather than a foreign key: it is flavour, and it has to survive
    # whoever dropped it leaving the realm for good.
    left_by: Mapped[str] = mapped_column(String(64), default="")
    dropped: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


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


class Achievement(Base):
    """An honour a character has earned - for now, a season's. See seasonal.py."""

    __tablename__ = "achievement"
    __table_args__ = (UniqueConstraint("player_id", "key", name="uq_achievement"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    player_id: Mapped[int] = mapped_column(ForeignKey("player.id", ondelete="CASCADE"))
    key: Mapped[str] = mapped_column(String(64))       # "Hallowtide 2026:1", ":kept"
    badge: Mapped[str] = mapped_column(String(16))
    title: Mapped[str] = mapped_column(String(128))
    earned: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Keepsake(Base):
    """A season's collectable - a mask, a gift, an egg's surprise. For show."""

    __tablename__ = "keepsake"
    __table_args__ = (UniqueConstraint("player_id", "name", name="uq_keepsake"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    player_id: Mapped[int] = mapped_column(ForeignKey("player.id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(String(64))
    season: Mapped[str] = mapped_column(String(32))
    found: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Tally(Base):
    """A running count toward an achievement: fights won, eggs found..."""

    __tablename__ = "tally"
    __table_args__ = (UniqueConstraint("player_id", "key", name="uq_tally"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    player_id: Mapped[int] = mapped_column(ForeignKey("player.id", ondelete="CASCADE"))
    key: Mapped[str] = mapped_column(String(64))
    value: Mapped[int] = mapped_column(BigInteger, default=0)


class SeasonMark(Base):
    """Where a character stood as a season began, to measure what they made
    of it. Cleared when the season ends."""

    __tablename__ = "season_mark"
    __table_args__ = (UniqueConstraint("season", "player_id", name="uq_season_mark"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    season: Mapped[str] = mapped_column(String(64))
    player_id: Mapped[int] = mapped_column(ForeignKey("player.id", ondelete="CASCADE"))
    progress: Mapped[int] = mapped_column(BigInteger, default=0)


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
    # Who it was about, and for a level-up the level reached: the history
    # the player page charts.
    player_id: Mapped[int | None] = mapped_column(Integer, default=None)
    level: Mapped[int | None] = mapped_column(Integer, default=None)
    at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, server_default=func.now()
    )


# Columns added after their table first shipped. create_all makes missing
# tables but never alters an existing one, so these are added by hand.
ADDED_COLUMNS = [
    ("platform_identity", "login_mask", "VARCHAR(255)"),
    # Enum names, as SQLAlchemy stores them; existing characters start neutral.
    ("player", "ethos", "VARCHAR(16) DEFAULT 'NEUTRAL'"),
    ("player", "prestige", "INTEGER DEFAULT 0"),
    ("player", "points", "INTEGER DEFAULT 0"),
    ("player", "perks", "VARCHAR(255) DEFAULT '{}'"),
    ("player", "npc", "VARCHAR(16)"),
    ("player", "fight_ready_at", "BIGINT DEFAULT 0"),
    ("player", "shield_until", "BIGINT DEFAULT 0"),
    ("player", "title", "VARCHAR(64)"),
    ("event_log", "player_id", "INTEGER"),
    ("event_log", "level", "INTEGER"),
]


def upgrade(db) -> None:
    """Bring an existing database up to the current models.

    Safe on every start and from two processes at once - the bot and the
    website both run it, and either may start first against an old database.
    """
    inspector = inspect(db)
    tables = set(inspector.get_table_names())
    guard = "IF NOT EXISTS " if db.dialect.name == "postgresql" else ""
    with db.begin() as conn:
        for table, column, ddl_type in ADDED_COLUMNS:
            if table not in tables:
                continue  # create_all makes it whole
            if column in {c["name"] for c in inspector.get_columns(table)}:
                continue
            conn.execute(text(
                f"ALTER TABLE {table} ADD COLUMN {guard}{column} {ddl_type}"
            ))
