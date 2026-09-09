"""IRC adapter.

Translates IRC into engine calls: joins and parts become presence, channel
chatter and nick changes become penalties, and private messages to the bot are
commands. It holds no game rules of its own.

IRC has no stable per-user identifier without services, so a character's IRC
identity is keyed on its own account name, with the nick currently bound to it
kept in display_name. The adapter maps live nicks to characters for the
duration of a connection.
"""

from __future__ import annotations

import asyncio
import logging
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
    "REGISTER <name> <password> <class> | LOGIN <name> <password> | "
    "LOGOUT | WHOAMI | LINK (for Discord)"
)


class IRCAdapter:
    def __init__(self, engine: Engine, config):
        self.engine = engine
        self.cfg = config.irc
        self.tick_seconds = config.tick_seconds
        self.reader: asyncio.StreamReader | None = None
        self.writer: asyncio.StreamWriter | None = None
        # nick -> character name, for this connection only
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
        name = self.bound.get(nick.lower())
        return self.engine.find_player(name) if name else None

    def bind(self, nick: str, player) -> None:
        self.bound[nick.lower()] = player.name
        identity = self.engine.find_identity(Platform.IRC, player.name)
        if identity:
            identity.display_name = nick
        self.engine.set_presence(Platform.IRC, player.name, Presence.ACTIVE)

    def unbind(self, nick: str, penalty: Penalty | None = None) -> None:
        player = self.character_for_nick(nick)
        if player is None:
            return
        if penalty is not None:
            self.engine.penalise(player, penalty, platform=Platform.IRC)
        self.engine.set_presence(Platform.IRC, player.name, Presence.OFFLINE)
        self.bound.pop(nick.lower(), None)

    # -------------------------------------------------------------- commands

    def handle_command(self, nick: str, text: str) -> None:
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
            self.bind(nick, player)
            self.notice(nick, f"Welcome, {player.name}. Now say nothing.")
            self.say(f"{player.name}, the {player.character_class}, joins the realm.")
        elif verb == "LOGIN":
            if len(args) < 2:
                self.notice(nick, "LOGIN <name> <password>")
                return
            player = self.engine.authenticate(args[0], args[1])
            if player is None:
                self.notice(nick, "Wrong name or password.")
                return
            self.bind(nick, player)
            self.notice(nick, f"Logged in as {player.name}, level {player.level}.")
        elif verb == "LOGOUT":
            player = self.character_for_nick(nick)
            if player is None:
                self.notice(nick, "You are not logged in.")
                return
            self.unbind(nick, Penalty.LOGOUT)
            self.notice(nick, "Logged out. Your timer took the usual penalty.")
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
                self.handle_command(msg.nick, msg.text)
        elif cmd == "PART":
            self.unbind(msg.nick, Penalty.PART)
        elif cmd == "QUIT":
            self.unbind(msg.nick, Penalty.QUIT)
        elif cmd == "KICK":
            victim = msg.params[1] if len(msg.params) > 1 else ""
            self.unbind(victim, Penalty.KICK)
        elif cmd == "NICK":
            player = self.character_for_nick(msg.nick)
            if player is not None:
                self.engine.penalise(player, Penalty.NICK, platform=Platform.IRC)
                self.bound.pop(msg.nick.lower(), None)
                self.bind(msg.text, player)

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

    async def _tick_loop(self) -> None:
        while True:
            await asyncio.sleep(self.tick_seconds)
            try:
                for up in self.engine.tick(self.tick_seconds):
                    self.say(
                        f"{up.player} has attained level {up.level}! "
                        f"Next level in {up.next_ttl}s."
                    )
                if self.writer:
                    await self.writer.drain()
            except Exception:
                log.exception("tick failed")

    async def run_forever(self) -> None:
        while True:
            try:
                await self.connect()
                await asyncio.gather(self._read_loop(), self._tick_loop())
            except Exception as exc:
                log.warning("disconnected: %s", exc)
            # Everyone loses presence when the link drops; they are not online
            # to us any more, and should not accrue time until they return.
            for nick in list(self.bound):
                self.engine.set_presence(
                    Platform.IRC, self.bound[nick], Presence.OFFLINE
                )
            self.bound.clear()
            if self.writer:
                self.writer.close()
                self.writer = None
            await asyncio.sleep(self.cfg.reconnect_seconds)
