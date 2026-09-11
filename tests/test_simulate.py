"""Tests for the balancing simulator: small, fast runs of the real engine."""

from __future__ import annotations

import json

import pytest

from idlerpg import events, simulate
from idlerpg.simulate import NINE, apply_overrides, parse_roster, run, summarise


class TestRoster:
    def test_defaults_to_every_alignment(self):
        assert parse_roster(None, 2) == [a for a in NINE for _ in range(2)]

    def test_counts_and_single_names(self):
        assert parse_roster("lawful good:2, chaotic evil", 0) == [
            "lawful good", "lawful good", "chaotic evil"]

    def test_unknown_alignments_are_refused(self):
        with pytest.raises(ValueError, match="unknown alignment"):
            parse_roster("sideways:3", 0)


class TestOverrides:
    def test_numbers_and_dict_entries_are_set_and_restored(self):
        before, luck = events.LAWFUL_PENALTY, events.LUCK["chaotic"]
        restore = apply_overrides(["LAWFUL_PENALTY=0.5", "LUCK.chaotic=2"])
        assert events.LAWFUL_PENALTY == 0.5 and events.LUCK["chaotic"] == 2.0
        restore()
        assert events.LAWFUL_PENALTY == before and events.LUCK["chaotic"] == luck

    def test_unknown_names_are_refused(self):
        with pytest.raises(ValueError, match="no NOPE"):
            apply_overrides(["NOPE=1"])


class TestRun:
    def test_a_short_run_reports_every_alignment(self):
        results = run(parse_roster(None, 1), days=2, step=1800, seed=3)
        assert len(results) == 9
        rows = summarise(results, 2)
        assert [r["alignment"] for r in rows] == NINE
        # Idling alone gives about 1.0; absences and penalties take some away.
        assert all(0.3 < r["pace"] < 1.6 for r in rows)

    def test_the_same_seed_gives_the_same_realm(self):
        a = run(parse_roster("true neutral:3", 0), days=1, step=1800, seed=9)
        b = run(parse_roster("true neutral:3", 0), days=1, step=1800, seed=9)
        assert [(r.level, round(r.pace, 9)) for r in a] == \
               [(r.level, round(r.pace, 9)) for r in b]

    def test_the_real_password_hash_is_put_back(self):
        from idlerpg import auth, engine
        run(parse_roster("true neutral", 0), days=0.1, step=3600, seed=1)
        assert engine.hash_password is auth.hash_password

    def test_the_command_line_prints_a_table_and_writes_json(self, tmp_path, capsys):
        out = tmp_path / "run.json"
        simulate.main(["--days", "1", "--step", "3600", "--per-alignment", "1",
                       "--json", str(out)])
        printed = capsys.readouterr().out
        assert "lawful good" in printed and "pace" in printed
        data = json.loads(out.read_text())
        assert len(data["players"]) == 9 and len(data["alignments"]) == 9
