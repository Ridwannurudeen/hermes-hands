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
    assert store.revoke("laptop") is True
    assert store.revoke("laptop") is False
    assert store.get_device("laptop") is None and store.device_by_id(device_id) is None
    _pair("laptop")


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


def test_session_marks(hands_db):
    assert store.get_session_marks("s1") == (None, None)
    store.set_session_marks("s1", "laptop", None)
    store.set_session_marks("s1", "server", "server")
    assert store.get_session_marks("s1") == ("server", "server")
    assert store.get_session_marks("s2") == (None, None)


def test_relay_heartbeat(hands_db, monkeypatch):
    assert store.relay_alive() is False
    store.write_relay_heartbeat()
    assert store.relay_alive() is True
    monkeypatch.setattr(time, "time", lambda: 10**10)
    assert store.relay_alive() is False
