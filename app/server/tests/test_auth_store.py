from __future__ import annotations

import json
import re
import stat
import threading

import pytest

from lynch_reader.auth.store import DeviceStore, InvalidCode, StoreCorrupt

CODE = re.compile(r"^[23456789A-HJ-NP-Z]{4}-[23456789A-HJ-NP-Z]{4}$")


@pytest.fixture
def path(tmp_path):
    return tmp_path / "app-auth" / "devices.json"


@pytest.fixture
def store(path, clock):
    return DeviceStore(path, clock=clock)


def test_a_code_pairs_one_device(store):
    code = store.create_pairing("手机")
    assert CODE.match(code)
    token = store.redeem(code)
    assert store.is_paired(token)
    assert not store.is_paired("someone-else")
    [d] = store.devices()
    assert d.name == "手机" and re.fullmatch(r"[0-9a-f]{8}", d.id) and d.paired_at


def test_code_ignores_case_hyphen_and_surrounding_space(store):
    code = store.create_pairing("x")
    assert store.is_paired(store.redeem(f"  {code.replace('-', '').lower()} "))


def test_a_code_works_once(store):
    code = store.create_pairing("x")
    store.redeem(code)
    with pytest.raises(InvalidCode):
        store.redeem(code)


def test_a_code_expires_after_ten_minutes(store, clock):
    code = store.create_pairing("x")
    clock.now += 600
    with pytest.raises(InvalidCode):
        store.redeem(code)


def test_a_code_still_works_just_before_expiry(store, clock):
    code = store.create_pairing("x")
    clock.now += 599
    store.redeem(code)


def test_five_wrong_guesses_void_the_code(store):
    code = store.create_pairing("x")
    for _ in range(5):
        with pytest.raises(InvalidCode):
            store.redeem("ZZZZ-ZZZZ" if code != "ZZZZ-ZZZZ" else "YYYY-YYYY")
    with pytest.raises(InvalidCode):
        store.redeem(code)


def test_four_wrong_guesses_leave_the_code_usable(store):
    code = store.create_pairing("x")
    wrong = "ZZZZ-ZZZZ" if code != "ZZZZ-ZZZZ" else "YYYY-YYYY"
    for _ in range(4):
        with pytest.raises(InvalidCode):
            store.redeem(wrong)
    store.redeem(code)


def test_a_new_code_replaces_the_pending_one(store):
    old = store.create_pairing("old")
    new = store.create_pairing("new")
    assert old != new  # 32^-8 chance of a false failure
    with pytest.raises(InvalidCode):
        store.redeem(old)
    store.redeem(new)
    assert [d.name for d in store.devices()] == ["new"]


def test_no_pending_code_is_invalid(store):
    with pytest.raises(InvalidCode):
        store.redeem("ABCD-EFGH")


def test_second_device_keeps_the_first(store):
    first = store.redeem(store.create_pairing("phone"))
    second = store.redeem(store.create_pairing("mac"))
    assert store.is_paired(first) and store.is_paired(second)
    mac = next(d for d in store.devices() if d.name == "mac")
    assert store.revoke(mac.id)
    assert store.is_paired(first) and not store.is_paired(second)


def test_revoke_unknown_id_is_false(store):
    store.redeem(store.create_pairing("x"))
    assert store.revoke("00000000") is False
    assert len(store.devices()) == 1


def test_file_holds_hashes_only_and_is_private(store, path):
    code = store.create_pairing("x")
    raw_pending = path.read_text()
    assert code not in raw_pending and code.replace("-", "") not in raw_pending
    token = store.redeem(code)
    assert token not in path.read_text()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700


def test_missing_file_means_no_devices(store, path):
    assert not path.exists()
    assert store.devices() == []
    assert not store.is_paired("anything")


def test_deleting_the_file_unpairs_everyone(store, path):
    token = store.redeem(store.create_pairing("x"))
    assert store.is_paired(token)  # now cached
    path.unlink()
    assert not store.is_paired(token)


def test_a_revoke_by_another_process_is_seen_at_once(path, clock):
    # The server and the CLI each hold their own DeviceStore on the same file.
    server, cli = DeviceStore(path, clock=clock), DeviceStore(path, clock=clock)
    token = cli.redeem(cli.create_pairing("x"))
    assert server.is_paired(token)
    cli.revoke(cli.devices()[0].id)
    assert not server.is_paired(token)


@pytest.mark.parametrize("content", ["not json", "[]", '{"devices": "x"}',
                                     '{"devices": [{"id": 1}], "pending": null}',
                                     '{"devices": [], "pending": {"code_sha256": "x"}}'])
def test_corrupt_file_refuses_everything_and_is_not_overwritten(path, clock, content):
    path.parent.mkdir(parents=True)
    path.write_text(content)
    store = DeviceStore(path, clock=clock)
    with pytest.raises(StoreCorrupt):
        store.is_paired("x")
    with pytest.raises(StoreCorrupt):
        store.devices()
    with pytest.raises(StoreCorrupt):
        store.create_pairing("x")
    with pytest.raises(StoreCorrupt):
        store.redeem("ABCD-EFGH")
    with pytest.raises(StoreCorrupt):
        store.revoke("00000000")
    assert path.read_text() == content


def test_two_devices_racing_for_one_code_only_one_wins(path, clock):
    code = DeviceStore(path, clock=clock).create_pairing("x")
    start = threading.Barrier(2)
    results: list[str] = []

    def race():
        start.wait()
        try:
            DeviceStore(path, clock=clock).redeem(code)
            results.append("ok")
        except InvalidCode:
            results.append("invalid")

    threads = [threading.Thread(target=race) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(results) == ["invalid", "ok"]
    assert len(json.loads(path.read_text())["devices"]) == 1
