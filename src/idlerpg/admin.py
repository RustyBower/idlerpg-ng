"""Admin commands, shared by IRC and Discord.

An adapter passes the logged-in character and the words after the verb; this
checks that character is an admin, acts through the engine, and returns the
reply. Nothing here knows one platform from the other.

What the original's admin commands became: DEL, DELOLD, MKADMIN, DELADMIN,
CHPASS, CHUSER, CHCLASS, HOG and PUSH much as they were; MOVE and PIT from the
ptnet fork; EVENT to fire any event on demand, for testing and tuning; PAUSE,
SILENT, TOPIC, INFO and RESTART for running the bot. REHASH, RELOADDB, JUMP,
BACKUP, DIE and CLEARQ have no counterpart: settings come from the
deployment, the database is always live, Velero takes the backups, scaling
the deployment to zero stops it, and there is no send queue to clear. The
raw IRC command is left out on purpose: a stolen admin password should not
be able to make the bot say anything on the network.
"""

from __future__ import annotations

import time
from collections import Counter
from datetime import timedelta, timezone

from . import __version__, events, quests
from .engine import Engine, RegistrationError, events_map_x, events_map_y
from .models import Player, utcnow
from .text import duration

EVENT_KINDS = ("hog", "calamity", "godsend", "chaos", "battle", "team", "war",
               "goodness", "evilness", "balance", "quest")

HELP = (
    "Admin: INFO | PAUSE on|off | SILENT on|off | TOPIC [text|clear] | RESTART"
    " | DEL <name> confirm | DELOLD <days> [confirm] | MKADMIN <name>"
    " | DELADMIN <name> | CHPASS <name> <password> | CHUSER <name> <new name>"
    " | CHCLASS <name> <class> | PUSH <name> <seconds> (toward the next level;"
    " negative pushes back) | MOVE <name> <x> <y> | HOG [name]"
    " | PIT <name> <name> | EVENT <" + "|".join(EVENT_KINDS) + "> [name]"
)


class Refused(Exception):
    """An admin command that cannot be carried out, and why."""


def _player(engine: Engine, name: str | None) -> Player:
    player = engine.find_player(name) if name else None
    if player is None:
        raise Refused(f"There is no character called {name}." if name else "Name a character.")
    return player


def _need(args: list[str], count: int, usage: str) -> list[str]:
    if len(args) < count:
        raise Refused(usage)
    return args


def _aware(value):
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _target(engine: Engine, args: list[str], index: int = 0) -> Player:
    """The named character, or a random one who is online."""
    if len(args) > index:
        return _player(engine, args[index])
    online = [p for p in engine.all_players() if p.is_idling]
    if not online:
        raise Refused("Nobody is online.")
    return engine.rng.choice(online)


def _announce(engine: Engine, outcomes) -> str:
    outcomes = [outcomes] if hasattr(outcomes, "message") else list(outcomes)
    if not outcomes:
        return "Nothing happened: the event had nobody suitable to happen to."
    engine.announce(outcomes)
    return "Done: " + " ".join(o.message for o in outcomes)


# ------------------------------------------------------------------ commands

def _admin(engine, actor, args):
    return HELP


def _info(engine, actor, args):
    players = engine.all_players()
    online = sum(p.is_idling for p in players)
    state = [f"idlerpg-ng {__version__}",
             f"up {duration(time.time() - engine.started)}",
             f"{online}/{len(players)} online"]
    if engine.paused:
        state.append("PAUSED")
    if engine.silent:
        state.append("SILENT")
    quest = quests.active_quest(engine.session)
    if quest is not None:
        state.append(f"quest: {quest.text[:60]}")
    else:
        state.append("quests resting" if quests.resting(engine.session) else "no quest")
    counts = Counter(p.alignment_name for p in players)
    state.append("alignments: " + ", ".join(f"{n} {c}" for n, c in counts.most_common()))
    return " | ".join(state)


def _switch(args: list[str], verb: str) -> bool:
    if not args or args[0].lower() not in ("on", "off"):
        raise Refused(f"{verb} on|off")
    return args[0].lower() == "on"


def _pause(engine, actor, args):
    on = _switch(args, "PAUSE")
    engine.set_setting(engine.PAUSED_KEY, "1" if on else "")
    return ("Paused: the clock has stopped, and nothing earns or costs anything "
            "until PAUSE off." if on else "Unpaused: the clock is running.")


def _silent(engine, actor, args):
    on = _switch(args, "SILENT")
    engine.set_setting(engine.SILENT_KEY, "1" if on else "")
    return ("Silent: events still happen and are logged, but nothing is "
            "announced." if on else "Announcing again.")


def _topic(engine, actor, args):
    if args and args[0].lower() == "clear":
        engine.set_setting(engine.TOPIC_KEY, "")
    elif args:
        engine.set_setting(engine.TOPIC_KEY, " ".join(args)[:200])
    engine.topic_requested = True
    return "The topic will be set at the next tick."


def _restart(engine, actor, args):
    engine.restart_requested = True
    return "Restarting. Logins resume when I am back."


def _del(engine, actor, args):
    if len(args) < 2 or args[1].lower() != "confirm":
        raise Refused("DEL <name> confirm - deletes the character for good.")
    target = _player(engine, args[0])
    if target.id == actor.id:
        raise Refused("Use REMOVEME to delete your own character.")
    name = target.name
    engine.delete_player(target, f"{name} has been removed from the realm.")
    return f"{name} is deleted."


def _delold(engine, actor, args):
    try:
        days = int(args[0])
    except (IndexError, ValueError):
        raise Refused("DELOLD <days> [confirm] - deletes characters not seen for "
                      "that many days.") from None
    if days < 1:
        raise Refused("At least one day.")
    cutoff = utcnow() - timedelta(days=days)
    stale = [p for p in engine.all_players()
             if not p.is_idling and not p.is_admin and not p.npc  # NPCs tend themselves
             and _aware(p.last_login or p.created) < cutoff]
    names = ", ".join(p.name for p in stale) or "nobody"
    if len(args) < 2 or args[1].lower() != "confirm":
        return f"Not seen for {days} days: {names}. Add confirm to delete them."
    for p in stale:
        engine.delete_player(p, f"{p.name} has faded from the realm.")
    return f"Deleted {len(stale)}: {names}."


def _mkadmin(engine, actor, args):
    target = _player(engine, _need(args, 1, "MKADMIN <name>")[0])
    engine.set_admin(target, True)
    return f"{target.name} is an admin."


def _deladmin(engine, actor, args):
    target = _player(engine, _need(args, 1, "DELADMIN <name>")[0])
    if engine.is_owner(target):
        raise Refused(f"{target.name} is an admin by the deployment's IDLERPG_ADMINS, "
                      f"and stays one.")
    engine.set_admin(target, False)
    return f"{target.name} is no longer an admin."


def _chpass(engine, actor, args):
    name, password = _need(args, 2, "CHPASS <name> <new password>")[:2]
    target = _player(engine, name)
    engine.reset_password(target, password)
    return f"{target.name}'s password is changed."


def _chuser(engine, actor, args):
    name, new = _need(args, 2, "CHUSER <name> <new name>")[:2]
    target = _player(engine, name)
    return f"Renamed to {engine.rename(target, new)}."


def _chclass(engine, actor, args):
    _need(args, 2, "CHCLASS <name> <class>")
    target = _player(engine, args[0])
    engine.set_class(target, " ".join(args[1:]))
    return f"{target.name} is now {target.character_class or 'classless'}."


def _push(engine, actor, args):
    name, raw = _need(args, 2, "PUSH <name> <seconds>")[:2]
    target = _player(engine, name)
    try:
        seconds = int(raw)
    except ValueError:
        raise Refused("PUSH <name> <seconds> - whole seconds, negative to push back.") from None
    engine.push(target, seconds)
    return f"{target.name} is now {duration(target.next_ttl)} from level {target.level + 1}."


def _move(engine, actor, args):
    name, x, y = _need(args, 3, "MOVE <name> <x> <y>")[:3]
    target = _player(engine, name)
    try:
        engine.move(target, int(x), int(y))
    except ValueError as exc:
        raise Refused(f"MOVE <name> <x> <y> - {exc}.") from None
    return f"{target.name} is at [{target.x},{target.y}]."


def _hog(engine, actor, args):
    return _announce(engine, events.hand_of_god(_target(engine, args), engine.rng))


def _pit(engine, actor, args):
    a, b = (_player(engine, n) for n in _need(args, 2, "PIT <name> <name>")[:2])
    if a.id == b.id:
        raise Refused("A character cannot fight themselves.")
    return _announce(engine, events.battle(a, b, engine.rng))


def _event(engine, actor, args):
    kind = args[0].lower() if args else ""
    if kind not in EVENT_KINDS:
        raise Refused("EVENT <" + "|".join(EVENT_KINDS) + "> [name]")
    online = [p for p in engine.all_players() if p.is_idling]
    rng, mx, my = engine.rng, events_map_x(), events_map_y()
    if kind in ("hog", "calamity", "godsend", "chaos"):
        fire = {"hog": events.hand_of_god, "calamity": events.calamity,
                "godsend": events.godsend, "chaos": events.chaos}[kind]
        return _announce(engine, fire(_target(engine, args, 1), rng))
    if kind == "battle":
        a = _target(engine, args, 1)
        others = [p for p in online if p.id != a.id]
        if not others:
            raise Refused("Nobody else is online to fight.")
        return _announce(engine, events.battle(a, rng.choice(others), rng))
    if kind == "balance":
        return _announce(engine, events.balance(_target(engine, args, 1), online, rng))
    if kind == "quest":
        started = quests.start(engine.session, online, rng, mx, my, force=True)
        if started is None:
            raise Refused("No quest: it needs four players online at level "
                          f"{quests.MIN_LEVEL} or above, and none already running.")
        return _announce(engine, started)
    fire = {"team": lambda: events.team_battle(online, rng, mx, my),
            "war": lambda: events.war(online, rng, mx, my),
            "goodness": lambda: events.goodness(online, rng),
            "evilness": lambda: events.evilness(online, rng)}[kind]
    return _announce(engine, fire())


COMMANDS = {
    "ADMIN": _admin, "INFO": _info, "PAUSE": _pause, "SILENT": _silent,
    "TOPIC": _topic, "RESTART": _restart, "DEL": _del, "DELOLD": _delold,
    "MKADMIN": _mkadmin, "DELADMIN": _deladmin, "CHPASS": _chpass,
    "CHUSER": _chuser, "CHCLASS": _chclass, "PUSH": _push, "MOVE": _move,
    "HOG": _hog, "PIT": _pit, "EVENT": _event,
}
VERBS = frozenset(COMMANDS)


def run(engine: Engine, actor: Player | None, verb: str, args: list[str]) -> str:
    """Carry out an admin command for ``actor``; returns the reply."""
    if actor is None or not actor.is_admin:
        return "That is an admin command."
    try:
        return COMMANDS[verb.upper()](engine, actor, args)
    except Refused as exc:
        return str(exc)
    except RegistrationError as exc:
        return f"Cannot: {exc}."
