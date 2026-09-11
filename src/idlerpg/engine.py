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
import time
from dataclasses import dataclass

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session, selectinload

from .auth import hash_password, verify_password
from .text import check_class, check_name, duration, safe

from .models import (
    Alignment,
    Ethos,
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
from . import events, npcs, quests
from .events import Outcome
from .rules import Curve, Penalty, penalty_seconds, ttl

log = logging.getLogger(__name__)

# One setting for the realm's size, read by the website too. IRPG_MAPX and
# IRPG_MAPY are the names the site used to read on its own; they still work.
MAP_X = int(os.environ.get("MAP_X") or os.environ.get("IRPG_MAPX") or 500)
MAP_Y = int(os.environ.get("MAP_Y") or os.environ.get("IRPG_MAPY") or 500)


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
    "Two parts, law first: lawful, neutral or chaotic, then good, neutral or "
    "evil - or one word to change one part. Good: now and then good players "
    "pray together for time off, though they land the fewest critical hits. "
    "Evil: the most critical hits, and now and then steal a better item from "
    "a good player or pay your dark patron. "
    "Lawful: 10% smaller penalties, calamities and godsends half as hard, "
    "likelier to be chosen for quests. Chaotic: calamities and godsends half "
    "as hard again, will fight anyone, and the odd random event. True neutral: "
    "tugged now and then toward the realm's middle level."
)


class Engine:
    # Settings an admin can change, kept in the database so a restart keeps them.
    PAUSED_KEY, SILENT_KEY, TOPIC_KEY = "paused", "silent", "topic_note"

    def __init__(self, session: Session, curve: Curve | None = None,
                 rng: random.Random | None = None):
        self.session = session
        self.curve = curve or Curve()
        self.rng = rng or random.Random()
        self.started = time.time()
        # Names that are always admins, from the deployment (IDLERPG_ADMINS).
        self.owners: frozenset[str] = frozenset()
        # Asked for by admins, and acted on by the clock outside the engine.
        self.restart_requested = False
        self.topic_requested = False
        # Things that happened outside a tick (a quest failing because someone
        # spoke) and still need announcing on the next one.
        self._pending: list[Outcome] = []
        # NPCs top the realm up to npc_realm characters, npc_max of them at
        # most (NPC_REALM, NPC_MAX); none unless configured. See npcs.py.
        self.npc_max = 0
        self.npc_realm = 12
        self.npc_wait = 0.0

    # ---------------------------------------------------------------- players

    def register(self, name: str, password: str, character_class: str,
                 platform: Platform, external_id: str) -> Player:
        try:
            name = check_name(name)
            character_class = check_class(character_class)
        except ValueError as exc:
            raise RegistrationError(str(exc)) from None
        if self.find_player(name) is not None:
            raise RegistrationError(f"{name} is already taken")
        if name.casefold() in self.owners:
            # An owner's character was deleted; whoever takes the name must
            # not inherit the admin rights that come with it.
            raise RegistrationError("that name is reserved")
        if self.find_identity(platform, external_id) is not None:
            raise RegistrationError("that account already has a character")

        x, y = events.spawn_point(MAP_X, MAP_Y, self.rng)
        player = Player(
            name=name,
            password_hash=hash_password(password),
            character_class=character_class.strip()[:64],
            level=0,
            next_ttl=int(ttl(0, self.curve)),
            alignment=Alignment.NEUTRAL,
            last_login=utcnow(),
            x=x,
            y=y,
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
            # Exact but case-blind. ilike() treated _ and % in a name as
            # wildcards, so "r_sty" collided with "rusty" and "p%" matched
            # whoever came first.
            .where(func.lower(Player.name) == name.strip().lower())
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
        if self.paused:
            # Nothing moves, but what admins queued still gets said.
            said, self._pending = list(self._pending), []
            for outcome in said:
                self.log_event(outcome.kind, outcome.message, commit=False)
            self.session.commit()
            return said

        players = self.session.scalars(
            select(Player)
            .options(selectinload(Player.identities), selectinload(Player.items))
        ).all()
        # The realm's own characters come, go and talk before anyone earns.
        self._pending.extend(npcs.tend(self, players, elapsed_seconds))

        announcements: list[Outcome] = list(self._pending)
        self._pending.clear()
        online = [p for p in players if p.is_idling]

        for player in online:
            remaining = player.next_ttl - elapsed_seconds
            while remaining <= 0:
                player.level += 1
                remaining += int(events.level_cost(player, player.level, self.curve))
                announcements.append(Outcome(
                    f"{player.name} the {player.character_class or 'wanderer'} "
                    f"reaches level {player.level}! "
                    f"Next level in {duration(remaining)}.",
                    kind="levelup",
                ))
                # Levelling is when the original hands out loot.
                found = events.find_item(player, self.rng)
                if found:
                    announcements.append(found)
            player.next_ttl = int(remaining)

        announcements.extend(self._world_events(online, elapsed_seconds))

        # A journey's party walks toward its waypoint; everyone else drifts.
        walked = quests.steer(self.session, elapsed_seconds, self.rng)
        for player in online:
            if player.id not in walked:
                events.move_player(player, events_map_x(), events_map_y(), self.rng)

        announcements.extend(self._quest_events(online, elapsed_seconds))

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

        # The law-chaos axis has events of its own: chaos finds the chaotic,
        # and the realm's balance tugs at the truly neutral.
        chaotic = [p for p in online if p.ethos is Ethos.CHAOTIC]
        if events.should_fire(events.CHAOS_INTERVAL, elapsed, len(chaotic), self.rng):
            out.append(events.chaos(self.rng.choice(chaotic), self.rng))
        balanced = [p for p in online
                    if p.ethos is Ethos.NEUTRAL and p.alignment is Alignment.NEUTRAL]
        if events.should_fire(events.BALANCE_INTERVAL, elapsed, len(balanced), self.rng):
            out.extend(events.balance(self.rng.choice(balanced), online, self.rng))

        # Battles are checked on the hour in the original rather than rolled
        # continuously, so scale a once-an-hour chance by the tick length.
        if len(online) > 1 and self.rng.random() < elapsed / BATTLE_CHECK_SECONDS:
            challenger = self.rng.choice(online)
            # Below level 25 most challenges are declined, as in bot.pl.
            if events.will_fight(challenger, self.rng):
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
        if self.paused:
            return 0

        seconds = penalty_seconds(
            kind, player.level, self.curve, message_length=message_length
        )
        seconds = events.scale_penalty(player, seconds)
        player.next_ttl += seconds
        if kind is not Penalty.QUEST:
            # A quester who talks, parts or quits fails it for the party.
            self._pending.extend(quests.fail(self.session, player, self.curve))
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

    # --------------------------------------------------------------- accounts

    def change_password(self, player: Player, current: str, new: str) -> None:
        """Change a password, given the current one.

        Asked for even when logged in: an IRC login can be resumed from a
        remembered address, and that alone must not be enough to lock the
        owner out.
        """
        if not verify_password(current, player.password_hash):
            raise RegistrationError("that is not the current password")
        if not new:
            raise RegistrationError("the new password cannot be empty")
        player.password_hash = hash_password(new)
        self.session.commit()

    def remove_player(self, player: Player, password: str) -> None:
        """Delete a character for good, given its password, and tell the realm.

        A quester leaving this way fails the quest, as quitting would.
        """
        if not verify_password(password, player.password_hash):
            raise RegistrationError("wrong password")
        self.delete_player(player, f"{player.name} has left the realm for good.")

    def delete_player(self, player: Player, farewell: str) -> None:
        """Delete a character and say ``farewell``. A quester leaving fails
        the quest, as quitting would."""
        self._pending.extend(quests.fail(self.session, player, self.curve))
        self.session.delete(player)
        self.session.commit()
        self._pending.append(Outcome(farewell, kind="remove"))

    # ------------------------------------------------------------------ admin

    @property
    def paused(self) -> bool:
        return self.get_setting(self.PAUSED_KEY) == "1"

    @property
    def silent(self) -> bool:
        return self.get_setting(self.SILENT_KEY) == "1"

    def apply_owners(self, names) -> None:
        """Make the deployment's named characters admins. Others made admin
        with MKADMIN stay so; only owners cannot be demoted."""
        self.owners = frozenset(n.strip().casefold() for n in names if n.strip())
        for player in self.session.scalars(select(Player)):
            if player.name.casefold() in self.owners and not player.is_admin:
                player.is_admin = True
                log.info("%s is an admin by IDLERPG_ADMINS", player.name)
        self.session.commit()

    def is_owner(self, player: Player) -> bool:
        return player.name.casefold() in self.owners

    def all_players(self) -> list[Player]:
        return list(self.session.scalars(
            select(Player).options(selectinload(Player.identities))))

    def announce(self, outcomes) -> None:
        """Queue outcomes for every platform, at the next tick."""
        self._pending.extend(outcomes)
        self.session.commit()

    def set_admin(self, player: Player, admin: bool) -> None:
        player.is_admin = admin
        self.session.commit()

    def reset_password(self, player: Player, new: str) -> None:
        if not new:
            raise RegistrationError("the new password cannot be empty")
        player.password_hash = hash_password(new)
        self.session.commit()

    def rename(self, player: Player, new: str) -> str:
        try:
            name = check_name(new)
        except ValueError as exc:
            raise RegistrationError(str(exc)) from None
        other = self.find_player(name)
        if other is not None and other.id != player.id:
            raise RegistrationError(f"{name} is already taken")
        old, player.name = player.name, name
        self.session.commit()
        self._pending.append(Outcome(f"{old} is now known as {name}.", kind="rename"))
        return name

    def set_class(self, player: Player, text: str) -> None:
        try:
            player.character_class = check_class(text)
        except ValueError as exc:
            raise RegistrationError(str(exc)) from None
        self.session.commit()

    def push(self, player: Player, seconds: int) -> None:
        """Move a timer: positive toward the next level, negative away."""
        player.next_ttl = max(1, player.next_ttl - seconds)
        self.session.commit()
        way = "toward" if seconds >= 0 else "away from"
        self._pending.append(Outcome(
            f"The gods' hand moves {player.name} {duration(abs(seconds))} {way} "
            f"level {player.level + 1}.", kind="push"))

    def move(self, player: Player, x: int, y: int) -> None:
        if not (0 <= x < MAP_X and 0 <= y < MAP_Y):
            raise ValueError(f"the realm is {MAP_X} by {MAP_Y}")
        player.x, player.y = x, y
        self.session.commit()

    def record_login(self, player: Player, platform: Platform,
                     announce: bool = True) -> None:
        """Note a login, and unless it is only a resumption, tell the realm.

        Resumptions after a restart are not announced, or every restart would
        read out the whole channel.
        """
        player.last_login = utcnow()
        self.session.commit()
        if announce:
            self._pending.append(Outcome(
                f"{player.name}, the level {player.level} "
                f"{player.character_class or 'wanderer'}, is now online from "
                f"{platform.value}. Next level in {duration(player.next_ttl)}.",
                kind="login",
            ))

    # -------------------------------------------------------------- alignment

    def set_alignment(self, player: Player, choice: str) -> str:
        """Change either part of a player's alignment; returns its new name.

        "lawful good" sets both parts, law first. One word changes one part:
        "chaotic" the law, "evil" the morals - and "neutral" alone the morals,
        as ALIGN always did. "true neutral" sets both to neutral. Free, and
        announced to the realm; choosing what you already are announces
        nothing.
        """
        morals = {a.value: a for a in Alignment}
        ethics = {e.value: e for e in Ethos}
        alignment, ethos = player.alignment, player.ethos or Ethos.NEUTRAL
        words = choice.lower().split()
        if words == ["true", "neutral"]:
            alignment, ethos = Alignment.NEUTRAL, Ethos.NEUTRAL
        elif len(words) == 1 and words[0] in morals:
            alignment = morals[words[0]]
        elif len(words) == 1 and words[0] in ethics:
            ethos = ethics[words[0]]
        elif len(words) == 2 and words[0] in ethics and words[1] in morals:
            ethos, alignment = ethics[words[0]], morals[words[1]]
        else:
            raise RegistrationError(
                "try lawful good, chaotic, evil or true neutral - law comes first")
        if (alignment, ethos) == (player.alignment, player.ethos):
            return player.alignment_name
        player.alignment, player.ethos = alignment, ethos
        self.session.commit()
        self._pending.append(Outcome(
            f"{player.name} is now {player.alignment_name}.", kind="alignment",
        ))
        return player.alignment_name

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
        self.session.add(EventLog(kind=kind, message=safe(message)[:1024]))
        if commit:
            self.session.commit()

    def top_players(self, count: int = 3) -> list[Player]:
        """Highest level first, then whoever is closest to the next one."""
        return list(self.session.scalars(
            select(Player).order_by(Player.prestige.desc(), Player.level.desc(),
                                    Player.next_ttl.asc())
            .limit(count)
        ).all())

    def online_players(self) -> list[Player]:
        players = self.session.scalars(
            select(Player).options(selectinload(Player.identities))
        ).all()
        return [p for p in players if p.is_idling]
