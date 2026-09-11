"""Tests for names that pass for other names: mixed alphabets and lookalikes."""

from __future__ import annotations

import random
from functools import partial

import pytest
from sqlalchemy import create_engine as sa_engine
from sqlalchemy.orm import Session

from idlerpg import auth
from idlerpg import engine as engine_module
from idlerpg.engine import Engine, RegistrationError
from idlerpg.models import Base, Platform
from idlerpg.rules import Curve
from idlerpg.text import check_name, skeleton


@pytest.fixture
def engine(monkeypatch):
    monkeypatch.setattr(engine_module, "hash_password",
                        partial(auth.hash_password, iterations=1))
    db = sa_engine("sqlite://")
    Base.metadata.create_all(db)
    with Session(db) as session:
        yield Engine(session, Curve(), rng=random.Random(1))


def register(engine, name):
    return engine.register(name, "pw", "x", Platform.IRC, name)


class TestAlphabets:
    @pytest.mark.parametrize("name", [
        "Rustу",                    # a Cyrillic u at the end
        "Ｒusty",                    # a full-width R among plain letters
        "Ρrofit",                   # a Greek capital rho
    ])
    def test_mixing_is_refused(self, name):
        with pytest.raises(ValueError, match="mix alphabets"):
            check_name(name)

    @pytest.mark.parametrize("name", [
        "Rusty", "Zoë", "Иван",         # Latin, Latin, Cyrillic
        "東京たろう",                         # kanji and kana
        "한국", "RustyCloud2",                            # hangul; digits are neutral
    ])
    def test_one_alphabet_is_fine(self, name):
        assert check_name(name) == name


class TestSkeletons:
    @pytest.mark.parametrize("a,b", [
        ("Rusty", "RUSTY"), ("Rusty", "Rüsty"),
        ("Rusty", "Ｒｕｓｔｙ"),               # all full-width
        ("RustyCloud", "RustyCIoud"), ("profit", "pr0fit"), ("Paul", "Pau1"),
        ("modern", "modem"),
        ("асе", "ace"),                            # all Cyrillic
    ])
    def test_these_pass_for_each_other(self, a, b):
        assert skeleton(a) == skeleton(b)

    @pytest.mark.parametrize("a,b", [("Rusty", "Rusti"), ("Bill", "Bili"), ("Rook", "Rock")])
    def test_these_do_not(self, a, b):
        assert skeleton(a) != skeleton(b)


class TestRegistration:
    def test_a_lookalike_of_a_character_is_refused(self, engine):
        register(engine, "profit")
        with pytest.raises(RegistrationError, match="could pass for profit"):
            register(engine, "pr0fit")

    def test_a_lookalike_of_an_admin_is_reserved_even_unregistered(self, engine):
        engine.apply_owners(["RustyCloud"])
        with pytest.raises(RegistrationError, match="reserved"):
            register(engine, "RustyCIoud")

    def test_renames_are_checked_but_not_against_yourself(self, engine):
        rusty, other = register(engine, "rusty"), register(engine, "other")
        with pytest.raises(RegistrationError, match="could pass for rusty"):
            engine.rename(other, "Rüsty")
        assert engine.rename(rusty, "Rusty") == "Rusty"

    def test_names_from_before_stand(self, engine):
        old = register(engine, "profit")
        old.name = "pr0fit"                 # as if registered before the check
        engine.session.commit()
        assert engine.find_player("pr0fit") is old
