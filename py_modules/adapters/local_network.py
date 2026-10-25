"""This machine's network addresses, and whether a host answers on a TCP port.

Standard library only: the backend runs inside Decky Loader's own Python, so
nothing outside it can be relied on to be importable.
"""

from __future__ import annotations

import fcntl
import socket
import struct

# ``SIOCGIFADDR`` from ``<linux/sockios.h>``: the IPv4 address of one interface.
_SIOCGIFADDR = 0x8915
# ``IFNAMSIZ`` from ``<net/if.h>``, the terminating NUL included.
_IFNAMSIZ = 16


class LocalNetworkAdapter:
    """Reads this machine's IPv4 addresses and probes a TCP port."""

    def ipv4_addresses(self) -> list[str]:
        """The IPv4 address of every interface that has one, in interface order.

        Every address, loopback and public included; which a session offers is
        the caller's decision. An interface without an IPv4 address is skipped,
        and a machine whose interfaces cannot be listed has none.
        """
        try:
            interfaces = socket.if_nameindex()
        except OSError:
            return []
        addresses: list[str] = []
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            for _index, name in interfaces:
                request = struct.pack("256s", name.encode()[: _IFNAMSIZ - 1])
                try:
                    answer = fcntl.ioctl(probe.fileno(), _SIOCGIFADDR, request)
                except OSError:
                    continue
                # ``struct ifreq``: the name, then a ``sockaddr_in`` whose address
                # sits after its two-byte family and two-byte port.
                addresses.append(socket.inet_ntoa(answer[_IFNAMSIZ + 4 : _IFNAMSIZ + 8]))
        return addresses

    def tcp_reachable(self, address: str, port: int, timeout: float) -> bool:
        """Whether a TCP connection to *address*:*port* opens within *timeout* seconds."""
        try:
            with socket.create_connection((address, port), timeout=timeout):
                return True
        except OSError:
            return False
