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

from sqlalchemy import select, update
from sqlalchemy.orm import Session, selectinload

from .auth import hash_password, verify_password

from .models import (
    Alignment,
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
from . import events, quests
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

# Penalties for going away, as opposed to for speaking. These exist to punish
# leaving the game; a player who is still present on another linked platform
# has not left it, they have changed client.
DEPARTURE_PENALTIES = frozenset(
    {Penalty.PART, Penalty.QUIT, Penalty.KICK, Penalty.LOGOUT}
)


class RegistrationError(Exception):
    pass


# What each alignment does, for the help text on both platforms. Kept next to
# set_alignment rather than in an adapter so the two cannot drift apart.
ALIGNMENT_HELP = (
    "good: more critical hits, and now and then two good players pray "
    "together for time off; evil: fewer critical hits, and now and then you "
    "steal a better item from a good player - or your god adds to your clock; "
    "neutral: in between, and neither event."
)


class Engine:
    def __init__(self, session: Session, curve: Curve | None = None,
                 rng: random.Random | None = None):
        self.session = session
        self.curve = curve or Curve()
        self.rng = rng or random.Random()
        # Things that happened outside a tick (a quest failing because someone
        # spoke) and still need announcing on the next one.
        self._pending: list[Outcome] = []

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
        # Queued rather than announced here, so it reaches every platform
        # instead of only the one the player happened to register on.
        self._pending.append(Outcome(
            f"{player.name}, the {player.character_class or 'nameless'}, "
            f"joins the realm from {platform.value}!",
            kind="register",
        ))
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

    def set_player_presence(self, player: Player, platform: Platform,
                            presence: Presence) -> None:
        """Move every one of the player's identities on ``platform`` together.

        A merge can leave a character holding two identities on one platform.
        On IRC both describe the same connection, so they must go on and
        offline as one, or the stale one keeps earning after the player leaves.
        """
        changed = False
        for identity in player.identities:
            if identity.platform is platform and identity.presence is not presence:
                identity.presence = presence
                identity.presence_since = utcnow()
                changed = True
        if changed:
            self.session.commit()

    def remember_login(self, identity: PlatformIdentity,
                       mask: str | None) -> None:
        """Record the connection an identity is logged in from, or forget it.

        Stored rather than held by the adapter, so a login the bot never saw
        end - it restarted, or a netsplit took the player away - can be
        resumed. A connection belongs to one identity at a time: logging in as
        another character from it moves it there.
        """
        mask = mask.lower() if mask else None
        if mask is not None:
            self.session.execute(
                update(PlatformIdentity)
                .where(
                    PlatformIdentity.platform == identity.platform,
                    PlatformIdentity.login_mask == mask,
                    PlatformIdentity.id != identity.id,
                )
                .values(login_mask=None)
            )
        identity.login_mask = mask
        self.session.commit()

    def resume_login(self, platform: Platform,
                     mask: str) -> PlatformIdentity | None:
        """The identity last logged in from this connection, if any."""
        return self.session.scalar(
            select(PlatformIdentity).where(
                PlatformIdentity.platform == platform,
                PlatformIdentity.login_mask == mask.lower(),
            )
        )

    def reset_presence(self, platform: Platform) -> None:
        """Mark everyone offline on ``platform``.

        For an adapter that has just lost or regained its connection: whatever
        presence was recorded before describes a connection that is gone.
        """
        self.session.execute(
            update(PlatformIdentity)
            .where(
                PlatformIdentity.platform == platform,
                PlatformIdentity.presence != Presence.OFFLINE,
            )
            .values(presence=Presence.OFFLINE, presence_since=utcnow())
        )
        self.session.commit()

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

        announcements: list[Outcome] = list(self._pending)
        self._pending.clear()
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
        announcements.extend(self._quest_events(online, elapsed_seconds))

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

        if events.should_fire(events.TEAM_BATTLE_INTERVAL, elapsed, count, self.rng):
            out.extend(events.team_battle(
                online, self.rng, events_map_x(), events_map_y()))
        if events.should_fire(events.GOODNESS_INTERVAL, elapsed,
                              sum(1 for p in online if p.alignment.value == "good"),
                              self.rng):
            out.extend(events.goodness(online, self.rng))
        if events.should_fire(events.EVILNESS_INTERVAL, elapsed,
                              sum(1 for p in online if p.alignment.value == "evil"),
                              self.rng):
            out.extend(events.evilness(online, self.rng))
        # War is not weighted by headcount in the original: the realm gets one
        # on its own schedule regardless of how many are about.
        if events.should_fire(events.WAR_INTERVAL, elapsed, 1, self.rng):
            out.extend(events.war(online, self.rng, events_map_x(), events_map_y()))

        # Battles are checked on the hour in the original rather than rolled
        # continuously, so scale a once-an-hour chance by the tick length.
        if len(online) > 1 and self.rng.random() < elapsed / BATTLE_CHECK_SECONDS:
            challenger = self.rng.choice(online)
            # Below level 25 most challenges are declined, as in bot.pl.
            if challenger.level >= 25 or self.rng.randrange(4) < 1:
                opponent = self.rng.choice([p for p in online if p is not challenger])
                out.extend(events.battle(challenger, opponent, self.rng))
        return out

    def _quest_events(self, online: list[Player], elapsed: float) -> list[Outcome]:
        """Start a quest if there is none, otherwise move the current one on."""
        out: list[Outcome] = []
        quest = quests.active_quest(self.session)
        if quest is None:
            # Roughly one attempt per six hours, matching the original cooldown.
            if self.rng.random() < elapsed / quests.COOLDOWN.total_seconds():
                started = quests.start(
                    self.session, online, self.rng, events_map_x(), events_map_y()
                )
                if started:
                    out.append(started)
        else:
            out.extend(quests.advance(self.session, quest, self.rng))
        return out

    # -------------------------------------------------------------- penalties

    def still_present_elsewhere(self, player: Player,
                                platform: Platform | None) -> bool:
        """Is this player still on some platform other than ``platform``?"""
        if platform is None:
            return False
        return any(
            identity.platform is not platform
            and identity.presence in (Presence.ACTIVE, Presence.AWAY)
            for identity in player.identities
        )

    def penalise(self, player: Player, kind: Penalty, *,
                 message_length: int | None = None,
                 platform: Platform | None = None) -> int:
        # Leaving one platform while still on another is not leaving. Without
        # this, linking two accounts doubles a player's exposure to departure
        # penalties while earning them nothing extra, which makes playing from
        # both strictly worse than playing from one - the opposite of the point.
        if kind in DEPARTURE_PENALTIES and self.still_present_elsewhere(player, platform):
            return 0

        seconds = penalty_seconds(
            kind, player.level, self.curve, message_length=message_length
        )
        player.next_ttl += seconds
        if kind is not Penalty.QUEST:
            # A quester who talks, parts or quits fails it for everyone.
            self._pending.extend(quests.fail(self.session, player))
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

    # ---------------------------------------------------------------- merging

    def merge_by_password(self, keeper: Player, name: str,
                          password: str) -> Outcome:
        """Fold the named character into ``keeper``, given its password.

        The password is the proof of ownership, exactly as for logging in, so
        nobody can fold someone else's progress into their own.
        """
        absorb = self.authenticate(name, password)
        if absorb is None:
            raise RegistrationError("wrong name or password")
        return self.merge(keeper, absorb)

    # -------------------------------------------------------------- alignment

    def set_alignment(self, player: Player, choice: str) -> Alignment:
        """Change a player's alignment, as the original ALIGN did.

        Free, and announced to the realm - on every platform, so queued rather
        than returned. Choosing the alignment you already have announces
        nothing.
        """
        alignment = {a.value: a for a in Alignment}.get(choice.strip().lower())
        if alignment is None:
            raise RegistrationError("choose good, neutral or evil")
        if alignment is player.alignment:
            return alignment
        player.alignment = alignment
        self.session.commit()
        self._pending.append(Outcome(
            f"{player.name} has changed alignment to: {alignment.value}.",
            kind="alignment",
        ))
        return alignment

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

    def merge(self, keep: Player, absorb: Player) -> Outcome:
        """Fold ``absorb`` into ``keep``, taking the better of the two.

        Progress is combined by maximum, never by sum. Summing would make
        registering twice a way to advance faster, which is exactly the
        farming the one-character-many-identities model exists to prevent.
        """
        if keep.id == absorb.id:
            raise RegistrationError("that is the same character")

        keep.level = max(keep.level, absorb.level)
        # Lower is better: it is time remaining, not time earned.
        keep.next_ttl = min(keep.next_ttl, absorb.next_ttl)
        keep.character_class = keep.character_class or absorb.character_class

        by_slot = {i.slot: i for i in keep.items}
        for item in absorb.items:
            mine = by_slot.get(item.slot)
            if mine is None:
                keep.items.append(Item(slot=item.slot, value=item.value, tag=item.tag))
            elif item.value > mine.value:
                mine.value, mine.tag = item.value, item.tag

        # Reparent with a direct UPDATE rather than through the collections.
        # identities cascades delete-orphan, so removing one from absorb marks
        # it deleted immediately and it cannot be re-appended; expiring the
        # stale collection afterwards leaves nothing for the cascade to take.
        self.session.execute(
            update(PlatformIdentity)
            .where(PlatformIdentity.player_id == absorb.id)
            .values(player_id=keep.id)
        )
        self.session.expire(absorb, ["identities"])
        self.session.expire(keep, ["identities"])

        name = absorb.name
        self.session.delete(absorb)
        self.session.commit()
        return Outcome(
            f"{name} has been folded into {keep.name}, who now plays from "
            f"both sides of the bridge.",
            kind="merge",
        )

    # ----------------------------------------------------------------- events

    def log_event(self, kind: str, message: str, commit: bool = True) -> None:
        self.session.add(EventLog(kind=kind, message=message[:1024]))
        if commit:
            self.session.commit()

    def top_players(self, count: int = 3) -> list[Player]:
        """Highest level first, then whoever is closest to the next one."""
        return list(self.session.scalars(
            select(Player).order_by(Player.level.desc(), Player.next_ttl.asc())
            .limit(count)
        ).all())

    def online_players(self) -> list[Player]:
        players = self.session.scalars(
            select(Player).options(selectinload(Player.identities))
        ).all()
        return [p for p in players if p.is_idling]
