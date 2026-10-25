"""Tests for ``adapters.libretro_lobby`` — reading libretro's public netplay lobby."""

from __future__ import annotations

import json
import urllib.error
from unittest.mock import MagicMock, patch

import pytest

from adapters.libretro_lobby import LibretroLobbyAdapter

_UA = "romm-tender/9.9.9"


def _resp(payload: bytes):
    resp = MagicMock()
    resp.read.return_value = payload
    resp.__enter__ = MagicMock(return_value=resp)
    resp.__exit__ = MagicMock(return_value=False)
    return resp


class TestListRooms:
    def test_reads_the_lobby_over_plain_http_with_the_plugins_user_agent(self):
        body = json.dumps([{"fields": {"username": "alice", "core_name": "Snes9x"}}]).encode()
        with patch("adapters.libretro_lobby.urllib.request.urlopen", return_value=_resp(body)) as mock_open:
            rooms = LibretroLobbyAdapter(user_agent=_UA).list_rooms()
        assert rooms == [{"username": "alice", "core_name": "Snes9x"}]
        req = mock_open.call_args[0][0]
        assert req.full_url == "http://lobby.libretro.com/list"
        assert req.get_header("User-agent") == _UA

    def test_entries_without_fields_are_dropped(self):
        body = json.dumps([{"fields": {"username": "a"}}, {"nope": 1}, "x", {"fields": "y"}]).encode()
        with patch("adapters.libretro_lobby.urllib.request.urlopen", return_value=_resp(body)):
            assert LibretroLobbyAdapter(user_agent=_UA).list_rooms() == [{"username": "a"}]

    def test_an_answer_that_is_not_a_list_is_no_rooms(self):
        with patch("adapters.libretro_lobby.urllib.request.urlopen", return_value=_resp(b'{"detail": 1}')):
            assert LibretroLobbyAdapter(user_agent=_UA).list_rooms() == []

    def test_a_transport_failure_raises(self):
        with (
            patch("adapters.libretro_lobby.urllib.request.urlopen", side_effect=urllib.error.URLError("down")),
            pytest.raises(urllib.error.URLError),
        ):
            LibretroLobbyAdapter(user_agent=_UA).list_rooms()
