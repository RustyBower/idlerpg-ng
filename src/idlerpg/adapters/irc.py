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
When the bot rejoins it asks WHO is in the channel and logs back in anyone it
recognises, as the original bot's autologin did; a player who turns up later
is logged in as they join. Quitting, parting, being kicked and LOGOUT end a
login. A netsplit does not.

Recognising them prefers the services account, where the network offers one
through account-notify and extended-join: it is authenticated, and it holds
across a new address, a cloak applied a moment after joining, and a reconnect
from anywhere. Not everybody registers with services, so the connection's
nick!user@host remains the fallback, and chghost keeps that address current
when services change it underneath a player.
"""

from __future__ import annotations

import asyncio
import logging
import re
import ssl
import time
from collections import deque
from dataclasses import dataclass

from .. import __version__, achievements, admin, fights, prestige, recap, seasonal
from ..engine import ALIGNMENT_HELP, Engine, RegistrationError
from ..models import Platform, Presence
from ..rules import Penalty
from ..text import duration, safe

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
    "character too) | LOGOUT | WHOAMI | "
    "ALIGN <lawful|neutral|chaotic> <good|neutral|evil> | "
    "NEWPASS <current> <new> | REMOVEME <password> | "
    "FIGHT [name] (once a day, from level 10) | ACHIEVEMENTS | RECAP | "
    "PRESTIGE (from level 60) | PERKS | PERK <name> | "
    "MERGE <name> <password> (fold another character of yours into this one)"
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
    # The server hears this many lines at once, then one every SEND_INTERVAL
    # seconds - well inside what IRC servers allow before disconnecting a
    # client for flooding, however busy the realm gets.
    SEND_BURST = 4
    SEND_INTERVAL = 2.0
    # The protocol itself goes at once, whatever is queued.
    IMMEDIATE = frozenset({"PONG", "PING", "NICK", "USER", "PASS", "CAP", "JOIN",
                           "WHO", "QUIT"})
    # What the bot can make use of, if the server offers it. account-notify
    # and extended-join say who is identified to services, which is a far
    # better key for a login than an address; chghost keeps a remembered
    # address current when a cloak lands; multi-prefix shows every rank at
    # once, so voice tracking does not have to infer the rest.
    WANTED_CAPS = ("account-notify", "extended-join", "chghost", "multi-prefix")
    VOICES_PER_LINE = 4
    # Channel modes that take a parameter either way, and those that take one
    # only when set, so a MODE line's parameters can be matched to its modes.
    PARAM_MODES = frozenset("ovhqabeIk")
    PARAM_WHEN_SET = frozenset("lLfjH")
    RANKS = {"~": "q", "&": "a", "@": "o", "%": "h"}

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
        # Nicks in the game channel. Only they earn: the game is sitting in
        # the channel, not being logged in from somewhere else.
        self.members: set[str] = set()
        # The nick we actually hold. It differs from cfg.nick while another
        # connection has ours - usually our own, from before a restart.
        self.nick = self.cfg.nick
        # Paced output, once run_forever's pump is running: replies to
        # people jump ahead of what the channel is told.
        self.replies: deque[str] = deque()
        self.chatter: deque[str] = deque()
        self.clock = time.monotonic
        self.tokens = float(self.SEND_BURST)
        self.refilled = self.clock()
        self.paced = False
        # Voice for whoever is logged in and in the channel. Giving it takes
        # a rank there: our own ranks (o, h, ...) and the voiced nicks are
        # followed from NAMES, WHO and MODE.
        self.ranks: set[str] = set()
        self.voiced: set[str] = set()
        # Told, this connection, why they cannot speak in a moderated channel.
        self.greeted: set[str] = set()
        # Capabilities the server agreed to, and what it says each nick's
        # services account is - learned from extended-join, ACCOUNT and WHOX,
        # and dropped with the connection that told us.
        self.caps: set[str] = set()
        self.offered: set[str] = set()
        self.accounts: dict[str, str] = {}
        # Whether the server's ISUPPORT advertises WHOX, which is the only way
        # to learn the accounts of people already here when the bot arrives.
        self.whox = False

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
        self.nick = self.cfg.nick
        # Ask what the server can do before registering. Anything it does not
        # offer is simply not used: CAP END follows either way, so a server
        # with no capabilities at all registers exactly as it always did.
        self.send("CAP LS 302")
        self.send(f"NICK {self.nick}")
        self.send(f"USER {self.cfg.user} 0 * :{self.cfg.realname}")

    def send(self, line: str) -> None:
        """Send a line, or queue it. The protocol goes at once; the rest is
        paced while the connection's pump runs, replies to people first."""
        if self.writer is None:
            return
        verb, _, rest = line.partition(" ")
        if not self.paced or verb.upper() in self.IMMEDIATE:
            self._write(line)
            return
        target = rest.split(" ", 1)[0].lower()
        to_channel = verb.upper() in ("PRIVMSG", "TOPIC") and target == self.cfg.channel.lower()
        lane = self.chatter if to_channel else self.replies
        lane.append(line)
        if len(lane) % 100 == 0:
            log.warning("IRC output is %d lines behind", len(lane))
        self.flush()

    def _write(self, line: str) -> None:
        log.debug(">> %s", line)
        self.writer.write((line + "\r\n").encode("utf-8", "replace"))

    def flush(self) -> None:
        """Send as much of the queue as the pace allows now."""
        now = self.clock()
        self.tokens = min(float(self.SEND_BURST),
                          self.tokens + (now - self.refilled) / self.SEND_INTERVAL)
        self.refilled = now
        while self.tokens >= 1 and self.writer is not None and (self.replies or self.chatter):
            self._write((self.replies or self.chatter).popleft())
            self.tokens -= 1

    async def _pump(self) -> None:
        """Drain the queue for as long as the connection lasts."""
        while True:
            self.flush()
            if self.writer is not None:
                await self.writer.drain()
            await asyncio.sleep(self.SEND_INTERVAL / 2)

    # Everything the game says goes through safe(): a name or class holding
    # bidi overrides or colour codes must not flip or paint the channel.
    def notice(self, target: str, text: str) -> None:
        self.send(f"NOTICE {target} :{safe(text)}")

    def say(self, text: str) -> None:
        self.send(f"PRIVMSG {self.cfg.channel} :{safe(text)}")

    def notice_lines(self, target: str, text: str, width: int = 380) -> None:
        """A long reply as several notices, split between its " | " items, so
        the server does not cut it off at 512 bytes."""
        line = ""
        for part in text.split(" | "):
            candidate = f"{line} | {part}" if line else part
            if len(candidate) > width and line:
                self.notice(target, line)
                line = part
            else:
                line = candidate
        if line:
            self.notice(target, line)

    # --------------------------------------------------------------- helpers

    def character_for_nick(self, nick: str):
        # Bound to the identity rather than the character's name, so a nick
        # follows its character through a merge made from Discord.
        external = self.bound.get(nick.lower())
        return self.engine.player_for(Platform.IRC, external) if external else None

    def bind(self, nick: str, player, mask: str | None = None,
             account: str | None = None) -> bool:
        """Attach ``nick`` to ``player``; returns whether they are now earning.

        Logging in is how a character reaches IRC, so one registered on Discord
        gains an IRC identity here. ``mask`` is remembered so the login
        outlives the bot. A nick holds one login at a time: logging in as
        another character ends the first, which would otherwise go on earning.
        Logged in from outside the channel, a character earns nothing until
        the nick joins it.
        """
        identity = next(
            (i for i in player.identities if i.platform is Platform.IRC), None
        )
        if identity is None:
            identity = self.engine.link(player, Platform.IRC, player.name, nick)
        previous = self.bound.get(nick.lower())
        if previous is not None and previous != identity.external_id:
            self.unbind(nick)
        self.bound[nick.lower()] = identity.external_id
        for irc_identity in player.identities:
            if irc_identity.platform is Platform.IRC:
                irc_identity.display_name = nick
        if mask:
            self.engine.remember_login(identity, mask)
        account = account or self.accounts.get(nick.lower())
        if account:
            self.engine.remember_account(identity, account)
        here = self._settle(player, identity.external_id)
        if here:
            self.voice(nick)
        return here

    def unbind(self, nick: str, penalty: Penalty | None = None,
               forget: bool = True) -> int:
        """Take ``nick`` offline and, unless ``forget`` is off, end its login.
        Returns what the penalty cost.

        Leaving the bot could not see as the player's choice - a netsplit -
        keeps the login, so it resumes when they come back.
        """
        external = self.bound.get(nick.lower())
        player = self.character_for_nick(nick)
        if player is None:
            self.bound.pop(nick.lower(), None)
            return 0
        cost = 0
        if penalty is not None:
            cost = self.engine.penalise(player, penalty, platform=Platform.IRC)
        if forget:
            identity = self.engine.find_identity(Platform.IRC, external)
            if identity is not None:
                self.engine.remember_login(identity, None)
                self.engine.remember_account(identity, None)
        self.devoice(nick)          # still in the channel, but logged out
        self.bound.pop(nick.lower(), None)
        self._settle(player, external)
        return cost

    def _settle(self, player, external: str) -> bool:
        """Set a character's IRC presence from the channel; returns it.

        Present while any nick logged in as it sits in the channel, so a
        character on two nicks is not taken offline when one of them leaves.
        """
        here = any(
            ext == external and nick in self.members
            for nick, ext in self.bound.items()
        )
        self.engine.set_player_presence(
            player, Platform.IRC, Presence.ACTIVE if here else Presence.OFFLINE
        )
        return here

    def _own_nick(self, new: str) -> None:
        self.nick = new
        if new.lower() == self.cfg.nick.lower() and self.cfg.nickserv_password:
            # Back on our own nick after a stand-in: identify to it.
            self.send(f"PRIVMSG NickServ :IDENTIFY {self.cfg.nickserv_password}")

    def resume(self, nick: str, mask: str) -> None:
        """Log ``nick`` back in if this is a login the bot never saw end.

        The services account first, where the network tells us of one: it is
        authenticated, and it survives a new address, a cloak applied a moment
        late, and a reconnect from anywhere. Failing that, the whole
        nick!user@host - anyone can take a nick and run up its owner's
        penalties, but a bouncer keeps user@host stable.
        """
        if nick.lower() in self.bound:
            return
        account = self.accounts.get(nick.lower())
        if account:
            identity = self.engine.resume_account(Platform.IRC, account)
            if identity is not None:
                self.bind(nick, identity.player, mask, account=account)
                self.engine.record_login(identity.player, Platform.IRC,
                                         announce=False)
                log.info("resumed %s as %s, by services account %s",
                         nick, identity.player.name, account)
                return
        if not mask:
            return
        identity = self.engine.resume_login(Platform.IRC, mask)
        if identity is None:
            # Say why, when it is somebody the bot has seen play. A client
            # that comes back on another address - a reconnect on a new IP, a
            # cloak applied a moment later - is a stranger to the remembered
            # login, and the player is left wondering where it went.
            remembered = self.engine.remembered_mask(Platform.IRC, nick)
            if remembered and remembered != mask.lower():
                log.info("not resuming %s: here as %s, remembered as %s",
                         nick, mask.lower(), remembered)
            return
        self.bind(nick, identity.player, mask)
        self.engine.record_login(identity.player, Platform.IRC, announce=False)
        log.info("resumed %s as %s", nick, identity.player.name)

    # ----------------------------------------------------------------- voice

    def _voice_lines(self, sign: str, nicks: list[str]) -> None:
        for i in range(0, len(nicks), self.VOICES_PER_LINE):
            chunk = nicks[i:i + self.VOICES_PER_LINE]
            self.send(f"MODE {self.cfg.channel} {sign}{'v' * len(chunk)} {' '.join(chunk)}")

    def voice(self, *nicks: str) -> None:
        """Voice those of ``nicks`` logged in and in the channel, if we hold
        a rank there to do it with."""
        if not self.cfg.voice or not self.ranks:
            return
        todo = [n.lower() for n in nicks
                if n.lower() in self.bound and n.lower() in self.members
                and n.lower() not in self.voiced]
        self.voiced.update(todo)
        if todo:
            self._voice_lines("+", todo)

    def devoice(self, nick: str) -> None:
        """Take the voice from a nick still in the channel but logged out.
        Only voice we can see: a voice from before is taken too, but one
        never given is not asked for."""
        if not self.cfg.voice or not self.ranks:
            return
        if nick.lower() in self.voiced and nick.lower() in self.members:
            self.voiced.discard(nick.lower())
            self._voice_lines("-", [nick.lower()])

    def _modes(self, modes: str, args: list[str]) -> None:
        """Follow a channel MODE line: who is voiced, and our own ranks."""
        sign, args, had = "+", list(args), bool(self.ranks)
        for m in modes:
            if m in "+-":
                sign = m
                continue
            if not (m in self.PARAM_MODES or (m in self.PARAM_WHEN_SET and sign == "+")):
                continue
            target = args.pop(0).lower() if args else ""
            if m == "v":
                (self.voiced.add if sign == "+" else self.voiced.discard)(target)
            elif m in "qaoh" and target == self.nick.lower():
                (self.ranks.add if sign == "+" else self.ranks.discard)(m)
        if self.ranks and not had:
            self._ranked()

    def _ranked(self) -> None:
        """Just given a rank in the channel - usually ChanServ's op, after we
        joined and logins resumed: moderate the channel if asked, and voice
        everyone already logged in."""
        if self.cfg.moderate:
            self.send(f"MODE {self.cfg.channel} +m")
        self.voice(*[n for n in self.bound if n in self.members])

    def _greet(self, nick: str) -> None:
        """In a moderated channel nobody logged out can speak, so tell them
        why and how to play - once a connection, not at every rejoin."""
        if not self.cfg.moderate or nick.lower() in self.greeted:
            return
        self.greeted.add(nick.lower())
        self.notice(nick, f"Welcome to {self.cfg.channel}. Only players who are logged "
                          f"in are voiced here, and only they can speak - though "
                          f"speaking costs them time. To play: /msg {self.nick} "
                          f"REGISTER <name> <password> <class>, or LOGIN <name> "
                          f"<password> if you have a character.")

    def _prefixed(self, name: str) -> str:
        """A NAMES or WHO entry's nick, noting its voice and, if it is us,
        our ranks."""
        nick = name.lstrip("~&@%+")
        prefixes = name[:len(name) - len(nick)]
        if "+" in prefixes:
            self.voiced.add(nick.lower())
        if nick.lower() == self.nick.lower():
            had = bool(self.ranks)
            self.ranks.update(self.RANKS[p] for p in prefixes if p in self.RANKS)
            if self.ranks and not had:
                self._ranked()
        return nick

    # -------------------------------------------------------------- commands

    def handle_command(self, nick: str, text: str, mask: str = "") -> None:
        parts = text.strip().split()
        if not parts:
            return
        verb, args = parts[0].upper(), parts[1:]

        if verb == "HELP":
            self.notice_lines(nick, HELP)
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
            here = self.bind(nick, player, mask)
            self.notice(nick, f"Welcome, {player.name}. Now say nothing."
                              + self._join_hint(here))
        elif verb == "LOGIN":
            if len(args) < 2:
                self.notice(nick, "LOGIN <name> <password>")
                return
            player = self.engine.authenticate(args[0], args[1])
            if player is None:
                self.notice(nick, "Wrong name or password.")
                return
            try:
                here = self.bind(nick, player, mask)
            except RegistrationError as exc:
                self.notice(nick, f"Cannot log in: {exc}")
                return
            self.engine.record_login(player, Platform.IRC)
            self.notice(nick, f"Logged in as {player.name}, level {player.level}."
                              + self._join_hint(here))
        elif verb == "LOGOUT":
            player = self.character_for_nick(nick)
            if player is None:
                self.notice(nick, "You are not logged in.")
                return
            cost = self.unbind(nick, Penalty.LOGOUT)
            if cost:
                self.notice(nick, f"Logged out. That cost you {duration(cost)}.")
            else:
                self.notice(nick, "Logged out of IRC. You are still playing "
                                  "elsewhere, so it cost nothing.")
        elif verb == "NEWPASS":
            player = self.character_for_nick(nick)
            if player is None:
                self.notice(nick, "Log in first, then NEWPASS <current> <new>.")
                return
            if len(args) < 2:
                self.notice(nick, "NEWPASS <current password> <new password>")
                return
            try:
                self.engine.change_password(player, args[0], args[1])
            except RegistrationError as exc:
                self.notice(nick, f"Cannot change it: {exc}.")
                return
            self.notice(nick, "Password changed.")
        elif verb == "REMOVEME":
            player = self.character_for_nick(nick)
            if player is None:
                self.notice(nick, "Log in first, then REMOVEME <password>.")
                return
            if not args:
                self.notice(nick, f"REMOVEME <password> deletes {player.name} for good.")
                return
            external, name = self.bound.get(nick.lower()), player.name
            try:
                self.engine.remove_player(player, args[0])
            except RegistrationError as exc:
                self.notice(nick, f"Cannot remove: {exc}.")
                return
            for bound_nick in [n for n, e in self.bound.items() if e == external]:
                self.devoice(bound_nick)
                self.bound.pop(bound_nick)
            self.notice(nick, f"{name} is gone. REGISTER any time to start again.")
        elif verb == "ALIGN":
            player = self.character_for_nick(nick)
            if player is None:
                self.notice(nick, "Log in first, then ALIGN lawful good (or any of the nine).")
                return
            if not args:
                self.notice(nick, f"You are {player.alignment_name}. {ALIGNMENT_HELP}")
                return
            try:
                name = self.engine.set_alignment(player, " ".join(args))
            except RegistrationError as exc:
                self.notice(nick, f"Cannot align: {exc}.")
                return
            self.notice(nick, f"You are now {name}.")
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
                f"{achievements.styled(player)}, level {player.level} {player.character_class}, "
                f"next level in {duration(player.next_ttl)}, "
                f"alignment {player.alignment_name}.{seasonal.honours_text(player)}"
                f"{achievements.summary(player)}"
                f"{achievements.rival_line(self.engine, player)}",
            )
        elif verb in achievements.VERBS:
            self.notice_lines(nick, achievements.command(
                self.engine, self.character_for_nick(nick), verb, args))
        elif verb in fights.VERBS:
            self.notice_lines(
                nick, fights.command(self.engine, self.character_for_nick(nick), verb, args))
        elif verb in recap.VERBS:
            self.notice_lines(
                nick, recap.command(self.engine, self.character_for_nick(nick), verb, args))
        elif verb in prestige.VERBS:
            self.notice_lines(
                nick, prestige.command(self.engine, self.character_for_nick(nick), verb, args))
        elif verb in admin.VERBS:
            self.notice_lines(
                nick, admin.run(self.engine, self.character_for_nick(nick), verb, args))
        else:
            self.notice_lines(nick, HELP)

    def handle_ctcp(self, nick: str, request: str) -> None:
        """Answer the CTCP queries clients send on their own. Anything else is
        ignored rather than answered with HELP, which is what every client's
        automatic VERSION request used to get."""
        verb, _, rest = request.partition(" ")
        if verb.upper() == "VERSION":
            self.notice(nick, f"\x01VERSION idlerpg-ng {__version__} - "
                              f"https://github.com/RustyBower/idlerpg-ng\x01")
        elif verb.upper() == "PING":
            self.notice(nick, f"\x01PING {rest}\x01")

    def _join_hint(self, here: bool) -> str:
        return "" if here else f" Join {self.cfg.channel} to start idling."

    def handle_notice(self, msg: Message) -> None:
        """Watch services: our ghost removed, or our own nick unregistered."""
        if msg.nick.lower() != "nickserv":
            return
        text = msg.text.lower()
        if "ghost" in text and self.nick.lower() != self.cfg.nick.lower():
            # Whatever held our nick is gone; take it back.
            self.send(f"NICK {self.cfg.nick}")
            return
        if self.registration_attempted or not self.cfg.nickserv_email:
            return
        if "not registered" in text or "isn\'t registered" in text:
            self.registration_attempted = True
            log.info("nick is unregistered; registering with services")
            self.send(
                f"PRIVMSG NickServ :REGISTER {self.cfg.nickserv_password} "
                f"{self.cfg.nickserv_email}"
            )

    # ----------------------------------------------------------- capabilities

    def capabilities(self, msg: Message) -> None:
        """Take what the server offers of WANTED_CAPS, then finish registering.

        CAP END is sent whatever happens - a server that offers nothing, or
        refuses everything, must still see registration completed, or the
        connection hangs before 001 and the realm never comes up.
        """
        sub = msg.params[1].upper() if len(msg.params) > 1 else ""
        if sub == "LS":
            self.offered.update(msg.text.split())
            # "CAP * LS * :..." means another line of the list follows.
            if len(msg.params) > 2 and msg.params[-2] == "*":
                return
            wanted = [c for c in self.WANTED_CAPS if c in self.offered]
            if wanted:
                self.send(f"CAP REQ :{' '.join(wanted)}")
            else:
                log.info("server offers none of the capabilities we use")
                self.send("CAP END")
        elif sub == "ACK":
            self.caps.update(msg.text.split())
            log.info("capabilities: %s", " ".join(sorted(self.caps)))
            self.send("CAP END")
        elif sub == "NAK":
            log.info("server refused capabilities: %s", msg.text)
            self.send("CAP END")

    def _account(self, nick: str, account: str) -> None:
        """Note who a nick is identified to, and log them in if that is a
        login the bot never saw end. "*" means they logged out of services."""
        key = nick.lower()
        if not account or account == "*":
            self.accounts.pop(key, None)
            return
        self.accounts[key] = account.lower()
        # Identifying after joining is the ordinary case for a client that
        # logs in on connect, and it is the moment their login can be found.
        self.resume(nick, "")

    # --------------------------------------------------------------- dispatch

    def handle(self, msg: Message) -> None:
        cmd = msg.command
        channel = self.cfg.channel.lower()
        if cmd == "PING":
            self.send(f"PONG :{msg.text}")
        elif cmd == "CAP":
            self.capabilities(msg)
        elif cmd == "005":
            # ISUPPORT. WHOX is the only way to ask for the accounts of people
            # already here; without it they are known only once they speak of
            # themselves through ACCOUNT, a fresh JOIN, or a changed host.
            if any(t.upper() == "WHOX" for t in msg.params[1:-1]):
                self.whox = True
        elif cmd == "ACCOUNT":
            self._account(msg.nick, msg.text)
        elif cmd == "CHGHOST":
            # A vhost landing after the player joined: the address the login
            # was remembered under has just changed under us. Follow it, and
            # try again for anyone it might now match.
            if len(msg.params) >= 2:
                fresh = f"{msg.nick}!{msg.params[0]}@{msg.params[1]}"
                external = self.bound.get(msg.nick.lower())
                if external is not None:
                    identity = self.engine.find_identity(Platform.IRC, external)
                    if identity is not None:
                        self.engine.remember_login(identity, fresh)
                else:
                    self.resume(msg.nick, fresh)
        elif cmd == "001":  # welcome
            if msg.params:
                self.nick = msg.params[0]
            # Nobody is bound on a fresh connection, so any IRC presence still
            # recorded - from before a restart - describes no one and must not
            # keep earning. Remembered logins come back once we have joined.
            self.engine.reset_presence(Platform.IRC)
            if self.cfg.nickserv_password:
                if self.nick.lower() != self.cfg.nick.lower():
                    # Something holds our nick - usually our own connection
                    # from before a restart, not yet timed out. Have services
                    # remove it; the NICK back follows their notice.
                    self.send(
                        f"PRIVMSG NickServ :GHOST {self.cfg.nick} "
                        f"{self.cfg.nickserv_password}"
                    )
                else:
                    self.send(
                        f"PRIVMSG NickServ :IDENTIFY {self.cfg.nickserv_password}"
                    )
                    if self.cfg.nickserv_email:
                        # Provokes "not registered" if it isn't, which drives
                        # handle_notice() into registering the nick.
                        self.send(f"PRIVMSG NickServ :INFO {self.cfg.nick}")
            self.registration_attempted = False
            self.send(f"JOIN {self.cfg.channel}")
        elif cmd == "433":
            # Nick in use. Take a stand-in and reclaim ours once it is free;
            # without this the connection never finishes registering.
            taken = msg.params[1] if len(msg.params) > 1 else self.nick
            self.nick = f"{taken}_"
            self.send(f"NICK {self.nick}")
        elif cmd == "NOTICE":
            self.handle_notice(msg)
        elif cmd == "PRIVMSG":
            target = msg.params[0] if msg.params else ""
            if target.lower() == channel:
                player = self.character_for_nick(msg.nick)
                if player is not None:
                    cost = self.engine.penalise(
                        player, Penalty.MESSAGE,
                        message_length=len(msg.text), platform=Platform.IRC,
                    )
                    if cost:
                        self.notice(msg.nick, f"That cost you {duration(cost)}: "
                                              f"talking in {self.cfg.channel} sets you back.")
            elif target.lower() == self.nick.lower():
                if msg.text.startswith("\x01"):
                    self.handle_ctcp(msg.nick, msg.text.strip("\x01"))
                else:
                    self.handle_command(msg.nick, msg.text, msg.prefix)
        elif cmd == "JOIN":
            where = msg.params[0] if msg.params else ""
            if where.lower() != channel:
                return
            if msg.nick.lower() == self.nick.lower():
                # We are in. Ask who else is: the replies fill in the members
                # and resume logins from before a restart.
                self.members.clear()
                self.voiced.clear()
                self.ranks.clear()
                self.send(f"WHO {self.cfg.channel}")
                return
            self.members.add(msg.nick.lower())
            # With extended-join the server names the joiner's services
            # account: ":nick!u@h JOIN #chan account :Real Name", "*" for none.
            if "extended-join" in self.caps and len(msg.params) >= 2:
                account = msg.params[1]
                if account and account != "*":
                    self.accounts[msg.nick.lower()] = account.lower()
            external = self.bound.get(msg.nick.lower())
            player = self.character_for_nick(msg.nick) if external else None
            if player is not None:
                self._settle(player, external)  # logged in first, joined now
                self.voice(msg.nick)
            else:
                self.resume(msg.nick, msg.prefix)
                if msg.nick.lower() not in self.bound:
                    self._greet(msg.nick)
        elif cmd == "353":
            # NAMES reply: me = channel :nick @op +voiced ...
            if len(msg.params) >= 4 and msg.params[2].lower() == channel:
                for name in msg.text.split():
                    self.members.add(self._prefixed(name).lower())
        elif cmd == "352":
            # WHO reply: me channel user host server nick flags :hops realname
            if len(msg.params) >= 6 and msg.params[1].lower() == channel:
                user, host, nick = msg.params[2], msg.params[3], msg.params[5]
                flags = msg.params[6] if len(msg.params) > 6 else ""
                # Flags like "Hr@+": only the rank symbols say anything here.
                self._prefixed("".join(c for c in flags if c in "~&@%+") + nick)
                if nick.lower() != self.nick.lower():
                    self.members.add(nick.lower())
                    self.resume(nick, f"{nick}!{user}@{host}")
        elif cmd == "PART":
            where = msg.params[0] if msg.params else ""
            if where.lower() != channel:
                return
            self.members.discard(msg.nick.lower())
            self.voiced.discard(msg.nick.lower())
            self.unbind(msg.nick, Penalty.PART)
        elif cmd == "QUIT":
            self.members.discard(msg.nick.lower())
            self.voiced.discard(msg.nick.lower())
            if (msg.nick.lower() == self.cfg.nick.lower()
                    and self.nick.lower() != self.cfg.nick.lower()):
                # Whatever held our nick has gone: take it back.
                self.send(f"NICK {self.cfg.nick}")
                return
            if NETSPLIT.match(msg.text):
                # Not the player's doing: no penalty, and the login stands, so
                # it resumes when the split heals and they rejoin.
                self.unbind(msg.nick, forget=False)
            else:
                self.unbind(msg.nick, Penalty.QUIT)
        elif cmd == "KICK":
            where = msg.params[0] if msg.params else ""
            victim = msg.params[1] if len(msg.params) > 1 else ""
            if where.lower() != channel:
                return
            self.members.discard(victim.lower())
            self.voiced.discard(victim.lower())
            self.unbind(victim, Penalty.KICK)
        elif cmd == "MODE":
            if len(msg.params) >= 2 and msg.params[0].lower() == channel:
                self._modes(msg.params[1], msg.params[2:])
        elif cmd == "NICK":
            new = msg.text
            if msg.nick.lower() == self.nick.lower():
                self._own_nick(new)
                return
            if msg.nick.lower() in self.members:
                self.members.discard(msg.nick.lower())
                self.members.add(new.lower())
            if msg.nick.lower() in self.voiced:      # a voice follows its nick
                self.voiced.discard(msg.nick.lower())
                self.voiced.add(new.lower())
            player = self.character_for_nick(msg.nick)
            if player is not None:
                cost = self.engine.penalise(player, Penalty.NICK, platform=Platform.IRC)
                self.bound.pop(msg.nick.lower(), None)
                self.bind(new, player, renamed(msg.prefix, new))
                if cost:
                    self.notice(new, f"That cost you {duration(cost)}: "
                                     f"changing nick sets you back.")

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
        self.send(f"TOPIC {self.cfg.channel} :{safe(text)}")
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
            pump = None
            try:
                await self.connect()
                self.paced = True
                pump = asyncio.create_task(self._pump())
                await self._read_loop()
            except Exception as exc:
                log.warning("disconnected: %s", exc)
            if pump is not None:
                pump.cancel()
            # What was queued was for a connection that is gone.
            self.paced = False
            self.replies.clear()
            self.chatter.clear()
            # Everyone loses presence when the link drops; they are not online
            # to us any more, and should not accrue time until they return.
            self.engine.reset_presence(Platform.IRC)
            self.bound.clear()
            self.members.clear()
            self.voiced.clear()
            self.ranks.clear()
            self.greeted.clear()
            # Capabilities and accounts belong to the connection that told us
            # of them, and are negotiated again on the next one.
            self.caps.clear()
            self.offered.clear()
            self.accounts.clear()
            self.whox = False
            if self.writer:
                self.writer.close()
                self.writer = None
            await asyncio.sleep(self.cfg.reconnect_seconds)
