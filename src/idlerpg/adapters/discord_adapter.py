"""Discord adapter.

Reports presence and relays commands, exactly as the IRC adapter does. All the
rules live in the engine, so a character earns time the same way regardless of
which side of the bridge they are sitting on.

Discord presence is coarser than an IRC connection: a client can report
"online" indefinitely. That is why the engine credits the character rather than
the connection, and why online and idle both count as present rather than
trying to infer real activity.
"""

from __future__ import annotations

import logging

import discord

from ..engine import Engine, RegistrationError
from ..models import Platform, Presence
from ..rules import Penalty

log = logging.getLogger(__name__)

# Discord's statuses mapped onto ours. "idle" and "dnd" still mean connected,
# which is what the game rewards; only offline earns nothing.
PRESENCE_MAP = {
    discord.Status.online: Presence.ACTIVE,
    discord.Status.idle: Presence.AWAY,
    discord.Status.dnd: Presence.AWAY,
    discord.Status.offline: Presence.OFFLINE,
}

HELP = (
    "Stay connected and quiet to level up. Commands: "
    "`!register <name> <password> <class>`, `!login <name> <password>`, "
    "`!link <code>` (get the code with LINK on IRC), `!whoami`"
)


class DiscordAdapter(discord.Client):
    def __init__(self, engine: Engine, channel_id: int | None = None):
        intents = discord.Intents.default()
        intents.presences = True
        intents.members = True
        intents.message_content = True
        super().__init__(intents=intents)
        self.engine = engine
        self.channel_id = channel_id

    # ---------------------------------------------------------------- events

    async def on_ready(self) -> None:
        log.info("connected to Discord as %s", self.user)
        # Seed presence for everyone already visible, otherwise nobody accrues
        # time until they next change status.
        for guild in self.guilds:
            for member in guild.members:
                self.engine.set_presence(
                    Platform.DISCORD,
                    str(member.id),
                    PRESENCE_MAP.get(member.status, Presence.OFFLINE),
                )

    async def on_presence_update(self, before: discord.Member,
                                 after: discord.Member) -> None:
        self.engine.set_presence(
            Platform.DISCORD,
            str(after.id),
            PRESENCE_MAP.get(after.status, Presence.OFFLINE),
        )

    def _in_scope(self, message: discord.Message) -> bool:
        """DMs always, plus the one configured channel. Nothing else.

        Without this the bot would answer commands in every channel it can see,
        which is both noisy and a way to leak a password into a busy room.
        """
        if isinstance(message.channel, discord.DMChannel):
            return True
        return bool(self.channel_id) and message.channel.id == self.channel_id

    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot or not self._in_scope(message):
            return
        content = message.content.strip()
        if content.startswith("!"):
            await self.handle_command(message, content[1:])
            return
        # Talking costs time, the same as on IRC, scaled by how much was said.
        if self.channel_id and message.channel.id == self.channel_id:
            player = self.engine.player_for(Platform.DISCORD, str(message.author.id))
            if player is not None:
                self.engine.penalise(
                    player, Penalty.MESSAGE,
                    message_length=len(content), platform=Platform.DISCORD,
                )

    # -------------------------------------------------------------- commands

    async def handle_command(self, message: discord.Message, raw: str) -> None:
        parts = raw.split()
        if not parts:
            return
        verb, args = parts[0].lower(), parts[1:]
        author = message.author
        external = str(author.id)
        is_dm = isinstance(message.channel, discord.DMChannel)

        async def reply(text: str) -> None:
            await message.reply(text, mention_author=False)

        # register and login take a password. On IRC these arrive as a private
        # message; the Discord equivalent is a DM. Refuse them in a channel and
        # delete the evidence, rather than echoing a password back to a room.
        if verb in ("register", "login") and not is_dm:
            try:
                await message.delete()
            except discord.HTTPException:
                pass
            try:
                await author.send(
                    "Send that to me in a DM, not a channel - it contains your "
                    "password. I deleted the message if I was able to."
                )
            except discord.HTTPException:
                pass
            return

        if verb == "help":
            await reply(HELP)
        elif verb == "register":
            if len(args) < 3:
                await reply("`!register <name> <password> <class>`")
                return
            try:
                player = self.engine.register(
                    args[0], args[1], " ".join(args[2:]),
                    Platform.DISCORD, external,
                )
            except RegistrationError as exc:
                await reply(f"Cannot register: {exc}")
                return
            await reply(f"Welcome, {player.name}. Now say nothing.")
        elif verb == "login":
            if len(args) < 2:
                await reply("`!login <name> <password>`")
                return
            player = self.engine.authenticate(args[0], args[1])
            if player is None:
                await reply("Wrong name or password.")
                return
            try:
                self.engine.link(player, Platform.DISCORD, external, str(author))
            except RegistrationError as exc:
                await reply(str(exc))
                return
            await reply(f"Logged in as {player.name}, level {player.level}.")
        elif verb == "link":
            if not args:
                await reply("Run `LINK` on IRC to get a code, then `!link <code>`.")
                return
            try:
                player = self.engine.redeem_link_code(
                    args[0], Platform.DISCORD, external, str(author)
                )
            except RegistrationError as exc:
                await reply(f"Cannot link: {exc}")
                return
            await reply(
                f"Linked to {player.name}. You are one character on both now."
            )
        elif verb == "whoami":
            player = self.engine.player_for(Platform.DISCORD, external)
            if player is None:
                await reply("No character linked to this account.")
                return
            await reply(
                f"{player.name}, level {player.level} {player.character_class}, "
                f"{player.next_ttl}s to go, alignment {player.alignment.value}."
            )
        else:
            await reply(HELP)
