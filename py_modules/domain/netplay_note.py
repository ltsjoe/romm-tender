"""The note a multiplayer host publishes on a ROM, and what a guest may read out of it.

A Tender session note is the one channel between two Tenders on the same RomM
server: the host writes it, a guest reads it and builds an emulator command
from it. That command reaches RetroDECK's ``run_game.sh``, which runs it through
``eval``, and the note was written by ANOTHER user. So this module is the
boundary: every value a guest may put on a command line is checked against a
character set that holds no quote, space, shell metacharacter or newline, and a
note carrying one value outside it is not a Tender note at all. Nothing here
ever repairs a value; a note either parses whole or parses to ``None``.

The format is versioned. A note opens with a sentence for whoever reads it on
RomM's website, then a fenced block whose info string is :data:`NOTE_INFO_STRING`
holding a JSON object with ``"v": 1``. A guest reads only the version it knows.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, replace
from typing import Any

from domain.iso_time import parse_iso

NOTE_TITLE = "Tender multiplayer session"
NOTE_INFO_STRING = "tender-netplay"
NOTE_FORMAT_VERSION = 1

KIND_RETROARCH = "retroarch"
KIND_PPSSPP = "ppsspp"

_NOTE_LEAD = (
    "This game is being hosted for multiplayer with Tender. "
    "Join it from Tender on a device signed in to this RomM server."
)

# A note this long is not one Tender wrote; reading it is not worth the risk.
_MAX_NOTE_CHARS = 8192
_MAX_ADDRESSES = 8

_FENCE_RE = re.compile(r"```" + re.escape(NOTE_INFO_STRING) + r"[ \t]*\r?\n(.*?)\r?\n```", re.DOTALL)
_DOTTED_QUAD_RE = re.compile(r"^(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})$")
_DIGITS_AND_DOTS_RE = re.compile(r"^[0-9.]+$")
_HOSTNAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.-]{0,252}$")
_RELAY_SESSION_RE = re.compile(r"^[A-Za-z0-9+/=]{1,64}$")
_PASSWORD_RE = re.compile(r"^[A-Za-z0-9]{4,32}$")
_CORE_SO_RE = re.compile(r"^[A-Za-z0-9_]+_libretro$")
_NICKNAME_RE = re.compile(r'^[^\x00-\x1f\x7f"\\]{1,64}$')
_STARTED_AT_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9:.]+(Z|[+-][0-9]{2}:[0-9]{2})?$")


@dataclass(frozen=True)
class RelayRoom:
    """Where libretro's relay serves a RetroArch host: relay address, port, session id."""

    host: str
    port: int
    session: str


@dataclass(frozen=True)
class SessionNote:
    """Everything a guest needs to join one hosted session.

    ``core_so`` is the bare libretro core name a RetroArch session runs on, and
    ``None`` for PPSSPP. A guest never loads it from the note: it looks the name
    up among its own emulator options. ``addresses`` are the host's direct
    addresses in the order a guest should try them; ``relay`` is set once the
    host found its relayed room. ``password`` is ``None`` for PPSSPP, whose ad
    hoc server has none. ``started_at`` is the host's own clock, which with the
    host's user id and the ROM id keys the session.
    """

    kind: str
    core_so: str | None
    addresses: tuple[str, ...]
    port: int
    relay: RelayRoom | None
    password: str | None
    nickname: str
    started_at: str

    def with_relay(self, relay: RelayRoom) -> SessionNote:
        """This note, carrying *relay*."""
        return replace(self, relay=relay)


def is_valid_address(value: object) -> bool:
    """Whether *value* is an IPv4 dotted quad or a plain hostname.

    A hostname is letters, digits, dots and dashes and never starts with a
    dash, so it cannot be read as an option. Anything made only of digits and
    dots must be a real dotted quad.
    """
    if not isinstance(value, str):
        return False
    if _DIGITS_AND_DOTS_RE.fullmatch(value):
        match = _DOTTED_QUAD_RE.fullmatch(value)
        return match is not None and all(int(part) <= 255 for part in match.groups())
    return _HOSTNAME_RE.fullmatch(value) is not None


def is_valid_port(value: object) -> bool:
    """Whether *value* is an ``int`` port in 1-65535 (a ``bool`` is not one)."""
    return isinstance(value, int) and not isinstance(value, bool) and 1 <= value <= 65535


def is_valid_relay_session(value: object) -> bool:
    """Whether *value* is a relay session id: base64 characters, at most 64."""
    return isinstance(value, str) and _RELAY_SESSION_RE.fullmatch(value) is not None


def is_valid_password(value: object) -> bool:
    """Whether *value* is a session password: 4 to 32 ASCII letters and digits."""
    return isinstance(value, str) and _PASSWORD_RE.fullmatch(value) is not None


def is_valid_core_so(value: object) -> bool:
    """Whether *value* is a bare libretro core name, never a path."""
    return isinstance(value, str) and _CORE_SO_RE.fullmatch(value) is not None


def _is_valid_nickname(value: object) -> bool:
    return isinstance(value, str) and _NICKNAME_RE.fullmatch(value) is not None


def _is_valid_started_at(value: object) -> bool:
    return isinstance(value, str) and _STARTED_AT_RE.fullmatch(value) is not None and parse_iso(value) is not None


def _is_valid_relay(relay: RelayRoom | None) -> bool:
    return relay is None or (
        is_valid_address(relay.host) and is_valid_port(relay.port) and is_valid_relay_session(relay.session)
    )


def _is_valid(note: SessionNote) -> bool:
    """Whether every field of *note* is one a guest may act on."""
    common = (
        len(note.addresses) <= _MAX_ADDRESSES
        and all(is_valid_address(a) for a in note.addresses)
        and is_valid_port(note.port)
        and _is_valid_relay(note.relay)
        and _is_valid_nickname(note.nickname)
        and _is_valid_started_at(note.started_at)
    )
    if not common:
        return False
    if note.kind == KIND_RETROARCH:
        return (
            is_valid_core_so(note.core_so)
            and is_valid_password(note.password)
            and (bool(note.addresses) or note.relay is not None)
        )
    if note.kind == KIND_PPSSPP:
        return (
            note.core_so is None
            and note.relay is None
            and bool(note.addresses)
            and (note.password is None or is_valid_password(note.password))
        )
    return False


def render_note(note: SessionNote) -> str:
    """Render *note* as the content of a RomM note.

    Raises ``ValueError`` for a note a guest would refuse, so a host can never
    publish a session nobody can join.
    """
    if not _is_valid(note):
        raise ValueError("session note carries a value a guest would refuse")
    payload: dict[str, Any] = {
        "v": NOTE_FORMAT_VERSION,
        "kind": note.kind,
        "core": note.core_so,
        "addresses": list(note.addresses),
        "port": note.port,
        "relay": (
            None
            if note.relay is None
            else {"host": note.relay.host, "port": note.relay.port, "session": note.relay.session}
        ),
        "password": note.password,
        "nickname": note.nickname,
        "started_at": note.started_at,
    }
    block = json.dumps(payload, indent=2)
    return f"{_NOTE_LEAD}\n\n```{NOTE_INFO_STRING}\n{block}\n```\n"


def _relay_from(raw: object) -> tuple[bool, RelayRoom | None]:
    """Read a payload's relay: ``(well_formed, room)``, the room ``None`` where none is stated."""
    if raw is None:
        return (True, None)
    if not isinstance(raw, dict):
        return (False, None)
    fields: dict[str, Any] = raw
    host, port, session = fields.get("host"), fields.get("port"), fields.get("session")
    if not (isinstance(host, str) and isinstance(port, int) and isinstance(session, str)):
        return (False, None)
    return (True, RelayRoom(host=host, port=port, session=session))


def _optional_str(value: object) -> tuple[bool, str | None]:
    """``(well_formed, value)`` for a field that is a string or absent."""
    if value is None:
        return (True, None)
    if isinstance(value, str):
        return (True, value)
    return (False, None)


def parse_note(content: object) -> SessionNote | None:
    """Read a Tender session out of a RomM note's *content*, or ``None``.

    ``None`` for anything that is not this format version or holds one value a
    guest may not act on — never a partial note.
    """
    if not isinstance(content, str) or len(content) > _MAX_NOTE_CHARS:
        return None
    match = _FENCE_RE.search(content)
    if match is None:
        return None
    try:
        decoded: Any = json.loads(match.group(1))
    except ValueError:
        return None
    if not isinstance(decoded, dict):
        return None
    payload: dict[str, Any] = decoded
    if payload.get("v") != NOTE_FORMAT_VERSION:
        return None
    kind, port = payload.get("kind"), payload.get("port")
    nickname, started_at = payload.get("nickname"), payload.get("started_at")
    raw_addresses = payload.get("addresses")
    core_ok, core_so = _optional_str(payload.get("core"))
    password_ok, password = _optional_str(payload.get("password"))
    relay_ok, relay = _relay_from(payload.get("relay"))
    if not (
        isinstance(kind, str)
        and isinstance(port, int)
        and isinstance(nickname, str)
        and isinstance(started_at, str)
        and isinstance(raw_addresses, list)
        and core_ok
        and password_ok
        and relay_ok
    ):
        return None
    addresses = tuple(a for a in raw_addresses if isinstance(a, str))
    if len(addresses) != len(raw_addresses):
        return None
    note = SessionNote(
        kind=kind,
        core_so=core_so,
        addresses=addresses,
        port=port,
        relay=relay,
        password=password,
        nickname=nickname,
        started_at=started_at,
    )
    return note if _is_valid(note) else None
