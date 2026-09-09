#!/usr/bin/env python3
"""The website.

Shares the engine's SQLAlchemy models rather than re-implementing the schema in
SQL, so it works against SQLite or Postgres unchanged and cannot drift from
what the bot actually writes. Read-only throughout: it opens its own session
and never commits.

Password hashes live on Player.password_hash and are never read into a record.
"""

import html
import json
import os
import random
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, quote

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session, selectinload

from .models import EventLog, PenaltyRecord, Player, Quest, QuestParticipant

DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite:////data/idlerpg.db")
PORT = int(os.environ.get("PORT", "8080"))
NETWORK = os.environ.get("IRPG_NETWORK", "129irc")
CHANNEL = os.environ.get("IRPG_CHANNEL", "#idlerpg")
BOT_NICK = os.environ.get("IRPG_BOT", "idlerpg")
MAP_X = int(os.environ.get("IRPG_MAPX", "500"))
MAP_Y = int(os.environ.get("IRPG_MAPY", "500"))
RP_BASE = int(os.environ.get("IRPG_RPBASE", "600"))
RP_STEP = float(os.environ.get("IRPG_RPSTEP", "1.12"))

_engine = None


def db():
    """Lazily built so importing this module never opens a connection."""
    global _engine
    if _engine is None:
        _engine = create_engine(DATABASE_URL, pool_pre_ping=True, future=True)
    return _engine


ONLINE = ("ACTIVE", "AWAY", "active", "away")


def _present(identity) -> bool:
    return str(identity.presence).split(".")[-1] in ONLINE


def load_players():
    try:
        with Session(db()) as s:
            rows = s.scalars(
                select(Player).options(
                    selectinload(Player.identities), selectinload(Player.items)
                )
            ).all()
            penalties = {}
            for pid, kind, total in s.execute(
                select(PenaltyRecord.player_id, PenaltyRecord.kind,
                       func.sum(PenaltyRecord.seconds))
                .group_by(PenaltyRecord.player_id, PenaltyRecord.kind)
            ):
                penalties.setdefault(pid, {})[kind] = int(total or 0)

            players = []
            for p in rows:
                plats = {
                    str(i.platform).split(".")[-1].lower(): _present(i)
                    for i in p.identities
                }
                nick = next((i.display_name for i in p.identities if i.display_name), "")
                players.append({
                    "username": p.name, "level": p.level,
                    "class": p.character_class or "", "next": p.next_ttl or 0,
                    "nick": nick, "online": any(plats.values()),
                    "x": p.x or 0, "y": p.y or 0,
                    "created": p.created, "lastlogin": p.last_login,
                    "alignment": str(p.alignment).split(".")[-1].capitalize(),
                    "admin": bool(p.is_admin),
                    "platforms": plats,
                    "items": {i.slot: i.value for i in p.items},
                    "item_tags": {i.slot: (i.tag or "") for i in p.items},
                    "itemsum": sum(i.value for i in p.items),
                    "penalties": penalties.get(p.id, {}),
                })
    except Exception:
        return []
    players.sort(key=lambda q: (-q["level"], q["next"]))
    return players


def load_quest():
    try:
        with Session(db()) as s:
            quest = s.scalar(
                select(Quest).options(
                    selectinload(Quest.participants).selectinload(QuestParticipant.player)
                ).order_by(Quest.id.desc()).limit(1)
            )
            if quest is None or not quest.participants:
                return None
            out = {
                "text": quest.text, "type": quest.kind,
                "questers": [
                    {"name": qp.player.name, "x": qp.player.x, "y": qp.player.y}
                    for qp in quest.participants
                ],
                "destinations": [],
            }
            if quest.kind == 2:
                out["destinations"] = [
                    {"x": quest.x1, "y": quest.y1}, {"x": quest.x2, "y": quest.y2}
                ]
                out["stage_or_time"] = quest.stage
            else:
                out["stage_or_time"] = (
                    int(quest.expires.timestamp()) if quest.expires else 0
                )
            return out
    except Exception:
        return None


def recent_events(limit=10):
    try:
        with Session(db()) as s:
            rows = s.scalars(
                select(EventLog).order_by(EventLog.id.desc()).limit(limit)
            ).all()
            return [{"kind": r.kind, "message": r.message, "at": r.at} for r in rows]
    except Exception:
        return []


def duration(seconds):
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds}s"
    out, units = [], (("d", 86400), ("h", 3600), ("m", 60))
    for suffix, size in units:
        if seconds >= size:
            out.append(f"{seconds // size}{suffix}")
            seconds %= size
        if len(out) == 2:
            break
    return " ".join(out)


def ago(ts):
    """Render a timestamp. SQLAlchemy hands back datetimes; SQLite's are naive."""
    if not ts:
        return "never"
    return str(ts).split(".")[0].replace("T", " ")


E = html.escape

STYLE = """
:root{--bg:#fbfaf8;--panel:#fff;--fg:#1c1b19;--muted:#6b6862;--line:#e3e0da;
--accent:#7a3e12;--soft:#f4ece4;--on:#3f7d3f;--off:#a8a49c;--good:#3f6d9e;--evil:#9e3f3f;
--sea-deep:#2f4f6d;--sea:#4a7fa5;--sand:#ddc9a0;--grass:#8aa262;
--forest:#5f7d4a;--hill:#a08d6a;--peak:#e8e4dc}
@media (prefers-color-scheme:dark){:root{--bg:#16151a;--panel:#1e1d23;--fg:#ecebe8;
--muted:#a09d97;--line:#302e36;--accent:#e0a271;--soft:#2a2118;--on:#7fc47f;
--off:#6b6862;--good:#8fb6e0;--evil:#e08f8f}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);
font:15px/1.55 ui-sans-serif,system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
.wrap{max-width:62rem;margin:0 auto;padding:2.5rem 1.25rem 4rem}
header{border-bottom:1px solid var(--line);padding-bottom:1rem;margin-bottom:1.5rem}
h1{margin:0;font-size:1.9rem;letter-spacing:-.02em;
font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}
h1 a{color:inherit;text-decoration:none}
h1 span{color:var(--accent)}
h2{font-size:.78rem;text-transform:uppercase;letter-spacing:.08em;color:var(--muted);
margin:2rem 0 .8rem;font-weight:600}
nav{margin-top:.7rem;display:flex;gap:1.1rem;flex-wrap:wrap;font-size:.92rem}
nav a{color:var(--muted);text-decoration:none;border-bottom:1px solid transparent;padding-bottom:2px}
nav a:hover{color:var(--fg)}
nav a.on{color:var(--accent);border-bottom-color:var(--accent)}
.meta{margin:.35rem 0 0;color:var(--muted);font-size:.93rem}
table{width:100%;border-collapse:collapse;font-size:.94rem}
th,td{text-align:left;padding:.5rem .5rem;border-bottom:1px solid var(--line)}
th{font-size:.72rem;text-transform:uppercase;letter-spacing:.07em;color:var(--muted);font-weight:600}
td.num,th.num{text-align:right;font-variant-numeric:tabular-nums}
td.rank{color:var(--muted);width:2.5rem}
tr.off td{color:var(--muted)}
.who{font-weight:600}.who a{color:inherit;text-decoration:none}
.who a:hover{color:var(--accent)}
.dot{display:inline-block;width:.5rem;height:.5rem;border-radius:50%;margin-right:.5rem;vertical-align:middle}
.dot.on{background:var(--on)}.dot.off{background:var(--off)}
.good{color:var(--good)}.evil{color:var(--evil)}
.card{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:1.1rem 1.3rem;margin-bottom:1rem}
.hero{background:var(--soft)}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(11rem,1fr));gap:.9rem}
.stat{background:var(--panel);border:1px solid var(--line);border-radius:9px;padding:.8rem .9rem}
.stat .k{font-size:.7rem;text-transform:uppercase;letter-spacing:.07em;color:var(--muted)}
.stat .v{font-size:1.25rem;font-weight:600;margin-top:.15rem;font-variant-numeric:tabular-nums}
.muted{color:var(--muted);font-size:.92rem}
code{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-size:.93em}
a{color:var(--accent)}
.empty{background:var(--soft);border:1px solid var(--line);border-radius:10px;padding:1.5rem;text-align:center}
svg.map{width:100%;height:auto;background:var(--panel);border:1px solid var(--line);
border-radius:10px;display:block}
svg.map .ground{fill:var(--sea-deep)}
svg.map .t-deep{fill:var(--sea-deep)}
svg.map .t-water{fill:var(--sea)}
svg.map .t-sand{fill:var(--sand)}
svg.map .t-grass{fill:var(--grass)}
svg.map .t-forest{fill:var(--forest)}
svg.map .t-hill{fill:var(--hill)}
svg.map .t-peak{fill:var(--peak)}
svg.map .grid{stroke:var(--fg);stroke-width:1;opacity:.06}
svg.map .quad{stroke:#fff;stroke-width:2;stroke-dasharray:10 8;opacity:.35}
svg.map .quadlabel{fill:#fff;font-size:19px;letter-spacing:3px;text-anchor:middle;
opacity:.5;font-family:ui-monospace,Menlo,monospace;paint-order:stroke;
stroke:#0006;stroke-width:3px}
svg.map .questpath{stroke:var(--accent);stroke-width:2.5;stroke-dasharray:10 8;opacity:.7}
svg.map .goal{fill:none;stroke:var(--accent);stroke-width:3;stroke-dasharray:7 5}
svg.map .goalnum{fill:var(--accent);font-size:18px;font-weight:700;text-anchor:middle}
svg.map .pin text{font-size:19px;text-anchor:middle;fill:var(--fg);
paint-order:stroke;stroke:var(--panel);stroke-width:4px}
svg.map .pin circle{stroke:var(--panel);stroke-width:3}
svg.map .pin.irc circle{fill:var(--accent)}
svg.map .pin.discord circle{fill:#5865f2}
svg.map .pin.both circle{fill:var(--on)}
svg.map .pin.off{opacity:.35}
footer{margin-top:2.5rem;padding-top:1rem;border-top:1px solid var(--line);color:var(--muted);font-size:.85rem}
.plat{display:inline-block;font-size:.68rem;font-weight:700;letter-spacing:.04em;
padding:.1rem .42rem;border-radius:4px;margin-right:.3rem;border:1px solid var(--line)}
.plat.irc.live{background:var(--soft);color:var(--accent);border-color:var(--accent)}
.plat.discord.live{background:#5865f21a;color:#5865f2;border-color:#5865f2}
@media (prefers-color-scheme:dark){.plat.discord.live{color:#9aa6ff;border-color:#5865f2}}
.plat.idle{opacity:.45}
.plat.none{opacity:.4;border:none}
.feed{list-style:none;padding:0;margin:0}
.feed li{padding:.5rem .1rem;border-bottom:1px solid var(--line);font-size:.93rem}
.feed .when{color:var(--muted);font-variant-numeric:tabular-nums;
margin-right:.7rem;font-size:.82rem}
.bar{height:.4rem;background:var(--line);border-radius:99px;overflow:hidden;margin-top:.4rem}
.bar i{display:block;height:100%;background:var(--accent)}
"""

NAV = [("/", "Standings"), ("/map", "World map"), ("/quest", "Quest"),
       ("/game", "How to play")]


def layout(title, body, current="/", refresh=60):
    nav = "".join(
        f'<a href="{p}" class="{"on" if p == current else ""}">{E(label)}</a>'
        for p, label in NAV
    )
    return f"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="refresh" content="{refresh}">
<title>{E(title)} &middot; IdleRPG</title>
<style>{STYLE}</style>
</head><body><div class="wrap">
<header>
  <h1><a href="/">Idle<span>RPG</span></a></h1>
  <p class="meta">{E(CHANNEL)} on {E(NETWORK)}</p>
  <nav>{nav}</nav>
</header>
{body}
<footer>
  Stay connected and stay quiet to level up.
  &middot; <a href="/api/players.json">players.json</a>
  &middot; <a href="/api/quest.json">quest.json</a>
  &middot; <a href="https://129irc.com">129irc.com</a>
</footer>
</div></body></html>"""


def register_hint():
    return f"""<div class="empty">
  <p>No players yet.</p>
  <p class="muted">Join <code>{E(CHANNEL)}</code> and register:<br>
  <code>/msg {E(BOT_NICK)} REGISTER &lt;name&gt; &lt;password&gt; &lt;class&gt;</code></p>
</div>"""


MAP_SEED = int(os.environ.get("IRPG_MAP_SEED", "1129"))
TERRAIN_CELLS = 64

# Height bands, low to high. Each is (upper bound, css class).
TERRAIN_BANDS = (
    (0.34, "deep"), (0.42, "water"), (0.46, "sand"),
    (0.60, "grass"), (0.72, "forest"), (0.85, "hill"), (1.01, "peak"),
)


def _lattice(seed, n):
    rng = random.Random(seed)
    return [[rng.random() for _ in range(n + 1)] for _ in range(n + 1)]


def _sample(grid, n, x, y):
    """Bilinear sample of a value-noise lattice, with a smoothstep easing."""
    gx, gy = x * n, y * n
    x0, y0 = int(gx), int(gy)
    fx, fy = gx - x0, gy - y0
    fx = fx * fx * (3 - 2 * fx)
    fy = fy * fy * (3 - 2 * fy)
    a = grid[y0][x0] * (1 - fx) + grid[y0][x0 + 1] * fx
    b = grid[y0 + 1][x0] * (1 - fx) + grid[y0 + 1][x0 + 1] * fx
    return a * (1 - fy) + b * fy


def _height_field(cells=TERRAIN_CELLS, seed=MAP_SEED):
    """Fractal value noise, pulled down at the edges so the land is an island.

    Deterministic: the same seed always yields the same realm, so the map is a
    place players can learn rather than a different world on every page load.
    """
    octaves = [(4, 1.0), (8, 0.5), (16, 0.25), (32, 0.12)]
    grids = [(n, w, _lattice(seed + i, n)) for i, (n, w) in enumerate(octaves)]
    total_weight = sum(w for _, w in octaves)

    field = []
    for cy in range(cells):
        row = []
        for cx in range(cells):
            u, v = cx / cells, cy / cells
            h = sum(_sample(g, n, u, v) * w for n, w, g in grids) / total_weight
            # Radial falloff: coast at the edges, land in the middle.
            dx, dy = u - 0.5, v - 0.5
            d = (dx * dx + dy * dy) ** 0.5 / 0.707
            h = h * (1.0 - d ** 2.2 * 1.15)
            row.append(h)
        field.append(row)

    lo = min(min(r) for r in field)
    hi = max(max(r) for r in field)
    span = (hi - lo) or 1
    return [[(v - lo) / span for v in row] for row in field]


def _terrain_paths(size=1000.0, cells=TERRAIN_CELLS):
    """One SVG path per terrain band, so the map is a handful of elements
    rather than four thousand rectangles."""
    field = _height_field(cells)
    step = size / cells
    runs = {name: [] for _, name in TERRAIN_BANDS}
    for cy, row in enumerate(field):
        y = cy * step
        cx = 0
        while cx < cells:
            band = next(n for limit, n in TERRAIN_BANDS if row[cx] < limit)
            start = cx
            while cx + 1 < cells and next(
                n for limit, n in TERRAIN_BANDS if row[cx + 1] < limit
            ) == band:
                cx += 1
            cx += 1
            # Overlap by a hair so neighbouring runs do not show seams.
            runs[band].append(
                f"M{start*step:.1f} {y:.1f}h{(cx-start)*step+0.6:.1f}v{step+0.6:.1f}"
                f"h-{(cx-start)*step+0.6:.1f}z"
            )
    return "".join(
        f'<path class="t-{name}" d="{"".join(d)}"/>' for _, name in TERRAIN_BANDS
        if (d := runs[name])
    )


_TERRAIN_CACHE = None


def terrain_svg():
    global _TERRAIN_CACHE
    if _TERRAIN_CACHE is None:
        _TERRAIN_CACHE = _terrain_paths()
    return _TERRAIN_CACHE


PLATFORM_LABEL = {"irc": "IRC", "discord": "Discord"}


def platform_badges(player):
    """Show where a player is playing from, and which of those are live."""
    out = []
    for plat in ("irc", "discord"):
        if plat not in player.get("platforms", {}):
            continue
        live = player["platforms"][plat]
        out.append(
            f'<span class="plat {plat} {"live" if live else "idle"}" '
            f'title="{PLATFORM_LABEL[plat]}: {"connected" if live else "offline"}">'
            f'{PLATFORM_LABEL[plat]}</span>'
        )
    return "".join(out) or '<span class="plat none">-</span>'


def event_feed(limit=10):
    rows = recent_events(limit)
    if not rows:
        return ""
    items = "".join(
        f'<li><span class="when">{E(str(r["at"]).split(".")[0].replace("T"," "))}</span>'
        f'{E(r["message"])}</li>'
        for r in rows
    )
    return f'<h2>Recently in the realm</h2><ul class="feed">{items}</ul>'


def page_index(players):
    if not players:
        return layout("Standings", register_hint() + event_feed())
    online = sum(1 for p in players if p["online"])
    rows = "".join(
        f'<tr class="{"on" if p["online"] else "off"}">'
        f'<td class="rank">{i}</td>'
        f'<td class="who"><span class="dot {"on" if p["online"] else "off"}"></span>'
        f'<a href="/player/{E(p["username"])}">{E(p["username"])}</a></td>'
        f'<td class="num">{p["level"]}</td><td>{E(p["class"])}</td>'
        f'<td class="num">{E(duration(p["next"]))}</td>'
        f'<td class="num">{p["itemsum"]}</td>'
        f'<td>{platform_badges(p)}</td>'
        f'<td class="{p["alignment"].lower()}">{p["alignment"]}</td></tr>'
        for i, p in enumerate(players, 1)
    )
    body = f"""<div class="grid">
  <div class="stat"><div class="k">Players</div><div class="v">{len(players)}</div></div>
  <div class="stat"><div class="k">Online</div><div class="v">{online}</div></div>
  <div class="stat"><div class="k">Top level</div><div class="v">{players[0]["level"]}</div></div>
</div>
<h2>Standings</h2>
<table><thead><tr><th>#</th><th>Player</th><th class="num">Level</th><th>Class</th>
<th class="num">Next level</th><th class="num">Items</th><th>Playing from</th>
<th>Alignment</th></tr></thead>
<tbody>{rows}</tbody></table>
{event_feed()}"""
    return layout("Standings", body, "/")


def page_map(players, quest):
    """The realm, as SVG.

    The quadrant lines are not decoration: war is fought between them, so it is
    worth being able to see which one you are standing in.
    """
    S = 1000.0

    def sx(x):
        return x / MAP_X * S

    def sy(y):
        return y / MAP_Y * S

    grid = "".join(
        f'<line x1="{i*S/10:.0f}" y1="0" x2="{i*S/10:.0f}" y2="{S:.0f}" class="grid"/>'
        f'<line x1="0" y1="{i*S/10:.0f}" x2="{S:.0f}" y2="{i*S/10:.0f}" class="grid"/>'
        for i in range(1, 10)
    )
    quads = (
        f'<line x1="{S/2:.0f}" y1="0" x2="{S/2:.0f}" y2="{S:.0f}" class="quad"/>'
        f'<line x1="0" y1="{S/2:.0f}" x2="{S:.0f}" y2="{S/2:.0f}" class="quad"/>'
        f'<text x="{S*0.75:.0f}" y="40" class="quadlabel">NORTHEAST</text>'
        f'<text x="{S*0.25:.0f}" y="40" class="quadlabel">NORTHWEST</text>'
        f'<text x="{S*0.75:.0f}" y="{S-18:.0f}" class="quadlabel">SOUTHEAST</text>'
        f'<text x="{S*0.25:.0f}" y="{S-18:.0f}" class="quadlabel">SOUTHWEST</text>'
    )

    goals = ""
    if quest and quest.get("destinations"):
        pts = [(sx(d["x"]), sy(d["y"])) for d in quest["destinations"]]
        if len(pts) == 2:
            goals += (
                f'<line x1="{pts[0][0]:.1f}" y1="{pts[0][1]:.1f}" '
                f'x2="{pts[1][0]:.1f}" y2="{pts[1][1]:.1f}" class="questpath"/>'
            )
        for n, (gx, gy) in enumerate(pts, 1):
            goals += (
                f'<circle cx="{gx:.1f}" cy="{gy:.1f}" r="18" class="goal"/>'
                f'<text x="{gx:.1f}" y="{gy+5:.1f}" class="goalnum">{n}</text>'
            )

    marks = []
    for p in sorted(players, key=lambda q: q["online"]):
        cx, cy = sx(p["x"]), sy(p["y"])
        plats = p.get("platforms", {})
        cls = "both" if len(plats) > 1 else ("discord" if "discord" in plats else "irc")
        state = "on" if p["online"] else "off"
        where = ", ".join(PLATFORM_LABEL.get(k, k) for k in plats) or "nowhere"
        marks.append(
            f'<g class="pin {cls} {state}">'
            f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="11"/>'
            f'<text x="{cx:.1f}" y="{cy-19:.1f}">{E(p["username"])}</text>'
            f'<title>{E(p["username"])} - level {p["level"]} at ({p["x"]}, {p["y"]}), '
            f'{where}</title></g>'
        )

    note = ("Dashed rings are the current quest's waypoints, numbered in order."
            if goals else "No quest is running.")
    body = f"""<h2>The realm</h2>
<svg class="map" viewBox="-10 -10 {S+20:.0f} {S+20:.0f}" role="img"
     aria-label="Map of the realm showing where each player stands">
  <rect x="0" y="0" width="{S:.0f}" height="{S:.0f}" class="ground"/>
  {terrain_svg()}
  {grid}{quads}{goals}{"".join(marks)}
</svg>
<p class="muted">
  <span class="plat irc live">IRC</span>
  <span class="plat discord live">Discord</span>
  <span class="plat none">both</span>
  &nbsp; Filled pins are online; faded ones are not. {note}
  The realm is {MAP_X}&times;{MAP_Y} and players drift a step at a time while they idle.
</p>"""
    return layout("World map", body, "/map")


def page_quest(quest, players):
    if not quest:
        body = """<h2>Quest</h2><div class="empty"><p>No quest is running.</p>
<p class="muted">The gods choose four idlers above level 40 and set them a task.
Finish it and a quarter of everyone's burden is lifted; speak during it and the
whole realm pays.</p></div>"""
        return layout("Quest", body, "/quest")

    by_name = {p["username"]: p for p in players}
    if quest["type"] == 1:
        remaining = quest.get("stage_or_time", 0) - int(time.time())
        detail = f"""<div class="stat"><div class="k">Type</div><div class="v">Timed</div></div>
<div class="stat"><div class="k">Time remaining</div>
<div class="v">{E(duration(remaining)) if remaining > 0 else "any moment"}</div></div>"""
    else:
        detail = f"""<div class="stat"><div class="k">Type</div><div class="v">Journey</div></div>
<div class="stat"><div class="k">Stage</div>
<div class="v">{quest.get("stage_or_time", 1)} of 2</div></div>"""

    rows = ""
    for q in quest["questers"]:
        p = by_name.get(q["name"])
        pos = f'({q["x"]}, {q["y"]})' if "x" in q else "&mdash;"
        lvl = p["level"] if p else "?"
        rows += (
            f'<tr><td class="who"><a href="/player/{E(q["name"])}">{E(q["name"])}</a></td>'
            f'<td class="num">{lvl}</td><td class="num">{pos}</td></tr>'
        )
    dests = "".join(
        f'<li><code>({d["x"]}, {d["y"]})</code></li>' for d in quest.get("destinations", [])
    )
    dest_block = f"<h2>Destinations</h2><ul>{dests}</ul>" if dests else ""
    body = f"""<h2>Current quest</h2>
<div class="card hero"><p style="margin:0">{E(quest["text"])}</p></div>
<div class="grid">{detail}
<div class="stat"><div class="k">Questers</div><div class="v">{len(quest["questers"])}</div></div></div>
<h2>Party</h2>
<table><thead><tr><th>Player</th><th class="num">Level</th><th class="num">Position</th></tr></thead>
<tbody>{rows}</tbody></table>
{dest_block}
<p class="muted">If a quester talks, parts or quits, the quest fails and everyone
in the realm is set back.</p>"""
    return layout("Quest", body, "/quest")


def page_player(player):
    items = "".join(
        f'<tr><td>{E(slot)}</td><td class="num">{value}</td>'
        f'<td class="muted">{E(player["item_tags"].get(slot, "") or "")}</td></tr>'
        for slot, value in sorted(player["items"].items(), key=lambda kv: -kv[1])
    )
    pens = "".join(
        f'<tr><td>{E(kind)}</td><td class="num">{E(duration(secs))}</td></tr>'
        for kind, secs in sorted(player["penalties"].items(), key=lambda kv: -kv[1])
        if secs
    ) or '<tr><td colspan="2" class="muted">No penalties. Well idled.</td></tr>'

    total_pen = sum(player["penalties"].values())
    status = "online" if player["online"] else "offline"
    body = f"""<h2>{E(player["username"])}</h2>
<div class="grid">
  <div class="stat"><div class="k">Level</div><div class="v">{player["level"]}</div></div>
  <div class="stat"><div class="k">Class</div><div class="v" style="font-size:1rem">{E(player["class"]) or "&mdash;"}</div></div>
  <div class="stat"><div class="k">Next level</div><div class="v" style="font-size:1rem">{E(duration(player["next"]))}</div></div>
  <div class="stat"><div class="k">Item sum</div><div class="v">{player["itemsum"]}</div></div>
  <div class="stat"><div class="k">Alignment</div>
    <div class="v {player["alignment"].lower()}" style="font-size:1rem">{player["alignment"]}</div></div>
  <div class="stat"><div class="k">Status</div><div class="v" style="font-size:1rem">
    <span class="dot {"on" if player["online"] else "off"}"></span>{status}</div></div>
</div>
<h2>Detail</h2>
<table>
  <tr><th>Playing from</th><td>{platform_badges(player)}</td></tr>
  <tr><th>Position</th><td>({player["x"]}, {player["y"]})</td></tr>
  <tr><th>Total penalties</th><td>{E(duration(total_pen))}</td></tr>
  <tr><th>Created</th><td>{E(ago(player["created"]))}</td></tr>
  <tr><th>Last login</th><td>{E(ago(player["lastlogin"]))}</td></tr>
  <tr><th>Admin</th><td>{"yes" if player["admin"] else "no"}</td></tr>
</table>
<h2>Items</h2>
<table><thead><tr><th>Slot</th><th class="num">Value</th><th>Tag</th></tr></thead>
<tbody>{items}</tbody></table>
<h2>Penalties</h2>
<table><thead><tr><th>Kind</th><th class="num">Added to timer</th></tr></thead>
<tbody>{pens}</tbody></table>"""
    return layout(player["username"], body, "/")


def page_game():
    levels = [10, 20, 30, 40, 50, 60, 70, 80]
    rows = ""
    cumulative = 0.0
    prev = 0
    for lvl in levels:
        for l in range(prev, lvl):
            cumulative += (
                RP_BASE * (RP_STEP**l) if l <= 60
                else RP_BASE * (RP_STEP**60) + 86400 * (l - 60)
            )
        prev = lvl
        step = (
            RP_BASE * (RP_STEP**lvl) if lvl <= 60
            else RP_BASE * (RP_STEP**60) + 86400 * (lvl - 60)
        )
        rows += (
            f'<tr><td class="num">{lvl}</td><td class="num">{E(duration(step))}</td>'
            f'<td class="num">{E(duration(cumulative))}</td></tr>'
        )
    body = f"""<h2>How to play</h2>
<div class="card hero">
  <p style="margin:0">Join <code>{E(CHANNEL)}</code>, register, and then do nothing.
  Every second you stay connected without speaking brings you closer to the next
  level. That is the whole game.</p>
</div>

<h2>Getting started on IRC</h2>
<table>
  <tr><th>Register</th><td><code>/msg {E(BOT_NICK)} REGISTER &lt;name&gt; &lt;password&gt; &lt;class&gt;</code></td></tr>
  <tr><th>Log in later</th><td><code>/msg {E(BOT_NICK)} LOGIN &lt;name&gt; &lt;password&gt;</code></td></tr>
  <tr><th>Check yourself</th><td><code>/msg {E(BOT_NICK)} WHOAMI</code></td></tr>
  <tr><th>Link Discord</th><td><code>/msg {E(BOT_NICK)} LINK</code></td></tr>
  <tr><th>Log out</th><td><code>/msg {E(BOT_NICK)} LOGOUT</code></td></tr>
</table>
<p class="muted">Connect to <code>irc.129irc.com</code> on port <code>6697</code> with
TLS and join <code>{E(CHANNEL)}</code>. The network blocks private messages from brand
new connections, so wait about two minutes after connecting before you register.</p>

<h2>Getting started on Discord</h2>
<table>
  <tr><th>Register</th><td><code>!register &lt;name&gt; &lt;password&gt; &lt;class&gt;</code></td></tr>
  <tr><th>Log in later</th><td><code>!login &lt;name&gt; &lt;password&gt;</code></td></tr>
  <tr><th>Check yourself</th><td><code>!whoami</code></td></tr>
  <tr><th>Link to an IRC character</th><td><code>!link &lt;code&gt;</code></td></tr>
  <tr><th>Commands</th><td><code>!help</code></td></tr>
</table>
<p class="muted"><strong>Send <code>!register</code> and <code>!login</code> to the bot
in a direct message, not in the channel</strong> - they contain your password. If you
put one in a channel the bot deletes it and replies privately instead. Everything else
works in {E(CHANNEL)} or a DM.</p>

<h2>One character on both</h2>
<p class="muted">You do not need two characters. Run <code>LINK</code> on IRC to get a
code, then send <code>!link &lt;code&gt;</code> to the bot on Discord within fifteen
minutes. After that you are one character on both, and you still earn exactly one
second per second - being in two places is neither a penalty nor a way to gain
time faster.</p>

<p class="muted">Your class is cosmetic - pick something you like.</p>

<h2>What sets you back</h2>
<p class="muted">Penalties are added to the time remaining on your level, and they
scale with your level, so the higher you climb the more a slip costs.</p>
<table><thead><tr><th>Action</th><th>Relative cost</th></tr></thead><tbody>
  <tr><td>Talking in the channel</td><td>Proportional to the length of what you said</td></tr>
  <tr><td>Changing nick</td><td>30&times;</td></tr>
  <tr><td>Quitting</td><td>20&times;</td></tr>
  <tr><td>Logging out</td><td>20&times;</td></tr>
  <tr><td>Parting the channel</td><td>200&times;</td></tr>
  <tr><td>Being kicked</td><td>250&times;</td></tr>
  <tr><td>Failing a quest</td><td>15&times;, for everyone</td></tr>
</tbody></table>

<h2>The climb</h2>
<p class="muted">Each level costs {RP_STEP}&times; the last, so progress is gentle
early and slow later. Past level 60 the curve flattens to a fixed day per level
instead of compounding forever.</p>
<table><thead><tr><th class="num">Level</th><th class="num">That level costs</th>
<th class="num">Total to reach it</th></tr></thead><tbody>{rows}</tbody></table>

<h2>Items, alignment and events</h2>
<p class="muted">You find items in ten slots - amulet, charm, helm, boots, gloves,
ring, leggings, shield, tunic and weapon. Their total is your strength in battles,
which the bot starts on its own.</p>
<p class="muted">Alignment changes how battles resolve: <span class="good">good</span>
players get a bonus fighting together, <span class="evil">evil</span> ones can steal
items but take a penalty. Neutral sits in between.</p>
<p class="muted">Quests send a party across the {MAP_X}&times;{MAP_Y} realm. Finish one
and everyone involved gains time; if a quester talks, parts or quits, it fails and
the whole realm is set back.</p>"""
    return layout("How to play", body, "/game")


class Handler(BaseHTTPRequestHandler):
    server_version = "idlerpg-site"

    def _send(self, body, ctype="text/html; charset=utf-8", status=200):
        payload = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html"):
            self._send(page_index(load_players()))
        elif path == "/map":
            self._send(page_map(load_players(), load_quest()))
        elif path == "/quest":
            self._send(page_quest(load_quest(), load_players()))
        elif path == "/game":
            self._send(page_game())
        elif path.startswith("/player/"):
            name = unquote(path[len("/player/"):]).strip("/")
            match = next(
                (p for p in load_players() if p["username"].lower() == name.lower()), None
            )
            if match:
                self._send(page_player(match))
            else:
                self._send(
                    layout("Unknown player",
                           f'<div class="empty"><p>No player called '
                           f'<code>{E(name)}</code>.</p></div>'),
                    status=404,
                )
        elif path == "/api/players.json":
            self._send(
                json.dumps(load_players(), indent=2, default=str),
                "application/json",
            )
        elif path == "/api/quest.json":
            self._send(
                json.dumps(load_quest(), indent=2, default=str),
                "application/json",
            )
        elif path in ("/healthz", "/health"):
            self._send("ok", "text/plain; charset=utf-8")
        else:
            self._send(
                layout("Not found", '<div class="empty"><p>Nothing here.</p></div>'),
                status=404,
            )

    def log_message(self, fmt, *args):
        # Log to stdout so requests show up in `kubectl logs`.
        sys.stdout.write(
            "%s - - [%s] %s\n"
            % (self.address_string(), self.log_date_time_string(), fmt % args)
        )
        sys.stdout.flush()


if __name__ == "__main__":
    ThreadingHTTPServer(("", PORT), Handler).serve_forever()
