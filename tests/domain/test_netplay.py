"""Tests for ``domain/netplay.py`` — which emulator a session runs, and what it is launched with."""

from __future__ import annotations

import shlex
from datetime import UTC, datetime
from typing import Any

import pytest
from fakes.fake_core_info_provider import libretro_option, standalone_option

from domain.netplay import (
    PCSX_REARMED_CORE,
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
from domain.netplay_note import KIND_PPSSPP, KIND_RETROARCH, RelayRoom, SessionNote
from domain.shortcut_data import EmulatorInvocation

_PPSSPP_COMMAND = "%EMULATOR_PPSSPP% %ROM%"
_CFG = "/var/config/tender-netplay/retroarch-host.cfg"


def _deterministic(*cores: str):
    return lambda core_so: core_so in cores


def _psx_options():
    return [
        libretro_option("swanstation_libretro", "SwanStation"),
        libretro_option("mednafen_psx_libretro", "Beetle PSX"),
        libretro_option(PCSX_REARMED_CORE, "PCSX ReARMed"),
    ]


class TestHostEmulator:
    def test_a_deterministic_libretro_core_hosts_on_retroarch(self):
        invocation = EmulatorInvocation.libretro("snes9x_libretro", "Snes9x")
        result = host_emulator("snes", invocation, [], _deterministic("snes9x_libretro"))
        assert isinstance(result, SessionEmulator)
        assert result.kind == KIND_RETROARCH
        assert result.invocation.core_so == "snes9x_libretro"
        assert not result.switched

    def test_a_core_not_marked_deterministic_is_refused(self):
        invocation = EmulatorInvocation.libretro("flycast_libretro", "Flycast")
        result = host_emulator("dreamcast", invocation, [], _deterministic())
        assert result == Refusal(REASON_CORE_NOT_SUPPORTED)

    def test_ps1_on_swanstation_switches_to_pcsx_rearmed_for_the_session(self):
        invocation = EmulatorInvocation.libretro("swanstation_libretro", "SwanStation")
        result = host_emulator("psx", invocation, _psx_options(), _deterministic(PCSX_REARMED_CORE))
        assert isinstance(result, SessionEmulator)
        assert result.invocation.core_so == PCSX_REARMED_CORE
        assert result.switched

    def test_ps1_on_a_deterministic_core_keeps_it(self):
        invocation = EmulatorInvocation.libretro("mednafen_psx_libretro", "Beetle PSX")
        result = host_emulator("psx", invocation, _psx_options(), _deterministic("mednafen_psx_libretro"))
        assert isinstance(result, SessionEmulator)
        assert result.invocation.core_so == "mednafen_psx_libretro"
        assert not result.switched

    def test_ps1_without_pcsx_rearmed_among_the_options_is_refused(self):
        invocation = EmulatorInvocation.libretro("swanstation_libretro", "SwanStation")
        options = [libretro_option("swanstation_libretro", "SwanStation")]
        assert host_emulator("psx", invocation, options, _deterministic(PCSX_REARMED_CORE)) == Refusal(
            REASON_CORE_NOT_SUPPORTED
        )

    def test_ps1_switch_needs_pcsx_rearmed_bakeable(self):
        invocation = EmulatorInvocation.libretro("swanstation_libretro", "SwanStation")
        options = [
            libretro_option("swanstation_libretro", "SwanStation"),
            standalone_option(
                f"%EMULATOR_RETROARCH% -L %CORE_RETROARCH%/{PCSX_REARMED_CORE}.so %ROM%",
                "PCSX ReARMed",
                status="unbakeable",
                reason="quoting",
            ),
        ]
        assert isinstance(host_emulator("psx", invocation, options, _deterministic(PCSX_REARMED_CORE)), Refusal)

    def test_ps1_on_duckstation_standalone_is_not_switched(self):
        # The switch keeps the memory card where the usual CORE saves it (D24);
        # a standalone emulator keeps its cards in a place of its own, which a
        # RetroArch session would not find.
        invocation = EmulatorInvocation.standalone("%EMULATOR_DUCKSTATION% -batch %ROM%", "DuckStation")
        result = host_emulator("psx", invocation, _psx_options(), _deterministic(PCSX_REARMED_CORE))
        assert result == Refusal(REASON_EMULATOR_NOT_SUPPORTED)

    def test_another_system_on_a_non_deterministic_core_is_not_switched(self):
        invocation = EmulatorInvocation.libretro("swanstation_libretro", "SwanStation")
        assert isinstance(host_emulator("snes", invocation, _psx_options(), _deterministic(PCSX_REARMED_CORE)), Refusal)

    def test_ppsspp_standalone_hosts_on_ppsspp(self):
        invocation = EmulatorInvocation.standalone(_PPSSPP_COMMAND, "PPSSPP (Standalone)")
        result = host_emulator("psp", invocation, [], _deterministic())
        assert isinstance(result, SessionEmulator)
        assert result.kind == KIND_PPSSPP

    def test_another_standalone_emulator_is_refused(self):
        invocation = EmulatorInvocation.standalone("%EMULATOR_PCSX2% -batch %ROM%", "PCSX2")
        assert host_emulator("ps2", invocation, [], _deterministic()) == Refusal(REASON_EMULATOR_NOT_SUPPORTED)

    def test_a_direct_launch_is_refused(self):
        invocation = EmulatorInvocation.direct("%EMULATOR_RPCS3% --no-gui %ROM%", "/app/x.sh", "RPCS3")
        assert host_emulator("ps3", invocation, [], _deterministic()) == Refusal(REASON_EMULATOR_NOT_SUPPORTED)

    def test_no_emulator_is_refused(self):
        assert host_emulator("gba", None, [], _deterministic()) == Refusal(REASON_EMULATOR_NOT_SUPPORTED)


class TestGuestEmulator:
    def _note(self, **overrides) -> SessionNote:
        fields = {
            "kind": KIND_RETROARCH,
            "core_so": PCSX_REARMED_CORE,
            "addresses": ("192.168.1.2",),
            "port": RETROARCH_NETPLAY_PORT,
            "relay": None,
            "password": "abcd1234",
            "nickname": "bob",
            "started_at": "2026-10-25T14:00:00+00:00",
        }
        fields.update(overrides)
        return SessionNote(**fields)

    def test_the_hosts_core_is_found_among_the_guests_own_options(self):
        invocation = guest_emulator(self._note(), _psx_options())
        assert invocation is not None
        assert invocation.core_so == PCSX_REARMED_CORE
        assert invocation.kind == "libretro"

    def test_a_core_the_guest_does_not_offer_is_none(self):
        assert guest_emulator(self._note(core_so="snes9x_libretro"), _psx_options()) is None

    def test_ppsspp_needs_the_guests_ppsspp_standalone(self):
        note = self._note(kind=KIND_PPSSPP, core_so=None, password=None, port=PPSSPP_ADHOC_SERVER_PORT)
        assert guest_emulator(note, _psx_options()) is None
        invocation = guest_emulator(note, [standalone_option(_PPSSPP_COMMAND, "PPSSPP (Standalone)")])
        assert invocation is not None
        assert invocation.command == _PPSSPP_COMMAND

    def test_an_uninstalled_ppsspp_is_none(self):
        note = self._note(kind=KIND_PPSSPP, core_so=None, password=None, port=PPSSPP_ADHOC_SERVER_PORT)
        options = [standalone_option(_PPSSPP_COMMAND, "PPSSPP", status="needs_setup", reason="not_installed")]
        assert guest_emulator(note, options) is None


class TestCommands:
    def test_retroarch_host_flags_go_before_the_rom(self):
        invocation = EmulatorInvocation.libretro("snes9x_libretro", "Snes9x")
        options = netplay_launch_options(invocation, retroarch_host_args(_CFG), "/roms/snes/My Game.sfc")
        assert options == (
            'flatpak run net.retrodeck.retrodeck -e "%EMULATOR_RETROARCH% -L '
            "/var/config/retroarch/cores/snes9x_libretro.so --host "
            f'--appendconfig={_CFG} %ROM%" "/roms/snes/My Game.sfc"'
        )

    def test_retroarch_guest_direct_flags(self):
        args = retroarch_guest_args("100.64.1.2", 55435, None, "/var/config/tender-netplay/retroarch-guest.cfg")
        assert args == (
            "--connect=100.64.1.2 --port=55435 --sram-mode=noload-nosave "
            "--appendconfig=/var/config/tender-netplay/retroarch-guest.cfg"
        )

    def test_retroarch_guest_relay_flags(self):
        args = retroarch_guest_args("nyc.relay.retroarch.com", 55435, "/wN25+L==", "/var/config/x.cfg")
        assert "--connect=nyc.relay.retroarch.com" in args
        assert "--mitm-session=/wN25+L==" in args
        assert "|" not in args

    def test_ppsspp_flags_go_before_the_rom(self):
        invocation = EmulatorInvocation.standalone(_PPSSPP_COMMAND, "PPSSPP")
        options = netplay_launch_options(
            invocation, ppsspp_args("/var/config/tender-netplay/ppsspp-host.ini"), "/r.iso"
        )
        assert options == (
            'flatpak run net.retrodeck.retrodeck -e "%EMULATOR_PPSSPP% '
            '--appendconfig=/var/config/tender-netplay/ppsspp-host.ini %ROM%" "/r.iso"'
        )

    @pytest.mark.parametrize("address", ['1.2.3.4"', "1.2.3.4;id", "$(id)", "a b", "1.2.3.4\n"])
    def test_a_bad_address_never_reaches_a_command(self, address):
        with pytest.raises(ValueError):
            retroarch_guest_args(address, 55435, None, "/var/config/x.cfg")

    @pytest.mark.parametrize("session", ["a|b", "abc\n"])
    def test_a_bad_session_never_reaches_a_command(self, session):
        with pytest.raises(ValueError):
            retroarch_guest_args("nyc.relay.retroarch.com", 55435, session, "/var/config/x.cfg")

    @pytest.mark.parametrize("path", ['/var/config/x".cfg', "/var/config/x.cfg\n"])
    def test_a_bad_config_path_never_reaches_a_command(self, path):
        with pytest.raises(ValueError):
            retroarch_host_args(path)

    def test_the_command_never_holds_a_quote_inside_the_e_string(self):
        invocation = EmulatorInvocation.libretro("snes9x_libretro", "Snes9x")
        args = retroarch_guest_args("192.168.1.2", 55435, "abc+/=", "/var/config/tender-netplay/g.cfg")
        options = netplay_launch_options(invocation, args, "/roms/snes/g.sfc")
        e_string = shlex.split(options)[4]
        assert '"' not in e_string
        assert e_string.endswith("%ROM%")

    def test_an_invocation_that_cannot_carry_flags_raises(self):
        invocation = EmulatorInvocation.direct("%EMULATOR_RPCS3% %ROM%", "/app/x.sh", "RPCS3")
        with pytest.raises(ValueError):
            netplay_launch_options(invocation, "--host", "/roms/x")

    def test_config_file_names(self):
        assert config_file_name(KIND_RETROARCH, host=True) == "retroarch-host.cfg"
        assert config_file_name(KIND_RETROARCH, host=False) == "retroarch-guest.cfg"
        assert config_file_name(KIND_PPSSPP, host=True) == "ppsspp-host.ini"
        assert config_file_name(KIND_PPSSPP, host=False) == "ppsspp-guest.ini"


class TestConfigs:
    def test_retroarch_host_config(self):
        text = retroarch_host_config(password="abcd1234", nickname="alice", save_dir=None)
        lines = text.splitlines()
        assert 'netplay_password = "abcd1234"' in lines
        assert 'netplay_nickname = "alice"' in lines
        assert 'netplay_ip_port = "55435"' in lines
        assert 'netplay_use_mitm_server = "true"' in lines
        assert 'netplay_mitm_server = "nyc"' in lines
        assert 'netplay_public_announce = "true"' in lines
        assert 'config_save_on_exit = "false"' in lines
        assert not any(line.startswith("savefile_directory") for line in lines)

    def test_a_ps1_switch_keeps_the_usual_save_folder(self):
        text = retroarch_host_config(password="abcd1234", nickname="alice", save_dir="/home/deck/retrodeck/saves/psx")
        lines = text.splitlines()
        assert 'savefile_directory = "/home/deck/retrodeck/saves/psx"' in lines
        assert 'sort_savefiles_enable = "false"' in lines
        assert 'sort_savefiles_by_content_enable = "false"' in lines

    def test_retroarch_guest_config(self):
        lines = retroarch_guest_config(password="abcd1234", nickname="bob").splitlines()
        assert 'netplay_password = "abcd1234"' in lines
        assert 'netplay_nickname = "bob"' in lines
        assert 'config_save_on_exit = "false"' in lines
        assert not any("mitm" in line or "announce" in line for line in lines)

    def test_ppsspp_host_and_guest(self):
        host = ppsspp_config(server_address="192.168.1.2", host=True).splitlines()
        guest = ppsspp_config(server_address="192.168.1.2", host=False).splitlines()
        assert host[0] == "[Network]" and guest[0] == "[Network]"
        assert "EnableWlan = True" in host and "EnableWlan = True" in guest
        assert "EnableAdhocServer = True" in host
        assert "EnableAdhocServer = False" in guest
        assert "proAdhocServer = 192.168.1.2" in host and "proAdhocServer = 192.168.1.2" in guest

    def test_a_bad_password_is_refused(self):
        with pytest.raises(ValueError):
            retroarch_guest_config(password='ab"cd', nickname="bob")

    @pytest.mark.parametrize("save_dir", ['/x"y', "/x\ny", "relative/dir"])
    def test_a_save_folder_a_config_cannot_hold_is_refused(self, save_dir):
        with pytest.raises(ValueError):
            retroarch_host_config(password="abcd1234", nickname="alice", save_dir=save_dir)

    def test_a_bad_ppsspp_server_is_refused(self):
        with pytest.raises(ValueError):
            ppsspp_config(server_address="1.2.3.4\nEnableWlan = False", host=False)


class TestNicknameAndPassword:
    def test_a_plain_username_is_kept(self):
        assert netplay_nickname("alice") == "alice"

    def test_quotes_and_control_characters_are_dropped(self):
        assert netplay_nickname('al"i\nce\t') == "alice"

    def test_the_nickname_fits_retroarchs_buffer(self):
        assert len(netplay_nickname("x" * 100)) == 31

    def test_an_empty_nickname_falls_back(self):
        assert netplay_nickname('""') == "Tender"
        assert netplay_nickname(None) == "Tender"

    def test_password_from_a_uuid(self):
        password = session_password("0f1e2d3c-4b5a-4987-8a6b-5c4d3e2f1a0b")
        assert password == "0f1e2d3c4b5a4987"

    def test_a_uuid_without_enough_letters_is_refused(self):
        with pytest.raises(ValueError):
            session_password("--")


class TestAddressOrder:
    def test_tailscale_first_then_lan_and_nothing_else(self):
        addresses = ["127.0.0.1", "192.168.1.20", "169.254.3.4", "8.8.8.8", "100.101.102.103", "10.0.0.5", "172.20.1.1"]
        assert order_host_addresses(addresses) == ("100.101.102.103", "192.168.1.20", "10.0.0.5", "172.20.1.1")

    def test_duplicates_and_junk_are_dropped(self):
        assert order_host_addresses(["10.0.0.5", "10.0.0.5", "not-an-ip", "", "172.32.0.1"]) == ("10.0.0.5",)

    def test_the_cgnat_edge(self):
        assert order_host_addresses(["100.63.255.255", "100.128.0.1", "100.127.255.254"]) == ("100.127.255.254",)


def _room(**overrides) -> dict[str, Any]:
    room = {
        "username": "alice",
        "core_name": "Snes9x",
        "game_crc": "00000000",
        "host_method": 3,
        "mitm_ip": "europe-west1.relay.retroarch.com",
        "mitm_port": 55435,
        "mitm_session": "/wN25LdlgMoS3OAE",
        "created": "2026-10-25T14:00:10.98629666Z",
    }
    room.update(overrides)
    return room


class TestRelayRoom:
    def test_the_hosts_relayed_room_is_found(self):
        room = find_relay_room([_room()], nickname="alice", core_name="Snes9x", not_before=None)
        assert room == RelayRoom(host="europe-west1.relay.retroarch.com", port=55435, session="/wN25LdlgMoS3OAE")

    def test_the_newest_room_wins(self):
        rooms = [_room(mitm_session="old"), _room(mitm_session="new", created="2026-10-25T14:05:00Z")]
        room = find_relay_room(rooms, nickname="alice", core_name="Snes9x", not_before=None)
        assert room is not None and room.session == "new"

    def test_another_players_room_is_ignored(self):
        assert (
            find_relay_room([_room(username="mallory")], nickname="alice", core_name="Snes9x", not_before=None) is None
        )

    def test_another_core_is_ignored(self):
        assert find_relay_room([_room(core_name="MAME")], nickname="alice", core_name="Snes9x", not_before=None) is None

    def test_the_core_name_is_matched_without_case(self):
        assert find_relay_room([_room(core_name="snes9x")], nickname="alice", core_name="Snes9x", not_before=None)

    def test_an_unknown_core_name_matches_any(self):
        assert find_relay_room([_room(core_name="MAME")], nickname="alice", core_name=None, not_before=None)

    def test_a_room_that_is_not_relayed_is_ignored(self):
        rooms = [_room(host_method=0), _room(mitm_session=""), _room(mitm_ip="")]
        assert find_relay_room(rooms, nickname="alice", core_name="Snes9x", not_before=None) is None

    def test_a_room_from_before_the_session_is_ignored(self):
        not_before = datetime(2026, 10, 25, 14, 1, tzinfo=UTC)
        assert find_relay_room([_room()], nickname="alice", core_name="Snes9x", not_before=not_before) is None

    def test_a_room_with_an_unsafe_relay_is_ignored(self):
        rooms = [_room(mitm_ip="1.2.3.4;id"), _room(mitm_session="a b"), _room(mitm_port="55435")]
        assert find_relay_room(rooms, nickname="alice", core_name="Snes9x", not_before=None) is None

    def test_non_dict_entries_are_skipped(self):
        assert find_relay_room(["x", None, _room()], nickname="alice", core_name=None, not_before=None) is not None


class TestActivitySupported:
    @pytest.mark.parametrize("version", ["5.0.0", "5.3.0", "6.0.0-beta.1", "development"])
    def test_supported(self, version):
        assert activity_supported(version)

    @pytest.mark.parametrize("version", ["4.9.2", "5.0.0-beta.5", None, "", "garbage"])
    def test_unsupported(self, version):
        assert not activity_supported(version)
