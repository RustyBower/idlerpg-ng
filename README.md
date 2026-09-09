# idlerpg-ng

Multi-platform IdleRPG. One character per person, across IRC and Discord, with
a shared engine so the two sides can share quests instead of being two
unrelated games.

The rules come from the classic Perl IdleRPG (via
[RustyBower/idlerpg](https://github.com/RustyBower/idlerpg)); the architecture
does not. The original keys a player to an IRC nick and stores everyone in one
flat file that it rewrites wholesale, which cannot represent someone who plays
from two places.

## Status

Early. Phase 1 of 4 — the engine core, with no chat integration yet.

1. **Engine, schema and tick** — pure logic, covered by tests ← *here*
2. IRC adapter, reaching parity with the Perl bot, then cut over
3. Discord adapter and identity linking
4. Cross-service quests

Phases 1–2 only replace something that already works. The payoff starts at 3.

## The two decisions that shape everything

**One character, many platform identities.** A `Player` is the character;
`PlatformIdentity` rows attach an IRC account or a Discord account to it. This
is what makes a shared game possible at all, and it is what the original cannot
be retrofitted to do.

**A player earns time while present and silent on at least one linked
platform.** IRC idling (connected and quiet) and Discord idling (present and
not posting) are not the same thing, and conflating them makes the game either
unfair or farmable. Crediting the character rather than each connection means
being on both platforms neither punishes you nor pays you twice.

## Tuning

`rpstep` defaults to **1.12**, not upstream's 1.16.

| rpstep | to level 60 | level 80 | level 100 |
|--------|-------------|----------|-----------|
| 1.16 (upstream) | 319.8d | 4.2y | 8.6y |
| 1.12 (default) | 51.9d | 1.0y | 3.0y |

The level-60 cap that switches the curve from exponential to +1 day/level is
often described as the fix for unreachable high levels. It is not sufficient:
at 1.16 a single level at 60 already costs ~51 days, so the linear term is
about 2% of the step. `rpstep` is the real lever. `tests/test_rules.py` pins
this so the reasoning is not lost.

## Development

```bash
python3 -m venv venv
./venv/bin/pip install -e '.[dev]'
./venv/bin/python -m pytest
```
