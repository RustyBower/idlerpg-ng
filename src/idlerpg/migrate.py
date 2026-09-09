"""Copy a realm from one database to another.

Used to move off SQLite, but works between any two SQLAlchemy URLs. Both sides
use the same models, so this is a row copy rather than a translation.

    python -m idlerpg.migrate sqlite:////data/idlerpg.db postgresql+psycopg://...

Refuses to run if the destination already holds players, so re-running it
cannot quietly duplicate a realm.
"""

from __future__ import annotations

import sys

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from .models import (
    Base, EventLog, Item, LinkCode, PenaltyRecord, PlatformIdentity, Player,
    Quest, QuestParticipant, Setting,
)

# Parents before children: foreign keys are enforced on Postgres.
ORDER = [
    Player, PlatformIdentity, Item, PenaltyRecord, LinkCode,
    Quest, QuestParticipant, Setting, EventLog,
]


def columns(model):
    return [c.name for c in model.__table__.columns]


def migrate(source_url: str, dest_url: str, force: bool = False) -> dict[str, int]:
    src = create_engine(source_url, future=True)
    dst = create_engine(dest_url, future=True)
    Base.metadata.create_all(dst)

    counts: dict[str, int] = {}
    with Session(src) as s, Session(dst) as d:
        existing = d.scalar(select(Player).limit(1))
        if existing is not None and not force:
            raise SystemExit(
                "destination already has players; refusing to merge realms "
                "(pass --force if that is really what you want)"
            )

        for model in ORDER:
            cols = columns(model)
            rows = s.scalars(select(model)).all()
            for row in rows:
                d.merge(model(**{c: getattr(row, c) for c in cols}))
            d.flush()
            counts[model.__tablename__] = len(rows)
        d.commit()

        # Postgres sequences do not know about ids inserted explicitly.
        if dst.dialect.name == "postgresql":
            for model in ORDER:
                pk = list(model.__table__.primary_key.columns)[0]
                if pk.autoincrement is False or pk.type.python_type is not int:
                    continue
                d.execute(
                    __import__("sqlalchemy").text(
                        f"SELECT setval(pg_get_serial_sequence('{model.__tablename__}',"
                        f" '{pk.name}'), COALESCE((SELECT MAX({pk.name}) FROM "
                        f"{model.__tablename__}), 1))"
                    )
                )
            d.commit()
    return counts


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if len(args) != 2:
        print(__doc__)
        return 2
    counts = migrate(args[0], args[1], force="--force" in sys.argv)
    for table, n in counts.items():
        print(f"{table:20s} {n}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
