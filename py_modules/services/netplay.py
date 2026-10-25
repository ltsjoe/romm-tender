"""NetplayService — hosting a game for multiplayer through RomM, and joining one.

Two Tenders on the same RomM server have no channel of their own, so RomM is
the channel. A host keeps RomM's activity heartbeat live for the ROM and a
public Tender note on it that carries what a guest needs; a guest reads the
activity feed and trusts a note only while its writer's activity entry is live,
so a host that crashed stops being offered once its entry expires.

This service owns the session's lifecycle on both sides and nothing about the
Steam shortcut: it answers the one-off launch command, and the frontend writes
it, launches, and puts the normal command back when the game exits. The
one-off command is never recorded as the shortcut's applied launch options.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import json
from dataclasses import dataclass, field
from datetime import timedelta
from typing import TYPE_CHECKING, Any

from domain.iso_time import parse_iso
from domain.netplay import (
    DETERMINISTIC_SAVESTATES,
    PPSSPP_ADHOC_SERVER_PORT,
    REASON_CORE_NOT_SUPPORTED,
    REASON_EMULATOR_NOT_SUPPORTED,
    RETROARCH_NETPLAY_PORT,
    Refusal,
    SessionEmulator,
    activity_supported,
    config_file_name,
    find_relay_room,
    guest_emulator,
    host_emulator,
    netplay_launch_options,
    netplay_nickname,
    order_host_addresses,
    ppsspp_args,
    ppsspp_config,
    retroarch_guest_args,
    retroarch_guest_config,
    retroarch_host_args,
    retroarch_host_config,
    session_password,
)
from domain.netplay_note import KIND_PPSSPP, KIND_RETROARCH, NOTE_TITLE, SessionNote, parse_note, render_note
from lib.errors import RommForbiddenError, RommNotFoundError, classify_error
from lib.list_result import ErrorCode

if TYPE_CHECKING:
    import logging
    from collections.abc import Callable

    from domain.shortcut_data import EmulatorInvocation
    from services.protocols import (
        ActiveCoreReader,
        Clock,
        CoreInfoProvider,
        DebugLogger,
        DeviceRegistrationFn,
        LobbyRoomsFn,
        LocalNetworkReader,
        NetplayConfigStore,
        RetroArchCoreInfoReader,
        RomLaunchPathReader,
        RommNetplayApi,
        RomSaveDirFn,
        Sleeper,
        SystemResolver,
        UnitOfWorkFactory,
        UuidGen,
    )

# ``kv_config`` row listing the notes of the sessions this device hosts or
# hosted and could not yet clean up after, so a start after a crash can delete
# the note the crashed session left behind.
HOST_SESSION_KEY = "netplay_host_session"

# RomM drops an activity entry 90 seconds after its last heartbeat
# (``ActivityHandler.ACTIVITY_TTL``, RomM 5.3.0), so a heartbeat every 30 seconds
# survives two lost ones.
HEARTBEAT_SECONDS = 30.0
# libretro's lobby is asked every 5 seconds for up to two minutes for the room
# RetroArch announces once it reaches the relay.
LOBBY_INTERVAL_SECONDS = 5.0
LOBBY_ATTEMPTS = 24
# The lobby stamps a room with its own clock; a room this much older than the
# session's start is still taken as the session's.
_LOBBY_CLOCK_SLACK = timedelta(seconds=30)
PROBE_TIMEOUT_SECONDS = 1.5

REASON_SESSION_ACTIVE = "session_active"
REASON_REAUTH_REQUIRED = "reauth_required"
REASON_SESSION_ENDED = "session_ended"
REASON_MISSING_CORE = "missing_core"
REASON_HOST_UNREACHABLE = "host_unreachable"
REASON_NO_NETWORK = "no_network"
REASON_SAVE_FOLDER_UNKNOWN = "save_folder_unknown"
REASON_NOT_INSTALLED = "not_installed"
REASON_CONFIG_WRITE_FAILED = "config_write_failed"

STATE_JOIN = "join"
STATE_DOWNLOAD = "download"
STATE_MISSING_CORE = "missing_core"
STATE_NOT_IN_LIBRARY = "not_in_library"

_MESSAGES = {
    ErrorCode.UNSUPPORTED.value: "Multiplayer needs RomM 5.0.0 or newer on the server.",
    REASON_NOT_INSTALLED: "Download this game to this device first.",
    REASON_EMULATOR_NOT_SUPPORTED: "This game's emulator has no multiplayer Tender can start.",
    REASON_CORE_NOT_SUPPORTED: "This game's emulator core does not support multiplayer.",
    REASON_SESSION_ACTIVE: "A multiplayer session is already running. End it before starting another.",
    REASON_REAUTH_REQUIRED: (
        "Your RomM sign-in does not allow multiplayer. Sign in again with your username and password, "
        "which gives Tender every permission it asks for."
    ),
    REASON_SESSION_ENDED: "This multiplayer session has ended.",
    REASON_MISSING_CORE: "RetroDECK on this device does not have the emulator core this session runs on.",
    REASON_HOST_UNREACHABLE: "Tender could not reach the host of this session.",
    REASON_NO_NETWORK: "This device has no network address another player could reach.",
    REASON_SAVE_FOLDER_UNKNOWN: "Tender could not find where this game keeps its saves, so it cannot host it.",
    REASON_CONFIG_WRITE_FAILED: "Tender could not write the multiplayer settings for the emulator.",
}

_SESSION_PORT = {KIND_RETROARCH: RETROARCH_NETPLAY_PORT, KIND_PPSSPP: PPSSPP_ADHOC_SERVER_PORT}


@dataclass(frozen=True)
class NetplayServiceConfig:
    """Frozen wiring bundle handed to ``NetplayService.__init__``.

    The RomM activity and notes surface, the settings dict (for the signed-in
    user's id), the UoW factory (the local library and the crash record in
    ``kv_config``), the launch-resolution seams every launch bakes from
    (``active_core``, ``core_info``, ``resolve_system``, ``launch_path``), the
    RetroArch core reader, the device-registration and save-folder seams save
    sync answers, the session config store, this machine's network, libretro's
    lobby, and the loop, loggers, clock, sleeper and UUID source.
    """

    romm_api: RommNetplayApi
    settings: dict[str, Any]
    uow_factory: UnitOfWorkFactory
    active_core: ActiveCoreReader
    core_info: CoreInfoProvider
    resolve_system: SystemResolver
    retroarch_cores: RetroArchCoreInfoReader
    launch_path: RomLaunchPathReader
    register_device: DeviceRegistrationFn
    save_dir_for_rom: RomSaveDirFn
    config_store: NetplayConfigStore
    network: LocalNetworkReader
    lobby_rooms: LobbyRoomsFn
    loop: asyncio.AbstractEventLoop
    logger: logging.Logger
    log_debug: DebugLogger
    clock: Clock
    sleeper: Sleeper
    uuid_gen: UuidGen


class _Refused(Exception):
    """A request this service declines, carrying the canonical ``reason`` / ``message``."""

    def __init__(self, reason: str, message: str | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.message = message or _MESSAGES.get(reason, reason)

    def answer(self) -> dict[str, Any]:
        return {"success": False, "reason": self.reason, "message": self.message}


@dataclass(frozen=True)
class _LocalRom:
    """The local library's view of one ROM: its Steam shortcut, its system, its launch path."""

    rom_id: int
    name: str
    app_id: int | None
    platform_slug: str
    system: str
    path: str | None


@dataclass
class _HostSession:
    """The session this device hosts, while it runs."""

    rom_id: int
    kind: str
    note_id: int
    device_id: str
    config_name: str
    core_name: str | None
    note: SessionNote
    record: dict[str, Any]
    tasks: list[asyncio.Task[None]] = field(default_factory=list)


def _romm_refusal(exc: Exception) -> _Refused:
    """Turn a RomM failure into a refusal; a 403 asks the user to sign in again (#1 D23)."""
    if isinstance(exc, RommForbiddenError):
        return _Refused(REASON_REAUTH_REQUIRED)
    reason, message = classify_error(exc)
    return _Refused(reason, message)


class NetplayService:
    """Hosts and joins multiplayer sessions found through RomM."""

    def __init__(self, *, config: NetplayServiceConfig) -> None:
        self._romm_api = config.romm_api
        self._settings = config.settings
        self._uow_factory = config.uow_factory
        self._active_core = config.active_core
        self._core_info = config.core_info
        self._resolve_system = config.resolve_system
        self._cores = config.retroarch_cores
        self._launch_path = config.launch_path
        self._register_device = config.register_device
        self._save_dir_for_rom = config.save_dir_for_rom
        self._config_store = config.config_store
        self._network = config.network
        self._lobby_rooms = config.lobby_rooms
        self._loop = config.loop
        self._logger = config.logger
        self._log_debug = config.log_debug
        self._clock = config.clock
        self._sleeper = config.sleeper
        self._uuid_gen = config.uuid_gen
        self._session: _HostSession | None = None
        # One session at a time, and its start, its end and the crash cleanup
        # never interleave: each reads and writes the same kv_config record.
        self._lock = asyncio.Lock()
        # Strong refs to every task this service starts; ``shutdown`` cancels them.
        self._tasks: set[asyncio.Task[None]] = set()

    @property
    def hosting(self) -> bool:
        """Whether this device hosts a session right now."""
        return self._session is not None

    async def _io(self, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        return await self._loop.run_in_executor(None, functools.partial(fn, *args, **kwargs))

    def _spawn(self, coro: Any) -> asyncio.Task[None]:
        task = self._loop.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    async def shutdown(self) -> None:
        """Cancel every task this service started and await them.

        The session's note is left for the next start's cleanup: an unload is no
        time for a network round trip, and the activity entry expires without one.
        """
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._session = None

    # ── Shared reads ────────────────────────────────────────────────────

    async def _require_activity_feed(self) -> None:
        """Refuse unless the RomM server has the activity feed (5.0.0+), probing its version once."""
        version = self._romm_api.get_version()
        if not version:
            try:
                heartbeat = await self._io(self._romm_api.heartbeat)
            except Exception as exc:
                raise _romm_refusal(exc) from exc
            system = heartbeat.get("SYSTEM") if isinstance(heartbeat, dict) else None
            raw = system.get("VERSION") if isinstance(system, dict) else None
            if isinstance(raw, str) and raw:
                self._romm_api.set_version(raw)
                version = raw
        if not activity_supported(version):
            raise _Refused(ErrorCode.UNSUPPORTED.value)

    def _local_rom_io(self, rom_id: int) -> _LocalRom | None:
        """Read one ROM's local state; the UoW is closed before the launch seams are asked."""
        with self._uow_factory() as uow:
            rom = uow.roms.get(rom_id)
        if rom is None:
            return None
        return _LocalRom(
            rom_id=rom_id,
            name=rom.name,
            app_id=rom.shortcut_app_id,
            platform_slug=rom.platform_slug,
            system=self._resolve_system(rom.platform_slug),
            path=self._launch_path.launch_path_for_rom(rom_id),
        )

    def _options_io(self, system: str) -> list[Any]:
        return list(self._core_info.get_emulator_options(system).get("options") or [])

    def _is_deterministic(self, core_so: str) -> bool:
        return self._cores.savestate_features(core_so) == DETERMINISTIC_SAVESTATES

    # ── Hosting ─────────────────────────────────────────────────────────

    def _session_emulator_io(self, rom_id: int) -> tuple[_LocalRom, SessionEmulator]:
        """Resolve the installed ROM and the emulator it hosts on, or refuse."""
        local = self._local_rom_io(rom_id)
        if local is None or local.path is None or local.app_id is None:
            raise _Refused(REASON_NOT_INSTALLED)
        invocation = self._active_core.active_emulator_for_rom(rom_id)
        verdict = host_emulator(local.system, invocation, self._options_io(local.system), self._is_deterministic)
        if isinstance(verdict, Refusal):
            raise _Refused(verdict.reason)
        if (
            verdict.switched
            and verdict.invocation.core_so
            and not self._cores.core_installed(verdict.invocation.core_so)
        ):
            raise _Refused(REASON_MISSING_CORE)
        return local, verdict

    async def get_capability(self, rom_id: int) -> dict[str, Any]:
        """Whether this ROM can be hosted, and on which kind of netplay.

        Returns ``{"success": True, "available", "kind", "reason", "message"}``;
        an unavailable ROM carries the ``reason`` slug and a sentence saying why.
        """
        try:
            await self._require_activity_feed()
            _local, emulator = await self._io(self._session_emulator_io, int(rom_id))
        except _Refused as refused:
            return {
                "success": True,
                "available": False,
                "kind": None,
                "reason": refused.reason,
                "message": refused.message,
            }
        return {"success": True, "available": True, "kind": emulator.kind, "reason": None, "message": None}

    def _host_config_io(
        self, local: _LocalRom, emulator: SessionEmulator, nickname: str, addresses: tuple[str, ...]
    ) -> tuple[str, str, str | None]:
        """Write the host's appended config; answer ``(config name, launch options, password)``."""
        password: str | None = None
        name = config_file_name(emulator.kind, host=True)
        try:
            if emulator.kind == KIND_RETROARCH:
                save_dir = None
                if emulator.switched:
                    save_dir = self._save_dir_for_rom(local.rom_id)
                    if save_dir is None:
                        raise _Refused(REASON_SAVE_FOLDER_UNKNOWN)
                password = session_password(self._uuid_gen.uuid4())
                text = retroarch_host_config(password=password, nickname=nickname, save_dir=save_dir)
            else:
                text = ppsspp_config(server_address=addresses[0], host=True)
            sandbox_path = self._config_store.write_config(name, text)
            args = retroarch_host_args(sandbox_path) if emulator.kind == KIND_RETROARCH else ppsspp_args(sandbox_path)
            return name, netplay_launch_options(emulator.invocation, args, local.path or ""), password
        except (OSError, ValueError) as exc:
            self._logger.warning("netplay: preparing %s failed: %s", name, exc)
            self._config_store.remove_config(name)
            raise _Refused(REASON_CONFIG_WRITE_FAILED) from exc

    def _publish_note_io(self, rom_id: int, user_id: object, note: SessionNote) -> int:
        """Replace this user's Tender note on the ROM with *note*; answer the new note's id.

        RomM keeps one note per ROM, user and title, so an earlier one of this
        user's — a crash's — has to go before the new one can be created.
        """
        for raw in self._romm_api.list_rom_notes(rom_id):
            if raw.get("user_id") == user_id and raw.get("title") == NOTE_TITLE and isinstance(raw.get("id"), int):
                with contextlib.suppress(RommNotFoundError):
                    self._romm_api.delete_rom_note(rom_id, raw["id"])
        created = self._romm_api.create_rom_note(rom_id, title=NOTE_TITLE, content=render_note(note), is_public=True)
        return int(created["id"])

    def _read_records_io(self) -> list[dict[str, Any]]:
        """The sessions this device recorded as hosting and has not yet cleaned up after.

        A record that does not name a ROM and a note names nothing, and is dropped.
        """
        with self._uow_factory() as uow:
            raw = uow.kv_config.get(HOST_SESSION_KEY)
        try:
            decoded: Any = json.loads(raw) if raw else []
        except ValueError:
            return []
        entries: list[Any] = decoded if isinstance(decoded, list) else [decoded]
        records: list[dict[str, Any]] = []
        for entry in entries:
            if isinstance(entry, dict):
                record: dict[str, Any] = entry
                if isinstance(record.get("rom_id"), int) and isinstance(record.get("note_id"), int):
                    records.append(record)
        return records

    def _write_records_io(self, records: list[dict[str, Any]]) -> None:
        with self._uow_factory() as uow:
            if records:
                uow.kv_config.set(HOST_SESSION_KEY, json.dumps(records))
            else:
                uow.kv_config.delete(HOST_SESSION_KEY)

    def _add_record_io(self, record: dict[str, Any]) -> None:
        self._write_records_io([*self._read_records_io(), record])

    def _drop_record_io(self, record: dict[str, Any]) -> None:
        self._write_records_io([r for r in self._read_records_io() if r != record])

    async def start_host_session(self, rom_id: int) -> dict[str, Any]:
        """Start hosting *rom_id*; answer the one-off launch command for the frontend to run.

        Registers this device with RomM when none is registered — without turning
        save sync on — sends the first activity heartbeat, publishes the session
        note, records it for crash cleanup, then keeps the heartbeat going (and,
        for RetroArch, looks for the relayed room) in the background. Returns
        ``{"success": True, "rom_id", "app_id", "kind", "launch_options"}`` or the
        canonical failure shape. Nothing is left behind by a start that fails.
        """
        rom_id = int(rom_id)
        async with self._lock:
            if self._session is not None:
                return _Refused(REASON_SESSION_ACTIVE).answer()
            await self._clean_up_records()
            try:
                return await self._start_host_session(rom_id)
            except _Refused as refused:
                return refused.answer()

    async def _start_host_session(self, rom_id: int) -> dict[str, Any]:
        await self._require_activity_feed()
        local, emulator = await self._io(self._session_emulator_io, rom_id)
        registration = await self._register_device()
        if not registration.get("success") or not registration.get("device_id"):
            raise _Refused(
                str(registration.get("reason") or ErrorCode.UNKNOWN.value),
                str(registration.get("message") or "Tender could not register this device with RomM."),
            )
        device_id = str(registration["device_id"])
        try:
            user = await self._io(self._romm_api.get_current_user)
        except Exception as exc:
            raise _romm_refusal(exc) from exc
        nickname = netplay_nickname(user.get("username") if isinstance(user, dict) else None)
        user_id = user.get("id") if isinstance(user, dict) else None
        addresses = order_host_addresses(await self._io(self._network.ipv4_addresses))
        if emulator.kind == KIND_PPSSPP and not addresses:
            raise _Refused(REASON_NO_NETWORK)

        config_name, launch_options, password = await self._io(
            self._host_config_io, local, emulator, nickname, addresses
        )
        note = SessionNote(
            kind=emulator.kind,
            core_so=emulator.invocation.core_so if emulator.kind == KIND_RETROARCH else None,
            addresses=addresses,
            port=_SESSION_PORT[emulator.kind],
            relay=None,
            password=password,
            nickname=nickname,
            started_at=self._clock.now().isoformat(),
        )
        try:
            await self._io(self._romm_api.send_activity_heartbeat, rom_id, device_id)
        except Exception as exc:
            await self._io(self._config_store.remove_config, config_name)
            raise _romm_refusal(exc) from exc
        try:
            note_id = await self._io(self._publish_note_io, rom_id, user_id, note)
        except Exception as exc:
            await self._clear_heartbeat(device_id)
            await self._io(self._config_store.remove_config, config_name)
            raise _romm_refusal(exc) from exc
        record = {"rom_id": rom_id, "note_id": note_id, "device_id": device_id}
        await self._io(self._add_record_io, record)

        core_so = emulator.invocation.core_so
        session = _HostSession(
            rom_id=rom_id,
            kind=emulator.kind,
            note_id=note_id,
            device_id=device_id,
            config_name=config_name,
            core_name=self._cores.get_corename(core_so) if core_so else None,
            note=note,
            record=record,
        )
        self._session = session
        session.tasks.append(self._spawn(self._keep_alive(session)))
        if emulator.kind == KIND_RETROARCH:
            session.tasks.append(self._spawn(self._find_relay(session)))
        self._log_debug(f"netplay: hosting rom {rom_id} on {emulator.kind} as note {note_id}")
        return {
            "success": True,
            "rom_id": rom_id,
            "app_id": local.app_id,
            "kind": emulator.kind,
            "launch_options": launch_options,
        }

    async def _keep_alive(self, session: _HostSession) -> None:
        """Send the activity heartbeat every 30 seconds until the session ends."""
        while True:
            await self._sleeper.sleep(HEARTBEAT_SECONDS)
            try:
                await self._io(self._romm_api.send_activity_heartbeat, session.rom_id, session.device_id)
            except Exception as exc:
                self._log_debug(f"netplay: heartbeat for rom {session.rom_id} failed: {exc}")

    async def _find_relay(self, session: _HostSession) -> None:
        """Find the host's relayed room in libretro's lobby and add it to the note."""
        started = parse_iso(session.note.started_at)
        not_before = started - _LOBBY_CLOCK_SLACK if started is not None else None
        for _attempt in range(LOBBY_ATTEMPTS):
            await self._sleeper.sleep(LOBBY_INTERVAL_SECONDS)
            try:
                rooms = await self._io(self._lobby_rooms)
            except Exception as exc:
                self._log_debug(f"netplay: reading libretro's lobby failed: {exc}")
                continue
            room = find_relay_room(
                rooms, nickname=session.note.nickname, core_name=session.core_name, not_before=not_before
            )
            if room is None:
                continue
            note = session.note.with_relay(room)
            try:
                await self._io(
                    functools.partial(self._romm_api.update_rom_note, content=render_note(note)),
                    session.rom_id,
                    session.note_id,
                )
            except Exception as exc:
                self._log_debug(f"netplay: adding the relay to note {session.note_id} failed: {exc}")
                continue
            session.note = note
            self._log_debug(f"netplay: relay room found for rom {session.rom_id}")
            return
        self._log_debug(f"netplay: no relayed room for rom {session.rom_id}; guests connect directly only")

    async def _clear_heartbeat(self, device_id: str) -> None:
        try:
            await self._io(self._romm_api.clear_activity_heartbeat, device_id)
        except Exception as exc:
            self._log_debug(f"netplay: clearing the activity heartbeat failed: {exc}")

    async def _delete_note(self, rom_id: int, note_id: int) -> bool:
        """Delete the session note; ``True`` once it is gone, an entity 404 included."""
        try:
            await self._io(self._romm_api.delete_rom_note, rom_id, note_id)
        except RommNotFoundError:
            return True
        except Exception as exc:
            self._log_debug(f"netplay: deleting note {note_id} failed, left for the next start: {exc}")
            return False
        return True

    async def _remove_configs(self, *names: str) -> None:
        for name in names:
            try:
                await self._io(self._config_store.remove_config, name)
            except OSError as exc:
                self._log_debug(f"netplay: removing {name} failed: {exc}")

    async def end_host_session(self) -> dict[str, Any]:
        """End the session this device hosts; safe to call when none runs.

        Stops the heartbeat, clears the activity entry, deletes the note and
        the appended config. Returns ``{"success": True, "note_removed": bool}``;
        a note RomM could not delete now stays recorded for the next start.
        """
        async with self._lock:
            session = self._session
            if session is None:
                return {"success": True, "note_removed": await self._clean_up_records()}
            self._session = None
            for task in session.tasks:
                task.cancel()
            await asyncio.gather(*session.tasks, return_exceptions=True)
            await self._clear_heartbeat(session.device_id)
            removed = await self._delete_note(session.rom_id, session.note_id)
            await self._remove_configs(session.config_name)
            if removed:
                await self._io(self._drop_record_io, session.record)
            return {"success": True, "note_removed": removed}

    async def _clean_up_records(self) -> bool:
        """Remove what sessions this device no longer runs left behind; ``True`` once nothing is.

        Each recorded note and its activity entry, and every appended config. A
        note RomM could not delete now stays recorded, for the next start to try.
        """
        await self._remove_configs(*(config_file_name(k, host=h) for k in _SESSION_PORT for h in (True, False)))
        records = await self._io(self._read_records_io)
        remaining: list[dict[str, Any]] = []
        for record in records:
            device_id = record.get("device_id")
            if isinstance(device_id, str) and device_id:
                await self._clear_heartbeat(device_id)
            if not await self._delete_note(record["rom_id"], record["note_id"]):
                remaining.append(record)
        await self._io(self._write_records_io, remaining)
        return not remaining

    async def clean_up_leftover_session(self) -> None:
        """Delete the note a crashed session left, at start-up; nothing while a session runs."""
        async with self._lock:
            if self._session is None:
                await self._clean_up_records()

    def start_leftover_cleanup(self) -> None:
        """Run :meth:`clean_up_leftover_session` in the background, so start-up never waits on RomM."""
        self._spawn(self.clean_up_leftover_session())

    # ── Joining ─────────────────────────────────────────────────────────

    def _session_note_io(self, rom_id: int, host_user_id: int, started_at: str | None) -> SessionNote | None:
        """The newest readable Tender note *host_user_id* wrote on *rom_id*, or ``None``."""
        candidates: list[tuple[str, SessionNote]] = []
        for raw in self._romm_api.list_rom_notes(rom_id):
            if raw.get("user_id") != host_user_id or raw.get("title") != NOTE_TITLE:
                continue
            note = parse_note(raw.get("content"))
            if note is None or (started_at is not None and note.started_at != started_at):
                continue
            candidates.append((str(raw.get("updated_at") or ""), note))
        if not candidates:
            return None
        return max(candidates, key=lambda candidate: candidate[0])[1]

    def _guest_invocation_io(self, local: _LocalRom, note: SessionNote) -> EmulatorInvocation | None:
        """This device's own invocation for joining *note*, ``None`` where it lacks the emulator or core."""
        invocation = guest_emulator(note, self._options_io(local.system))
        if invocation is None:
            return None
        if note.kind == KIND_RETROARCH and not (note.core_so and self._cores.core_installed(note.core_so)):
            return None
        return invocation

    def _guest_state_io(self, rom_id: int, note: SessionNote) -> tuple[str, _LocalRom | None]:
        local = self._local_rom_io(rom_id)
        if local is None or local.app_id is None:
            return STATE_NOT_IN_LIBRARY, local
        if self._guest_invocation_io(local, note) is None:
            return STATE_MISSING_CORE, local
        if local.path is None:
            return STATE_DOWNLOAD, local
        return STATE_JOIN, local

    async def _own_user_id(self) -> object:
        user_id = self._settings.get("romm_user_id")
        if user_id is not None:
            return user_id
        try:
            user = await self._io(self._romm_api.get_current_user)
        except Exception as exc:
            self._log_debug(f"netplay: reading this user's id failed: {exc}")
            return None
        return user.get("id") if isinstance(user, dict) else None

    async def _live_entries(self) -> list[dict[str, Any]]:
        try:
            entries = await self._io(self._romm_api.list_activity)
        except Exception as exc:
            raise _romm_refusal(exc) from exc
        return [e for e in entries if isinstance(e, dict)]

    async def list_hosted_sessions(self) -> dict[str, Any]:
        """Every live session another user hosts, with what this device can do about it.

        A session is a live activity entry by another user on a ROM that carries a
        Tender note by that same user. Returns ``{"success": True, "sessions":
        [...]}``; each item carries ``key`` (host user id, ROM id and start),
        ``rom_id``, ``rom_name``, ``app_id``, ``host_user_id``,
        ``host_username``, ``kind``, ``started_at`` and ``state`` — ``join``,
        ``download``, ``missing_core`` or ``not_in_library``.
        """
        try:
            await self._require_activity_feed()
            entries = await self._live_entries()
        except _Refused as refused:
            return refused.answer()
        own_user_id = await self._own_user_id()
        live: dict[tuple[int, int], dict[str, Any]] = {}
        for entry in entries:
            user_id, rom_id = entry.get("user_id"), entry.get("rom_id")
            if isinstance(user_id, int) and isinstance(rom_id, int) and user_id != own_user_id:
                live.setdefault((user_id, rom_id), entry)

        sessions: list[dict[str, Any]] = []
        for (user_id, rom_id), entry in live.items():
            try:
                note = await self._io(self._session_note_io, rom_id, user_id, None)
            except Exception as exc:
                self._log_debug(f"netplay: reading the notes on rom {rom_id} failed: {exc}")
                continue
            if note is None:
                continue
            state, local = await self._io(self._guest_state_io, rom_id, note)
            sessions.append(
                {
                    "key": f"{user_id}:{rom_id}:{note.started_at}",
                    "rom_id": rom_id,
                    "rom_name": entry.get("rom_name") or (local.name if local is not None else ""),
                    "app_id": local.app_id if local is not None else None,
                    "host_user_id": user_id,
                    "host_username": entry.get("username") or note.nickname,
                    "kind": note.kind,
                    "started_at": note.started_at,
                    "state": state,
                }
            )
        return {"success": True, "sessions": sessions}

    def _reach_host_io(self, note: SessionNote) -> str | None:
        """The first of the host's addresses that answers on the session's port, in the note's order."""
        for address in note.addresses:
            if self._network.tcp_reachable(address, note.port, PROBE_TIMEOUT_SECONDS):
                return address
        return None

    def _guest_config_io(self, note: SessionNote, nickname: str, address: str | None) -> tuple[str, str]:
        """Write the guest's appended config; answer ``(launch args, connection)``."""
        name = config_file_name(note.kind, host=False)
        try:
            if note.kind == KIND_RETROARCH:
                text = retroarch_guest_config(password=note.password or "", nickname=nickname)
            else:
                text = ppsspp_config(server_address=address or "", host=False)
            sandbox_path = self._config_store.write_config(name, text)
        except (OSError, ValueError) as exc:
            self._logger.warning("netplay: writing %s failed: %s", name, exc)
            raise _Refused(REASON_CONFIG_WRITE_FAILED) from exc
        if note.kind == KIND_PPSSPP:
            return ppsspp_args(sandbox_path), "direct"
        if address is not None:
            return retroarch_guest_args(address, note.port, None, sandbox_path), "direct"
        relay = note.relay
        if relay is None:
            raise _Refused(REASON_HOST_UNREACHABLE)
        return retroarch_guest_args(relay.host, relay.port, relay.session, sandbox_path), "relay"

    async def prepare_join_session(self, rom_id: int, host_user_id: int, started_at: str) -> dict[str, Any]:
        """Prepare this device to join the session *host_user_id* started on *rom_id* at *started_at*.

        Re-reads the note and requires the host's activity entry to still be
        live, tries the host's direct addresses in order, else the relay
        (RetroArch only), writes the guest's appended config, and answers
        ``{"success": True, "rom_id", "app_id", "kind", "connection",
        "launch_options"}`` or the canonical failure shape.
        """
        try:
            return await self._prepare_join(int(rom_id), int(host_user_id), str(started_at))
        except _Refused as refused:
            return refused.answer()

    async def _prepare_join(self, rom_id: int, host_user_id: int, started_at: str) -> dict[str, Any]:
        await self._require_activity_feed()
        entries = await self._live_entries()
        if not any(e.get("user_id") == host_user_id and e.get("rom_id") == rom_id for e in entries):
            raise _Refused(REASON_SESSION_ENDED)
        try:
            note = await self._io(self._session_note_io, rom_id, host_user_id, started_at)
        except Exception as exc:
            raise _romm_refusal(exc) from exc
        if note is None:
            raise _Refused(REASON_SESSION_ENDED)
        local = await self._io(self._local_rom_io, rom_id)
        if local is None or local.path is None or local.app_id is None:
            raise _Refused(REASON_NOT_INSTALLED)
        invocation = await self._io(self._guest_invocation_io, local, note)
        if invocation is None:
            raise _Refused(REASON_MISSING_CORE)
        address = await self._io(self._reach_host_io, note)
        if address is None and (note.kind == KIND_PPSSPP or note.relay is None):
            raise _Refused(REASON_HOST_UNREACHABLE)
        try:
            user = await self._io(self._romm_api.get_current_user)
        except Exception as exc:
            self._log_debug(f"netplay: reading this user's name failed, joining as the fallback nickname: {exc}")
            user = None
        nickname = netplay_nickname(user.get("username") if isinstance(user, dict) else None)
        args, connection = await self._io(self._guest_config_io, note, nickname, address)
        self._log_debug(f"netplay: joining rom {rom_id} hosted by user {host_user_id} over {connection}")
        return {
            "success": True,
            "rom_id": rom_id,
            "app_id": local.app_id,
            "kind": note.kind,
            "connection": connection,
            "launch_options": netplay_launch_options(invocation, args, local.path),
        }

    async def end_join_session(self) -> dict[str, Any]:
        """Remove the guest's appended config once the joined game exits. Always ``{"success": True}``."""
        await self._remove_configs(*(config_file_name(kind, host=False) for kind in _SESSION_PORT))
        return {"success": True}
