"""Discord adapter.

Reports presence and relays commands, exactly as the IRC adapter does. All the
rules live in the engine, so a character earns time the same way regardless of
which side of the bridge they are sitting on.

Being present on Discord means having the game channel: a registered player
idles for as long as they hold the opt-in role, the way an IRC player idles for
as long as they sit in the channel. Losing the role is parting, and leaving the
server is quitting. Online status plays no part - a client can report "online"
indefinitely, and "offline" is also how an invisible user looks.

Without an opt-in role configured there is no channel access to go on, so
status stands in for it: online, idle and dnd count as present, offline does
not.
"""

from __future__ import annotations

import logging
from typing import Literal

import discord
from discord import app_commands

from .. import achievements, admin, fights, prestige, seasonal
from ..engine import ALIGNMENT_HELP, Engine, RegistrationError
from ..events import item_sum
from ..models import Platform, Presence
from ..rules import Penalty
from ..text import duration, safe

log = logging.getLogger(__name__)

# Discord's statuses mapped onto ours, used only when there is no opt-in role.
# "idle" and "dnd" still mean connected; only offline earns nothing.
PRESENCE_MAP = {
    discord.Status.online: Presence.ACTIVE,
    discord.Status.idle: Presence.AWAY,
    discord.Status.dnd: Presence.AWAY,
    discord.Status.offline: Presence.OFFLINE,
}

HELP = (
    "Stay in the game channel and stay quiet to level up. Every command is also "
    "a slash command - `/register`, `/whoami`, `/fight` and the rest - answered so "
    "only you see it, which is the safest way to send a password. Commands: "
    "`!register <name> <password> <class>`, `!login <name> <password>` "
    "(an IRC character works too, making it one character on both), "
    "`!merge <name> <password>` (fold another character of yours into this "
    "one), `!align <lawful|neutral|chaotic> <good|neutral|evil>`, "
    "`!newpass <current> <new>`, "
    "`!removeme <password>`, `!fight [name]` (once a day, from level 10), "
    "`!achievements`, "
    "`!prestige` (from level 60), `!perks`, "
    "`!perk <name>`, `!whoami`. Anything with a password goes in a DM."
)

# These carry a password, so they are accepted only in a DM.
PASSWORD_VERBS = frozenset({"register", "login", "merge", "newpass", "removeme"})


OPTIN_MESSAGE_KEY = "discord_optin_message_id"
# The channel that message was posted in, so moving the note is noticed.
OPTIN_CHANNEL_KEY = "discord_optin_channel_id"

OPTIN_TEXT = (
    "**IdleRPG** - a game you play by doing nothing.\n"
    "Press **Play** to make a character - it gives you the game channel too. "
    "Already playing on IRC? Press **I play on IRC** and log in, and it becomes "
    "one character on both. (Or react with {emoji} and I will DM you how.)\n\n"
    "Your character idles for as long as you keep the game role. Removing your "
    "reaction takes the role away, and with a character that counts as leaving "
    "the game. Every command is a slash command too - `/whoami`, `/fight`, "
    "`/achievements` - answered so only you see it."
)

# Sent to someone who reacts to the note without a character: reacting gets
# them the channel but nothing to play with.
HOW_TO_PLAY = (
    "**Welcome to IdleRPG** - a game you play by doing nothing.\n"
    "Press **Play** on the pinned note, or use `/register`, or reply here: "
    "`!register <name> <password> <class>` (the class is just for show). "
    "Already playing on IRC? Send "
    "`!login <name> <password>` instead and it becomes one character on both.\n"
    "After that, just stay: your character levels up for as long as you keep "
    "the game role, and talking in the game channel sets it back. `!help` "
    "lists the rest."
)


class PlayView(discord.ui.View):
    """The pinned note's buttons. Persistent - fixed ids, and re-added at every
    start - so a note posted long ago still answers."""

    def __init__(self, adapter: "DiscordAdapter"):
        super().__init__(timeout=None)
        self.adapter = adapter

    @discord.ui.button(label="Play", emoji="\N{GAME DIE}", style=discord.ButtonStyle.success,
                       custom_id="idlerpg:play")
    async def play(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self.adapter.play(interaction)

    @discord.ui.button(label="I play on IRC", style=discord.ButtonStyle.secondary,
                       custom_id="idlerpg:login")
    async def login(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.send_modal(LoginModal(self.adapter))


class RegisterModal(discord.ui.Modal, title="Make a character"):
    """The Play button's form. What is typed in it reaches only the bot."""

    name = discord.ui.TextInput(label="Name", max_length=16)
    password = discord.ui.TextInput(label="Password - to log in from IRC too", max_length=64)
    klass = discord.ui.TextInput(label="Class - just for show", max_length=30,
                                 required=False, placeholder="Sysadmin")

    def __init__(self, adapter: "DiscordAdapter"):
        super().__init__()
        self.adapter = adapter

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.submit(interaction, self.name.value, self.password.value, self.klass.value)

    async def submit(self, interaction, name: str, password: str, klass: str) -> None:
        await self.adapter.run_command(
            interaction.user, "register",
            [name.strip(), password, *((klass or "adventurer").split())],
            self.adapter._private_reply(interaction))


class LoginModal(discord.ui.Modal, title="Log in as your character"):
    name = discord.ui.TextInput(label="Name", max_length=16)
    password = discord.ui.TextInput(label="Password", max_length=64)

    def __init__(self, adapter: "DiscordAdapter"):
        super().__init__()
        self.adapter = adapter

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.submit(interaction, self.name.value, self.password.value)

    async def submit(self, interaction, name: str, password: str) -> None:
        await self.adapter.run_command(interaction.user, "login", [name.strip(), password],
                                       self.adapter._private_reply(interaction))


class DiscordAdapter(discord.Client):
    def __init__(self, engine: Engine, channel_id: int | None = None,
                 optin_channel_id: int = 0, optin_role_id: int = 0,
                 optin_emoji: str = "\N{GAME DIE}"):
        intents = discord.Intents.default()
        intents.presences = True
        intents.members = True
        intents.message_content = True
        intents.reactions = True
        # Nothing the game says may ping anyone: a character named after a
        # role, or "@everyone" in a class, would otherwise notify the server.
        super().__init__(intents=intents,
                         allowed_mentions=discord.AllowedMentions.none())
        self.engine = engine
        self.channel_id = channel_id
        self.optin_channel_id = optin_channel_id
        self.optin_role_id = optin_role_id
        self.optin_emoji = optin_emoji
        # Slash commands, registered to the home server once a process: a
        # server's commands appear at once, global ones take up to an hour.
        self.tree = app_commands.CommandTree(self)
        self._slash_commands()
        self._synced = False

    async def setup_hook(self) -> None:
        # The note's buttons answer for notes posted before this start too.
        self.add_view(PlayView(self))

    @property
    def optin_enabled(self) -> bool:
        return bool(self.optin_channel_id and self.optin_role_id)

    # --------------------------------------------------------------- opt-in

    @property
    def optin_text(self) -> str:
        return OPTIN_TEXT.format(emoji=self.optin_emoji)

    async def ensure_optin_message(self) -> None:
        """Post the opt-in message once, pin it, and remember which one it is.

        The id is stored so a restart reuses the existing post rather than
        littering the channel with a new one every time the bot starts. A
        reused post is brought up to date, so rewording it needs no repost.
        """
        if not self.optin_enabled:
            return
        channel = self.get_channel(self.optin_channel_id)
        if channel is None:
            log.warning("opt-in channel %s not visible", self.optin_channel_id)
            return

        stored = self.engine.get_setting(OPTIN_MESSAGE_KEY)
        stored_channel = self.engine.get_setting(OPTIN_CHANNEL_KEY)
        if stored and stored_channel not in (None, str(self.optin_channel_id)):
            # The note is in the channel it was posted to. Looking for it here
            # would fail, or without Read Message History be taken on trust
            # and leave the new channel with no note at all.
            log.info(
                "opt-in channel moved from %s; posting a new note (the old one "
                "can be deleted)", stored_channel,
            )
            stored = None
        if stored:
            try:
                existing = await channel.fetch_message(int(stored))
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
            else:
                self._remember_optin(existing)
                await self._tidy_optin(existing)
                return

        try:
            message = await channel.send(self.optin_text, view=PlayView(self))
        except discord.HTTPException:
            log.exception("could not post the opt-in message")
            return
        # Remembered before anything else can fail: a note that is posted but
        # not recorded gets posted again on every start.
        self._remember_optin(message)
        log.info("posted opt-in message %s", message.id)
        await self._tidy_optin(message)

    def _remember_optin(self, message) -> None:
        self.engine.set_setting(OPTIN_MESSAGE_KEY, str(message.id))
        self.engine.set_setting(OPTIN_CHANNEL_KEY, str(self.optin_channel_id))

    async def _tidy_optin(self, message) -> None:
        """Bring the note up to date: today's wording, its reaction, pinned.

        Each step checks before it acts, so running this on every start
        changes nothing once the note is right, and a step that failed last
        time - say for want of a permission since granted - is retried.
        """
        if message.content != self.optin_text:
            try:
                await message.edit(content=self.optin_text)
            except discord.HTTPException:
                log.warning("could not update the opt-in message text")
        if not getattr(message, "components", None):
            try:
                await message.edit(view=PlayView(self))
            except discord.HTTPException:
                log.warning("could not add the Play button to the opt-in message")
        if not any(r.me and str(r.emoji) == self.optin_emoji
                   for r in message.reactions):
            try:
                await message.add_reaction(self.optin_emoji)
            except discord.HTTPException:
                log.warning("cannot react to the opt-in message - needs Add Reactions")
        if not message.pinned:
            await self._pin(message)

    async def _pin(self, message) -> None:
        try:
            await message.pin(reason="IdleRPG: how to join")
        except discord.Forbidden:
            log.warning("cannot pin the opt-in message - needs Pin Messages")
        except discord.HTTPException:
            log.warning("could not pin the opt-in message")

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
        await self._explain(member)

    async def _explain(self, member) -> None:
        """DM someone who reacted without a character how to make one."""
        if self.engine.player_for(Platform.DISCORD, str(member.id)) is not None:
            return
        try:
            await member.send(HOW_TO_PLAY)
        except discord.HTTPException:
            # DMs from server members switched off. The pinned note says the
            # same thing, so there is nothing more to do.
            log.info("could not DM %s how to play", member)

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

    # -------------------------------------------------------------- presence

    def _in_home_guild(self, guild) -> bool:
        """Does this guild's membership count? Only the one holding the role."""
        if not self.optin_enabled:
            return True
        return guild is not None and guild.get_role(self.optin_role_id) is not None

    def _home_member(self, user_id: int):
        for guild in self.guilds:
            if self._in_home_guild(guild):
                member = guild.get_member(user_id)
                if member is not None:
                    return member
        return None

    def _has_role(self, member) -> bool:
        return any(role.id == self.optin_role_id
                   for role in getattr(member, "roles", ()))

    def presence_of(self, member) -> Presence:
        """Is this member in the game? The role decides, or failing that status."""
        if member is None:
            return Presence.OFFLINE
        if self.optin_enabled:
            return Presence.ACTIVE if self._has_role(member) else Presence.OFFLINE
        return PRESENCE_MAP.get(member.status, Presence.OFFLINE)

    async def _seat(self, user) -> str:
        """Give a newly attached account the game role and record its presence.

        Registering is joining. With an opt-in role the role is what makes a
        player present, and the opt-in message may sit in a channel they cannot
        see, so it is granted here rather than left to a reaction. Returns a
        note for the reply when they are still not idling, saying why.
        """
        member = self._home_member(user.id)
        presence = self.presence_of(member)
        note = ""
        if presence is Presence.OFFLINE:
            if not self.optin_enabled:
                note = " You are not idling yet - you show as offline."
            elif member is None:
                note = " You are not idling yet - join the server first."
            elif await self._grant_role(member):
                presence = Presence.ACTIVE
            else:
                note = (" You are not idling yet - I could not give you the "
                        "game role, so ask an admin.")
        self.engine.set_presence(Platform.DISCORD, str(user.id), presence)
        return note

    async def _grant_role(self, member) -> bool:
        role = member.guild.get_role(self.optin_role_id)
        if role is None:
            return False
        try:
            await member.add_roles(role, reason="IdleRPG registration")
        except discord.Forbidden:
            log.warning("cannot grant %s - check Manage Roles and role order", role)
            return False
        except discord.HTTPException:
            log.exception("failed granting the game role")
            return False
        log.info("granted the game role to %s", member)
        return True

    async def set_topic(self, text: str) -> None:
        """Set the channel topic, if we are allowed to."""
        if not self.channel_id:
            return
        channel = self.get_channel(self.channel_id)
        if channel is None:
            return
        try:
            await channel.edit(topic=discord.utils.escape_markdown(safe(text))[:1024])
        except discord.Forbidden:
            log.warning("cannot set the Discord topic - needs Manage Channels")
        except discord.HTTPException:
            log.debug("could not set the Discord topic")

    async def announce(self, text: str) -> None:
        """Post to the game channel, if one is configured and reachable."""
        if not self.channel_id:
            return
        channel = self.get_channel(self.channel_id)
        if channel is None:
            return
        try:
            # The game's own text carries no markdown, so any in it came from
            # a name or class and is shown literally.
            await channel.send(discord.utils.escape_markdown(safe(text)))
        except discord.HTTPException:
            log.debug("could not announce to Discord")

    # ------------------------------------------------------ slash and buttons

    @staticmethod
    def _private_reply(interaction):
        """A reply only the sender sees, however many a command sends."""
        async def reply(text: str) -> None:
            if interaction.response.is_done():
                await interaction.followup.send(safe(text), ephemeral=True)
            else:
                await interaction.response.send_message(safe(text), ephemeral=True)
        return reply

    def _home_guild(self):
        channel = self.get_channel(self.channel_id) if self.channel_id else None
        if channel is not None and getattr(channel, "guild", None) is not None:
            return channel.guild
        return next((g for g in self.guilds if self._in_home_guild(g)), None)

    async def sync_commands(self) -> bool:
        """Register the slash commands with the home server. Needs the bot
        invited with the applications.commands scope; without it the !
        commands carry on as before."""
        guild = self._home_guild()
        if guild is None:
            return False
        try:
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
        except Exception:
            log.warning("could not register slash commands - invite the bot with the "
                        "applications.commands scope; the ! commands still work")
            return False
        log.info("slash commands registered in %s", guild)
        return True

    def _slash_commands(self) -> None:
        """Every command as a slash command, answered so only its sender sees."""
        tree = self.tree

        async def run(interaction, verb: str, *args) -> None:
            words = [w for a in args if a for w in str(a).split()]
            await self.run_command(interaction.user, verb, words,
                                   self._private_reply(interaction))

        @tree.command(name="register", description="Make a character: a name, a password, and a class just for show")
        @app_commands.rename(klass="class")
        async def register(interaction: discord.Interaction, name: str, password: str,
                           klass: str = "adventurer") -> None:
            await run(interaction, "register", name, password, klass)

        @tree.command(name="login", description="Log in as your character - an IRC one too, making it one on both")
        async def login(interaction: discord.Interaction, name: str, password: str) -> None:
            await run(interaction, "login", name, password)

        @tree.command(name="merge", description="Fold another character of yours into this one")
        async def merge(interaction: discord.Interaction, name: str, password: str) -> None:
            await run(interaction, "merge", name, password)

        @tree.command(name="newpass", description="Change your password")
        async def newpass(interaction: discord.Interaction, current: str, new: str) -> None:
            await run(interaction, "newpass", current, new)

        @tree.command(name="removeme", description="Delete your character for good")
        async def removeme(interaction: discord.Interaction, password: str) -> None:
            await run(interaction, "removeme", password)

        @tree.command(name="align", description="Choose your alignment: law or chaos, then good or evil")
        async def align(interaction: discord.Interaction,
                        law: Literal["lawful", "neutral", "chaotic"],
                        moral: Literal["good", "neutral", "evil"]) -> None:
            await run(interaction, "align", law, moral)

        @tree.command(name="whoami", description="Your character at a glance")
        async def whoami(interaction: discord.Interaction) -> None:
            player = self.engine.player_for(Platform.DISCORD, str(interaction.user.id))
            if player is None:
                await interaction.response.send_message(
                    "No character linked to this account yet - /register, or press "
                    "Play on the pinned note.", ephemeral=True)
                return
            await interaction.response.send_message(embed=self.card(player), ephemeral=True)

        @tree.command(name="fight", description="Once a day: challenge someone, or see who is in reach")
        async def fight(interaction: discord.Interaction, name: str = "") -> None:
            await run(interaction, "fight", name)

        @tree.command(name="achievements", description="What you have earned and collected")
        async def achievements_(interaction: discord.Interaction) -> None:
            await run(interaction, "achievements")

        @tree.command(name="prestige", description="From level 60: see, or confirm, starting over for perks")
        async def prestige_(interaction: discord.Interaction, confirm: bool = False) -> None:
            await run(interaction, "prestige", "confirm" if confirm else "")

        @tree.command(name="perks", description="What your prestige points buy")
        async def perks(interaction: discord.Interaction) -> None:
            await run(interaction, "perks")

        @tree.command(name="perk", description="Buy a rank of a perk")
        async def perk(interaction: discord.Interaction, name: str) -> None:
            await run(interaction, "perk", name)

        @tree.command(name="help", description="How to play, and every command")
        async def help_(interaction: discord.Interaction) -> None:
            await run(interaction, "help")

        @tree.command(name="admin", description="Admins only: an admin command, e.g. info or pause on")
        async def admin_(interaction: discord.Interaction, command: str) -> None:
            verb, _, rest = command.strip().partition(" ")
            await run(interaction, verb.lower() or "admin", rest)

    def card(self, player) -> discord.Embed:
        """/whoami: a character at a glance, only for its owner."""
        def esc(text) -> str:
            return discord.utils.escape_markdown(safe(str(text)))
        embed = discord.Embed(
            title=esc(achievements.styled(player)),
            description=esc(f"Level {player.level} {player.character_class or 'wanderer'}"))
        embed.add_field(name="Next level", value=duration(player.next_ttl))
        embed.add_field(name="Alignment", value=player.alignment_name)
        embed.add_field(name="Items", value=str(item_sum(player)))
        if player.prestige:
            embed.add_field(name="Prestige", value=f"★{player.prestige}")
        earned = sum(1 for a in player.achievements if a.key in achievements.BY_KEY)
        embed.add_field(name="Achievements", value=f"{earned} of {len(achievements.FEATS)}")
        nemesis = achievements.rival(self.engine, player)
        if nemesis:
            embed.add_field(name="Rival", value=esc(nemesis))
        if player.keepsakes:
            embed.add_field(name="Keepsakes", inline=False,
                            value=esc(", ".join(k.name for k in player.keepsakes))[:1024])
        honours = seasonal.honours_text(player).strip()
        if honours:
            embed.set_footer(text=safe(honours))
        return embed

    async def play(self, interaction) -> None:
        """The note's Play button: straight in for a player, a form for anyone new."""
        player = self.engine.player_for(Platform.DISCORD, str(interaction.user.id))
        if player is None:
            await interaction.response.send_modal(RegisterModal(self))
            return
        note = await self._seat(interaction.user)
        await interaction.response.send_message(
            safe(f"You are playing as {player.name}.{note} Stay in the game channel "
                 f"and say nothing - that is the game."), ephemeral=True)

    # ---------------------------------------------------------------- events

    async def on_ready(self) -> None:
        log.info("connected to Discord as %s", self.user)
        if not self._synced:
            self._synced = await self.sync_commands()
        await self.ensure_optin_message()
        # What was recorded before describes a session that is gone, and
        # anyone who lost the role or left while we were away must not keep
        # earning. Then seed everyone who is here, or nobody accrues time until
        # they next change.
        self.engine.reset_presence(Platform.DISCORD)
        for guild in self.guilds:
            if not self._in_home_guild(guild):
                continue
            for member in guild.members:
                presence = self.presence_of(member)
                if presence is not Presence.OFFLINE:
                    self.engine.set_presence(
                        Platform.DISCORD, str(member.id), presence
                    )

    async def on_presence_update(self, before: discord.Member,
                                 after: discord.Member) -> None:
        # With a game role, holding it is presence and status is irrelevant.
        if self.optin_enabled:
            return
        self.engine.set_presence(
            Platform.DISCORD, str(after.id), self.presence_of(after)
        )

    async def on_member_update(self, before: discord.Member,
                               after: discord.Member) -> None:
        """Gaining or losing the game role is joining or parting the channel.

        Handled here rather than on the reaction so that a role granted or
        removed by hand counts the same way.
        """
        if not self.optin_enabled:
            return
        had, has = self._has_role(before), self._has_role(after)
        if had == has:
            return
        external = str(after.id)
        if not has:
            player = self.engine.player_for(Platform.DISCORD, external)
            if player is not None:
                self.engine.penalise(player, Penalty.PART, platform=Platform.DISCORD)
        self.engine.set_presence(
            Platform.DISCORD, external,
            Presence.ACTIVE if has else Presence.OFFLINE,
        )

    async def on_member_remove(self, member: discord.Member) -> None:
        """Leaving the server is quitting."""
        if not self._in_home_guild(member.guild):
            return
        identity = self.engine.find_identity(Platform.DISCORD, str(member.id))
        if identity is None or identity.presence is Presence.OFFLINE:
            return
        self.engine.penalise(identity.player, Penalty.QUIT, platform=Platform.DISCORD)
        self.engine.set_presence(Platform.DISCORD, str(member.id), Presence.OFFLINE)

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
                cost = self.engine.penalise(
                    player, Penalty.MESSAGE,
                    message_length=len(content), platform=Platform.DISCORD,
                )
                if cost:
                    # A reaction, not a DM: one per chatty line would be spam.
                    try:
                        await message.add_reaction("\N{HOURGLASS WITH FLOWING SAND}")
                    except discord.HTTPException:
                        pass

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
            await message.reply(safe(text), mention_author=False)

        # These take a password. On IRC they arrive as a private message; the
        # Discord equivalent is a DM. Refuse them in a channel and delete the
        # evidence, rather than echoing a password back to a room.
        # Admin commands too: they are nobody else's business, and CHPASS
        # carries a password of its own.
        if (verb in PASSWORD_VERBS or verb.upper() in admin.VERBS) and not is_dm:
            try:
                await message.delete()
            except discord.HTTPException:
                pass
            try:
                await author.send(
                    "Send that to me in a DM, not a channel - it may contain a "
                    "password. I deleted the message if I was able to."
                )
            except discord.HTTPException:
                pass
            return
        await self.run_command(author, verb, args, reply)

    async def run_command(self, author, verb: str, args: list[str], reply) -> None:
        """One command - from a message, a slash command or a form - answered
        through ``reply``. Keeping passwords out of channels is the caller's
        job: a slash command's options and a form reach only the bot."""
        external = str(author.id)
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
            note = await self._seat(author)
            await reply(f"Welcome, {player.name}. Now say nothing.{note}")
        elif verb == "login":
            if len(args) < 2:
                await reply("`!login <name> <password>`")
                return
            player = self.engine.authenticate(args[0], args[1])
            if player is None:
                await reply("Wrong name or password.")
                return
            current = self.engine.player_for(Platform.DISCORD, external)
            if current is not None and current.id != player.id:
                await reply(
                    f"This Discord account already plays {current.name}. To make "
                    f"them one character, keep {current.name} and send "
                    f"`!merge {player.name} <password>`."
                )
                return
            self.engine.link(player, Platform.DISCORD, external, str(author))
            self.engine.record_login(player, Platform.DISCORD)
            note = await self._seat(author)
            await reply(f"Logged in as {player.name}, level {player.level}.{note}")
        elif verb == "merge":
            player = self.engine.player_for(Platform.DISCORD, external)
            if player is None:
                await reply(
                    "You have no character here yet. `!login` as the one to "
                    "keep first."
                )
                return
            if len(args) < 2:
                await reply(
                    "`!merge <name> <password>` folds that character into this one."
                )
                return
            try:
                outcome = self.engine.merge_by_password(player, args[0], args[1])
            except RegistrationError as exc:
                await reply(f"Cannot merge: {exc}")
                return
            await reply(outcome.message)
        elif verb in ("newpass", "removeme"):
            player = self.engine.player_for(Platform.DISCORD, external)
            if player is None:
                await reply("You have no character here yet.")
                return
            if verb == "newpass":
                if len(args) < 2:
                    await reply("`!newpass <current password> <new password>`")
                    return
                try:
                    self.engine.change_password(player, args[0], args[1])
                except RegistrationError as exc:
                    await reply(f"Cannot change it: {exc}.")
                    return
                await reply("Password changed.")
                return
            if not args:
                await reply(f"`!removeme <password>` deletes {player.name} for good.")
                return
            name = player.name
            try:
                self.engine.remove_player(player, args[0])
            except RegistrationError as exc:
                await reply(f"Cannot remove: {exc}.")
                return
            await reply(f"{name} is gone. `!register` any time to start again.")
        elif verb == "align":
            player = self.engine.player_for(Platform.DISCORD, external)
            if player is None:
                await reply("You have no character here yet - `!register` first.")
                return
            if not args:
                await reply(f"You are {player.alignment_name}. {ALIGNMENT_HELP}")
                return
            try:
                name = self.engine.set_alignment(player, " ".join(args))
            except RegistrationError as exc:
                await reply(f"Cannot align: {exc}.")
                return
            await reply(f"You are now {name}.")
        elif verb == "whoami":
            player = self.engine.player_for(Platform.DISCORD, external)
            if player is None:
                await reply("No character linked to this account.")
                return
            await reply(
                f"{achievements.styled(player)}, level {player.level} {player.character_class}, "
                f"next level in {duration(player.next_ttl)}, "
                f"alignment {player.alignment_name}.{seasonal.honours_text(player)}"
                f"{achievements.summary(player)}"
                f"{achievements.rival_line(self.engine, player)}"
            )
        elif verb.upper() in achievements.VERBS:
            await reply(achievements.command(
                self.engine, self.engine.player_for(Platform.DISCORD, external),
                verb, args))
        elif verb.upper() in fights.VERBS:
            await reply(fights.command(
                self.engine, self.engine.player_for(Platform.DISCORD, external),
                verb, args))
        elif verb.upper() in prestige.VERBS:
            await reply(prestige.command(
                self.engine, self.engine.player_for(Platform.DISCORD, external),
                verb, args))
        elif verb.upper() in admin.VERBS:
            await reply(admin.run(
                self.engine, self.engine.player_for(Platform.DISCORD, external),
                verb, args))
        else:
            await reply(HELP)
