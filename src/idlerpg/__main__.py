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
        try:
            asyncio.run(adapter.run_forever())
        except KeyboardInterrupt:
            log.info("shutting down")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
