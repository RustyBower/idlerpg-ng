"""Tests for IRC capability negotiation and services accounts.

The bot has to register successfully whatever the server does: offer
everything, offer nothing, refuse the request, or split the list over several
lines. Failing to send CAP END at the end of any of those hangs the
connection before 001, and the realm never comes up.
"""

from __future__ import annotations

from test_irc_adapter import FakeWriter, adapter, feed, sent  # noqa: F401


class TestNegotiating:
    def test_it_requests_only_what_is_offered_and_wanted(self, adapter):
        feed(adapter, ":server CAP * LS :account-notify chghost sasl multi-prefix")
        asked = [line for line in sent(adapter).splitlines()
                 if line.startswith("CAP REQ")]
        assert asked, "nothing was requested"
        assert "account-notify" in asked[0]
        assert "chghost" in asked[0]
        assert "multi-prefix" in asked[0]
        assert "sasl" not in asked[0]           # offered, but not ours to use
        assert "extended-join" not in asked[0]  # wanted, but not offered

    def test_a_split_list_is_gathered_before_requesting(self, adapter):
        feed(adapter, ":server CAP * LS * :account-notify")
        assert "CAP REQ" not in sent(adapter)   # the rest is still coming
        feed(adapter, ":server CAP * LS :chghost")
        asked = [line for line in sent(adapter).splitlines()
                 if line.startswith("CAP REQ")][0]
        assert "account-notify" in asked and "chghost" in asked

    def test_registration_finishes_when_nothing_useful_is_offered(self, adapter):
        feed(adapter, ":server CAP * LS :sasl away-notify")
        assert "CAP REQ" not in sent(adapter)
        assert "CAP END" in sent(adapter)

    def test_an_ack_records_them_and_ends(self, adapter):
        feed(adapter, ":server CAP * LS :account-notify chghost")
        feed(adapter, ":server CAP idlerpg ACK :account-notify chghost")
        assert adapter.caps == {"account-notify", "chghost"}
        assert "CAP END" in sent(adapter)

    def test_a_refusal_still_ends(self, adapter):
        feed(adapter, ":server CAP * LS :account-notify")
        feed(adapter, ":server CAP idlerpg NAK :account-notify")
        assert adapter.caps == set()
        assert "CAP END" in sent(adapter)


class TestWhox:
    def test_isupport_says_when_whox_is_there(self, adapter):
        assert adapter.whox is False
        feed(adapter, ":server 005 idlerpg WHOX SAFELIST :are supported")
        assert adapter.whox is True

    def test_a_server_without_it_says_so_by_silence(self, adapter):
        feed(adapter, ":server 005 idlerpg SAFELIST CHANTYPES=# :are supported")
        assert adapter.whox is False

    def test_the_trailing_words_are_not_mistaken_for_tokens(self, adapter):
        """"are supported" is prose, not ISUPPORT, and must not be read."""
        feed(adapter, ":server 005 idlerpg SAFELIST :these WHOX are supported")
        assert adapter.whox is False


class TestAccounts:
    def test_extended_join_notes_the_account(self, adapter):
        adapter.caps.add("extended-join")
        feed(adapter, ":someone!u@h JOIN #idlerpg theiraccount :Real Name")
        assert adapter.accounts["someone"] == "theiraccount"

    def test_an_unregistered_joiner_is_a_star(self, adapter):
        adapter.caps.add("extended-join")
        feed(adapter, ":someone!u@h JOIN #idlerpg * :Real Name")
        assert "someone" not in adapter.accounts

    def test_without_the_cap_the_parameter_is_not_an_account(self, adapter):
        """Plain JOIN has no account argument; reading one would invent it."""
        feed(adapter, ":someone!u@h JOIN #idlerpg")
        assert "someone" not in adapter.accounts

    def test_identifying_later_is_noticed(self, adapter):
        feed(adapter, ":someone!u@h ACCOUNT theiraccount")
        assert adapter.accounts["someone"] == "theiraccount"

    def test_logging_out_of_services_forgets_it(self, adapter):
        adapter.accounts["someone"] = "theiraccount"
        feed(adapter, ":someone!u@h ACCOUNT *")
        assert "someone" not in adapter.accounts
