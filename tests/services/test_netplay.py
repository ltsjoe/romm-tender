"""Unit tests for ``NetplayService`` — hosting a game through RomM, and joining one.

The host keeps RomM's activity heartbeat live and a public Tender note on the
ROM; a guest trusts a note only while its writer's activity entry is live.
Every RomM call goes through ``FakeRommApi``; the background heartbeat and relay
search run on a sleeper the test steps by hand.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

import pytest
from fakes.fake_active_core_resolver import FakeActiveCoreResolver
from fakes.fake_core_info_provider import FakeCoreInfoProvider, libretro_option, standalone_option
from fakes.fake_local_network import FakeLocalNetworkReader
from fakes.fake_netplay_config_store import FakeLobbyRooms, FakeNetplayConfigStore
from fakes.fake_retroarch_core_info import FakeRetroArchCoreInfoReader
from fakes.fake_rom_launch_path import FakeRomLaunchPathReader
from fakes.fake_romm_api import FakeRommApi
from fakes.fake_unit_of_work import FakeUnitOfWorkFactory
from fakes.system_time import FakeClock, FakeUuidGen

from domain.netplay import PCSX_REARMED_CORE
from domain.netplay_note import NOTE_TITLE, RelayRoom, SessionNote, parse_note, render_note
from domain.rom import Rom
from domain.rom_install import RomInstall
from domain.shortcut_data import EmulatorInvocation
from lib.errors import RommConnectionError, RommForbiddenError, RommNotFoundError
from services.netplay import HOST_SESSION_KEY, NetplayService, NetplayServiceConfig

ROM_ID = 7
APP_ID = 3_000_000_007
ROM_PATH = "/home/deck/retrodeck/roms/snes/Game.sfc"
UUID = "0f1e2d3c-4b5a-4987-8a6b-5c4d3e2f1a0b"
PASSWORD = "0f1e2d3c4b5a4987"
PPSSPP_COMMAND = "%EMULATOR_PPSSPP% %ROM%"
SYSTEMS = {"snes": "snes", "ps": "psx", "psp": "psp", "gba": "gba"}


class _SteppedSleeper:
    """``Sleeper`` whose sleeps stay parked until the test releases them."""

    def __init__(self) -> None:
        self.calls: list[float] = []
        self._parked: list[tuple[float, asyncio.Future[None]]] = []

    async def sleep(self, seconds: float) -> None:
        self.calls.append(seconds)
        future: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self._parked.append((seconds, future))
        await future

    def parked(self, seconds: float) -> int:
        return sum(1 for s, f in self._parked if s == seconds and not f.done())

    def release(self, seconds: float) -> None:
        for s, future in self._parked:
            if s == seconds and not future.done():
                future.set_result(None)


async def _until(predicate, timeout: float = 2.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() > deadline:
            raise AssertionError("condition never held")
        await asyncio.sleep(0.005)


async def _register_ok() -> dict[str, Any]:
    return {"success": True, "device_id": "device-9", "device_name": "deck"}


class _World:
    """Everything a ``NetplayService`` is wired to, seeded for one test."""

    def __init__(self) -> None:
        self.romm = FakeRommApi()
        self.romm.set_version("5.3.0")
        self.romm.current_user = {"id": 1, "username": "alice"}
        self.settings: dict[str, Any] = {"romm_user_id": 1}
        self.uow_factory = FakeUnitOfWorkFactory()
        self.active_core = FakeActiveCoreResolver(
            per_rom_emulator={ROM_ID: EmulatorInvocation.libretro("snes9x_libretro", "Snes9x")}
        )
        self.core_info = FakeCoreInfoProvider(options=[libretro_option("snes9x_libretro", "Snes9x")])
        self.cores = FakeRetroArchCoreInfoReader(
            features={"snes9x_libretro": "deterministic", PCSX_REARMED_CORE: "deterministic"},
            installed={"snes9x_libretro", PCSX_REARMED_CORE},
            corenames={"snes9x_libretro": "Snes9x", PCSX_REARMED_CORE: "PCSX-ReARMed"},
        )
        self.launch_path = FakeRomLaunchPathReader({ROM_ID: ROM_PATH})
        self.register_calls = 0
        self.register_answer: dict[str, Any] = {"success": True, "device_id": "device-9"}
        self.save_dirs: dict[int, str] = {ROM_ID: "/home/deck/retrodeck/saves/psx"}
        self.config_store = FakeNetplayConfigStore()
        self.network = FakeLocalNetworkReader(addresses=["127.0.0.1", "192.168.1.20", "100.101.102.103"])
        self.lobby = FakeLobbyRooms()
        self.clock = FakeClock()
        self.sleeper = _SteppedSleeper()
        self.uuid_gen = FakeUuidGen([UUID])
        self.debug: list[str] = []

    async def _register(self) -> dict[str, Any]:
        self.register_calls += 1
        return dict(self.register_answer)

    def seed_rom(
        self, rom_id: int = ROM_ID, *, platform_slug: str = "snes", app_id: int | None = APP_ID, installed: bool = True
    ) -> None:
        with self.uow_factory() as uow:
            uow.roms.save(
                Rom.synced(
                    rom_id=rom_id,
                    platform_slug=platform_slug,
                    name=f"Game {rom_id}",
                    fs_name=f"game-{rom_id}.sfc",
                    shortcut_app_id=app_id,
                    synced_at="2026-01-01T00:00:00",
                )
            )
            if installed:
                uow.rom_installs.save(
                    RomInstall.mark_installed(
                        rom_id=rom_id,
                        file_path=ROM_PATH,
                        rom_dir=None,
                        platform_slug=platform_slug,
                        system=SYSTEMS.get(platform_slug, platform_slug),
                        installed_at="2026-01-01T00:00:00",
                    )
                )

    def kv(self, key: str) -> str | None:
        with self.uow_factory() as uow:
            return uow.kv_config.get(key)

    def build(self) -> NetplayService:
        return NetplayService(
            config=NetplayServiceConfig(
                romm_api=self.romm,
                settings=self.settings,
                uow_factory=self.uow_factory,
                active_core=self.active_core,
                core_info=self.core_info,
                resolve_system=lambda platform_slug, platform_fs_slug=None: SYSTEMS.get(platform_slug, platform_slug),
                retroarch_cores=self.cores,
                launch_path=self.launch_path,
                register_device=self._register,
                save_dir_for_rom=lambda rom_id: self.save_dirs.get(rom_id),
                config_store=self.config_store,
                network=self.network,
                lobby_rooms=self.lobby,
                loop=asyncio.get_running_loop(),
                logger=logging.getLogger("test"),
                log_debug=lambda msg: self.debug.append(msg),
                clock=self.clock,
                sleeper=self.sleeper,
                uuid_gen=self.uuid_gen,
            )
        )


@pytest.fixture
async def world() -> _World:
    w = _World()
    w.seed_rom()
    return w


def _host_note(**overrides) -> SessionNote:
    fields: dict[str, Any] = {
        "kind": "retroarch",
        "core_so": "snes9x_libretro",
        "addresses": ("100.70.0.2", "192.168.1.30"),
        "port": 55435,
        "relay": RelayRoom(host="nyc.relay.retroarch.com", port=55435, session="/wN25L+dlg=="),
        "password": "hostpass1234",
        "nickname": "bob",
        "started_at": "2026-01-01T00:00:00+00:00",
    }
    fields.update(overrides)
    return SessionNote(**fields)


def _seed_hosted(world: _World, *, note: SessionNote | None = None, user_id: int = 2, live: bool = True) -> None:
    """User *user_id* is hosting ROM 7: a public Tender note and, if *live*, an activity entry."""
    note = note or _host_note()
    world.romm.rom_notes.setdefault(ROM_ID, []).append(
        {
            "id": 77,
            "title": NOTE_TITLE,
            "content": render_note(note),
            "is_public": True,
            "user_id": user_id,
            "username": "bob",
            "updated_at": "2026-01-01T00:00:01+00:00",
        }
    )
    if live:
        world.romm.activity.append(
            {"user_id": user_id, "username": "bob", "rom_id": ROM_ID, "rom_name": "Game 7", "device_id": "d-2"}
        )


# ── Capability ─────────────────────────────────────────────────────────────


class TestCapability:
    async def test_a_deterministic_core_can_host_on_retroarch(self, world):
        result = await world.build().get_capability(ROM_ID)
        assert result == {"success": True, "available": True, "kind": "retroarch", "reason": None, "message": None}

    async def test_a_server_older_than_the_activity_feed_cannot(self, world):
        world.romm.set_version("4.9.2")
        result = await world.build().get_capability(ROM_ID)
        assert result["available"] is False
        assert result["reason"] == "unsupported"

    async def test_an_unknown_version_is_probed_once_and_kept(self, world):
        world.romm.set_version(None)
        world.romm.heartbeat_response = {"SYSTEM": {"VERSION": "5.1.0"}}
        service = world.build()
        assert (await service.get_capability(ROM_ID))["available"] is True
        assert world.romm.get_version() == "5.1.0"

    async def test_an_unreachable_server_cannot(self, world):
        world.romm.set_version(None)
        world.romm.heartbeat_side_effect = RommConnectionError("down")
        result = await world.build().get_capability(ROM_ID)
        assert result["available"] is False
        assert result["reason"] == "server_unreachable"

    async def test_a_game_that_is_not_installed_cannot(self, world):
        world.launch_path.paths.clear()
        result = await world.build().get_capability(ROM_ID)
        assert result["available"] is False
        assert result["reason"] == "not_installed"

    async def test_an_emulator_without_netplay_cannot(self, world):
        world.active_core.per_rom_emulator[ROM_ID] = EmulatorInvocation.standalone("%EMULATOR_PCSX2% %ROM%", "PCSX2")
        result = await world.build().get_capability(ROM_ID)
        assert result["available"] is False
        assert result["reason"] == "emulator_not_supported"
        assert result["message"]

    async def test_a_core_without_deterministic_savestates_cannot(self, world):
        world.cores.features.clear()
        result = await world.build().get_capability(ROM_ID)
        assert result["reason"] == "core_not_supported"


# ── Hosting ────────────────────────────────────────────────────────────────


class TestStartHostSession:
    async def test_a_retroarch_session(self, world):
        service = world.build()
        result = await service.start_host_session(ROM_ID)

        assert result["success"] is True
        assert result["app_id"] == APP_ID
        assert result["kind"] == "retroarch"
        assert result["rom_id"] == ROM_ID
        assert result["launch_options"] == (
            'flatpak run net.retrodeck.retrodeck -e "%EMULATOR_RETROARCH% -L '
            "/var/config/retroarch/cores/snes9x_libretro.so --host "
            f'--appendconfig=/var/config/tender-netplay/retroarch-host.cfg %ROM%" "{ROM_PATH}"'
        )
        config = world.config_store.files["retroarch-host.cfg"].splitlines()
        assert f'netplay_password = "{PASSWORD}"' in config
        assert 'netplay_nickname = "alice"' in config
        assert not any(line.startswith("savefile_directory") for line in config)

        assert [(e["rom_id"], e["device_id"]) for e in world.romm.list_activity()] == [(ROM_ID, "device-9")]
        [note] = world.romm.rom_notes[ROM_ID]
        assert note["title"] == NOTE_TITLE
        assert note["is_public"] is True
        parsed = parse_note(note["content"])
        assert parsed is not None
        assert parsed.addresses == ("100.101.102.103", "192.168.1.20")
        assert parsed.port == 55435
        assert parsed.password == PASSWORD
        assert parsed.core_so == "snes9x_libretro"
        assert parsed.relay is None
        assert json.loads(world.kv(HOST_SESSION_KEY) or "") == [
            {"rom_id": ROM_ID, "note_id": note["id"], "device_id": "device-9"}
        ]
        await service.shutdown()

    async def test_registers_the_device_without_save_sync(self, world):
        service = world.build()
        await service.start_host_session(ROM_ID)
        assert world.register_calls == 1
        assert "save_sync_enabled" not in world.settings
        await service.shutdown()

    async def test_a_ps1_game_on_swanstation_runs_on_pcsx_rearmed_and_keeps_its_card(self, world):
        world.seed_rom(platform_slug="ps")
        world.active_core.per_rom_emulator[ROM_ID] = EmulatorInvocation.libretro("swanstation_libretro", "SwanStation")
        world.core_info.options = [
            libretro_option("swanstation_libretro", "SwanStation"),
            libretro_option(PCSX_REARMED_CORE, "PCSX ReARMed"),
        ]
        service = world.build()
        result = await service.start_host_session(ROM_ID)

        assert result["success"] is True
        assert f"/var/config/retroarch/cores/{PCSX_REARMED_CORE}.so --host" in result["launch_options"]
        config = world.config_store.files["retroarch-host.cfg"].splitlines()
        assert 'savefile_directory = "/home/deck/retrodeck/saves/psx"' in config
        assert 'sort_savefiles_enable = "false"' in config
        parsed = parse_note(world.romm.rom_notes[ROM_ID][0]["content"])
        assert parsed is not None and parsed.core_so == PCSX_REARMED_CORE
        with world.uow_factory() as uow:
            rom = uow.roms.get(ROM_ID)
        assert rom is not None and rom.emulator_override is None
        await service.shutdown()

    async def test_a_switched_session_without_a_known_save_folder_is_refused(self, world):
        world.seed_rom(platform_slug="ps")
        world.active_core.per_rom_emulator[ROM_ID] = EmulatorInvocation.libretro("swanstation_libretro", "SwanStation")
        world.core_info.options = [libretro_option(PCSX_REARMED_CORE, "PCSX ReARMed")]
        world.save_dirs.clear()
        result = await world.build().start_host_session(ROM_ID)
        assert result["success"] is False
        assert result["reason"] == "save_folder_unknown"
        assert world.romm.rom_notes == {}

    async def test_a_save_folder_a_config_cannot_hold_is_refused(self, world):
        world.seed_rom(platform_slug="ps")
        world.active_core.per_rom_emulator[ROM_ID] = EmulatorInvocation.libretro("swanstation_libretro", "SwanStation")
        world.core_info.options = [libretro_option(PCSX_REARMED_CORE, "PCSX ReARMed")]
        world.save_dirs[ROM_ID] = '/home/deck/saves/"psx"'
        result = await world.build().start_host_session(ROM_ID)
        assert result["reason"] == "config_write_failed"
        assert world.config_store.files == {}

    async def test_a_config_that_cannot_be_written_is_refused(self, world):
        world.config_store.write_side_effect = OSError("read-only")
        result = await world.build().start_host_session(ROM_ID)
        assert result["reason"] == "config_write_failed"
        assert world.romm.list_activity() == []

    async def test_a_ppsspp_session(self, world):
        world.seed_rom(platform_slug="psp")
        world.active_core.per_rom_emulator[ROM_ID] = EmulatorInvocation.standalone(PPSSPP_COMMAND, "PPSSPP")
        service = world.build()
        result = await service.start_host_session(ROM_ID)

        assert result["kind"] == "ppsspp"
        assert (
            "%EMULATOR_PPSSPP% --appendconfig=/var/config/tender-netplay/ppsspp-host.ini %ROM%"
            in (result["launch_options"])
        )
        config = world.config_store.files["ppsspp-host.ini"].splitlines()
        assert "EnableAdhocServer = True" in config
        assert "proAdhocServer = 100.101.102.103" in config
        parsed = parse_note(world.romm.rom_notes[ROM_ID][0]["content"])
        assert parsed is not None
        assert parsed.port == 27312 and parsed.password is None and parsed.core_so is None
        await service.shutdown()

    async def test_a_ppsspp_session_needs_an_address(self, world):
        world.active_core.per_rom_emulator[ROM_ID] = EmulatorInvocation.standalone(PPSSPP_COMMAND, "PPSSPP")
        world.network.addresses = ["127.0.0.1", "8.8.8.8"]
        result = await world.build().start_host_session(ROM_ID)
        assert result["reason"] == "no_network"

    async def test_a_second_session_is_refused(self, world):
        service = world.build()
        await service.start_host_session(ROM_ID)
        result = await service.start_host_session(ROM_ID)
        assert result == {
            "success": False,
            "reason": "session_active",
            "message": result["message"],
        }
        await service.shutdown()

    async def test_an_old_server_is_refused(self, world):
        world.romm.set_version("4.9.0")
        result = await world.build().start_host_session(ROM_ID)
        assert result["reason"] == "unsupported"
        assert world.register_calls == 0

    async def test_a_game_that_is_not_installed_is_refused(self, world):
        world.launch_path.paths.clear()
        result = await world.build().start_host_session(ROM_ID)
        assert result["reason"] == "not_installed"

    async def test_an_unsupported_emulator_is_refused(self, world):
        world.active_core.per_rom_emulator[ROM_ID] = None
        result = await world.build().start_host_session(ROM_ID)
        assert result["reason"] == "emulator_not_supported"

    async def test_a_device_that_cannot_register_is_refused_with_its_reason(self, world):
        world.register_answer = {"success": False, "reason": "server_unreachable", "message": "down"}
        result = await world.build().start_host_session(ROM_ID)
        assert result == {"success": False, "reason": "server_unreachable", "message": "down"}
        assert world.config_store.files == {}

    async def test_a_forbidden_heartbeat_asks_to_sign_in_again(self, world):
        world.romm.send_activity_heartbeat_side_effect = RommForbiddenError("forbidden")
        result = await world.build().start_host_session(ROM_ID)
        assert result["success"] is False
        assert result["reason"] == "reauth_required"
        assert "username and password" in result["message"]
        assert world.config_store.files == {}
        assert world.romm.rom_notes == {}

    async def test_a_forbidden_note_asks_to_sign_in_again_and_clears_the_heartbeat(self, world):
        world.romm.create_rom_note_side_effect = RommForbiddenError("forbidden")
        result = await world.build().start_host_session(ROM_ID)
        assert result["reason"] == "reauth_required"
        assert world.romm.list_activity() == []
        assert world.config_store.files == {}
        assert world.kv(HOST_SESSION_KEY) is None

    async def test_an_unreachable_server_at_the_heartbeat_is_classified(self, world):
        world.romm.send_activity_heartbeat_side_effect = RommConnectionError("down")
        result = await world.build().start_host_session(ROM_ID)
        assert result["reason"] == "server_unreachable"

    async def test_this_users_earlier_note_on_the_rom_is_replaced(self, world):
        world.romm.create_rom_note(ROM_ID, title=NOTE_TITLE, content="stale", is_public=True)
        service = world.build()
        result = await service.start_host_session(ROM_ID)
        assert result["success"] is True
        [note] = world.romm.rom_notes[ROM_ID]
        assert note["content"] != "stale"
        await service.shutdown()

    async def test_a_crashed_note_that_cannot_be_deleted_yet_stays_recorded_beside_the_new_one(self, world):
        with world.uow_factory() as uow:
            uow.kv_config.set(HOST_SESSION_KEY, json.dumps([{"rom_id": 8, "note_id": 5, "device_id": "d"}]))
        world.romm.delete_rom_note_side_effect = RommConnectionError("down")
        service = world.build()
        await service.start_host_session(ROM_ID)
        records = json.loads(world.kv(HOST_SESSION_KEY) or "")
        assert [(r["rom_id"], r["note_id"]) for r in records] == [(8, 5), (ROM_ID, records[1]["note_id"])]
        world.romm.delete_rom_note_side_effect = None
        await service.end_host_session()
        assert json.loads(world.kv(HOST_SESSION_KEY) or "") == [{"rom_id": 8, "note_id": 5, "device_id": "d"}]

    async def test_a_note_left_by_a_crash_is_removed_first(self, world):
        stale = world.romm.create_rom_note(8, title=NOTE_TITLE, content="stale", is_public=True)
        with world.uow_factory() as uow:
            uow.kv_config.set(HOST_SESSION_KEY, json.dumps({"rom_id": 8, "note_id": stale["id"], "device_id": "d"}))
        service = world.build()
        await service.start_host_session(ROM_ID)
        assert world.romm.rom_notes[8] == []
        await service.shutdown()


class TestWhileHosting:
    async def test_the_heartbeat_is_sent_every_thirty_seconds(self, world):
        service = world.build()
        await service.start_host_session(ROM_ID)
        sent = lambda: sum(1 for c in world.romm.call_log if c[0] == "send_activity_heartbeat")  # noqa: E731
        assert sent() == 1
        await _until(lambda: world.sleeper.parked(30) == 1)
        world.sleeper.release(30)
        await _until(lambda: sent() == 2)
        await _until(lambda: world.sleeper.parked(30) == 1)
        await service.shutdown()

    async def test_a_failing_heartbeat_does_not_stop_the_session(self, world):
        service = world.build()
        await service.start_host_session(ROM_ID)
        world.romm.send_activity_heartbeat_side_effect = RommConnectionError("blip")
        await _until(lambda: world.sleeper.parked(30) == 1)
        world.sleeper.release(30)
        await _until(lambda: world.sleeper.parked(30) == 1)
        assert service.hosting
        await service.shutdown()

    async def test_the_relay_room_is_written_into_the_note_once_found(self, world):
        world.lobby.rooms = [
            {
                "username": "alice",
                "core_name": "Snes9x",
                "host_method": 3,
                "mitm_ip": "nyc.relay.retroarch.com",
                "mitm_port": 55435,
                "mitm_session": "/wN25LdlgMoS3OAE",
                "created": "2026-01-01T00:00:20Z",
            }
        ]
        service = world.build()
        await service.start_host_session(ROM_ID)
        await _until(lambda: world.sleeper.parked(5) == 1)
        world.sleeper.release(5)
        await _until(lambda: world.lobby.calls == 1)
        note = world.romm.rom_notes[ROM_ID][0]
        await _until(lambda: (parse_note(note["content"]) or _host_note()).relay is not None)
        parsed = parse_note(note["content"])
        assert parsed is not None
        assert parsed.relay == RelayRoom(host="nyc.relay.retroarch.com", port=55435, session="/wN25LdlgMoS3OAE")
        assert parsed.addresses == ("100.101.102.103", "192.168.1.20")
        await asyncio.sleep(0.02)
        assert world.sleeper.parked(5) == 0
        await service.shutdown()

    async def test_the_search_gives_up_after_two_minutes(self, world):
        service = world.build()
        await service.start_host_session(ROM_ID)
        for attempt in range(1, 25):
            await _until(lambda: world.sleeper.parked(5) == 1)
            world.sleeper.release(5)
            await _until(lambda a=attempt: world.lobby.calls == a)
        await asyncio.sleep(0.02)
        assert world.sleeper.parked(5) == 0
        assert world.lobby.calls == 24
        await service.shutdown()

    async def test_a_lobby_that_cannot_be_read_is_tried_again(self, world):
        world.lobby.side_effect = OSError("lobby down")
        service = world.build()
        await service.start_host_session(ROM_ID)
        for attempt in (1, 2):
            await _until(lambda: world.sleeper.parked(5) == 1)
            world.sleeper.release(5)
            await _until(lambda a=attempt: world.lobby.calls == a)
        await service.shutdown()

    async def test_a_ppsspp_session_searches_no_lobby(self, world):
        world.active_core.per_rom_emulator[ROM_ID] = EmulatorInvocation.standalone(PPSSPP_COMMAND, "PPSSPP")
        service = world.build()
        await service.start_host_session(ROM_ID)
        await _until(lambda: world.sleeper.parked(30) == 1)
        assert world.sleeper.parked(5) == 0
        await service.shutdown()


class TestEndHostSession:
    async def test_ends_everything_the_session_left(self, world):
        service = world.build()
        await service.start_host_session(ROM_ID)
        await _until(lambda: world.sleeper.parked(30) == 1)

        result = await service.end_host_session()

        assert result == {"success": True, "note_removed": True}
        assert not service.hosting
        assert world.romm.list_activity() == []
        assert world.romm.rom_notes[ROM_ID] == []
        assert world.config_store.files == {}
        assert world.kv(HOST_SESSION_KEY) is None
        sent = sum(1 for c in world.romm.call_log if c[0] == "send_activity_heartbeat")
        world.sleeper.release(30)
        await asyncio.sleep(0.02)
        assert sum(1 for c in world.romm.call_log if c[0] == "send_activity_heartbeat") == sent

    async def test_ending_twice_is_harmless(self, world):
        service = world.build()
        await service.start_host_session(ROM_ID)
        await service.end_host_session()
        assert await service.end_host_session() == {"success": True, "note_removed": True}

    async def test_a_note_that_cannot_be_deleted_is_left_for_the_next_start(self, world):
        service = world.build()
        await service.start_host_session(ROM_ID)
        world.romm.delete_rom_note_side_effect = RommConnectionError("down")
        result = await service.end_host_session()
        assert result == {"success": True, "note_removed": False}
        assert world.kv(HOST_SESSION_KEY) is not None
        assert world.config_store.files == {}

    async def test_a_note_already_gone_is_removed(self, world):
        service = world.build()
        await service.start_host_session(ROM_ID)
        world.romm.delete_rom_note_side_effect = RommNotFoundError("Note not found or not owned by user")
        assert (await service.end_host_session())["note_removed"] is True
        assert world.kv(HOST_SESSION_KEY) is None


class TestLeftoverCleanup:
    async def test_a_note_recorded_by_a_crashed_session_is_deleted(self, world):
        note = world.romm.create_rom_note(ROM_ID, title=NOTE_TITLE, content="x", is_public=True)
        world.romm.send_activity_heartbeat(ROM_ID, "device-9")
        world.config_store.files["retroarch-host.cfg"] = "x"
        world.config_store.files["ppsspp-guest.ini"] = "x"
        with world.uow_factory() as uow:
            uow.kv_config.set(
                HOST_SESSION_KEY, json.dumps({"rom_id": ROM_ID, "note_id": note["id"], "device_id": "device-9"})
            )

        await world.build().clean_up_leftover_session()

        assert world.romm.rom_notes[ROM_ID] == []
        assert world.romm.list_activity() == []
        assert world.kv(HOST_SESSION_KEY) is None
        assert world.config_store.files == {}

    async def test_nothing_recorded_asks_romm_nothing(self, world):
        world.romm.call_log.clear()
        await world.build().clean_up_leftover_session()
        assert world.romm.call_log == []

    async def test_a_failed_delete_keeps_the_record(self, world):
        with world.uow_factory() as uow:
            uow.kv_config.set(HOST_SESSION_KEY, json.dumps({"rom_id": ROM_ID, "note_id": 5, "device_id": "d"}))
        world.romm.delete_rom_note_side_effect = RommConnectionError("down")
        await world.build().clean_up_leftover_session()
        assert world.kv(HOST_SESSION_KEY) is not None

    async def test_an_unreadable_record_is_dropped(self, world):
        with world.uow_factory() as uow:
            uow.kv_config.set(HOST_SESSION_KEY, "{not json")
        await world.build().clean_up_leftover_session()
        assert world.kv(HOST_SESSION_KEY) is None

    async def test_the_cleanup_runs_in_the_background(self, world):
        with world.uow_factory() as uow:
            uow.kv_config.set(HOST_SESSION_KEY, json.dumps({"rom_id": ROM_ID, "note_id": 5, "device_id": "d"}))
        service = world.build()
        service.start_leftover_cleanup()
        await _until(lambda: world.kv(HOST_SESSION_KEY) is None)
        await service.shutdown()


# ── Joining ────────────────────────────────────────────────────────────────


class TestListHostedSessions:
    async def test_a_live_session_another_user_hosts(self, world):
        _seed_hosted(world)
        result = await world.build().list_hosted_sessions()
        assert result == {
            "success": True,
            "sessions": [
                {
                    "key": f"2:{ROM_ID}:2026-01-01T00:00:00+00:00",
                    "rom_id": ROM_ID,
                    "rom_name": "Game 7",
                    "app_id": APP_ID,
                    "host_user_id": 2,
                    "host_username": "bob",
                    "kind": "retroarch",
                    "started_at": "2026-01-01T00:00:00+00:00",
                    "state": "join",
                }
            ],
        }

    async def test_this_users_own_session_is_not_listed(self, world):
        _seed_hosted(world, user_id=1)
        assert (await world.build().list_hosted_sessions())["sessions"] == []

    async def test_a_note_whose_host_is_not_live_is_ignored(self, world):
        _seed_hosted(world, live=False)
        assert (await world.build().list_hosted_sessions())["sessions"] == []

    async def test_a_note_by_someone_other_than_the_live_player_is_ignored(self, world):
        _seed_hosted(world, user_id=3, live=False)
        world.romm.activity.append({"user_id": 2, "username": "bob", "rom_id": ROM_ID, "device_id": "d-2"})
        assert (await world.build().list_hosted_sessions())["sessions"] == []

    async def test_a_note_tender_cannot_read_is_ignored(self, world):
        _seed_hosted(world)
        world.romm.rom_notes[ROM_ID][0]["content"] = '```tender-netplay\n{"v": 2}\n```'
        assert (await world.build().list_hosted_sessions())["sessions"] == []

    async def test_a_game_not_in_this_library(self, world):
        _seed_hosted(world)
        with world.uow_factory() as uow:
            uow.roms.delete(ROM_ID)
        [item] = (await world.build().list_hosted_sessions())["sessions"]
        assert item["state"] == "not_in_library"
        assert item["app_id"] is None

    async def test_a_game_not_on_this_device(self, world):
        _seed_hosted(world)
        with world.uow_factory() as uow:
            uow.rom_installs.delete(ROM_ID)
        world.launch_path.paths.clear()
        [item] = (await world.build().list_hosted_sessions())["sessions"]
        assert item["state"] == "download"

    async def test_a_core_this_device_lacks(self, world):
        _seed_hosted(world)
        world.cores.installed.clear()
        [item] = (await world.build().list_hosted_sessions())["sessions"]
        assert item["state"] == "missing_core"

    async def test_a_core_this_device_does_not_offer(self, world):
        _seed_hosted(world, note=_host_note(core_so="bsnes_libretro"))
        [item] = (await world.build().list_hosted_sessions())["sessions"]
        assert item["state"] == "missing_core"

    async def test_an_old_server_is_unsupported(self, world):
        world.romm.set_version("4.9.0")
        result = await world.build().list_hosted_sessions()
        assert result["success"] is False and result["reason"] == "unsupported"

    async def test_an_unreadable_feed_is_a_failure(self, world):
        world.romm.list_activity_side_effect = RommConnectionError("down")
        result = await world.build().list_hosted_sessions()
        assert result["success"] is False and result["reason"] == "server_unreachable"

    async def test_a_roms_notes_that_cannot_be_read_skip_only_that_rom(self, world):
        _seed_hosted(world)
        world.romm.list_rom_notes_side_effect = RommConnectionError("down")
        assert await world.build().list_hosted_sessions() == {"success": True, "sessions": []}


class TestPrepareJoin:
    async def test_the_first_direct_address_that_answers_is_used(self, world):
        _seed_hosted(world)
        world.network.reachable = {("192.168.1.30", 55435)}
        result = await world.build().prepare_join_session(ROM_ID, 2, "2026-01-01T00:00:00+00:00")

        assert result["success"] is True
        assert result["connection"] == "direct"
        assert result["app_id"] == APP_ID
        assert result["launch_options"] == (
            'flatpak run net.retrodeck.retrodeck -e "%EMULATOR_RETROARCH% -L '
            "/var/config/retroarch/cores/snes9x_libretro.so --connect=192.168.1.30 --port=55435 "
            "--sram-mode=noload-nosave --appendconfig=/var/config/tender-netplay/retroarch-guest.cfg "
            f'%ROM%" "{ROM_PATH}"'
        )
        assert [(a, p) for a, p, _t in world.network.probes] == [("100.70.0.2", 55435), ("192.168.1.30", 55435)]
        config = world.config_store.files["retroarch-guest.cfg"].splitlines()
        assert 'netplay_password = "hostpass1234"' in config
        assert 'netplay_nickname = "alice"' in config

    async def test_the_relay_when_no_address_answers(self, world):
        _seed_hosted(world)
        result = await world.build().prepare_join_session(ROM_ID, 2, "2026-01-01T00:00:00+00:00")
        assert result["connection"] == "relay"
        assert "--connect=nyc.relay.retroarch.com --port=55435 --mitm-session=/wN25L+dlg==" in result["launch_options"]

    async def test_no_way_to_reach_the_host(self, world):
        _seed_hosted(world, note=_host_note(relay=None))
        result = await world.build().prepare_join_session(ROM_ID, 2, "2026-01-01T00:00:00+00:00")
        assert result["success"] is False and result["reason"] == "host_unreachable"
        assert world.config_store.files == {}

    async def test_a_session_whose_host_left(self, world):
        _seed_hosted(world, live=False)
        result = await world.build().prepare_join_session(ROM_ID, 2, "2026-01-01T00:00:00+00:00")
        assert result["reason"] == "session_ended"

    async def test_a_session_that_was_replaced(self, world):
        _seed_hosted(world)
        result = await world.build().prepare_join_session(ROM_ID, 2, "2026-01-01T00:05:00+00:00")
        assert result["reason"] == "session_ended"

    async def test_a_missing_core(self, world):
        _seed_hosted(world)
        world.cores.installed.clear()
        result = await world.build().prepare_join_session(ROM_ID, 2, "2026-01-01T00:00:00+00:00")
        assert result["reason"] == "missing_core"

    async def test_a_game_not_on_this_device(self, world):
        _seed_hosted(world)
        world.launch_path.paths.clear()
        result = await world.build().prepare_join_session(ROM_ID, 2, "2026-01-01T00:00:00+00:00")
        assert result["reason"] == "not_installed"

    async def test_a_ppsspp_session(self, world):
        note = _host_note(kind="ppsspp", core_so=None, port=27312, relay=None, password=None)
        _seed_hosted(world, note=note)
        world.core_info.options = [standalone_option(PPSSPP_COMMAND, "PPSSPP (Standalone)")]
        world.network.reachable = {("100.70.0.2", 27312)}
        result = await world.build().prepare_join_session(ROM_ID, 2, "2026-01-01T00:00:00+00:00")
        assert result["success"] is True
        assert "--appendconfig=/var/config/tender-netplay/ppsspp-guest.ini %ROM%" in result["launch_options"]
        config = world.config_store.files["ppsspp-guest.ini"].splitlines()
        assert "EnableAdhocServer = False" in config
        assert "proAdhocServer = 100.70.0.2" in config

    async def test_an_old_server_is_unsupported(self, world):
        world.romm.set_version("4.9.0")
        result = await world.build().prepare_join_session(ROM_ID, 2, "2026-01-01T00:00:00+00:00")
        assert result["reason"] == "unsupported"


class TestEndJoin:
    async def test_removes_the_guest_configs(self, world):
        world.config_store.files = {"retroarch-guest.cfg": "x", "ppsspp-guest.ini": "y", "retroarch-host.cfg": "z"}
        assert await world.build().end_join_session() == {"success": True}
        assert world.config_store.files == {"retroarch-host.cfg": "z"}


class TestShutdown:
    async def test_cancels_the_session_tasks(self, world):
        service = world.build()
        await service.start_host_session(ROM_ID)
        await _until(lambda: world.sleeper.parked(30) == 1)
        await service.shutdown()
        sent = sum(1 for c in world.romm.call_log if c[0] == "send_activity_heartbeat")
        world.sleeper.release(30)
        await asyncio.sleep(0.02)
        assert sum(1 for c in world.romm.call_log if c[0] == "send_activity_heartbeat") == sent
