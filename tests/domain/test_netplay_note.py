"""Tests for ``domain/netplay_note.py`` — the Tender session note and its validation.

A guest builds a command that RetroDECK's ``run_game.sh`` hands to ``eval`` out
of a note ANOTHER user wrote, so the refusals here are the safety boundary: a
value that could carry a quote, a space or a shell metacharacter must never parse.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from domain.netplay_note import (
    KIND_PPSSPP,
    KIND_RETROARCH,
    NOTE_TITLE,
    RelayRoom,
    SessionNote,
    is_valid_address,
    is_valid_core_so,
    is_valid_password,
    is_valid_port,
    is_valid_relay_session,
    parse_note,
    render_note,
)


def _retroarch_note(**overrides) -> SessionNote:
    fields = {
        "kind": KIND_RETROARCH,
        "core_so": "snes9x_libretro",
        "addresses": ("100.101.102.103", "192.168.1.20"),
        "port": 55435,
        "relay": None,
        "password": "a1b2c3d4e5f60718",
        "nickname": "alice",
        "started_at": "2026-10-25T14:00:00+00:00",
    }
    fields.update(overrides)
    return SessionNote(**fields)


def _ppsspp_note(**overrides) -> SessionNote:
    fields = {
        "kind": KIND_PPSSPP,
        "core_so": None,
        "addresses": ("192.168.1.20",),
        "port": 27312,
        "relay": None,
        "password": None,
        "nickname": "alice",
        "started_at": "2026-10-25T14:00:00+00:00",
    }
    fields.update(overrides)
    return SessionNote(**fields)


def _content(payload: Any) -> str:
    """A note body carrying *payload* in the Tender fence, as a hand-crafted note would."""
    return f"Hosted with Tender.\n\n```tender-netplay\n{json.dumps(payload)}\n```\n"


def _payload(**overrides) -> dict[str, Any]:
    payload = {
        "v": 1,
        "kind": "retroarch",
        "core": "snes9x_libretro",
        "addresses": ["100.101.102.103"],
        "port": 55435,
        "relay": None,
        "password": "a1b2c3d4e5f60718",
        "nickname": "alice",
        "started_at": "2026-10-25T14:00:00+00:00",
    }
    payload.update(overrides)
    return payload


class TestRoundTrip:
    def test_a_retroarch_note_parses_back_to_itself(self):
        note = _retroarch_note()
        assert parse_note(render_note(note)) == note

    def test_a_retroarch_note_with_a_relay_parses_back_to_itself(self):
        note = _retroarch_note(
            relay=RelayRoom(host="europe-west1.relay.retroarch.com", port=55435, session="/wN25L+dlg==")
        )
        assert parse_note(render_note(note)) == note

    def test_a_ppsspp_note_parses_back_to_itself(self):
        note = _ppsspp_note()
        assert parse_note(render_note(note)) == note

    def test_the_rendered_note_opens_with_a_sentence_and_carries_the_fenced_block(self):
        text = render_note(_retroarch_note())
        first_line = text.splitlines()[0]
        assert "Tender" in first_line
        assert not first_line.startswith("```")
        assert "```tender-netplay\n" in text
        assert '"v": 1' in text

    def test_the_title_names_tender(self):
        assert NOTE_TITLE == "Tender multiplayer session"

    def test_with_relay_returns_a_copy_carrying_it(self):
        relay = RelayRoom(host="nyc.relay.retroarch.com", port=55435, session="abc")
        note = _retroarch_note()
        assert note.with_relay(relay).relay == relay
        assert note.relay is None


class TestUnrecognisedNotesAreIgnored:
    def test_a_note_without_the_marker(self):
        assert parse_note("Just a note about this game.") is None

    def test_a_json_block_under_another_info_string(self):
        assert parse_note(f"```json\n{json.dumps(_payload())}\n```") is None

    def test_another_format_version(self):
        assert parse_note(_content(_payload(v=2))) is None

    def test_a_missing_version(self):
        payload = _payload()
        del payload["v"]
        assert parse_note(_content(payload)) is None

    def test_a_block_that_is_not_json(self):
        assert parse_note("```tender-netplay\n{not json\n```") is None

    def test_a_block_that_is_not_an_object(self):
        assert parse_note(_content([1, 2, 3])) is None

    def test_an_unknown_kind(self):
        assert parse_note(_content(_payload(kind="dolphin"))) is None

    def test_an_oversized_note(self):
        assert parse_note(_content(_payload()) + "x" * 20000) is None

    def test_none_and_non_text(self):
        assert parse_note(None) is None
        assert parse_note(42) is None

    def test_a_valid_note_with_an_extra_key_still_parses(self):
        assert parse_note(_content(_payload(comment="ignored"))) is not None


# Every value below would put a shell-significant character into the command a
# guest builds. Each must make the WHOLE note unreadable, never be passed through.
_INJECTIONS = [
    '1.2.3.4"',
    "1.2.3.4 ; rm -rf ~",
    "1.2.3.4;id",
    "1.2.3.4|id",
    "1.2.3.4&id",
    "$(id)",
    "`id`",
    "1.2.3.4\nid",
    "'1.2.3.4'",
    "-oProxyCommand=x",
    "host name",
]


class TestInjectionRefusals:
    @pytest.mark.parametrize("value", _INJECTIONS)
    def test_an_address(self, value):
        assert parse_note(_content(_payload(addresses=[value]))) is None

    @pytest.mark.parametrize("value", _INJECTIONS)
    def test_a_relay_host(self, value):
        relay = {"host": value, "port": 55435, "session": "abc"}
        assert parse_note(_content(_payload(relay=relay))) is None

    @pytest.mark.parametrize("value", [*_INJECTIONS, "a" * 65, "", "abc-def", "abc_def"])
    def test_a_relay_session(self, value):
        relay = {"host": "nyc.relay.retroarch.com", "port": 55435, "session": value}
        assert parse_note(_content(_payload(relay=relay))) is None

    @pytest.mark.parametrize("value", [*_INJECTIONS, "abc", "a" * 33, "pass word", "pässword1", ""])
    def test_a_password(self, value):
        assert parse_note(_content(_payload(password=value))) is None

    @pytest.mark.parametrize(
        "value", [*_INJECTIONS, "../../evil_libretro", "/usr/lib/x_libretro", "snes9x", "snes9x_libretro.so"]
    )
    def test_a_core(self, value):
        assert parse_note(_content(_payload(core=value))) is None

    @pytest.mark.parametrize("value", [0, -1, 65536, "55435", 55435.0, True, None])
    def test_a_port(self, value):
        assert parse_note(_content(_payload(port=value))) is None

    @pytest.mark.parametrize("value", [0, 65536, "55435", True])
    def test_a_relay_port(self, value):
        relay = {"host": "nyc.relay.retroarch.com", "port": value, "session": "abc"}
        assert parse_note(_content(_payload(relay=relay))) is None

    @pytest.mark.parametrize("value", ['al"ice', "al\nice", "", "x" * 65, 7])
    def test_a_nickname(self, value):
        assert parse_note(_content(_payload(nickname=value))) is None

    @pytest.mark.parametrize("value", ["yesterday", "", "2026-10-25T14:00:00; id", 1])
    def test_a_start_time(self, value):
        assert parse_note(_content(_payload(started_at=value))) is None

    def test_addresses_that_are_not_a_list(self):
        assert parse_note(_content(_payload(addresses="1.2.3.4"))) is None

    def test_too_many_addresses(self):
        assert parse_note(_content(_payload(addresses=[f"10.0.0.{i}" for i in range(1, 20)]))) is None

    def test_a_relay_that_is_not_an_object(self):
        assert parse_note(_content(_payload(relay="nyc.relay.retroarch.com"))) is None


class TestATrailingNewlineIsRefused:
    """A ``$`` anchor also matches before a final newline, which a shell reads as a new command."""

    def test_an_address(self):
        assert not is_valid_address("192.168.1.20\n")
        assert not is_valid_address("relay.retroarch.com\n")
        assert parse_note(_content(_payload(addresses=["192.168.1.20\n"]))) is None

    def test_a_relay_session(self):
        assert not is_valid_relay_session("abc\n")

    def test_a_password(self):
        assert not is_valid_password("abcd\n")

    def test_a_core(self):
        assert not is_valid_core_so("snes9x_libretro\n")

    def test_a_nickname(self):
        assert parse_note(_content(_payload(nickname="alice\n"))) is None

    def test_a_start_time(self):
        assert parse_note(_content(_payload(started_at="2026-10-25T14:00:00+00:00\n"))) is None


class TestKindRules:
    def test_retroarch_needs_a_password(self):
        assert parse_note(_content(_payload(password=None))) is None

    def test_retroarch_needs_a_core(self):
        assert parse_note(_content(_payload(core=None))) is None

    def test_retroarch_needs_an_address_or_a_relay(self):
        assert parse_note(_content(_payload(addresses=[]))) is None
        relay = {"host": "nyc.relay.retroarch.com", "port": 55435, "session": "abc"}
        assert parse_note(_content(_payload(addresses=[], relay=relay))) is not None

    def test_ppsspp_names_no_core(self):
        payload = _payload(kind="ppsspp", core="ppsspp_libretro", password=None, port=27312)
        assert parse_note(_content(payload)) is None

    def test_ppsspp_has_no_relay(self):
        relay = {"host": "nyc.relay.retroarch.com", "port": 55435, "session": "abc"}
        payload = _payload(kind="ppsspp", core=None, password=None, port=27312, relay=relay)
        assert parse_note(_content(payload)) is None

    def test_ppsspp_needs_an_address(self):
        payload = _payload(kind="ppsspp", core=None, password=None, port=27312, addresses=[])
        assert parse_note(_content(payload)) is None

    def test_rendering_an_invalid_note_raises(self):
        with pytest.raises(ValueError):
            render_note(_retroarch_note(password="no good"))


class TestValidators:
    @pytest.mark.parametrize("value", ["192.168.1.20", "100.64.0.1", "relay.retroarch.com", "nyc", "a-b.c"])
    def test_good_addresses(self, value):
        assert is_valid_address(value)

    @pytest.mark.parametrize("value", [*_INJECTIONS, "", "256.1.1.1", "1.2.3", "::1", "a" * 254, None, 5])
    def test_bad_addresses(self, value):
        assert not is_valid_address(value)

    def test_ports(self):
        assert is_valid_port(1) and is_valid_port(65535)
        assert not is_valid_port(0) and not is_valid_port(True) and not is_valid_port("1")

    def test_sessions(self):
        assert is_valid_relay_session("/wN25LdlgMoS3OAE")
        assert not is_valid_relay_session("a b")

    def test_passwords(self):
        assert is_valid_password("abcd") and is_valid_password("A1" * 16)
        assert not is_valid_password("abc")

    def test_cores(self):
        assert is_valid_core_so("pcsx_rearmed_libretro")
        assert is_valid_core_so("mame2003_plus_libretro")
        assert not is_valid_core_so("pcsx rearmed_libretro")
