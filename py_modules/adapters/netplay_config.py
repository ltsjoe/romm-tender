"""The config files a multiplayer session appends to its emulator's own.

A session's emulator is started with ``--appendconfig=<file>``, and the
emulator runs inside RetroDECK's flatpak sandbox, so the file has to sit where
the sandbox can read it. RetroDECK's sandbox ``/var/config`` is the flatpak
app's per-user ``~/.var/app/net.retrodeck.retrodeck/config`` tree (the same
mapping ``adapters/es_find_rules.py`` reads ``staticpath`` entries through), so
a folder of Tender's own there is reachable from both sides.
"""

from __future__ import annotations

import contextlib
import os
import re

_RETRODECK_APP_ID = "net.retrodeck.retrodeck"
_FOLDER = "tender-netplay"
_SANDBOX_DIR = f"/var/config/{_FOLDER}"
_NAME_RE = re.compile(r"^[a-z0-9-]+\.(cfg|ini)$")


class NetplayConfigAdapter:
    """Writes and removes the appended config files, one folder of Tender's own."""

    def __init__(self, *, user_home: str) -> None:
        self._host_dir = os.path.join(user_home, ".var", "app", _RETRODECK_APP_ID, "config", _FOLDER)

    @staticmethod
    def _checked(name: str) -> str:
        if not _NAME_RE.fullmatch(name):
            raise ValueError(f"not a session config file name: {name!r}")
        return name

    def write_config(self, name: str, text: str) -> str:
        """Write *text* as the config *name*, replacing any earlier one; answer its sandbox path.

        The file is written beside itself and renamed into place, so an emulator
        starting at the same moment reads either the old file or the new one. It
        holds the session password, so only the owner may read it.
        """
        name = self._checked(name)
        os.makedirs(self._host_dir, exist_ok=True)
        target = os.path.join(self._host_dir, name)
        staging = f"{target}.tmp"
        fd = os.open(staging, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(text)
            os.chmod(staging, 0o600)
            os.replace(staging, target)
        except BaseException:
            with contextlib.suppress(OSError):
                os.remove(staging)
            raise
        return f"{_SANDBOX_DIR}/{name}"

    def remove_config(self, name: str) -> None:
        """Remove the config *name*; one that is not there is already removed."""
        name = self._checked(name)
        with contextlib.suppress(FileNotFoundError):
            os.remove(os.path.join(self._host_dir, name))
