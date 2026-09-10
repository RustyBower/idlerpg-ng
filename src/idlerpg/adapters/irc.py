"""IRC adapter.

Translates IRC into engine calls: joins and parts become presence, channel
chatter and nick changes become penalties, and private messages to the bot are
commands. It holds no game rules of its own.

IRC has no stable per-user identifier without services, so a character's IRC
identity is keyed on the character's name when it first reaches IRC - at
REGISTER, or at LOGIN for a character registered on Discord - with the nick
currently bound to it kept in display_name. The adapter maps live nicks to
those identities for the duration of a connection.

That map dies with the bot, so each login's nick!user@host is also stored.
When the bot rejoins it asks WHO is in the channel and logs back in anyone
connected from a remembered mask, as the original bot's autologin did; a
player who turns up later from one is logged in as they join. Quitting,
parting, being kicked and LOGOUT end a login. A netsplit does not.
"""

from __future__ import annotations

import asyncio
import logging
import re
import ssl
from dataclasses import dataclass

from ..engine import Engine, RegistrationError
from ..models import Platform, Presence
from ..rules import Penalty

log = logging.getLogger(__name__)


@dataclass
class Message:
    prefix: str
    command: str
    params: list[str]

    @property
    def nick(self) -> str:
        return self.prefix.split("!", 1)[0] if self.prefix else ""

    @property
    def text(self) -> str:
        return self.params[-1] if self.params else ""


def parse(line: str) -> Message | None:
    if not line:
        return None
    prefix = ""
    if line.startswith(":"):
        prefix, _, line = line[1:].partition(" ")
    head, _, trailing = line.partition(" :")
    parts = head.split()
    if not parts:
        return None
    command, args = parts[0].upper(), parts[1:]
    if trailing or " :" in line:
        args = args + [trailing]
    return Message(prefix=prefix, command=command, params=args)


HELP = (
    "Stay connected and quiet to level up. "
    "REGISTER <name> <password> <class> | LOGIN <name> <password> (a Discord "
    "character too) | LOGOUT | WHOAMI | MERGE <name> <password> (fold another "
    "character of yours into this one)"
)


# A netsplit quits everyone behind it with the two server names as the reason.
# Users cannot fake one: servers prefix their own quit messages with "Quit:".
NETSPLIT = re.compile(r"^\S+\.\S+ \S+\.\S+$")


def renamed(prefix: str, new_nick: str) -> str | None:
    """The mask a connection has after changing nick."""
    if "!" not in prefix:
        return None
    return f"{new_nick}!{prefix.split('!', 1)[1]}"


class IRCAdapter:
    def __init__(self, engine: Engine, config):
        self.engine = engine
        self.cfg = config.irc
        self.tick_seconds = config.tick_seconds
        self.reader: asyncio.StreamReader | None = None
        self.writer: asyncio.StreamWriter | None = None
        # nick -> IRC identity external_id, for this connection only
        self.bound: dict[str, str] = {}
        # Only attempt self-registration once per connection.
        self.registration_attempted = False

    # ------------------------------------------------------------------ wire

    async def connect(self) -> None:
        context = None
        if self.cfg.tls:
            context = ssl.create_default_context()
            if not self.cfg.verify:
                context.check_hostname = False
                context.verify_mode = ssl.CERT_NONE
        self.reader, self.writer = await asyncio.open_connection(
            self.cfg.host, self.cfg.port, ssl=context
        )
        log.info("connected to %s:%s", self.cfg.host, self.cfg.port)
        self.send(f"NICK {self.cfg.nick}")
        self.send(f"USER {self.cfg.user} 0 * :{self.cfg.realname}")

    def send(self, line: str) -> None:
        if self.writer is None:
            return
        log.debug(">> %s", line)
        self.writer.write((line + "\r\n").encode("utf-8", "replace"))

    def notice(self, target: str, text: str) -> None:
        self.send(f"NOTICE {target} :{text}")

    def say(self, text: str) -> None:
        self.send(f"PRIVMSG {self.cfg.channel} :{text}")

    # --------------------------------------------------------------- helpers

    def character_for_nick(self, nick: str):
        # Bound to the identity rather than the character's name, so a nick
        # follows its character through a merge made from Discord.
        external = self.bound.get(nick.lower())
        return self.engine.player_for(Platform.IRC, external) if external else None

    def bind(self, nick: str, player, mask: str | None = None) -> None:
        """Attach ``nick`` to ``player`` and mark them present on IRC.

        Logging in is how a character reaches IRC, so one registered on Discord
        gains an IRC identity here. Without it they would show as logged in
        and earn nothing. ``mask`` is remembered so the login outlives the bot.
        """
        identity = next(
            (i for i in player.identities if i.platform is Platform.IRC), None
        )
        if identity is None:
            identity = self.engine.link(player, Platform.IRC, player.name, nick)
        self.bound[nick.lower()] = identity.external_id
        for irc_identity in player.identities:
            if irc_identity.platform is Platform.IRC:
                irc_identity.display_name = nick
        if mask:
            self.engine.remember_login(identity, mask)
        self.engine.set_player_presence(player, Platform.IRC, Presence.ACTIVE)

    def unbind(self, nick: str, penalty: Penalty | None = None,
               forget: bool = True) -> None:
        """Take ``nick`` offline and, unless ``forget`` is off, end its login.

        Leaving the bot could not see as the player's choice - a netsplit -
        keeps the login, so it resumes when they come back.
        """
        external = self.bound.get(nick.lower())
        player = self.character_for_nick(nick)
        if player is None:
            return
        if penalty is not None:
            self.engine.penalise(player, penalty, platform=Platform.IRC)
        if forget:
            identity = self.engine.find_identity(Platform.IRC, external)
            if identity is not None:
                self.engine.remember_login(identity, None)
        self.engine.set_player_presence(player, Platform.IRC, Presence.OFFLINE)
        self.bound.pop(nick.lower(), None)

    def resume(self, nick: str, mask: str) -> None:
        """Log ``nick`` back in if ``mask`` is a login the bot never saw end.

        The full mask rather than the nick: anyone can take a nick and run up
        its owner's penalties, but a bouncer keeps user@host stable.
        """
        if not mask or nick.lower() in self.bound:
            return
        identity = self.engine.resume_login(Platform.IRC, mask)
        if identity is None:
            return
        self.bind(nick, identity.player, mask)
        log.info("resumed %s as %s", nick, identity.player.name)

    # -------------------------------------------------------------- commands

    def handle_command(self, nick: str, text: str, mask: str = "") -> None:
        parts = text.strip().split()
        if not parts:
            return
        verb, args = parts[0].upper(), parts[1:]

        if verb == "HELP":
            self.notice(nick, HELP)
        elif verb == "REGISTER":
            if len(args) < 3:
                self.notice(nick, "REGISTER <name> <password> <class>")
                return
            name, password, klass = args[0], args[1], " ".join(args[2:])
            try:
                player = self.engine.register(
                    name, password, klass, Platform.IRC, name
                )
            except RegistrationError as exc:
                self.notice(nick, f"Cannot register: {exc}")
                return
            self.bind(nick, player, mask)
            self.notice(nick, f"Welcome, {player.name}. Now say nothing.")
        elif verb == "LOGIN":
            if len(args) < 2:
                self.notice(nick, "LOGIN <name> <password>")
                return
            player = self.engine.authenticate(args[0], args[1])
            if player is None:
                self.notice(nick, "Wrong name or password.")
                return
            try:
                self.bind(nick, player, mask)
            except RegistrationError as exc:
                self.notice(nick, f"Cannot log in: {exc}")
                return
            self.notice(nick, f"Logged in as {player.name}, level {player.level}.")
        elif verb == "LOGOUT":
            player = self.character_for_nick(nick)
            if player is None:
                self.notice(nick, "You are not logged in.")
                return
            self.unbind(nick, Penalty.LOGOUT)
            self.notice(nick, "Logged out. Your timer took the usual penalty.")
        elif verb == "MERGE":
            player = self.character_for_nick(nick)
            if player is None:
                self.notice(nick, "Log in as the character to keep, then MERGE.")
                return
            if len(args) < 2:
                self.notice(
                    nick,
                    "MERGE <name> <password> - folds that character into the "
                    "one you are logged in as.",
                )
                return
            try:
                outcome = self.engine.merge_by_password(player, args[0], args[1])
            except RegistrationError as exc:
                self.notice(nick, f"Cannot merge: {exc}")
                return
            self.notice(nick, outcome.message)
        elif verb == "WHOAMI":
            player = self.character_for_nick(nick)
            if player is None:
                self.notice(nick, "You are not logged in.")
                return
            self.notice(
                nick,
                f"{player.name}, level {player.level} {player.character_class}, "
                f"{player.next_ttl}s to go, alignment {player.alignment.value}.",
            )
        else:
            self.notice(nick, HELP)

    def handle_notice(self, msg: Message) -> None:
        """Watch for services telling us our own nick is unregistered."""
        if msg.nick.lower() != "nickserv":
            return
        text = msg.text.lower()
        if self.registration_attempted or not self.cfg.nickserv_email:
            return
        if "not registered" in text or "isn\'t registered" in text:
            self.registration_attempted = True
            log.info("nick is unregistered; registering with services")
            self.send(
                f"PRIVMSG NickServ :REGISTER {self.cfg.nickserv_password} "
                f"{self.cfg.nickserv_email}"
            )

    # --------------------------------------------------------------- dispatch

    def handle(self, msg: Message) -> None:
        cmd = msg.command
        if cmd == "PING":
            self.send(f"PONG :{msg.text}")
        elif cmd == "001":  # welcome
            # Nobody is bound on a fresh connection, so any IRC presence still
            # recorded - from before a restart - describes no one and must not
            # keep earning. Remembered logins come back once we have joined.
            self.engine.reset_presence(Platform.IRC)
            if self.cfg.nickserv_password:
                self.send(
                    f"PRIVMSG NickServ :IDENTIFY {self.cfg.nickserv_password}"
                )
                if self.cfg.nickserv_email:
                    # Provokes "not registered" if it isn't, which drives
                    # handle_notice() into registering the nick.
                    self.send(f"PRIVMSG NickServ :INFO {self.cfg.nick}")
            self.registration_attempted = False
            self.send(f"JOIN {self.cfg.channel}")
        elif cmd == "NOTICE":
            self.handle_notice(msg)
        elif cmd == "PRIVMSG":
            target = msg.params[0] if msg.params else ""
            if target.lower() == self.cfg.channel.lower():
                player = self.character_for_nick(msg.nick)
                if player is not None:
                    self.engine.penalise(
                        player, Penalty.MESSAGE,
                        message_length=len(msg.text), platform=Platform.IRC,
                    )
            elif target.lower() == self.cfg.nick.lower():
                self.handle_command(msg.nick, msg.text, msg.prefix)
        elif cmd == "JOIN":
            channel = msg.params[0] if msg.params else ""
            if channel.lower() != self.cfg.channel.lower():
                return
            if msg.nick.lower() == self.cfg.nick.lower():
                # We are in. Ask who else is, so logins from before a restart
                # resume from the replies.
                self.send(f"WHO {self.cfg.channel}")
            else:
                self.resume(msg.nick, msg.prefix)
        elif cmd == "352":
            # WHO reply: me channel user host server nick flags :hops realname
            if len(msg.params) >= 6 and msg.params[1].lower() == self.cfg.channel.lower():
                user, host, nick = msg.params[2], msg.params[3], msg.params[5]
                if nick.lower() != self.cfg.nick.lower():
                    self.resume(nick, f"{nick}!{user}@{host}")
        elif cmd == "PART":
            self.unbind(msg.nick, Penalty.PART)
        elif cmd == "QUIT":
            if NETSPLIT.match(msg.text):
                # Not the player's doing: no penalty, and the login stands, so
                # it resumes when the split heals and they rejoin.
                self.unbind(msg.nick, forget=False)
            else:
                self.unbind(msg.nick, Penalty.QUIT)
        elif cmd == "KICK":
            victim = msg.params[1] if len(msg.params) > 1 else ""
            self.unbind(victim, Penalty.KICK)
        elif cmd == "NICK":
            player = self.character_for_nick(msg.nick)
            if player is not None:
                self.engine.penalise(player, Penalty.NICK, platform=Platform.IRC)
                self.bound.pop(msg.nick.lower(), None)
                self.bind(msg.text, player, renamed(msg.prefix, msg.text))

    # ------------------------------------------------------------------- run

    async def _read_loop(self) -> None:
        assert self.reader is not None
        while True:
            raw = await self.reader.readline()
            if not raw:
                raise ConnectionError("server closed the connection")
            line = raw.decode("utf-8", "replace").rstrip("\r\n")
            log.debug("<< %s", line)
            msg = parse(line)
            if msg:
                try:
                    self.handle(msg)
                except Exception:  # one bad line must not kill the bot
                    log.exception("failed handling: %s", line)
            if self.writer:
                await self.writer.drain()

    async def set_topic(self, text: str) -> None:
        """Set the channel topic. Needs ops, which ChanServ grants on join."""
        if self.writer is None:
            return
        self.send(f"TOPIC {self.cfg.channel} :{text}")
        try:
            await self.writer.drain()
        except Exception:
            log.debug("could not flush topic")

    async def announce(self, text: str) -> None:
        """Say something in the game channel, if we are connected."""
        if self.writer is None:
            return
        self.say(text)
        try:
            await self.writer.drain()
        except Exception:
            log.debug("could not flush announcement")

    async def run_forever(self) -> None:
        while True:
            try:
                await self.connect()
                await self._read_loop()
            except Exception as exc:
                log.warning("disconnected: %s", exc)
            # Everyone loses presence when the link drops; they are not online
            # to us any more, and should not accrue time until they return.
            self.engine.reset_presence(Platform.IRC)
            self.bound.clear()
            if self.writer:
                self.writer.close()
                self.writer = None
            await asyncio.sleep(self.cfg.reconnect_seconds)
