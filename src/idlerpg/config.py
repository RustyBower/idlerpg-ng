"""Runtime configuration, read from the environment."""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from .rules import Curve


def _bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


@dataclass
class IRCConfig:
    host: str = field(default_factory=lambda: os.environ.get("IRC_HOST", "irc.129irc.com"))
    port: int = field(default_factory=lambda: int(os.environ.get("IRC_PORT", "6697")))
    tls: bool = field(default_factory=lambda: _bool("IRC_TLS", True))
    verify: bool = field(default_factory=lambda: _bool("IRC_TLS_VERIFY", True))
    nick: str = field(default_factory=lambda: os.environ.get("IRC_NICK", "idlerpg"))
    user: str = field(default_factory=lambda: os.environ.get("IRC_USER", "idlerpg"))
    realname: str = field(
        default_factory=lambda: os.environ.get("IRC_REALNAME", "IdleRPG")
    )
    channel: str = field(default_factory=lambda: os.environ.get("IRC_CHANNEL", "#idlerpg"))
    nickserv_password: str = field(
        default_factory=lambda: os.environ.get("IRC_NICKSERV_PASSWORD", "")
    )
    # If set, the bot registers its own nick the first time services tell it
    # the nick is unregistered. Anope runs db_sql here, which overwrites any
    # row not written by Anope itself, so registering through NickServ is the
    # only path that sticks.
    nickserv_email: str = field(
        default_factory=lambda: os.environ.get("IRC_NICKSERV_EMAIL", "")
    )
    reconnect_seconds: int = field(
        default_factory=lambda: int(os.environ.get("IRC_RECONNECT_SECONDS", "30"))
    )


@dataclass
class DiscordConfig:
    token: str = field(default_factory=lambda: os.environ.get("DISCORD_TOKEN", ""))
    channel_id: int = field(
        default_factory=lambda: int(os.environ.get("DISCORD_CHANNEL_ID", "0"))
    )

    @property
    def enabled(self) -> bool:
        return bool(self.token)


@dataclass
class Config:
    database_url: str = field(
        default_factory=lambda: os.environ.get("DATABASE_URL", "sqlite:///idlerpg.db")
    )
    tick_seconds: int = field(default_factory=lambda: int(os.environ.get("TICK_SECONDS", "5")))
    irc: IRCConfig = field(default_factory=IRCConfig)
    discord: DiscordConfig = field(default_factory=DiscordConfig)
    curve: Curve = field(
        default_factory=lambda: Curve(
            base_seconds=int(os.environ.get("RP_BASE", "600")),
            step=float(os.environ.get("RP_STEP", "1.12")),
            penalty_step=float(os.environ.get("RP_PENALTY_STEP", "1.14")),
        )
    )
