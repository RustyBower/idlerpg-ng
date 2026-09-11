# Changelog

Newest first. Lines marked *Operators* matter only to whoever runs the bot.
Releases before 0.11.0 are in the git history.

## 0.17.0 - 2026-09-11

- **Fights.** Once a day, from level 10, `FIGHT <name>` (`!fight` on Discord)
  challenges someone in the game and no more than 5 levels below you. The
  winner takes 5% of the loser's remaining time, at most 5% of their own
  level's cost; whoever is challenged is shielded for a day. `FIGHT` alone
  shows who is in reach.
- The rules were settled in simulation first: `python -m idlerpg.fairness`
  pits candidate rules against bullies, underdogs and prestiged veterans in a
  realm shaped like the live one, and at the level-60 wall.

## 0.16.2 - 2026-09-11

- **Voice on IRC.** Players are voiced while they are logged in and in the
  channel, and lose it when they log out, as in the original. It takes ops in
  the channel, which the bot already has for the topic.
- The bot paces what it says on IRC - a few lines at once, then one every
  two seconds, with replies to players ahead of channel news - so a busy
  moment can no longer get it disconnected for flooding.
- NPCs that join together no longer level up in the same moment ever after.
- *Operators:* `IRC_VOICE=false` turns voicing off.

## 0.16.1 - 2026-09-11

- **The map has a heartland.** Characters drift through the middle 80% of the
  map and wander back in from the wilds at its rim, instead of vanishing off
  one edge and reappearing at the other. New characters start inside it, and
  the realm's places - so its journeys - now lie inside it too.
- The website has a changelog: **what's new**, linked from every page.

## 0.16.0 - 2026-09-11

- **NPCs** make up the numbers in a small realm: they join while it is quiet
  and leave as people arrive, keeping their level for next time. They idle,
  talk and wander off like an average player, never fail a quest, never
  prestige, and are marked `NPC` on the site.
- **Prestige.** From level 60, `PRESTIGE` shows what starting over would do
  and `PRESTIGE confirm` does it: back to level 0 with fresh items, keeping
  name, alignment and perks, for a ★ on the site - where prestige now ranks
  first - and points: two for reaching 60, one more for every five levels past
  it. `PERKS` lists what they buy and `PERK <name>` buys a rank: swiftness,
  composure, fortune, warding, heirloom, stride and champion, and endurance,
  which opens only after ten ranks elsewhere.
- **Level 60 is a wall.** Past 60 each level costs a quarter more than the
  last, where it used to add a day: 70 is now about seven months past 60, and
  80 six years. Endurance eases the wall back toward the ordinary curve.
  Penalties past 60 grow as before.
- **Smaller penalties at high levels.** They now grow 10% a level, not 14%,
  which outran the levels themselves. A typical player keeps about 40% more of
  each day, and no alignment gains much more from it than another.
- The website's climb table follows the realm's actual curve.
- Fixed: the help said good lands the most critical strikes; evil does.
- Fixed: the website logged a traceback whenever a visitor hung up early.
- The simulator runs many seeded realms at once, reports how sure it is, and
  models kinds of player (`--profile quiet|average|chatty`) and curves
  (`--rp-step`, `--penalty-step`, `--post-cap-step`).
- *Operators:* `RP_POST_CAP_STEP` (default `1.25`; `linear` for the original).
  `NPC_MAX` (default `0`, none) and `NPC_REALM` (default `12`) turn NPCs on.

## 0.15.0 - 2026-09-10

- **Admin commands**, by `/msg` on IRC and by DM on Discord: `INFO`, `PAUSE`,
  `SILENT`, `TOPIC`, `RESTART`, `DEL`, `DELOLD`, `MKADMIN`, `DELADMIN`,
  `CHPASS`, `CHUSER`, `CHCLASS`, `PUSH`, `MOVE`, `HOG`, `PIT`, and `EVENT` to
  fire any event on demand. `ADMIN` lists them.
- Pause and silence survive a restart. While paused, nothing earns and nothing
  costs anything.
- A balancing simulator: `python -m idlerpg.simulate` runs the real engine with
  simulated players and reports how each alignment fares.
- *Operators:* `IDLERPG_ADMINS` names the characters that are always admins. A
  listed name cannot be registered by anyone else.

## 0.14.0 - 2026-09-10

- **Nine alignments.** Alignment gains a law-chaos axis: `ALIGN lawful good`,
  `ALIGN chaotic`, `ALIGN true neutral`. Everyone starts neutral on it.
  - Lawful: 10% smaller penalties, calamities and godsends half as hard, and
    likelier to be chosen for quests.
  - Chaotic: calamities and godsends half as hard again, will fight at any
    level, and attracts the odd random event.
  - True neutral: tugged now and then toward the realm's middle level.
- Every line of game text taken from the original bot is reworded in the
  realm's own voice.
- Fixed: three messages from 0.13.0 ran words together ("towardlevel").

## 0.13.0 - 2026-09-10

- The realm's own lore: thirty named places on the map, and composed
  calamities, godsends and quests - tens of thousands of distinct lines.
- Times read as durations, like `3d 4h`, everywhere. A level-up says when the
  next one is due.
- On IRC a penalty tells you what it cost; on Discord the message gets an ⏳.
- `NEWPASS` and `REMOVEME` on both platforms.
- Logins are announced to the realm. The bot answers CTCP `VERSION`, and the
  website shows the running version.
- *Operators:* `EVENTS_FILE` mixes in lines from your own copy of the classic
  `events.txt`, which cannot be bundled.

## 0.12.1 - 2026-09-10

- Names and classes can no longer garble the channel with right-to-left
  overrides or colour codes. New names are letters and digits in any script,
  plus `- _ . '`, up to 16 characters.
- Everything the bot says is cleaned on the way out, and Discord announcements
  cannot ping anyone.
- Fixed: looking up a name treated `_` and `%` as wildcards.

## 0.12.0 - 2026-09-10

- Journey quests walk the party to each waypoint, and give up after a day
  without blame. Before, a journey could never finish.
- A failed quest sets back the party only, and then the gods rest for 12 hours.
- War moves clocks: the winners 15% closer to their next level, the routed 15%
  further.
- You earn only while in `#idlerpg`, and a second login from one nick ends the
  first.
- The bot takes a stand-in nick when its own is in use, and takes it back.
- *Operators:* the website reads the bot's map and curve settings.

## 0.11.3 - 2026-09-10

- `ALIGN good|neutral|evil` and `!align`. Alignment had effects, but nothing
  could set it.

## 0.11.2 - 2026-09-10

- IRC logins survive the bot restarting: it logs you back in by
  `nick!user@host`. A netsplit costs nothing.
- Reacting to the pinned note DMs anyone without a character how to register.

## 0.11.1 - 2026-09-10

- The pinned note on how to join survives restarts and channel moves without
  reposting.

## 0.11.0 - 2026-09-10

- Logging in on the other platform links your character to it. `MERGE` takes a
  name and password.
- On Discord, holding the game role is what makes you idle, and registering
  grants it.
