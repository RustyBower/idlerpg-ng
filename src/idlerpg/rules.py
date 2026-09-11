"""Game rules, ported from the classic Perl IdleRPG bot.

The constants here are not invented: they are read out of ``bot.pl`` in the
RustyBower/idlerpg fork, which is itself descended from idlerpg.net. Twenty
years of tuning live in these numbers, so they are kept faithful and the
provenance is noted where it matters.

Nothing in this module touches a database, a clock or a network. It is pure
arithmetic so it can be tested directly.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

# Seconds added per level once the exponential curve is capped. bot.pl uses a
# literal 86400 (one day).
LINEAR_STEP_SECONDS = 86400

# The level where the curve changes character. In the original it stops
# compounding and adds a day a level; here, by default, it compounds harder.
DEFAULT_CAP_LEVEL = 60

# How much more each level past the cap costs than the last. The original has
# no such thing - it goes linear - and a linear stretch makes every level past
# 60 merely slow, so the leaders' lead only ever grows. Compounding harder
# makes 60 a wall that prestige is the way past, and the Endurance perk the
# way through. None restores the original's day-a-level.
DEFAULT_POST_CAP_STEP = 1.25


class Penalty(str, Enum):
    """Penalty kinds, matching the pen_* columns of the original database."""

    MESSAGE = "message"
    NICK = "nick"
    PART = "part"
    KICK = "kick"
    QUIT = "quit"
    QUEST = "quest"
    LOGOUT = "logout"


# Multipliers applied as int(multiplier * penttl(level) / rpbase), taken from
# sub penalize() in bot.pl. MESSAGE is special: the original uses the length of
# the message itself as the multiplier, so it has no fixed value here.
PENALTY_MULTIPLIERS: dict[Penalty, int] = {
    Penalty.QUIT: 20,
    Penalty.NICK: 30,
    Penalty.PART: 200,
    Penalty.KICK: 250,
    Penalty.LOGOUT: 20,
    Penalty.QUEST: 15,
}


@dataclass(frozen=True)
class Curve:
    """Tuning for the level curve and penalties.

    rpstep defaults to 1.12 rather than the upstream 1.16. At 1.16 a single
    level at 60 costs about 51 days and level 100 is ~8.6 years away, which
    puts the late game out of reach; the cap only stops the curve running away,
    it does not make those levels attainable. At 1.12 level 60 arrives in about
    52 days. Changing this later silently rescales every player's remaining
    requirement, so it wants deciding while the database is empty.

    Past cap_level each level costs post_cap_step times the last - 1.25 by
    default, so 70 is about seven months past 60 and 80 several years. That
    is deliberate: 60 is a wall, and prestige is the way on. post_cap_step
    None is the original's rule, a day more per level. Penalties keep that
    linear rule whatever the curve does, so talking past 60 does not become
    ruinous.
    """

    base_seconds: int = 600
    step: float = 1.12
    penalty_step: float = 1.10  # below step, as the original's 1.14 is below 1.16
    cap_level: int = DEFAULT_CAP_LEVEL
    penalty_limit_seconds: int = 604800  # limitpen: one week
    post_cap_step: float | None = DEFAULT_POST_CAP_STEP

    def __post_init__(self) -> None:
        if self.base_seconds <= 0:
            raise ValueError("base_seconds must be positive")
        if self.step <= 1 or self.penalty_step <= 1:
            raise ValueError("step and penalty_step must be greater than 1")
        if self.cap_level < 0:
            raise ValueError("cap_level must not be negative")
        if self.post_cap_step is not None and self.post_cap_step < 1:
            raise ValueError("post_cap_step must be at least 1, or None for linear")


def _curve(level: int, base: int, step: float, cap: int,
           post_step: float | None = None) -> float:
    if level < 0:
        raise ValueError("level must not be negative")
    if level <= cap:
        return base * (step**level)
    if post_step is None:
        return base * (step**cap) + LINEAR_STEP_SECONDS * (level - cap)
    return base * (step**cap) * post_step ** (level - cap)


def ttl(level: int, curve: Curve | None = None) -> float:
    """Seconds of idling needed to advance from ``level`` to the next one."""
    c = curve or Curve()
    return _curve(level, c.base_seconds, c.step, c.cap_level, c.post_cap_step)


def penalty_ttl(level: int, curve: Curve | None = None) -> float:
    """The same curve, using penalty_step. Penalties scale with it."""
    c = curve or Curve()
    return _curve(level, c.base_seconds, c.penalty_step, c.cap_level)


def penalty_seconds(
    kind: Penalty,
    level: int,
    curve: Curve | None = None,
    message_length: int | None = None,
) -> int:
    """Seconds added to a player's timer for ``kind`` at ``level``.

    MESSAGE requires message_length: the original scales the penalty by how
    much you said, so a long line costs more than a short one.
    """
    c = curve or Curve()
    if kind is Penalty.MESSAGE:
        if message_length is None:
            raise ValueError("message_length is required for MESSAGE penalties")
        if message_length < 0:
            raise ValueError("message_length must not be negative")
        multiplier = message_length
    else:
        multiplier = PENALTY_MULTIPLIERS[kind]

    seconds = int(multiplier * penalty_ttl(level, c) / c.base_seconds)
    if c.penalty_limit_seconds:
        seconds = min(seconds, c.penalty_limit_seconds)
    return seconds


def seconds_to_reach(level: int, curve: Curve | None = None) -> float:
    """Total idling to get from level 0 to ``level``, ignoring penalties."""
    c = curve or Curve()
    return sum(ttl(l, c) for l in range(level))


def item_sum(items: dict[str, int]) -> int:
    """Battle strength: the sum of a player's item values."""
    return sum(items.values())
