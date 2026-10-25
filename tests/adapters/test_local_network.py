"""Tests for ``adapters.local_network`` — this machine's IPv4 addresses and a TCP probe."""

from __future__ import annotations

import socket
import struct
from unittest.mock import patch

from adapters.local_network import LocalNetworkAdapter


def _ifreq_answer(address: str) -> bytes:
    # struct ifreq: 16-byte name, then a sockaddr_in (family, port, address).
    return b"\0" * 16 + struct.pack("!HH4s", socket.AF_INET, 0, socket.inet_aton(address)) + b"\0" * 8


class TestIpv4Addresses:
    def test_every_interface_with_an_ipv4_address(self):
        answers = {"lo": "127.0.0.1", "wlan0": "192.168.1.20", "tailscale0": "100.101.102.103"}

        def fake_ioctl(_fd, _request, packed):
            name = packed[:16].rstrip(b"\0").decode()
            if name not in answers:
                raise OSError(99, "Cannot assign requested address")
            return _ifreq_answer(answers[name])

        interfaces = [(1, "lo"), (2, "wlan0"), (3, "docker0"), (4, "tailscale0")]
        with (
            patch("adapters.local_network.socket.if_nameindex", return_value=interfaces),
            patch("adapters.local_network.fcntl.ioctl", side_effect=fake_ioctl),
        ):
            assert LocalNetworkAdapter().ipv4_addresses() == ["127.0.0.1", "192.168.1.20", "100.101.102.103"]

    def test_no_interface_listing_is_no_addresses(self):
        with patch("adapters.local_network.socket.if_nameindex", side_effect=OSError("nope")):
            assert LocalNetworkAdapter().ipv4_addresses() == []

    def test_reads_this_machine(self):
        addresses = LocalNetworkAdapter().ipv4_addresses()
        assert all(isinstance(a, str) and socket.inet_aton(a) for a in addresses)


class TestTcpReachable:
    def test_a_listening_port_is_reachable(self):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
            server.bind(("127.0.0.1", 0))
            server.listen(1)
            port = server.getsockname()[1]
            assert LocalNetworkAdapter().tcp_reachable("127.0.0.1", port, 1.0) is True

    def test_a_closed_port_is_not(self):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        assert LocalNetworkAdapter().tcp_reachable("127.0.0.1", port, 1.0) is False

    def test_an_unresolvable_name_is_not(self):
        with patch("adapters.local_network.socket.create_connection", side_effect=socket.gaierror("no")):
            assert LocalNetworkAdapter().tcp_reachable("nowhere.invalid", 55435, 1.0) is False
