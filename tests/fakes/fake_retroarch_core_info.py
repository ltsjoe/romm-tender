"""In-memory ``RetroArchCoreInfoReader`` for service tests."""

from __future__ import annotations


class FakeRetroArchCoreInfoReader:
    """Answers a core's ``.info`` fields and install from seeded maps.

    ``features`` maps a core name to its ``savestate_features``; ``installed``
    holds the cores whose ``.so`` is present; ``corenames`` maps a core name to
    its ``corename``. Anything unseeded answers ``None`` / ``False``.
    """

    def __init__(
        self,
        *,
        features: dict[str, str] | None = None,
        installed: set[str] | None = None,
        corenames: dict[str, str] | None = None,
    ) -> None:
        self.features: dict[str, str] = dict(features or {})
        self.installed: set[str] = set(installed or ())
        self.corenames: dict[str, str] = dict(corenames or {})

    def get_corename(self, core_so: str) -> str | None:
        return self.corenames.get(core_so)

    def savestate_features(self, core_so: str) -> str | None:
        return self.features.get(core_so)

    def core_installed(self, core_so: str) -> bool:
        return core_so in self.installed
