# idlerpg-ng

A game you play by doing nothing. Stay connected and stay quiet, and your
character levels up. Talking, parting, quitting and changing your nick all set
you back.

The rules come from the classic Perl IdleRPG (via
[RustyBower/idlerpg](https://github.com/RustyBower/idlerpg)); the architecture
does not. **One person is one character across IRC and Discord**, with a shared
engine, so the two sides are one game rather than two unrelated ones.

Running at [idlerpg.129irc.com](https://idlerpg.129irc.com/) on
[129irc](https://129irc.com). What changed in each release is in
[CHANGELOG.md](CHANGELOG.md).

## Playing

**IRC** — connect to `irc.129irc.com` on port `6697` with TLS, join `#idlerpg`:

    /msg idlerpg REGISTER <name> <password> <class>
    /msg idlerpg LOGIN <name> <password>
    /msg idlerpg WHOAMI
    /msg idlerpg ALIGN <lawful|neutral|chaotic> <good|neutral|evil>
    /msg idlerpg NEWPASS <current> <new>
    /msg idlerpg REMOVEME <password>        # deletes your character for good
    /msg idlerpg PRESTIGE                   # from level 60; then PERKS, PERK <name>
    /msg idlerpg MERGE <name> <password>   # fold another character into this one

The network blocks private messages from brand new connections, so wait about
two minutes after connecting before registering.

Logins survive the bot restarting: it logs you back in when it rejoins, as
long as you are connected from the same `nick!user@host` - which a bouncer
keeps stable. Quitting, parting, being kicked and `LOGOUT` end a login; a
netsplit does not, and costs nothing.

You earn only while you are in `#idlerpg`. A `LOGIN` from outside it logs you
in, and you start earning when you join. One nick holds one login: logging in
as another character ends the first.

**Discord** — send the bot a **direct message**. Registering or logging in
gives you the game role, which is what lets you see the game channel and what
keeps your character idling. Reacting to the bot's pinned note gets you the
role too, and a DM explaining how to register if you have no character yet:

    !register <name> <password> <class>
    !login <name> <password>
    !whoami
    !align <lawful|neutral|chaotic> <good|neutral|evil>
    !newpass <current> <new>
    !removeme <password>
    !prestige, !perks, !perk <name>
    !merge <name> <password>

`!register`, `!login`, `!merge`, `!newpass` and `!removeme` carry a password,
so they are refused in a channel; the bot deletes the message where it can and replies privately.

**Both at once** — log in on the other platform with the same name and
password. You are then one character on both, still earning exactly one second
per second: being in two places is neither a penalty nor a shortcut. If you
already registered on each, log in as the one to keep and `MERGE` the other
into it; it keeps the better level, timer and item per slot, never the sum.

## How the game plays

**Penalties.** Talking, changing nick, parting, quitting, being kicked and
logging out all add time, more at higher levels. On IRC the bot tells you what
each one cost; on Discord a message that cost time gets an ⏳. Leaving one
platform while you are still on the other costs nothing.

**Quests.** Once four players online are at level 40 or above, the gods may
choose them. A vigil lasts 12 to 24 hours. A journey walks the party, a step
every 30 seconds, to two named places in turn, and is abandoned without blame
after a day. Finishing takes a quarter off each quester's remaining time. If a
quester talks or leaves, the quest fails: each quester is set back fifteen
penalty steps, and no quest is offered for 12 hours.

**War.** Now and then the map's quadrants fight. A quadrant that beats both its
neighbours moves its players 15% closer to their next level; one that loses to
both is set back 15%. The original halves and doubles clocks, which in a realm
this size outweighs days of idling.

**Alignment** has two parts, as in the tabletop's nine: lawful, neutral or
chaotic, then good, neutral or evil. Good now and then prays with another good
player for time off, but lands the fewest critical hits in battle; evil lands
the most, and now and then steals a better item from a good player or pays its
dark patron.
Lawful takes 10% smaller penalties, feels calamities and godsends half as hard
and is likelier to be chosen for quests; chaotic feels them half as hard again,
will fight anyone, and attracts the odd random event. True neutral is tugged
now and then toward the realm's middle level. The numbers sit together at the
top of `events.py`, to be tuned as the realm is watched.

**Prestige.** From level 60 you may start over: `PRESTIGE` shows what it
would do, `PRESTIGE confirm` does it. You go back to level 0 with fresh items,
keep your name, alignment and perks, gain a ★ on the site - where prestige
ranks first - and earn points to spend with `PERK <name>`: two for reaching 60
and one more for every five levels past it. `PERKS` lists them: swiftness,
composure, fortune, warding, heirloom, stride and champion at a point a rank,
each capped, and endurance - which eases the wall past 60 - opening only after
ten ranks elsewhere, at two points a rank. It is never forced, but past 60
each level costs a quarter more than the last: 65 is seven weeks on, 70 half a
year, 80 six years. Waiting earns more points at a real price, and 60 is where
most characters will want to begin again.

**Fights.** Once a day, from level 10, `FIGHT <name>` challenges someone in
the game and no more than 5 levels below you; `FIGHT` alone shows who is in
reach. Both roll against their items, and the winner takes 5% of the
loser's remaining time, but never more than 5% of what the winner's own
level costs. Whoever is challenged is shielded from challenges for a day.
The rules come from `python -m idlerpg.fairness` (below): sized by the loser,
bullying newcomers wins next to nothing; capped by the winner, nobody wins
days at the level-60 wall; and with no bonus, fights move time between
players without making any. As in the original, two characters from level 10
who land on the same tile fight too, on the same terms - at most once a day
for any pair, and never two questers on the same quest.

**The map.** Characters drift at random through the middle 80% of the map,
the heartland, and wander back in from the wilds at its rim. The realm's
places - and so its journeys - all lie inside the heartland. The map no
longer wraps from one edge to the other.

**NPCs** make up the numbers in a small realm, so quests, team battles and
fights have enough people. While fewer than `NPC_REALM` characters have been
about in the last week, NPCs join - `NPC_MAX` at most - and they set off for
distant lands as people arrive, keeping their level for next time. They play
like an average player: they talk now and then and wander off, paying the same
penalties, but never on a quest, so no party loses one to a bot. They never
prestige, nobody can log in as one, and the site marks them `NPC`.

**Events** are composed from the realm's own lore: thirty named places on the
map, a cast of creatures and helpers, and small grammars that make tens of
thousands of distinct calamities, godsends and quests, mixed with hand-written
ones. The classic IdleRPG `events.txt` is not bundled - its licence forbids
redistributing it - but `EVENTS_FILE` can point at your own copy to mix its
lines in. In Hallowtide (October), Midwinter (15 December to 6 January) and
Springtide (20 March to 20 April) a third of them come from the season's own
creatures, helpers, treasures and quests, and the realm is told when each
begins and ends - the site says when the next one begins from two weeks
ahead. Only the words change, never how often events happen or
what they do. An admin's `SEASON` shows, forces or switches off the season.
Times read as durations throughout, like `3d 4h`.

## Running it

Admins are characters with the admin flag. `IDLERPG_ADMINS` names the
characters that always are - set it in the deployment, never in this
repository - and `MKADMIN` and `DELADMIN` manage the rest. Once a name is
listed, nobody else can register it, so deleting an owner's character does not
hand the rights to whoever takes the name next. Admin commands go by `/msg` on
IRC and by DM on Discord (`!info`, `!pause on` and so on); `ADMIN` lists them.

    INFO                        version, uptime, who is online, alignments, quest
    PAUSE on|off                stop the clock: nothing earns or costs anything
    SILENT on|off               events carry on and are logged, but nothing is said
    TOPIC [text|clear]          set the topic now, optionally led by a note
    RESTART                     exit; Kubernetes starts it again and logins resume
    DEL <name> confirm          delete a character
    DELOLD <days> [confirm]     list, then delete, characters not seen for that long
    MKADMIN <name>              DELADMIN <name>
    CHPASS <name> <password>    CHUSER <name> <new name>    CHCLASS <name> <class>
    PUSH <name> <seconds>       toward the next level; negative pushes back
    MOVE <name> <x> <y>         HOG [name]                  PIT <name> <name>
    EVENT <kind> [name]         hog, calamity, godsend, chaos, battle, team, war,
                                goodness, evilness, balance or quest

Pause and silence survive a restart. The original's `REHASH`, `RELOADDB`,
`JUMP`, `BACKUP`, `DIE` and `CLEARQ` have no counterpart: settings come from the
deployment, the database is always live, backups belong to the cluster, and
scaling the deployment to zero stops the bot. Its raw-IRC command is left out:
a stolen admin password should not be able to make the bot say anything.

## Balancing

`python -m idlerpg.simulate` runs the real engine against an in-memory
database with simulated players, and reports how each alignment fared:

    python -m idlerpg.simulate --days 60 --per-alignment 5 --talk 1 --step 900
    python -m idlerpg.simulate --players "lawful good:5,chaotic evil:5"
    python -m idlerpg.simulate --set LUCK.chaotic=1.25 --json run.json
    python -m idlerpg.simulate --start-level 55 --days 180 --post-cap-step linear

The number to watch is pace - progress earned over time elapsed. Idling alone
gives 1.0 less time away; events push it up and penalties pull it down.
`--set` tries a tuning number from `events.py` without editing it, and runs are
seeded, so a change can be compared against the same luck.

`python -m idlerpg.fairness` tests a feature's fairness before it is built -
first `FIGHT`, the proposed daily duel. It runs a realm shaped like the live
one, bolts candidate rules and strategies (bullying the weakest, fighting at
random, aiming high, abstaining) onto the engine from outside, and reports
each level tier's and strategy's pace against the same seed without fights.

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

Levelling, items including uniques, single and team battles, the Hand of God,
calamities, godsends, war between the map's quadrants, the nine alignments and
their events, quests as vigils and journeys, the world map, all seven penalty
types, logins that survive restarts, channel topics, admin commands, a
balancing simulator, and a website with standings, a map, quest status,
per-player pages and an event feed.

Not yet: the original's eight named uniques, a battle at each level-up, and
items left lying on the map.

## Tuning

`rpstep` defaults to **1.12**, not upstream's 1.16, and past level 60 the
curve compounds harder, at **1.25**, where upstream adds a day a level.

| curve | to level 60 | level 65 | level 70 | level 80 |
|-------|-------------|----------|----------|----------|
| 1.16, then +1 day (upstream) | 320d | 1.6y | 2.4y | 4.2y |
| 1.12, then +1 day | 52d | 93d | 159d | 1.0y |
| 1.12, then 1.25 (default) | 52d | 103d | 259d | 6.0y |
| 1.12, then 1.5 | 52d | 134d | 2.1y | 114y |

The level-60 cap that flattens the curve to +1 day per level is often described
as the fix for unreachable high levels. It is not sufficient: at 1.16 a single
level at 60 already costs about 51 days, so the linear term is roughly 2% of
the step. `rpstep` is the real lever, and 1.12 brings 60 within two months.

Past 60 the question changes. A day a level on top of a six-day level is barely
a curve at all, so the leaders would keep climbing and their lead would only
grow. Compounding at 1.25 instead makes 60 a wall - each level a quarter
dearer than the last - and prestige the way on. The Endurance perk eases the
wall toward the ordinary 1.12 step, all the way at five ranks: 70 then comes
3.6 months past 60 rather than 6.8. Penalties keep upstream's day a level past
60 whatever `RP_POST_CAP_STEP` says. `tests/test_rules.py` asserts the upstream
numbers too, so the reasoning stays visible rather than becoming an unexplained
constant.

Penalties grow at **1.10** a level. Upstream's 1.14 sits below its 1.16
curve; kept beside a 1.12 curve it outgrew the levels, and in simulation a
typical player spent about half of every day paying penalties off. Across 40
seeded 18-player realms idling 60 days from level 30, 1.10 lifts a typical
player's pace from 0.58 to 0.98 and narrows the gap between the best and worst
alignment from 7% to 2%, within luck; for chatty players, from 21% to 6%.

Event odds are expressed as rates - once per N days per online player - and
scaled by real elapsed time. The original rolls them once per tick, which ties
its probabilities to how often it happens to wake up; here changing the tick
length does not change the game.

## Configuration

| Variable | Default | |
|---|---|---|
| `DATABASE_URL` | `sqlite:///idlerpg.db` | Postgres works too |
| `IDLERPG_ADMINS` | | character names that are always admins, e.g. `Alice,Bob` |
| `IRC_HOST` / `IRC_PORT` | `irc.129irc.com` / `6697` | |
| `IRC_TLS` / `IRC_TLS_VERIFY` | `true` / `true` | |
| `IRC_NICK` / `IRC_CHANNEL` | `idlerpg` / `#idlerpg` | |
| `IRC_USER` / `IRC_REALNAME` | `idlerpg` / `IdleRPG` | see the note on realnames below |
| `IRC_RECONNECT_SECONDS` | `30` | |
| `IRC_NICKSERV_PASSWORD` | | identifies on connect, and ghosts a stale connection holding the nick |
| `IRC_NICKSERV_EMAIL` | | registers the nick if unregistered |
| `IRC_VOICE` | `true` | voice players while they are logged in; needs ops in the channel |
| `IRC_MODERATE` | `false` | keep the channel `+m`, so only logged-in (voiced) players can speak, and tell anyone joining logged out how to play |
| `DISCORD_TOKEN` | | omit to run IRC only |
| `DISCORD_CHANNEL_ID` | | the game channel |
| `DISCORD_OPTIN_ROLE_ID` | | the game role; registering grants it |
| `DISCORD_OPTIN_CHANNEL_ID` | | where the bot pins how to join - pick one everyone can see |
| `DISCORD_OPTIN_EMOJI` | 🎲 | the reaction on that note |
| `TICK_SECONDS` | `5` | |
| `TOPIC_SECONDS` | `36000` | Discord throttles channel edits hard |
| `SITE_URL` | `https://idlerpg.129irc.com/` | leads the channel topic |
| `RP_BASE` / `RP_STEP` | `600` / `1.12` | the level curve; the website reads these too |
| `RP_PENALTY_STEP` | `1.10` | how fast penalties grow with level |
| `RP_POST_CAP_STEP` | `1.25` | how much more each level past 60 costs than the last; `linear` for the original's day a level |
| `NPC_MAX` | `0` | the most NPCs at once; `0` for none |
| `NPC_REALM` | `12` | NPCs join while fewer characters than this have been about in the last week, and leave once there are 3 more |
| `MAP_X` / `MAP_Y` | `500` / `500` | the realm's size; the website reads these too |
| `EVENTS_FILE` | | a classic `events.txt` whose lines join the realm's own |
| `LOG_LEVEL` | `INFO` | `DEBUG` logs every IRC line |

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
