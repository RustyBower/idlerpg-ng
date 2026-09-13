# Changelog

Newest first. Lines marked *Operators* matter only to whoever runs the bot.
Releases before 0.11.0 are in the git history.

## 0.28.0 - 2026-09-12

- **Your page says what happened to you.** Every player's page now carries
  their own history - the calamities, godsends, hands of god, finds, fights,
  level-ups and quests the realm put them through, newest first. Asked for by
  antiroach, who remembered the old version having one.
- The events that happen *to* one person - the Hand of God, calamities,
  godsends, chaos, the scales of the realm, and Hallowtide's knock at the
  door - now record whose they were, as level-ups and finds already did. Older
  entries, and the ones with two people in them, are found by name instead, so
  a history reaches back past this release.

## 0.27.0 - 2026-09-12

- **Logins follow your services account, not just your address.** The bot now
  negotiates IRC capabilities, and where the network says who is identified to
  services it remembers that alongside the connection. A login is resumed by
  account first, which holds across a new address, a reconnect from anywhere,
  and a cloak applied a moment after joining. Nobody has to be registered:
  without an account the connection's `nick!user@host` works exactly as before.
- **A vhost landing late no longer strands you.** Services often apply a vhost
  after you have joined, which silently invalidated the address the bot had
  remembered - and nothing ever re-checked, so you stayed logged out until you
  logged in by hand. `CHGHOST` is now followed, and identifying after joining
  logs you straight in.
- When somebody the bot has seen play is *not* logged back in, it now says so
  in the log, with where they are and what it remembered. The miss used to be
  silent, which is indistinguishable from the bot being broken.
- *Operators*: capabilities are taken only if offered - `account-notify`,
  `extended-join`, `chghost` and `multi-prefix` - and a server that speaks no
  CAP at all registers exactly as it did before. One added column,
  `platform_identity.login_account`, applied on start like the others.

## 0.26.0 - 2026-09-12

- **The eight uniques do something now.** Each grants what its story suggests
  on top of being worth a great deal: the Lantern of Small Mercies softens
  calamities, the Kumquat of Ages quickens levels, the Last Honest Ledger and
  the Bell That Must Not Ring shrink penalties, the Moon-Rake strengthens
  godsends, the Crown of Minor Kings and the Door That Was a Mimic add battle
  strength, and the Seven-League Courser carries a quest party as it always
  has. The find announcement and your page on the site say what each one does.
- Grants are written as ranks of the prestige perks and added by the single
  function every rule already reads, so an item and a bought rank compose, and
  nothing that applies a modifier had to learn that items exist. Each unique
  now carries its own tag to key that on; nobody held one yet, so nothing was
  lost.
- *Judgement, not measurement*: these are 2-5% modifiers on items that turn up
  about once in a climb from 1 to 80, well below what the simulator can
  resolve - it cannot separate 2% from its own noise without far more seeds
  than the effect is worth. They are set by eye, and said so rather than
  blessed by a measurement that could not have failed. The one to watch is
  battle strength, where the Crown and the Door alongside five bought ranks
  reach +16%.

## 0.25.0 - 2026-09-12

- **Items left lying on the map.** The item you replace is no longer thrown
  away if it was worth keeping: a unique always, or gear better than your own
  average, is left where you stood. Anyone who drifts within fifty squares
  takes it if it beats what they carry, leaving theirs in its place. Nothing
  is created - a dropped item was already in play - so this is never a second
  source of loot on top of level-ups. About seven items a week are worth
  leaving, 46% of them are found, and the map carries nought to three at a
  time; everything else is discarded exactly as before.
- **The world map updates as you watch it.** It swaps in a fresh map every
  fifteen seconds without reloading the page, and draws what is lying about,
  each marker naming the item's level, what it is and who left it.
- **The realm is told what a new version brought**, once, when it comes up on
  one: the headlines from this file and a link to the rest. A restart says
  nothing, and neither does a version it has already announced, so a crash
  loop cannot spam the channel.
- *Measurement*: no measurable cost to the realm's pace. Over eight seeds the
  difference against a control with pickups switched off is -0.6% +/- 1.6%,
  which does not clear zero. An earlier four-seed run suggested a 1.4% drag
  and did not survive four more seeds, one of which swung 4.3% the other way.
  Alignment spread sits inside the harness's noise at this sample size, and
  nothing in the rule looks at alignment.

## 0.24.0 - 2026-09-11

- **A weekly recap**, posted to both platforms on Sunday evening (18:00 UTC):
  levels gained, fights fought, quests finished and items found over the week,
  who climbed hardest, who won most, who is new, and who leads the realm.
  `RECAP`, or `/recap` on Discord, shows the week so far and when the next one
  lands.
- Duels, battles, finds and arrivals now record who they were about, the way
  level-ups already did, so a week can be attributed to whoever earned it. The
  site's per-player charts are unchanged: they read level-ups alone.
- *Operators*: nothing to do. The recap's window is a range of event ids
  rather than a span of clock time, so a restart can neither lose a week nor
  report one twice, and the first run after upgrading only starts the clock -
  the first recap covers the week that follows, not all of recorded history.

## 0.23.0 - 2026-09-11

- **A battle on levelling**, as the original does, from level 25 - where
  challenges stop being declined. In simulation it lifts battles from 21 to
  30 a week each and the realm's pace from 0.900 to 0.917, and *narrows* the
  gap between alignments (4.9% to 3.1%). At the level-60 wall, where
  level-ups are rare, it changes almost nothing.
- **The eight named uniques**, each in its own slot behind its own level,
  worth far more than the curve can roll: the Lantern of Small Mercies, the
  Kumquat of Ages, the Seven-League Courser, the Last Honest Ledger, the
  Bell That Must Not Ring, the Moon-Rake, the Crown of Minor Kings and the
  Door That Was a Mimic. A climb from 1 to 80 turns up about one. The
  Courser is a mount: it carries a whole quest party, two ranks of Stride
  faster. Nameless uniques are gone - they out-numbered the eight and made
  their worth meaningless. Ones already found keep their value.
- **Rivals and trophies.** Whoever beats you most is named in `WHOAMI`, on
  the `/whoami` card and on your page; beating someone ten or more levels
  above you leaves a trophy to keep.
- **Not shipped: the original's ±10% good-and-evil battle modifier.** Good
  gains too much: the gap between alignments doubles (3.1% to 7.7%), with
  chaotic good +4.7% and neutral evil -3.0% beyond luck. At ±5% it is still
  +3.4% for lawful good and a 5.7% spread. The switch (`MORAL_BATTLE`)
  stays at 1.0 for everyone.

## 0.22.0 - 2026-09-11

- **Slash commands on Discord.** Every command is also a slash command -
  `/register`, `/login`, `/whoami`, `/fight`, `/achievements`, `/align` with
  its choices, `/prestige`, `/perk`, and `/admin` for admins - answered so
  only you see it. A slash command's options reach only the bot, so a
  password no longer needs a DM.
- **A Play button** on the pinned note opens a form to make a character, and
  **I play on IRC** one to log in as yours; a player who has one is seated at
  once. The buttons keep working across restarts.
- **`/whoami` is a card**: level, next level, alignment, items, prestige,
  achievements and keepsakes at a glance.
- **The level charts go back to the beginning.** Level-ups have been
  announced since the first day, and the old ones are read back out of the
  event log and linked to their characters, once, at startup - so a chart
  shows the whole climb rather than starting at 0.21.0.
- *Operators:* slash commands need the bot invited with the
  `applications.commands` scope. Without it they are skipped with a warning,
  and the `!` commands work as before.

## 0.21.0 - 2026-09-11

- **Achievements.** Earned once and announced: reaching levels 10, 25, 40
  and 60, a first and third prestige, taking a side in `ALIGN`, a first and
  a tenth fight won, beating someone five levels above you, completing one
  quest and five, a week without a single penalty, a unique item - and
  Loose Lips, for breaking a quest's silence. `ACHIEVEMENTS` lists yours;
  your page on the site lists them all, and how to earn each.
- **Seasonal achievements and keepsakes**, after the holiday achievements of
  long-running online games. Hallowtide: knock on doors, gather treats,
  collect every mask and costume, find the Horseman's lantern. Midwinter:
  gifts for everyone about on Midwinter Day, the longest night, a snowball
  fight, a full stocking. Springtide: an egg hunt, the golden egg, a full
  basket, five levels in the season. Keeping a season counts, and earning
  all of one grants its title - the Lantern-Bearer, Keeper of the Long
  Night, the Blossoming - worn beside the name. None of it touches a clock,
  an item or a fight.
- **Level history.** Level-ups now record who and which level, and each
  player's page charts it as it fills in.

## 0.20.0 - 2026-09-11

- **Seasons bend a rule.** Hallowtide brings trick or treat - now and then a
  small treat or trick, even odds, worth up to 5% of a level. Midwinter's long
  nights have everyone earning 5% faster. In Springtide's fresh starts, anyone
  below the middle level of those about earns 10% faster.
- **Seasonal honours.** When a season that ran its course ends, the realm
  names its three most devoted idlers - the most progress toward their next
  levels, so a newcomer can beat a veteran - and everyone who idled through
  half of it wears its badge, 🎃 ❄ 🌱, beside their name on the site. `WHOAMI`
  lists a character's honours. NPCs are not honoured, a prestige keeps what
  a season gave, and a season an admin forced honours nobody.
- The site says when the next season begins, from two weeks before it, and
  what it brings: "Hallowtide begins on 1 October: until 31 October, a third
  of the realm's calamities, godsends and quests will come with headless
  horsemen, pumpkins with ambitions and trick-or-treating goblins." While a
  season lasts, its banner says when it ends.
- The how-to-play page explains seasons, with each one's dates and what to
  expect.

## 0.19.0 - 2026-09-11

- **Seasons.** In Hallowtide (October), Midwinter (15 December to 6 January)
  and Springtide (20 March to 20 April) a third of the realm's calamities,
  godsends and quests come from the season's own creatures, helpers,
  treasures and cargo. The realm is told when each begins and ends, and the
  site shows a banner while one lasts. Only the words change: how often
  events happen and what they do stay the same.
- Admins: `SEASON` shows the season; `SEASON <name>` forces one, `auto`
  follows the calendar, `off` has none.

## 0.18.0 - 2026-09-11

- **Meetings on the map.** Two characters from level 10 who land on the same
  tile fight, as in the original, on `FIGHT`'s terms - at most once a day for
  any pair, and never two questers on the same quest. Tested first: about two
  meetings a week each, and no level or strategy gained or lost beyond luck,
  in a realm like the live one or at the level-60 wall.
- **Names that pass for others are refused.** A new name, or an admin's
  rename, may not mix alphabets, and may not pass for an existing
  character's or an admin's name by case, accents, width, digits for letters
  or letters of other alphabets that look Latin. Names already taken stand.

## 0.17.1 - 2026-09-11

- **Only the logged in speak in the game channel.** With `IRC_MODERATE` the
  bot keeps it moderated (`+m`) whenever it holds ops, so the voice that marks
  a logged-in player is also what lets them talk, and anyone who joins logged
  out is told, once, why they cannot and how to play.
- The how-to-play page says what the `+` by a name means.
- *Operators:* `IRC_MODERATE=true` turns moderation on.

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
