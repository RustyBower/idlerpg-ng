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


OPTIN_MESSAGE_KEY = "discord_optin_message_id"

OPTIN_TEXT = (
    "**IdleRPG**\n"
    "React with {emoji} to get access to the game channel. "
    "Remove your reaction to leave again.\n\n"
    "Nothing happens to you for joining: you only start playing once you "
    "register a character, and the game ignores everyone else entirely."
)


class DiscordAdapter(discord.Client):
    def __init__(self, engine: Engine, channel_id: int | None = None,
                 optin_channel_id: int = 0, optin_role_id: int = 0,
                 optin_emoji: str = "\N{GAME DIE}"):
        intents = discord.Intents.default()
        intents.presences = True
        intents.members = True
        intents.message_content = True
        intents.reactions = True
        super().__init__(intents=intents)
        self.engine = engine
        self.channel_id = channel_id
        self.optin_channel_id = optin_channel_id
        self.optin_role_id = optin_role_id
        self.optin_emoji = optin_emoji

    @property
    def optin_enabled(self) -> bool:
        return bool(self.optin_channel_id and self.optin_role_id)

    # --------------------------------------------------------------- opt-in

    async def ensure_optin_message(self) -> None:
        """Post the opt-in message once and remember which one it is.

        The id is stored so a restart reuses the existing post rather than
        littering the channel with a new one every time the bot starts.
        """
        if not self.optin_enabled:
            return
        channel = self.get_channel(self.optin_channel_id)
        if channel is None:
            log.warning("opt-in channel %s not visible", self.optin_channel_id)
            return

        stored = self.engine.get_setting(OPTIN_MESSAGE_KEY)
        if stored:
            try:
                await channel.fetch_message(int(stored))
                return  # still there, nothing to do
            except discord.NotFound:
                log.info("opt-in message was deleted; posting a new one")
            except discord.Forbidden:
                # We can post but not read history here. Reposting on that
                # basis would add a fresh message on every restart, so trust
                # the stored id instead: reactions are delivered raw and do
                # not need the message to be readable.
                log.warning(
                    "cannot read history in the opt-in channel; keeping the "
                    "existing message. Grant Read Message History to verify it."
                )
                return
            except (discord.HTTPException, ValueError):
                log.warning("could not verify the opt-in message; keeping it")
                return

        try:
            message = await channel.send(OPTIN_TEXT.format(emoji=self.optin_emoji))
            await message.add_reaction(self.optin_emoji)
        except discord.HTTPException:
            log.exception("could not post the opt-in message")
            return
        self.engine.set_setting(OPTIN_MESSAGE_KEY, str(message.id))
        log.info("posted opt-in message %s", message.id)

    def _is_optin_reaction(self, payload) -> bool:
        if not self.optin_enabled or payload.guild_id is None:
            return False
        stored = self.engine.get_setting(OPTIN_MESSAGE_KEY)
        if not stored or str(payload.message_id) != stored:
            return False
        return str(payload.emoji) == self.optin_emoji

    async def on_raw_reaction_add(self, payload) -> None:
        if not self._is_optin_reaction(payload):
            return
        guild = self.get_guild(payload.guild_id)
        role = guild.get_role(self.optin_role_id) if guild else None
        member = payload.member or (guild.get_member(payload.user_id) if guild else None)
        if role is None or member is None or member.bot:
            return
        try:
            await member.add_roles(role, reason="IdleRPG opt-in")
            log.info("opted in %s", member)
        except discord.Forbidden:
            # Almost always the role sitting above the bot's own in the list.
            log.warning("cannot grant %s - check Manage Roles and role order", role)
        except discord.HTTPException:
            log.exception("failed granting the opt-in role")

    async def on_raw_reaction_remove(self, payload) -> None:
        if not self._is_optin_reaction(payload):
            return
        guild = self.get_guild(payload.guild_id)
        role = guild.get_role(self.optin_role_id) if guild else None
        member = guild.get_member(payload.user_id) if guild else None
        if role is None or member is None:
            return
        try:
            await member.remove_roles(role, reason="IdleRPG opt-out")
            log.info("opted out %s", member)
        except discord.Forbidden:
            log.warning("cannot remove %s - check Manage Roles and role order", role)
        except discord.HTTPException:
            log.exception("failed removing the opt-in role")

    async def announce(self, text: str) -> None:
        """Post to the game channel, if one is configured and reachable."""
        if not self.channel_id:
            return
        channel = self.get_channel(self.channel_id)
        if channel is None:
            return
        try:
            await channel.send(text)
        except discord.HTTPException:
            log.debug("could not announce to Discord")

    # ---------------------------------------------------------------- events

    async def on_ready(self) -> None:
        log.info("connected to Discord as %s", self.user)
        await self.ensure_optin_message()
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
