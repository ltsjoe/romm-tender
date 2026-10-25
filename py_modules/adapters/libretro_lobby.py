"""libretro's public netplay lobby, where a relayed RetroArch host announces its room.

The lobby is the only place the relay publishes a room's session id, and
RetroArch cannot be pointed at a private lobby. It answers over plain HTTP; its
HTTPS endpoint refuses the request. Not a RomM request, so it never goes
through the RomM transport and never touches its reachability state.
"""

from __future__ import annotations

import json
import urllib.request
from typing import Any

_LOBBY_LIST_URL = "http://lobby.libretro.com/list"


class LibretroLobbyAdapter:
    """Reads the lobby's room list.

    Parameters
    ----------
    user_agent:
        Outgoing ``User-Agent`` — ``"<package name>/<version>"``, the same one
        every adapter talking to a server off this machine sends.
    timeout:
        Seconds a read may take before it fails.
    """

    def __init__(self, *, user_agent: str, timeout: float = 10.0) -> None:
        self._user_agent = user_agent
        self._timeout = timeout

    def list_rooms(self) -> list[dict[str, Any]]:
        """Every announced room's ``fields`` object, unvalidated; raises on a transport failure."""
        req = urllib.request.Request(_LOBBY_LIST_URL, method="GET")
        req.add_header("User-Agent", self._user_agent)
        with urllib.request.urlopen(req, timeout=self._timeout) as resp:
            payload: Any = json.loads(resp.read().decode())
        if not isinstance(payload, list):
            return []
        rooms: list[dict[str, Any]] = []
        for entry in payload:
            fields = entry.get("fields") if isinstance(entry, dict) else None
            if isinstance(fields, dict):
                rooms.append(fields)
        return rooms
