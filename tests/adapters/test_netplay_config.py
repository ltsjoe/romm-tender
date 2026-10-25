"""Tests for ``adapters.netplay_config`` — the appended config files RetroDECK's emulators read."""

from __future__ import annotations

import os
import stat

import pytest

from adapters.netplay_config import NetplayConfigAdapter


def _host_dir(home) -> str:
    return os.path.join(str(home), ".var", "app", "net.retrodeck.retrodeck", "config", "tender-netplay")


class TestWrite:
    def test_writes_into_retrodecks_config_tree_and_answers_the_sandbox_path(self, tmp_path):
        adapter = NetplayConfigAdapter(user_home=str(tmp_path))
        sandbox_path = adapter.write_config("retroarch-host.cfg", 'netplay_password = "x"\n')
        assert sandbox_path == "/var/config/tender-netplay/retroarch-host.cfg"
        with open(os.path.join(_host_dir(tmp_path), "retroarch-host.cfg"), encoding="utf-8") as f:
            assert f.read() == 'netplay_password = "x"\n'

    def test_an_existing_file_is_replaced(self, tmp_path):
        adapter = NetplayConfigAdapter(user_home=str(tmp_path))
        adapter.write_config("ppsspp-guest.ini", "old")
        adapter.write_config("ppsspp-guest.ini", "new")
        with open(os.path.join(_host_dir(tmp_path), "ppsspp-guest.ini"), encoding="utf-8") as f:
            assert f.read() == "new"
        assert os.listdir(_host_dir(tmp_path)) == ["ppsspp-guest.ini"]

    def test_only_the_owner_can_read_it(self, tmp_path):
        adapter = NetplayConfigAdapter(user_home=str(tmp_path))
        adapter.write_config("retroarch-guest.cfg", "x")
        mode = stat.S_IMODE(os.stat(os.path.join(_host_dir(tmp_path), "retroarch-guest.cfg")).st_mode)
        assert mode == 0o600

    @pytest.mark.parametrize("name", ["../evil.cfg", "a/b.cfg", "x.sh", "", "UPPER.cfg", "x.cfg\n"])
    def test_a_name_outside_the_folder_is_refused(self, tmp_path, name):
        with pytest.raises(ValueError):
            NetplayConfigAdapter(user_home=str(tmp_path)).write_config(name, "x")


class TestRemove:
    def test_removes_the_file(self, tmp_path):
        adapter = NetplayConfigAdapter(user_home=str(tmp_path))
        adapter.write_config("retroarch-host.cfg", "x")
        adapter.remove_config("retroarch-host.cfg")
        assert not os.path.exists(os.path.join(_host_dir(tmp_path), "retroarch-host.cfg"))

    def test_a_missing_file_is_already_removed(self, tmp_path):
        NetplayConfigAdapter(user_home=str(tmp_path)).remove_config("retroarch-host.cfg")

    def test_a_name_outside_the_folder_is_refused(self, tmp_path):
        with pytest.raises(ValueError):
            NetplayConfigAdapter(user_home=str(tmp_path)).remove_config("../x.cfg")
