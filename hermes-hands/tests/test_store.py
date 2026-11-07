import sqlite3
import time

import pytest

from hands import protocol, store
from hands_fakes import hands_db  # noqa: F401  (fixture)


def _pair(name="laptop", policy_hash="p0"):
    code = store.mint_pairing_code(name)
    return store.complete_pairing(
        code,
        name=name,
        public_key=protocol.b64u(b"k" * 32),
        os="Linux",
        home="/home/r",
        policy_hash=policy_hash,
    )


def test_pairing_code_is_single_use(hands_db):
    code = store.mint_pairing_code("laptop")
    assert store.pairing_code_name(code) == "laptop"
    device_id = store.complete_pairing(
        code,
        name="laptop",
        public_key=protocol.b64u(b"k" * 32),
        os="Linux",
        home="/home/r",
        policy_hash="p0",
    )
    assert len(device_id) == 16
    assert store.pairing_code_name(code) is None
    with pytest.raises(store.PairingError) as burned:
        store.complete_pairing(
            code, name="laptop", public_key="x", os="Linux", home="/", policy_hash="p0"
        )
    assert (burned.value.status, burned.value.error) == (401, "bad_code")


def test_expired_code_is_refused(hands_db):
    code = store.mint_pairing_code("laptop", ttl=-1)
    assert store.pairing_code_name(code) is None
    with pytest.raises(store.PairingError) as expired:
        store.complete_pairing(
            code, name="laptop", public_key="x", os="Linux", home="/", policy_hash="p0"
        )
    assert expired.value.status == 401


def test_name_rules(hands_db):
    _pair("laptop")
    with pytest.raises(store.PairingError) as taken:
        _pair("laptop")
    assert (taken.value.status, taken.value.error, taken.value.name) == (
        409,
        "name_taken",
        "laptop",
    )
    code = store.mint_pairing_code("desk")
    with pytest.raises(store.PairingError) as mismatch:
        store.complete_pairing(
            code, name="other", public_key="x", os="Linux", home="/", policy_hash="p0"
        )
    assert (mismatch.value.error, mismatch.value.name) == ("name_mismatch", "desk")
    assert store.pairing_code_name(code) == "desk"  # a refused pairing does not burn the code


def test_device_listing_and_revoke_frees_the_name(hands_db):
    device_id = _pair("laptop")
    [listed] = store.list_devices()
    assert listed == {**store.get_device("laptop")}
    assert set(listed) == {"device_id", "name", "os", "home", "last_seen", "online", "policy_ok"}
    assert listed["online"] and listed["policy_ok"] and listed["device_id"] == device_id
    assert store.device_by_id(device_id)["public_key"] == b"k" * 32
    assert store.revoked_key(device_id) is None
    assert store.revoke("laptop") is True
    assert store.revoke("laptop") is False
    assert store.get_device("laptop") is None and store.device_by_id(device_id) is None
    assert store.list_devices() == []
    assert store.revoked_key(device_id) == b"k" * 32  # the tombstone keeps the key
    again = _pair("laptop")
    assert [d["device_id"] for d in store.list_devices()] == [again]
    assert store.revoked_key(again) is None and store.revoked_key(device_id) == b"k" * 32


def test_revoke_keeps_a_tombstone_in_a_hands_db_from_before_tombstones(hands_db):
    """An existing hands.db (no revoked_devices table) is migrated in place on first use."""
    conn = sqlite3.connect(hands_db / "hands.db")
    conn.execute(
        "CREATE TABLE devices (device_id TEXT PRIMARY KEY, name TEXT NOT NULL UNIQUE,"
        " public_key TEXT NOT NULL, os TEXT NOT NULL, home TEXT NOT NULL,"
        " policy_hash TEXT NOT NULL, reported_policy_hash TEXT NOT NULL,"
        " policy_ok INTEGER NOT NULL DEFAULT 1, paired_at INTEGER NOT NULL,"
        " last_seen INTEGER NOT NULL)"
    )
    conn.execute(
        "INSERT INTO devices VALUES ('d1', 'laptop', ?, 'Linux', '/h', 'p0', 'p0', 1, 0, 0)",
        (protocol.b64u(b"k" * 32),),
    )
    conn.commit()
    conn.close()
    assert store.get_device("laptop")["device_id"] == "d1"
    assert store.revoke("laptop") is True
    assert store.revoked_key("d1") == b"k" * 32 and store.get_device("laptop") is None


def test_offline_after_last_seen(hands_db, monkeypatch):
    _pair("laptop")
    monkeypatch.setattr(store, "OFFLINE_AFTER", -1)
    assert store.get_device("laptop")["online"] is False


def test_policy_change_refuses_until_accepted(hands_db):
    device_id = _pair("laptop", policy_hash="p0")
    assert store.record_seen(device_id, policy_hash="p0", os="Windows", home="C:\\Users\\r") is True
    assert store.get_device("laptop")["os"] == "Windows"
    assert store.record_seen(device_id, policy_hash="p1") is False
    assert store.record_seen(device_id, policy_hash="p0") is False  # stays refused until accepted
    assert store.record_seen(device_id, policy_hash="p1") is False
    assert store.accept_policy("laptop") is True
    assert store.get_device("laptop")["policy_ok"] is True
    assert store.record_seen(device_id, policy_hash="p1") is True
    assert store.record_seen("nope") is None
    assert store.accept_policy("nope") is False


def test_session_targets(hands_db):
    assert store.get_session_target("s1") is None
    store.set_session_target("s1", "laptop")
    store.set_session_target("s1", "server")
    store.set_session_target("s2", "laptop")
    assert (store.get_session_target("s1"), store.get_session_target("s2")) == ("server", "laptop")


def test_session_last_run(hands_db):
    assert store.get_last_run("s1") is None
    store.set_last_run("s1", "laptop")
    store.set_last_run("s1", "server")
    assert (store.get_last_run("s1"), store.get_last_run("s2")) == ("server", None)


def test_pairing_codes_are_matched_with_compare_digest(hands_db, monkeypatch):
    codes = [store.mint_pairing_code(name) for name in ("a", "b", "c")]
    calls = []
    real = store.hmac.compare_digest

    def spy(left, right):
        calls.append((left, right))
        return real(left, right)

    monkeypatch.setattr(store.hmac, "compare_digest", spy)
    assert store.pairing_code_name(codes[1]) == "b"
    assert len(calls) == 3  # every live candidate, not an SQL equality on the hash
    calls.clear()
    assert store.pairing_code_name("not-a-code") is None and len(calls) == 3
    calls.clear()
    store.complete_pairing(
        codes[2], name="c", public_key="AA", os="Linux", home="/", policy_hash="p"
    )
    assert len(calls) == 3
    assert store.pairing_code_name(codes[2]) is None and store.pairing_code_name(codes[0]) == "a"


def test_relay_heartbeat(hands_db, monkeypatch):
    assert store.relay_alive() is False
    store.write_relay_heartbeat()
    assert store.relay_alive() is True
    monkeypatch.setattr(time, "time", lambda: 10**10)
    assert store.relay_alive() is False
