from __future__ import annotations

import re

import pytest

from lynch_reader.auth.__main__ import main
from lynch_reader.auth.store import DeviceStore

CODE = r"[23456789A-HJ-NP-Z]{4}-[23456789A-HJ-NP-Z]{4}"


@pytest.fixture
def store(tmp_path):
    return DeviceStore(tmp_path / "app-auth" / "devices.json")


def test_pair_prints_the_code_and_the_link(store, capsys):
    assert main(["pair", "Mac 浏览器"], store) == 0
    out = capsys.readouterr().out
    code = re.search(CODE, out).group(0)
    assert f"http://pivot-home.tail090ab2.ts.net:3151/pair?code={code}" in out
    assert "10 分钟" in out
    store.redeem(code)
    assert [d.name for d in store.devices()] == ["Mac 浏览器"]


def test_pair_without_a_name_uses_the_time(store, capsys):
    assert main(["pair"], store) == 0
    code = re.search(CODE, capsys.readouterr().out).group(0)
    store.redeem(code)
    assert re.fullmatch(r"设备 \d{4}-\d{2}-\d{2} \d{2}:\d{2}", store.devices()[0].name)


def test_pair_name_after_double_dash_may_start_with_a_dash(store, capsys):
    # `just app-pair` passes the name after `--`, so a leading "-" is not read as an option.
    assert main(["pair", "--", "-手机"], store) == 0
    store.redeem(re.search(CODE, capsys.readouterr().out).group(0))
    assert [d.name for d in store.devices()] == ["-手机"]


def test_pair_empty_name_after_double_dash_uses_the_time(store, capsys):
    assert main(["pair", "--", ""], store) == 0
    store.redeem(re.search(CODE, capsys.readouterr().out).group(0))
    assert re.fullmatch(r"设备 \d{4}-\d{2}-\d{2} \d{2}:\d{2}", store.devices()[0].name)


def test_devices_lists_id_time_and_name(store, capsys):
    assert main(["devices"], store) == 0
    assert "还没有" in capsys.readouterr().out
    store.redeem(store.create_pairing("手机"))
    assert main(["devices"], store) == 0
    out = capsys.readouterr().out
    d = store.devices()[0]
    assert d.id in out and "手机" in out and d.paired_at[:16].replace("T", " ") in out


def test_revoke(store, capsys):
    store.redeem(store.create_pairing("手机"))
    device_id = store.devices()[0].id
    assert main(["revoke", device_id], store) == 0
    assert store.devices() == []
    assert main(["revoke", device_id], store) == 1
    assert device_id in capsys.readouterr().err


def test_corrupt_file_is_left_alone(store, capsys):
    store.path.parent.mkdir(parents=True)
    store.path.write_text("not json")
    for argv in (["pair"], ["devices"], ["revoke", "00000000"]):
        assert main(argv, store) == 1, argv
        assert "损坏" in capsys.readouterr().err
    assert store.path.read_text() == "not json"
