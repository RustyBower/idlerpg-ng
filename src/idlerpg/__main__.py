"""Entry point: wire the database, engine and adapters together."""

from __future__ import annotations

import asyncio
import logging
import os
import sys

from sqlalchemy import create_engine as sa_create_engine
from sqlalchemy.orm import Session

from . import history
from .adapters.irc import IRCAdapter
from .config import Config
from .engine import Engine
from .models import Base, upgrade

log = logging.getLogger("idlerpg")


def build_topic(engine: Engine, site_url: str) -> str | None:
    """An admin's note if there is one, then the site link and the top three,
    as the original did."""
    note = engine.get_setting(engine.TOPIC_KEY) or ""
    top = engine.top_players(3)
    if not top and not note:
        return None
    parts = [
        f"#{i}: {p.name}, lv. {p.level} {p.character_class or 'wanderer'}"
        for i, p in enumerate(top, 1)
    ]
    topic = f"{site_url} " + "; ".join(parts) if parts else site_url
    return f"{note} | {topic}" if note else topic


async def set_topic(adapters: list, topic: str) -> None:
    for a in adapters:
        try:
            await a.set_topic(topic)
        except Exception:
            log.debug("topic update failed", exc_info=True)


async def tick_once(engine: Engine, adapters: list, seconds: float,
                    site_url: str) -> None:
    """One turn of the clock, and whatever admins asked of it since the last.

    The clock lives here, not in an adapter: one tick drives the whole world
    and its announcements go to every platform, so nobody is credited twice
    and neither side is silent - unless an admin has made it so.
    """
    try:
        outcomes = engine.tick(seconds)
    except Exception:
        log.exception("tick failed")
        return
    if engine.topic_requested:
        engine.topic_requested = False
        topic = build_topic(engine, site_url)
        if topic:
            await set_topic(adapters, topic)
    if not engine.silent:
        for outcome in outcomes:
            for a in adapters:
                try:
                    await a.announce(outcome.message)
                except Exception:
                    log.debug("announce failed", exc_info=True)
    if engine.restart_requested:
        # Exit cleanly; Kubernetes starts a fresh process and logins resume.
        log.info("restarting at an admin's request")
        raise SystemExit(0)


def main() -> int:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        stream=sys.stdout,
    )

    config = Config()
    db = sa_create_engine(config.database_url, future=True)
    Base.metadata.create_all(db)
    upgrade(db)
    log.info("database ready at %s", config.database_url.split("@")[-1])

    with Session(db) as session:
        engine = Engine(session, config.curve)
        engine.apply_owners(config.admins)
        engine.npc_max, engine.npc_realm = config.npc_max, config.npc_realm
        # Once: the level-ups announced before the event log recorded who
        # they were about, so the charts show the whole climb.
        history.backfill(engine)
        adapter = IRCAdapter(engine, config)
        log.info(
            "connecting to %s:%s as %s in %s",
            config.irc.host, config.irc.port, config.irc.nick, config.irc.channel,
        )
        adapters: list = [adapter]

        async def tick_loop() -> None:
            while True:
                await asyncio.sleep(config.tick_seconds)
                await tick_once(engine, adapters, config.tick_seconds, config.site_url)

        async def topic_loop() -> None:
            # Set it shortly after startup rather than making the first update
            # wait a full cycle, then settle into the slow cadence.
            delay = 60
            while True:
                await asyncio.sleep(delay)
                delay = config.topic_seconds
                topic = build_topic(engine, config.site_url)
                if topic is not None:
                    await set_topic(adapters, topic)

        async def run_all() -> None:
            tasks = [
                asyncio.create_task(adapter.run_forever()),
                asyncio.create_task(tick_loop()),
                asyncio.create_task(topic_loop()),
            ]
            if config.discord.enabled:
                # Imported lazily so the bot still starts without discord.py
                # installed when only IRC is configured.
                from .adapters.discord_adapter import DiscordAdapter

                discord_bot = DiscordAdapter(
                    engine,
                    config.discord.channel_id or None,
                    optin_channel_id=config.discord.optin_channel_id,
                    optin_role_id=config.discord.optin_role_id,
                    optin_emoji=config.discord.optin_emoji,
                )
                adapters.append(discord_bot)
                log.info("starting Discord adapter")
                tasks.append(
                    asyncio.create_task(discord_bot.start(config.discord.token))
                )
            else:
                log.info("no DISCORD_TOKEN set; running IRC only")
            await asyncio.gather(*tasks)

        try:
            asyncio.run(run_all())
        except KeyboardInterrupt:
            log.info("shutting down")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
