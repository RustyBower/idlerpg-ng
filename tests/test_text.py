"""Tests for the text helpers: durations, sanitising, and what a name may be.

Several cases are the real ones a player used to garble the channel: a
right-to-left override in a name and a class, and IRC colour codes in both.
Invisible characters are written as escapes so they can be seen in review.
"""

from __future__ import annotations

import pytest

from idlerpg.text import check_class, check_name, duration, safe

RLO = "\u202e"          # RIGHT-TO-LEFT OVERRIDE
ZWSP = "\u200b"         # ZERO WIDTH SPACE
ZWJ = "\u200d"          # ZERO WIDTH JOINER
ACUTE, CIRCUMFLEX, TILDE, DIAERESIS = "\u0301", "\u0302", "\u0303", "\u0308"
POTATO = "\U0001f954"
FARMER = "\U0001f9d1" + ZWJ + "\U0001f33e"
ZOE = "Zo\u00eb"


class TestDuration:
    @pytest.mark.parametrize("seconds,expected", [
        (0, "0s"), (59, "59s"), (60, "1m"), (3600, "1h"), (3661, "1h 1m"),
        (90061, "1d 1h"), (-5, "0s"),
    ])
    def test_the_two_largest_units(self, seconds, expected):
        assert duration(seconds) == expected


class TestSafe:
    def test_bidi_overrides_are_removed(self):
        assert safe(f"{RLO}profit-on-irc") == "profit-on-irc"

    def test_irc_colour_and_bold_are_removed(self):
        assert safe("\x0304,02\x02profit") == "profit"

    def test_ordinary_text_and_emoji_are_untouched(self):
        text = f"{ZOE} the {POTATO}, level 6"
        assert safe(text) == text


class TestNames:
    @pytest.mark.parametrize("name", [
        "rusty", ZOE, "\u042f\u0440\u043e\u0441\u043b\u0430\u0432",
        "\u5c71\u7530", "o'neil", "r_sty", "chug.diesel", "x-1",
    ])
    def test_real_names_pass(self, name):
        assert check_name(name) == name

    @pytest.mark.parametrize("name,why", [
        (f"{RLO}profit-on-irc", "letters"),
        ("\x0304,02\x02profit", "letters"),
        (f"zero{ZWSP}width", "letters"),
        ("two words", "letters"),
        ("@everyone", "letters"),
        ("a" * 17, "at most 16"),
        ("admin", "reserved"),
        ("NickServ", "reserved"),
        # x has no precomposed accented form, so all three marks stay stacked.
        ("x" + ACUTE + CIRCUMFLEX + TILDE, "accents"),
        (ACUTE + "abc", "accents"),
        ("   ", "needs a name"),
    ])
    def test_tricks_are_refused_with_a_reason(self, name, why):
        with pytest.raises(ValueError, match=why):
            check_name(name)

    def test_two_accents_on_a_letter_are_fine(self):
        assert check_name("x" + ACUTE + CIRCUMFLEX + "y")

    def test_names_are_normalised(self):
        assert check_name("Zoe" + DIAERESIS) == ZOE


class TestClasses:
    def test_emoji_pass_including_joined_ones(self):
        assert check_class(POTATO) == POTATO
        assert check_class(f"the {FARMER}") == f"the {FARMER}"

    def test_whitespace_is_collapsed(self):
        assert check_class("  The   IRC  Memelord ") == "The IRC Memelord"

    @pytest.mark.parametrize("text", [f"rtl override{RLO}", "\x0302,04colored"])
    def test_hidden_formatting_is_refused(self, text):
        with pytest.raises(ValueError, match="control or formatting"):
            check_class(text)

    def test_long_classes_are_refused(self):
        with pytest.raises(ValueError, match="at most 30"):
            check_class("x" * 31)
