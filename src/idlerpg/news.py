"""What this version brought, said once when the realm comes up on it.

The changelog is the source. It is written for every release anyway, it
already ships in the image for the website's what's-new page, and a second
hand-written list of headlines would only drift away from it.

Said once per version rather than once per start: the version last announced
is kept in the database, so a restart says nothing and a crash loop cannot
spam the channel. Only the bold lead-in of each entry is read out - the whole
entry belongs on the site, which the line links to.
"""

from __future__ import annotations

import re
from pathlib import Path

from . import __version__
from .events import Outcome

SEEN_KEY = "version_announced"
MOST = 3        # headlines read out; the rest are a link away

# "- **Items left lying on the map.** The item you replace..." -> the bold part.
HEADLINE = re.compile(r"^- \*\*(.+?)\*\*")
# Entries that are not news for the realm. Operator notes are written in
# italics and so never match HEADLINE in the first place, but a bold one
# would. "Not shipped" entries record a change that was measured and
# rejected, and those are bold - 0.23.0 has one, kept out of the channel only
# by MOST, which is no guard at all if such an entry comes first.
SKIP = re.compile(r"^\s*(\*?operators\*?|not shipped)\b", re.IGNORECASE)


def changelog_text() -> str:
    """CHANGELOG.md: beside the working directory in the image, or at the top
    of a checkout - the same two places the website looks."""
    for path in (Path.cwd() / "CHANGELOG.md",
                 Path(__file__).resolve().parents[2] / "CHANGELOG.md"):
        try:
            return path.read_text(encoding="utf-8")
        except OSError:
            continue
    return ""


def headlines(text: str, version: str, most: int = MOST) -> list[str]:
    """The bold lead-in of each entry under ``version``, best first."""
    found: list[str] = []
    inside = False
    for raw in text.splitlines():
        line = raw.rstrip()
        if line.startswith("## "):
            if inside:
                break
            inside = line[3:].split(" - ")[0].strip() == version
            continue
        if not inside:
            continue
        match = HEADLINE.match(line)
        if match and not SKIP.match(match.group(1)):
            found.append(match.group(1).strip().rstrip(".:"))
            if len(found) >= most:
                break
    return found


def announce(engine, site_url: str = "") -> list[Outcome]:
    """Tell the realm what this version brought, once; what was said.

    The version is recorded before anything is announced, so a build with no
    changelog to read - or a failure to say it - does not try again on every
    restart for the rest of the release.
    """
    if engine.get_setting(SEEN_KEY) == __version__:
        return []
    engine.set_setting(SEEN_KEY, __version__)
    said = headlines(changelog_text(), __version__)
    if not said:
        return []
    where = f" What's new: {site_url.rstrip('/')}/changes" if site_url else ""
    out = [Outcome(f"IdleRPG is now {__version__}: {'; '.join(said)}.{where}",
                   kind="news")]
    engine.announce(out)
    return out
