"""In-memory ``NetplayConfigStore`` and ``LobbyRoomsFn`` for service tests."""

from __future__ import annotations

from typing import Any

SANDBOX_DIR = "/var/config/tender-netplay"


class FakeNetplayConfigStore:
    """Holds the session config files by name; answers the sandbox path a write lands at."""

    def __init__(self) -> None:
        self.files: dict[str, str] = {}
        self.write_side_effect: Exception | None = None

    def write_config(self, name: str, text: str) -> str:
        if self.write_side_effect is not None:
            raise self.write_side_effect
        self.files[name] = text
        return f"{SANDBOX_DIR}/{name}"

    def remove_config(self, name: str) -> None:
        self.files.pop(name, None)


class FakeLobbyRooms:
    """libretro's lobby: answers ``rooms`` (or raises ``side_effect``) and counts reads."""

    def __init__(self, rooms: list[dict[str, Any]] | None = None) -> None:
        self.rooms: list[dict[str, Any]] = list(rooms or [])
        self.side_effect: Exception | None = None
        self.calls = 0

    def __call__(self) -> list[dict[str, Any]]:
        self.calls += 1
        if self.side_effect is not None:
            raise self.side_effect
        return [dict(r) for r in self.rooms]
