"""How the game writes things for people to read.

Shared by the bot and the website, so a timer reads the same in a channel as
on a page.
"""

from __future__ import annotations

import re
import unicodedata

UNITS = (("d", 86400), ("h", 3600), ("m", 60))

NAME_MAX = 16          # the original's limit
CLASS_MAX = 30         # likewise
NAME_PUNCTUATION = frozenset("-_.'")
# Names that would pass for the bot, the services or the staff.
RESERVED = frozenset({
    "admin", "administrator", "root", "system", "idlerpg", "everyone", "here",
    "nickserv", "chanserv", "operserv", "memoserv", "hostserv", "botserv",
})

# Invisible characters that reorder or restyle the text around them: bidi
# controls, and IRC's bold/colour/underline codes.
BIDI = frozenset("\u061c\u200e\u200f\u202a\u202b\u202c\u202d\u202e"
                 "\u2066\u2067\u2068\u2069")
IRC_FORMAT = frozenset("\x02\x03\x0f\x11\x16\x1d\x1e\x1f")
IRC_COLOUR = re.compile(r"\x03(\d{1,2}(,\d{1,2})?)?")
# Characters a class may never hold. ZWJ is let through: emoji like the
# farmer are built with it.
CLASS_FORBIDDEN = frozenset({"Cc", "Cf", "Co", "Cs", "Cn", "Zl", "Zp"})
ZWJ = "\u200d"


def duration(seconds: float) -> str:
    """A short human duration: the two largest units, like '3d 4h' or '12m'.

    Under a minute it is seconds. Precision past two units is noise for a
    timer measured in days.
    """
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds}s"
    out = []
    for suffix, size in UNITS:
        if seconds >= size:
            out.append(f"{seconds // size}{suffix}")
            seconds %= size
        if len(out) == 2:
            break
    return " ".join(out)


def safe(text: str) -> str:
    """Text with bidi controls and IRC formatting removed.

    Applied to everything the game says, so a name registered before names
    were checked - or any other text - cannot flip or recolour a channel.
    """
    text = IRC_COLOUR.sub("", text)
    return "".join(c for c in text if c not in BIDI and c not in IRC_FORMAT)


def _stacked_marks(text: str) -> bool:
    """More than two combining marks in a row: the stuff of zalgo text."""
    run = 0
    for c in text:
        run = run + 1 if unicodedata.category(c).startswith("M") else 0
        if run > 2:
            return True
    return False


def check_name(name: str) -> str:
    """The name to store, or ValueError saying what is wrong with it.

    Letters and digits in any script, plus - _ . and '. Nothing that hides,
    reorders or restyles text, and nothing that passes for the bot or staff.
    """
    name = unicodedata.normalize("NFC", name.strip())
    if not name:
        raise ValueError("a character needs a name")
    if len(name) > NAME_MAX:
        raise ValueError(f"names are at most {NAME_MAX} characters")
    if unicodedata.category(name[0]).startswith("M") or _stacked_marks(name):
        raise ValueError("that name has too many accents stacked up")
    for c in name:
        if unicodedata.category(c)[0] in "LMN" or c in NAME_PUNCTUATION:
            continue
        raise ValueError("names may use letters, digits and - _ . ' only")
    if name.casefold() in RESERVED:
        raise ValueError("that name is reserved")
    return name


def check_class(text: str) -> str:
    """The class to store, or ValueError. Free text, emoji welcome, but one
    printable line of at most CLASS_MAX characters."""
    text = unicodedata.normalize("NFC", " ".join(text.split()))
    if len(text) > CLASS_MAX:
        raise ValueError(f"classes are at most {CLASS_MAX} characters")
    if _stacked_marks(text):
        raise ValueError("that class has too many accents stacked up")
    for c in text:
        if c != ZWJ and unicodedata.category(c) in CLASS_FORBIDDEN:
            raise ValueError("classes cannot contain control or formatting characters")
    return text
