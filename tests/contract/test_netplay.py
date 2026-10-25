"""Contract tests for the multiplayer callables over the real ``Plugin``.

Hosting and joining cross the wire as the frontend calls them: positional,
JSON-shaped arguments, and answers pinned against the shapes declared in
``src/api/backend.ts``. The real SQLite store holds the crash record and the
device id; the session config files are written for real under the harness
home; RomM, the network and libretro's lobby are the harness fakes.
"""

from __future__ import annotations

import asyncio
import json
import os

import pytest

from domain.netplay_note import NOTE_TITLE, RelayRoom, SessionNote, parse_note, render_note
from lib.errors import RommForbiddenError
from services.netplay import HOST_SESSION_KEY

from ._seed import seed_es_systems, seed_install

STARTED_AT = "2026-01-01T00:00:00+00:00"


class _ParkedSleeper:
    """Every sleep waits until its task is cancelled, so a session's background loop never spins."""

    async def sleep(self, seconds: float) -> None:
        await asyncio.Event().wait()


@pytest.fixture
async def netplay(harness):
    """The harness with a hostable GBA game on a deterministic core, and the service's loops parked."""
    service = harness.plugin._netplay_service
    service._sleeper = _ParkedSleeper()
    seed_es_systems(harness)
    seed_install(harness, 42, system="gba", platform_slug="gba", file_name="pokemon.gba")
    harness.retroarch_cores.features["mgba_libretro"] = "deterministic"
    harness.retroarch_cores.installed.add("mgba_libretro")
    harness.retroarch_cores.corenames["mgba_libretro"] = "mGBA"
    harness.network.addresses = ["127.0.0.1", "192.168.1.20"]
    harness.romm.set_version("5.3.0")
    harness.romm.current_user = {"id": 1, "username": "alice"}
    yield harness
    await service.shutdown()


def _config_path(harness, name: str) -> str:
    return os.path.join(
        str(harness.tmp_path), "home", ".var", "app", "net.retrodeck.retrodeck", "config", "tender-netplay", name
    )


def _kv(harness, key: str) -> str | None:
    with harness.uow_factory() as uow:
        return uow.kv_config.get(key)


def _seed_bobs_session(harness, *, relay: RelayRoom | None) -> None:
    note = SessionNote(
        kind="retroarch",
        core_so="mgba_libretro",
        addresses=("100.70.0.2",),
        port=55435,
        relay=relay,
        password="hostpass1234",
        nickname="bob",
        started_at=STARTED_AT,
    )
    harness.romm.rom_notes[42] = [
        {"id": 77, "title": NOTE_TITLE, "content": render_note(note), "is_public": True, "user_id": 2}
    ]
    harness.romm.activity.append({"user_id": 2, "username": "bob", "rom_id": 42, "rom_name": "Pokemon"})


async def test_capability(netplay):
    result = await netplay.plugin.get_netplay_capability(42)
    assert result == {"success": True, "available": True, "kind": "retroarch", "reason": None, "message": None}


async def test_capability_on_an_old_server(netplay):
    netplay.romm.set_version("4.9.2")
    result = await netplay.plugin.get_netplay_capability(42)
    assert set(result) == {"success", "available", "kind", "reason", "message"}
    assert result["available"] is False
    assert result["reason"] == "unsupported"


async def test_hosting_then_ending_a_session(netplay):
    result = await netplay.plugin.start_host_session(42)

    assert set(result) == {"success", "rom_id", "app_id", "kind", "launch_options", "prune_lease_token"}
    assert result["success"] is True
    assert result["app_id"] == 42
    assert result["kind"] == "retroarch"
    assert result["prune_lease_token"].startswith("netplay_launch:")
    assert (
        "/mgba_libretro.so --host --appendconfig=/var/config/tender-netplay/retroarch-host.cfg %ROM%"
        in result["launch_options"]
    )
    with open(_config_path(netplay, "retroarch-host.cfg"), encoding="utf-8") as f:
        assert 'netplay_nickname = "alice"' in f.read().splitlines()
    [note] = netplay.romm.rom_notes[42]
    parsed = parse_note(note["content"])
    assert parsed is not None and parsed.addresses == ("192.168.1.20",)
    [record] = json.loads(_kv(netplay, HOST_SESSION_KEY) or "")
    assert record["rom_id"] == 42 and record["note_id"] == note["id"]
    # D22: a RomM device exists now, and save sync is still off.
    assert _kv(netplay, "device_id") == record["device_id"]
    assert netplay.plugin.settings.get("save_sync_enabled") is not True
    # D8: the one-off command is never recorded as the shortcut's own.
    with netplay.uow_factory() as uow:
        rom = uow.roms.get(42)
    assert rom is not None and rom.applied_launch_options is None
    await netplay.plugin.release_prune_conflict_lease(result["prune_lease_token"])

    ended = await netplay.plugin.end_host_session()

    assert ended == {"success": True, "note_removed": True}
    assert netplay.romm.rom_notes[42] == []
    assert netplay.romm.list_activity() == []
    assert not os.path.exists(_config_path(netplay, "retroarch-host.cfg"))
    assert _kv(netplay, HOST_SESSION_KEY) is None


async def test_a_token_without_the_scope_is_asked_to_sign_in_again(netplay):
    netplay.romm.send_activity_heartbeat_side_effect = RommForbiddenError("forbidden")

    result = await netplay.plugin.start_host_session(42)

    assert set(result) == {"success", "reason", "message"}
    assert result["success"] is False
    assert result["reason"] == "reauth_required"
    assert not os.path.exists(_config_path(netplay, "retroarch-host.cfg"))
    assert netplay.romm.rom_notes.get(42, []) == []


async def test_hosting_on_an_old_server_is_refused(netplay):
    netplay.romm.set_version("4.9.0")
    result = await netplay.plugin.start_host_session(42)
    assert result == {"success": False, "reason": "unsupported", "message": result["message"]}


async def test_listing_another_users_session(netplay):
    _seed_bobs_session(netplay, relay=None)

    result = await netplay.plugin.list_hosted_sessions()

    assert result == {
        "success": True,
        "sessions": [
            {
                "key": f"2:42:{STARTED_AT}",
                "rom_id": 42,
                "rom_name": "Pokemon",
                "app_id": 42,
                "host_user_id": 2,
                "host_username": "bob",
                "kind": "retroarch",
                "started_at": STARTED_AT,
                "state": "join",
            }
        ],
    }


async def test_listing_when_romm_cannot_be_reached(netplay):
    from lib.errors import RommConnectionError

    netplay.romm.list_activity_side_effect = RommConnectionError("down")
    result = await netplay.plugin.list_hosted_sessions()
    assert set(result) == {"success", "reason", "message"}
    assert result["reason"] == "server_unreachable"


async def test_joining_over_the_relay_then_ending(netplay):
    _seed_bobs_session(netplay, relay=RelayRoom(host="nyc.relay.retroarch.com", port=55435, session="abcd"))

    result = await netplay.plugin.prepare_join_session(42, 2, STARTED_AT)

    assert set(result) == {
        "success",
        "rom_id",
        "app_id",
        "kind",
        "connection",
        "launch_options",
        "prune_lease_token",
    }
    assert result["connection"] == "relay"
    assert (
        "--connect=nyc.relay.retroarch.com --port=55435 --mitm-session=abcd --sram-mode=noload-nosave"
        in (result["launch_options"])
    )
    assert os.path.exists(_config_path(netplay, "retroarch-guest.cfg"))
    await netplay.plugin.release_prune_conflict_lease(result["prune_lease_token"])

    assert await netplay.plugin.end_join_session() == {"success": True}
    assert not os.path.exists(_config_path(netplay, "retroarch-guest.cfg"))


async def test_joining_a_session_that_ended(netplay):
    _seed_bobs_session(netplay, relay=None)
    netplay.romm.activity.clear()

    result = await netplay.plugin.prepare_join_session(42, 2, STARTED_AT)

    assert result == {"success": False, "reason": "session_ended", "message": result["message"]}


async def test_a_crashed_sessions_note_is_cleaned_up(netplay):
    stale = netplay.romm.create_rom_note(42, title=NOTE_TITLE, content="stale", is_public=True)
    with netplay.uow_factory() as uow:
        uow.kv_config.set(HOST_SESSION_KEY, json.dumps({"rom_id": 42, "note_id": stale["id"], "device_id": "d"}))

    await netplay.plugin._netplay_service.clean_up_leftover_session()

    assert netplay.romm.rom_notes[42] == []
    assert _kv(netplay, HOST_SESSION_KEY) is None
