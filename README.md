# idlerpg-ng

A game you play by doing nothing. Stay connected and stay quiet, and your
character levels up. Talking, parting, quitting and changing your nick all set
you back.

The rules come from the classic Perl IdleRPG (via
[RustyBower/idlerpg](https://github.com/RustyBower/idlerpg)); the architecture
does not. **One person is one character across IRC and Discord**, with a shared
engine, so the two sides are one game rather than two unrelated ones.

Running at [idlerpg.129irc.com](https://idlerpg.129irc.com/) on
[129irc](https://129irc.com).

## Playing

**IRC** — connect to `irc.129irc.com` on port `6697` with TLS, join `#idlerpg`:

    /msg idlerpg REGISTER <name> <password> <class>
    /msg idlerpg LOGIN <name> <password>
    /msg idlerpg WHOAMI
    /msg idlerpg ALIGN <good|neutral|evil>
    /msg idlerpg NEWPASS <current> <new>
    /msg idlerpg REMOVEME <password>        # deletes your character for good
    /msg idlerpg MERGE <name> <password>   # fold another character into this one

The network blocks private messages from brand new connections, so wait about
two minutes after connecting before registering.

Logins survive the bot restarting: it logs you back in when it rejoins, as
long as you are connected from the same `nick!user@host` - which a bouncer
keeps stable. Quitting, parting, being kicked and `LOGOUT` end a login; a
netsplit does not, and costs nothing.

**Discord** — send the bot a **direct message**. Registering or logging in
gives you the game role, which is what lets you see the game channel and what
keeps your character idling. Reacting to the bot's pinned note gets you the
role too, and a DM explaining how to register if you have no character yet:

    !register <name> <password> <class>
    !login <name> <password>
    !whoami
    !align <good|neutral|evil>
    !newpass <current> <new>
    !removeme <password>
    !merge <name> <password>

`!register`, `!login`, `!merge`, `!newpass` and `!removeme` carry a password,
so they are refused in a channel; the bot deletes the message where it can and replies privately.

**Both at once** — log in on the other platform with the same name and
password. You are then one character on both, still earning exactly one second
per second: being in two places is neither a penalty nor a shortcut. If you
already registered on each, log in as the one to keep and `MERGE` the other
into it; it keeps the better level, timer and item per slot, never the sum.

## The two decisions that shape this

**One character, many platform identities.** A `Player` is the character;
`PlatformIdentity` rows attach an IRC or Discord account to it. The original
keys a player to an IRC nick, which cannot represent someone playing from two
places.

**You earn time while present and silent on at least one linked platform.** IRC
idling (in the channel and quiet) and Discord idling (holding the game role and
not posting) are not the same thing, and conflating them makes the game unfair
or farmable. Losing the role counts as parting and leaving the server as
quitting. Without an opt-in role configured, Discord status stands in for it:
online, idle and dnd count as present.
Crediting the character rather than each connection is what stops two platforms
paying twice.

## What is implemented

Levelling, items including uniques, single and team battles, hand of god,
calamities, godsends, war between the map's quadrants, the good and evil
alignment events, quests in both timed and journey forms, the world map, all
seven penalty types, channel topics, and a website with standings, a map, quest
status, per-player pages and an event feed.

Not implemented: the original's admin commands (`PAUSE`, `DELOLD`, `JUMP` and
friends) and items decaying where they are dropped on the map.

## Tuning

`rpstep` defaults to **1.12**, not upstream's 1.16.

| rpstep | to level 60 | level 80 | level 100 |
|--------|-------------|----------|-----------|
| 1.16 (upstream) | 319.8d | 4.2y | 8.6y |
| 1.12 (default) | 51.9d | 1.0y | 3.0y |

The level-60 cap that flattens the curve to +1 day per level is often described
as the fix for unreachable high levels. It is not sufficient: at 1.16 a single
level at 60 already costs about 51 days, so the linear term is roughly 2% of
the step. `rpstep` is the real lever. `tests/test_rules.py` asserts the upstream
numbers too, so the reasoning stays visible rather than becoming an unexplained
constant.

Event odds are expressed as rates - once per N days per online player - and
scaled by real elapsed time. The original rolls them once per tick, which ties
its probabilities to how often it happens to wake up; here changing the tick
length does not change the game.

## Configuration

| Variable | Default | |
|---|---|---|
| `DATABASE_URL` | `sqlite:///idlerpg.db` | Postgres works too |
| `IRC_HOST` / `IRC_PORT` | `irc.129irc.com` / `6697` | |
| `IRC_TLS` / `IRC_TLS_VERIFY` | `true` / `true` | |
| `IRC_NICK` / `IRC_CHANNEL` | `idlerpg` / `#idlerpg` | |
| `IRC_NICKSERV_PASSWORD` | | identifies on connect |
| `IRC_NICKSERV_EMAIL` | | registers the nick if unregistered |
| `DISCORD_TOKEN` | | omit to run IRC only |
| `DISCORD_CHANNEL_ID` | | the game channel |
| `DISCORD_OPTIN_ROLE_ID` | | the game role; registering grants it |
| `DISCORD_OPTIN_CHANNEL_ID` | | where the bot pins how to join - pick one everyone can see |
| `TICK_SECONDS` | `5` | |
| `TOPIC_SECONDS` | `36000` | Discord throttles channel edits hard |
| `RP_BASE` / `RP_STEP` | `600` / `1.12` | |

The bot needs a **descriptive realname** on networks running UnrealIRCd's
`antirandom` module, which kills clients whose nick, ident and realname look
machine-generated.

On Discord it needs the **server members** and **message content** intents -
members is how it sees who holds the game role - and the **presence** intent
when no opt-in role is configured, since status is then what decides who is
idling. Manage Roles is needed for the game role (and the bot's own role must
sit above it), Pin Messages to pin its note, Manage Channels for the topic.

## Development

```bash
python3 -m venv venv
./venv/bin/pip install -e '.[dev]'
./venv/bin/python -m pytest
```

Adapters hold no game rules: they report presence and relay commands, and the
engine decides what that means. The clock lives in the engine rather than an
adapter, so one tick drives the world and its announcements reach every
platform - an adapter owning it would credit time twice and leave the other
side silent.
