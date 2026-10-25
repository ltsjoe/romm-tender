"""Which emulator a multiplayer session runs, and what it is launched with.

The pure half of hosting and joining: deciding whether a launch can host at all
and on which emulator, turning a session into the flags and the appended config
the emulator is started with, ordering the host's addresses, and finding the
host's own room in libretro's lobby. The session note itself, and the character
sets that keep a guest's command shell-inert, live in :mod:`domain.netplay_note`.

**Everything that reaches a command line is checked here again**, even where it
was read through a validated note: the ``-e`` string is run through ``eval`` by
RetroDECK's ``run_game.sh``. A value that could carry a quote, a space or a
shell metacharacter (a nickname, a password) never goes on the command line at
all; it goes only into the appended config file, which no shell reads.
"""

from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from domain.iso_time import parse_iso
from domain.netplay_note import (
    KIND_PPSSPP,
    KIND_RETROARCH,
    RelayRoom,
    SessionNote,
    is_valid_address,
    is_valid_password,
    is_valid_port,
    is_valid_relay_session,
)
from domain.shortcut_data import EmulatorInvocation, build_launch_options, invocation_with_launch_args
from domain.version import meets_min_version

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Sequence
    from datetime import datetime

    from domain.emulator_commands import EmulatorOption

# RetroArch's default netplay port, which the host's appended config pins.
RETROARCH_NETPLAY_PORT = 55435
# PPSSPP's built-in ad hoc server listens on this TCP port (``SERVER_PORT`` in
# PPSSPP's ``Core/HLE/proAdhocServer``); a guest reaches the host there.
PPSSPP_ADHOC_SERVER_PORT = 27312

# The activity feed a guest finds a hosted game through arrived in RomM 5.0.0.
ACTIVITY_MIN_VERSION = (5, 0, 0)

# RetroArch refuses netplay for a core whose ``.info`` does not carry this.
DETERMINISTIC_SAVESTATES = "deterministic"

PS1_SYSTEM = "psx"
PCSX_REARMED_CORE = "pcsx_rearmed_libretro"

REASON_EMULATOR_NOT_SUPPORTED = "emulator_not_supported"
REASON_CORE_NOT_SUPPORTED = "core_not_supported"

# RetroArch keeps a nickname in a 32-byte buffer (``NETPLAY_NICK_LEN``).
_NICKNAME_MAX = 31
_FALLBACK_NICKNAME = "Tender"
_PASSWORD_LENGTH = 16

_PPSSPP_COMMAND_RE = re.compile(r"^%EMULATOR_PPSSPP%(\s|$)")
# What may sit in one argument on the command line: no quote, space, ``;``,
# ``|``, ``&``, ``$``, backtick or newline can ever match.
_SAFE_ARG_RE = re.compile(r"^[A-Za-z0-9._:/=+-]+$")
# What may sit inside a double-quoted RetroArch config value: RetroArch's config
# reader ends a quoted value at the next ``"`` and has no escape for one.
_CONFIG_UNSAFE_RE = re.compile(r'["\\\x00-\x1f\x7f]')

_TAILSCALE = ipaddress.IPv4Network("100.64.0.0/10")
_PRIVATE_LAN = (
    ipaddress.IPv4Network("10.0.0.0/8"),
    ipaddress.IPv4Network("172.16.0.0/12"),
    ipaddress.IPv4Network("192.168.0.0/16"),
)

# libretro's lobby states a relayed host with this ``host_method``.
_LOBBY_HOST_METHOD_RELAYED = 3


@dataclass(frozen=True)
class SessionEmulator:
    """The emulator a session is launched on.

    ``invocation`` is what the session launches with. ``switched`` is set where
    the session runs on another core than the game's own — a PS1 game moved to
    PCSX ReARMed — so the session can be pointed at the game's usual save folder.
    """

    kind: str
    invocation: EmulatorInvocation
    switched: bool = False


@dataclass(frozen=True)
class Refusal:
    """Why a launch cannot host, as a ``reason`` slug."""

    reason: str


def activity_supported(version: str | None) -> bool:
    """Whether a RomM server of *version* has the activity feed sessions are found through.

    A ``development`` build is taken as current, the same reading the
    connection check gives it.
    """
    return version == "development" or meets_min_version(version, ACTIVITY_MIN_VERSION)


def _is_ppsspp(invocation: EmulatorInvocation) -> bool:
    return (
        invocation.kind == "standalone"
        and invocation.command is not None
        and _PPSSPP_COMMAND_RE.match(invocation.command) is not None
    )


def _bakeable_libretro(options: Sequence[EmulatorOption], core_so: str) -> EmulatorOption | None:
    for option in options:
        if option.kind == "libretro" and option.core_so == core_so and option.status == "bakeable":
            return option
    return None


def host_emulator(
    system: str,
    invocation: EmulatorInvocation | None,
    options: Sequence[EmulatorOption],
    is_deterministic: Callable[[str], bool],
) -> SessionEmulator | Refusal:
    """Decide the emulator a launch of *invocation* on *system* hosts on.

    A libretro core whose ``.info`` marks deterministic savestates hosts on
    RetroArch; PPSSPP standalone hosts its ad hoc mode. A PS1 game on a libretro
    core that is not deterministic is moved to PCSX ReARMed for the session where
    the system offers it — the game's own core is never changed. *options* are
    the system's emulator options; *is_deterministic* answers for a core name.
    """
    if invocation is None:
        return Refusal(REASON_EMULATOR_NOT_SUPPORTED)
    if _is_ppsspp(invocation):
        return SessionEmulator(kind=KIND_PPSSPP, invocation=invocation)
    if invocation.kind != "libretro" or not invocation.core_so:
        return Refusal(REASON_EMULATOR_NOT_SUPPORTED)
    if is_deterministic(invocation.core_so):
        return SessionEmulator(kind=KIND_RETROARCH, invocation=invocation)
    if system != PS1_SYSTEM:
        return Refusal(REASON_CORE_NOT_SUPPORTED)
    option = _bakeable_libretro(options, PCSX_REARMED_CORE)
    if option is None or not is_deterministic(PCSX_REARMED_CORE):
        return Refusal(REASON_CORE_NOT_SUPPORTED)
    return SessionEmulator(
        kind=KIND_RETROARCH,
        invocation=EmulatorInvocation.libretro(PCSX_REARMED_CORE, option.label, option.emulator),
        switched=True,
    )


def guest_emulator(note: SessionNote, options: Sequence[EmulatorOption]) -> EmulatorInvocation | None:
    """The guest's own invocation for joining *note*, looked up in its *options*.

    A RetroArch session needs the very core the host runs, found by name among
    the guest's own bakeable options and never taken from the note as a path.
    A PPSSPP session needs the guest's bakeable PPSSPP standalone. ``None`` where
    the guest has neither.
    """
    if note.kind == KIND_RETROARCH and note.core_so is not None:
        option = _bakeable_libretro(options, note.core_so)
        if option is None:
            return None
        return EmulatorInvocation.libretro(note.core_so, option.label, option.emulator)
    if note.kind == KIND_PPSSPP:
        for option in options:
            if option.kind == "standalone" and option.status == "bakeable":
                invocation = EmulatorInvocation.standalone(option.command, option.label, option.emulator)
                if _is_ppsspp(invocation):
                    return invocation
    return None


def _arg(flag: str, value: object) -> str:
    """One ``--flag=value`` argument, refusing a value that is not shell-inert."""
    text = str(value)
    if not _SAFE_ARG_RE.fullmatch(text) or text.startswith("-"):
        raise ValueError(f"unsafe value for {flag}")
    return f"{flag}={text}"


def retroarch_host_args(config_path: str) -> str:
    """The flags a RetroArch host is launched with."""
    return f"--host {_arg('--appendconfig', config_path)}"


def retroarch_guest_args(address: str, port: int, relay_session: str | None, config_path: str) -> str:
    """The flags a RetroArch guest is launched with.

    *address* and *port* are the host's, or the relay's where *relay_session* is
    set. ``--sram-mode=noload-nosave`` keeps the guest's own save file out of the
    session: RetroArch copies the host's save RAM over the guest's on connecting.
    RetroArch's ``addr|port|session`` form for ``--connect`` is not used: a
    ``|`` is a shell pipe.
    """
    if not is_valid_address(address) or not is_valid_port(port):
        raise ValueError("unsafe host address")
    parts = [_arg("--connect", address), _arg("--port", port)]
    if relay_session is not None:
        if not is_valid_relay_session(relay_session):
            raise ValueError("unsafe relay session")
        parts.append(_arg("--mitm-session", relay_session))
    parts.append("--sram-mode=noload-nosave")
    parts.append(_arg("--appendconfig", config_path))
    return " ".join(parts)


def ppsspp_args(config_path: str) -> str:
    """The flags a PPSSPP host or guest is launched with."""
    return _arg("--appendconfig", config_path)


def netplay_launch_options(invocation: EmulatorInvocation, args: str, rom_path: str) -> str:
    """The full one-off Steam launch command for a session.

    Raises ``ValueError`` for an invocation that cannot carry flags (a
    ``direct`` launch) and for args that are not shell-inert.
    """
    if not all(_SAFE_ARG_RE.fullmatch(part) for part in args.split(" ")):
        raise ValueError("unsafe launch arguments")
    rendered = invocation_with_launch_args(invocation, args)
    if rendered is None:
        raise ValueError("this emulator cannot be launched for a session")
    return build_launch_options(rendered, rom_path)


def config_file_name(kind: str, *, host: bool) -> str:
    """The appended config's file name for a *kind* session, host or guest side."""
    role = "host" if host else "guest"
    extension = "ini" if kind == KIND_PPSSPP else "cfg"
    return f"{kind}-{role}.{extension}"


def _config_value(value: str) -> str:
    if _CONFIG_UNSAFE_RE.search(value):
        raise ValueError("value cannot be written to a config file")
    return value


def _config_lines(entries: Iterable[tuple[str, str]]) -> str:
    return "".join(f'{key} = "{_config_value(value)}"\n' for key, value in entries)


def _password(password: str) -> str:
    if not is_valid_password(password):
        raise ValueError("invalid session password")
    return password


def retroarch_host_config(*, password: str, nickname: str, save_dir: str | None) -> str:
    """The RetroArch config a host appends.

    ``config_save_on_exit`` is off because RetroArch writes every appended
    setting into the main ``retroarch.cfg`` when it saves it, on exit included
    (``--appendconfig`` in RetroArch's own ``--help``), which would leave the
    relay, the public announce and a moved save folder behind for every later
    game. *save_dir* is set for a session switched onto another core: the
    memory card stays in the folder the game's own core saves in, with neither
    sort-by-core nor sort-by-content moving it.
    """
    entries = [
        ("netplay_password", _password(password)),
        ("netplay_nickname", nickname),
        ("netplay_ip_port", str(RETROARCH_NETPLAY_PORT)),
        ("netplay_use_mitm_server", "true"),
        ("netplay_mitm_server", "nyc"),
        ("netplay_public_announce", "true"),
        ("config_save_on_exit", "false"),
    ]
    if save_dir is not None:
        if not save_dir.startswith("/"):
            raise ValueError("save folder must be absolute")
        entries += [
            ("savefile_directory", save_dir),
            ("sort_savefiles_enable", "false"),
            ("sort_savefiles_by_content_enable", "false"),
        ]
    return _config_lines(entries)


def retroarch_guest_config(*, password: str, nickname: str) -> str:
    """The RetroArch config a guest appends: the session's password and its own nickname."""
    return _config_lines(
        [
            ("netplay_password", _password(password)),
            ("netplay_nickname", nickname),
            ("config_save_on_exit", "false"),
        ]
    )


def ppsspp_config(*, server_address: str, host: bool) -> str:
    """The PPSSPP config a host or guest appends.

    The host runs PPSSPP's built-in ad hoc server; both sides point their ad hoc
    client at *server_address*, the host's address.
    """
    if not is_valid_address(server_address):
        raise ValueError("invalid ad hoc server address")
    adhoc_server = "True" if host else "False"
    return f"[Network]\nEnableWlan = True\nEnableAdhocServer = {adhoc_server}\nproAdhocServer = {server_address}\n"


def netplay_nickname(username: str | None) -> str:
    """The nickname a session is played under, from the RomM *username*.

    Characters RetroArch's config cannot hold are dropped rather than escaped,
    and the result fits RetroArch's nickname buffer. The host's lobby search
    matches on this same value, so it is computed once and used for both.
    """
    cleaned = _CONFIG_UNSAFE_RE.sub("", username or "")[:_NICKNAME_MAX].strip()
    return cleaned or _FALLBACK_NICKNAME


def session_password(uuid_text: str) -> str:
    """A fresh session password from one UUID's random letters and digits."""
    password = "".join(ch for ch in uuid_text if ch.isascii() and ch.isalnum())[:_PASSWORD_LENGTH]
    return _password(password)


def order_host_addresses(addresses: Iterable[str]) -> tuple[str, ...]:
    """Order this machine's IPv4 *addresses* the way a guest should try them.

    A Tailscale address (100.64.0.0/10) first, then private LAN addresses;
    loopback, link-local and public addresses are dropped, as is anything that
    is not a dotted quad. The relay serves a guest none of these reach.
    """
    tailscale: list[str] = []
    lan: list[str] = []
    for raw in addresses:
        try:
            address = ipaddress.IPv4Address(raw)
        except ValueError:
            continue
        text = str(address)
        if text in tailscale or text in lan:
            continue
        if address in _TAILSCALE:
            tailscale.append(text)
        elif any(address in network for network in _PRIVATE_LAN):
            lan.append(text)
    return (*tailscale, *lan)


def _relayed_room(room: dict[str, Any]) -> RelayRoom | None:
    if room.get("host_method") != _LOBBY_HOST_METHOD_RELAYED:
        return None
    host, port, session = room.get("mitm_ip"), room.get("mitm_port"), room.get("mitm_session")
    if not (isinstance(host, str) and isinstance(port, int) and isinstance(session, str)):
        return None
    if not (is_valid_address(host) and is_valid_port(port) and is_valid_relay_session(session)):
        return None
    return RelayRoom(host=host, port=port, session=session)


def find_relay_room(
    rooms: Iterable[object], *, nickname: str, core_name: str | None, not_before: datetime | None
) -> RelayRoom | None:
    """Find the host's own relayed room among libretro's lobby *rooms*.

    The newest room announced under *nickname* on the session's core, relayed,
    and created no earlier than *not_before*. *core_name* is the core's
    ``corename``; ``None`` matches any core. The ROM's CRC is not matched: what
    RetroArch announces as ``game_crc`` for content it loads by path is not a
    value Tender can reproduce without reading the whole file.
    """
    best: tuple[datetime, RelayRoom] | None = None
    for room in rooms:
        if not isinstance(room, dict):
            continue
        fields: dict[str, Any] = room
        if fields.get("username") != nickname:
            continue
        room_core = fields.get("core_name")
        if core_name is not None and (not isinstance(room_core, str) or room_core.casefold() != core_name.casefold()):
            continue
        relay = _relayed_room(fields)
        created = parse_iso(fields.get("created")) if isinstance(fields.get("created"), str) else None
        if relay is None or created is None:
            continue
        if not_before is not None and created < not_before:
            continue
        if best is None or created > best[0]:
            best = (created, relay)
    return best[1] if best is not None else None
