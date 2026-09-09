"""Entry point: wire the database, engine and adapters together."""

from __future__ import annotations

import asyncio
import logging
import os
import sys

from sqlalchemy import create_engine as sa_create_engine
from sqlalchemy.orm import Session

from .adapters.irc import IRCAdapter
from .config import Config
from .engine import Engine
from .models import Base


def main() -> int:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        stream=sys.stdout,
    )
    log = logging.getLogger("idlerpg")

    config = Config()
    db = sa_create_engine(config.database_url, future=True)
    Base.metadata.create_all(db)
    log.info("database ready at %s", config.database_url.split("@")[-1])

    with Session(db) as session:
        engine = Engine(session, config.curve)
        adapter = IRCAdapter(engine, config)
        log.info(
            "connecting to %s:%s as %s in %s",
            config.irc.host, config.irc.port, config.irc.nick, config.irc.channel,
        )
        adapters: list = [adapter]

        async def tick_loop() -> None:
            """The clock lives here, not in an adapter.

            One tick drives the whole world and its announcements go to every
            platform, so nobody is credited twice and neither side is silent.
            """
            while True:
                await asyncio.sleep(config.tick_seconds)
                try:
                    outcomes = engine.tick(config.tick_seconds)
                except Exception:
                    log.exception("tick failed")
                    continue
                for outcome in outcomes:
                    for a in adapters:
                        try:
                            await a.announce(outcome.message)
                        except Exception:
                            log.debug("announce failed", exc_info=True)

        async def run_all() -> None:
            tasks = [
                asyncio.create_task(adapter.run_forever()),
                asyncio.create_task(tick_loop()),
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
