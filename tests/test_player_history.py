"""Tests for a player's own history on their page.

The hard part is deciding which of the realm's events were about one player.
Rows recorded since 0.21.0 carry an id; older ones, and the ones with two
people in them, only mention a name - and a name match that is too eager puts
somebody else's misfortunes on your page.
"""

from __future__ import annotations

from types import SimpleNamespace

from idlerpg import web


def row(message, player_id=None, kind="event", at="2026-09-12 18:00:00"):
    return SimpleNamespace(message=message, player_id=player_id, kind=kind, at=at)


class TestWhoseEventIsIt:
    def test_an_id_makes_it_theirs(self):
        rows = [row("something happened", player_id=7)]
        assert len(web.about(rows, 7, "rusty")) == 1

    def test_somebody_elses_id_does_not(self):
        rows = [row("something happened to someone", player_id=9)]
        assert web.about(rows, 7, "rusty") == []

    def test_an_older_row_is_found_by_name(self):
        """Events from before the engine recorded whose they were."""
        rows = [row("rusty was struck by lightning")]
        assert len(web.about(rows, 7, "rusty")) == 1

    def test_a_longer_name_is_not_a_match(self):
        """profit must not inherit everything profit-on-irc did."""
        rows = [row("profit-on-irc fell down a well")]
        assert web.about(rows, 7, "profit") == []

    def test_a_name_inside_a_word_is_not_a_match(self):
        rows = [row("the trusty old bridge collapsed")]
        assert web.about(rows, 7, "rusty") == []

    def test_punctuation_around_the_name_still_matches(self):
        rows = [row("rusty's shield was damaged!")]
        assert len(web.about(rows, 7, "rusty")) == 1

    def test_a_regex_in_a_name_is_taken_literally(self):
        rows = [row("a.c fell over"), row("abc fell over")]
        found = web.about(rows, 7, "a.c")
        assert len(found) == 1 and found[0]["message"].startswith("a.c")

    def test_it_stops_at_the_limit(self):
        rows = [row(f"rusty did thing {i}") for i in range(50)]
        assert len(web.about(rows, 7, "rusty", limit=10)) == 10

    def test_newest_order_is_whatever_it_was_given(self):
        rows = [row("rusty did the last thing"), row("rusty did an earlier thing")]
        assert web.about(rows, 7, "rusty")[0]["message"].endswith("last thing")


class TestRendering:
    def test_it_lists_what_happened(self):
        html = web.player_history([
            {"kind": "calamity", "message": "rusty fell in a hole",
             "at": "2026-09-12 18:00:00"},
        ])
        assert "What happened" in html
        assert "rusty fell in a hole" in html
        assert 'class="feed"' in html

    def test_an_empty_history_says_so(self):
        html = web.player_history([])
        assert "Nothing recorded yet" in html
        assert "<li>" not in html

    def test_a_message_cannot_inject_markup(self):
        html = web.player_history([
            {"kind": "event", "message": "<script>alert(1)</script>",
             "at": "2026-09-12 18:00:00"},
        ])
        assert "<script>" not in html
        assert "&lt;script&gt;" in html


class TestOnThePage:
    def test_the_player_page_carries_a_history(self):
        player = {
            "username": "rusty", "level": 32, "class": "Memelord", "next": 600,
            "items": {"shield": 20}, "item_tags": {}, "itemsum": 20,
            "penalties": {}, "online": True, "platforms": {"irc": True},
            "x": 1, "y": 2, "created": "2026-09-09", "lastlogin": "2026-09-12",
            "alignment": "True Neutral", "prestige": 0, "admin": False,
            "id": None, "title": "", "feats": {}, "keepsakes": [],
            "honours": [], "rival": "", "npc": False, "nick": "rusty",
        }
        html = web.page_player(player)
        assert "What happened" in html
