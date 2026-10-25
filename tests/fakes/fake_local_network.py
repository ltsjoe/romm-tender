"""In-memory ``LocalNetworkReader`` for service tests."""

from __future__ import annotations


class FakeLocalNetworkReader:
    """Answers this machine's addresses and which ``(address, port)`` pairs answer.

    ``probes`` records every reachability question in the order it was asked.
    """

    def __init__(self, *, addresses: list[str] | None = None, reachable: set[tuple[str, int]] | None = None) -> None:
        self.addresses: list[str] = list(addresses or [])
        self.reachable: set[tuple[str, int]] = set(reachable or ())
        self.probes: list[tuple[str, int, float]] = []

    def ipv4_addresses(self) -> list[str]:
        return list(self.addresses)

    def tcp_reachable(self, address: str, port: int, timeout: float) -> bool:
        self.probes.append((address, port, timeout))
        return (address, port) in self.reachable
