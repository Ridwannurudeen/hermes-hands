import asyncio
import base64
import json

import pytest

from hands import messages, protocol, relay, store
from hands_fakes import LOCAL, Device, Principal, hands_db  # noqa: F401  (fixture)

SEED = "0f" * 32


@pytest.fixture
def hub(hands_db, monkeypatch):
    monkeypatch.setattr(relay, "_TICK", 0.01)
    return relay.Relay(relay.derive_keys(SEED))


def run(coro):
    return asyncio.run(coro)


def body(reply):
    return json.loads(reply.body)


def verified(hub, reply, path, nonce=None):
    """The device's check of a signed reply; returns the parsed body."""
    headers = reply.headers
    assert protocol.verify_request(
        hub._keys.relay_public,
        headers["X-Hands-Signature"],
        protocol.R2D,
        int(headers["X-Hands-Ts"]),
        headers["X-Hands-Nonce"],
        "POST",
        path,
        reply.body,
    )
    if nonce is not None:
        assert headers["X-Hands-Nonce"] == nonce
    return json.loads(reply.body)


def local(hub, handler, payload, headers=None):
    return run(
        getattr(hub, handler)(
            LOCAL, headers or {}, relay.API + "/local/x", json.dumps(payload).encode()
        )
    )


def device_call(hub, device, handler, route, payload, **kwargs):
    principal, headers, path, raw = device.request(route, payload, **kwargs)
    return run(getattr(hub, handler)(principal, headers, path, raw)), path, headers


def post_result(hub, device, job_id, seq, chunk="", done=False, **extra):
    payload = {
        "job_id": job_id,
        "seq": seq,
        "chunk": chunk,
        "done": done,
        "policy_hash": device.policy_hash,
        **extra,
    }
    reply, path, _ = device_call(hub, device, "device_result", "/device/result", payload)
    return reply, path


def test_derive_keys_is_stable_and_separates_labels():
    a, b = relay.derive_keys(SEED), relay.derive_keys(SEED)
    assert a == b and a != relay.derive_keys("1" + SEED[1:])
    assert len(a.relay_private) == 32 and len(a.relay_public) == 32
    assert protocol.verify(a.relay_public, protocol.sign(a.relay_private, b"m"), b"m")
    assert protocol.unb64u(a.local_bearer) != a.relay_private


def test_exec_round_trip_in_seq_order(hub):
    device = Device()
    job_id = body(
        local(hub, "local_exec", {"device": "laptop", "script": "echo hi", "timeout": 30})
    )["job_id"]
    reply, path, headers = device_call(hub, device, "device_poll", "/device/poll", {"wait": 1})
    bearer_nonce = protocol.parse_bearer(headers["authorization"].split(" ", 1)[1]).nonce
    job = verified(hub, reply, path, nonce=bearer_nonce)
    assert job == {
        "job_id": job_id,
        "type": "exec",
        "script": "echo hi",
        "login": False,
        "timeout": 30,
    }
    assert post_result(hub, device, job_id, 1, "world")[0].status == 200  # out of order: buffered
    reply, path = post_result(hub, device, job_id, 0, "hello ")
    assert verified(hub, reply, path) == {"ok": True, "cancel": False}
    first = body(local(hub, "local_output", {"job_id": job_id, "after": 0, "wait": 0}))
    assert (first["chunks"], first["next"], first["done"]) == (["hello ", "world"], 2, False)
    post_result(hub, device, job_id, 2, "", done=True, returncode=3)
    post_result(hub, device, job_id, 2, "dup", done=True, returncode=9)
    last = body(local(hub, "local_output", {"job_id": job_id, "after": 2, "wait": 1}))
    assert (last["chunks"], last["done"], last["returncode"], last["error"]) == (
        [""],
        True,
        3,
        None,
    )
    gone = local(hub, "local_output", {"job_id": job_id, "after": 3, "wait": 0})
    assert (gone.status, body(gone)["detail"]) == (404, messages.RELAY_LOST)


def test_poll_times_out_with_204(hub):
    reply, _, _ = device_call(hub, Device(), "device_poll", "/device/poll", {"wait": 0})
    assert (reply.status, reply.body) == (204, b"")


def test_device_request_signature_is_checked(hub):
    device = Device()
    reply, path, _ = device_call(
        hub, device, "device_poll", "/device/poll", {"wait": 0}, tamper=True
    )
    assert reply.status == 401 and verified(hub, reply, path)["error"] == "bad_signature"
    principal, headers, path, raw = device.request("/device/poll", {"wait": 0})
    other = Principal("someone-else", scopes=(relay.SCOPE_DEVICE,))
    assert run(hub.device_poll(other, headers, path, raw)).status == 401
    assert run(hub.device_poll(None, headers, path, raw)).status == 401
    assert run(hub.device_poll(LOCAL, headers, path, raw)).status == 401


def test_unknown_device_is_told_so_signed(hub):
    device = Device()
    store.revoke("laptop")
    principal, headers, path, raw = device.request(
        "/device/heartbeat", {"policy_hash": "p0", "os": "x", "home": "y"}
    )
    unknown = Principal(device.device_id, scopes=(relay.SCOPE_UNKNOWN,))
    reply = run(hub.device_heartbeat(unknown, headers, path, raw))
    assert reply.status == 401 and verified(hub, reply, path)["error"] == "unknown_device"
    reply = run(
        hub.device_heartbeat(principal, headers, path, raw)
    )  # revoked between auth and handler
    assert reply.status == 401 and verified(hub, reply, path)["error"] == "unknown_device"


def test_local_routes_refuse_forwarded_and_foreign_principals(hub):
    Device()
    payload = json.dumps({"device": "laptop", "script": "true", "timeout": 5}).encode()
    for header in ("x-forwarded-for", "forwarded"):
        reply = run(hub.local_exec(LOCAL, {header: "1.2.3.4"}, relay.API + "/local/exec", payload))
        assert (reply.status, body(reply)["error"]) == (403, "forwarded")
    device_principal = Principal("abc", scopes=(relay.SCOPE_DEVICE,))
    assert (
        run(hub.local_exec(device_principal, {}, relay.API + "/local/exec", payload)).status == 401
    )
    foreign = Principal("local", provider="basic", scopes=(relay.SCOPE_LOCAL,))
    assert run(hub.local_state(foreign, {}, relay.API + "/local/state", b"{}")).status == 401


def test_exec_refused_at_once_for_unknown_offline_and_policy(hub, monkeypatch):
    device = Device()
    missing = local(hub, "local_exec", {"device": "desk", "script": "true", "timeout": 5})
    assert (missing.status, body(missing)["detail"]) == (404, messages.unknown_device("desk"))
    monkeypatch.setattr(store, "OFFLINE_AFTER", -1)
    offline = local(hub, "local_exec", {"device": "laptop", "script": "true", "timeout": 5})
    assert (offline.status, body(offline)["detail"]) == (409, messages.offline("laptop"))
    monkeypatch.setattr(store, "OFFLINE_AFTER", 20)
    store.record_seen(device.device_id, policy_hash="changed")
    refused = local(hub, "local_exec", {"device": "laptop", "script": "true", "timeout": 5})
    assert (refused.status, body(refused)["detail"]) == (409, messages.policy_changed("laptop"))


def test_policy_change_fails_running_job_and_refuses_device(hub):
    device = Device()
    job_id = body(
        local(hub, "local_exec", {"device": "laptop", "script": "sleep 9", "timeout": 30})
    )["job_id"]
    device_call(hub, device, "device_poll", "/device/poll", {"wait": 0})
    device.policy_hash = "edited"
    reply, path = post_result(hub, device, job_id, 0, "x")
    assert reply.status == 409 and verified(hub, reply, path)["error"] == "policy_changed"
    out = body(local(hub, "local_output", {"job_id": job_id, "after": 0, "wait": 0}))
    assert (out["done"], out["returncode"], out["error"]) == (
        True,
        255,
        messages.policy_changed("laptop"),
    )
    reply, path, _ = device_call(hub, device, "device_poll", "/device/poll", {"wait": 0})
    assert reply.status == 409
    store.accept_policy("laptop")
    assert device_call(hub, device, "device_poll", "/device/poll", {"wait": 0})[0].status == 204


def test_cancel_queues_a_cancel_job_and_drops_late_output(hub):
    device = Device()
    job_id = body(
        local(hub, "local_exec", {"device": "laptop", "script": "sleep 9", "timeout": 30})
    )["job_id"]
    device_call(hub, device, "device_poll", "/device/poll", {"wait": 0})
    assert body(local(hub, "local_cancel", {"job_id": job_id})) == {"ok": True}
    reply, path, _ = device_call(hub, device, "device_poll", "/device/poll", {"wait": 0})
    cancel = verified(hub, reply, path)
    assert (cancel["type"], cancel["target_job_id"]) == ("cancel", job_id)
    reply, path = post_result(hub, device, job_id, 0, "late")
    assert verified(hub, reply, path) == {"ok": True, "cancel": True}
    out = body(local(hub, "local_output", {"job_id": job_id, "after": 0, "wait": 0}))
    assert (out["done"], out["returncode"], out["chunks"]) == (True, relay.CANCELLED, [])


def test_cancel_before_pickup_never_reaches_the_device(hub):
    device = Device()
    job_id = body(local(hub, "local_exec", {"device": "laptop", "script": "true", "timeout": 5}))[
        "job_id"
    ]
    local(hub, "local_cancel", {"job_id": job_id})
    assert device_call(hub, device, "device_poll", "/device/poll", {"wait": 0})[0].status == 204


def test_revoke_mid_command_fails_the_wait(hub):
    device = Device()
    job_id = body(
        local(hub, "local_exec", {"device": "laptop", "script": "sleep 9", "timeout": 30})
    )["job_id"]
    device_call(hub, device, "device_poll", "/device/poll", {"wait": 0})
    store.revoke("laptop")
    out = body(local(hub, "local_output", {"job_id": job_id, "after": 0, "wait": 5}))
    assert (out["done"], out["returncode"], out["error"]) == (True, 255, messages.revoked("laptop"))


def test_device_going_offline_fails_the_wait(hub, monkeypatch):
    device = Device()
    job_id = body(
        local(hub, "local_exec", {"device": "laptop", "script": "sleep 9", "timeout": 30})
    )["job_id"]
    device_call(hub, device, "device_poll", "/device/poll", {"wait": 0})
    post_result(hub, device, job_id, 0, "partial")
    first = body(local(hub, "local_output", {"job_id": job_id, "after": 0, "wait": 5}))
    assert (first["chunks"], first["done"]) == (["partial"], False)
    monkeypatch.setattr(store, "OFFLINE_AFTER", -1)
    out = body(local(hub, "local_output", {"job_id": job_id, "after": first["next"], "wait": 5}))
    assert (out["chunks"], out["returncode"], out["error"]) == (
        [],
        255,
        messages.went_offline("laptop"),
    )


def test_screenshot_job_and_size_cap(hub):
    device = Device()
    job_id = body(local(hub, "local_exec", {"device": "laptop", "type": "screenshot"}))["job_id"]
    reply, path, _ = device_call(hub, device, "device_poll", "/device/poll", {"wait": 0})
    assert verified(hub, reply, path) == {"job_id": job_id, "type": "screenshot"}
    png = base64.b64encode(b"\x89PNG fake").decode()
    post_result(hub, device, job_id, 0, done=True, png_b64=png)
    assert (
        body(local(hub, "local_output", {"job_id": job_id, "after": 0, "wait": 0}))["png_b64"]
        == png
    )
    job_id = body(local(hub, "local_exec", {"device": "laptop", "type": "screenshot"}))["job_id"]
    device_call(hub, device, "device_poll", "/device/poll", {"wait": 0})
    reply, path = post_result(
        hub, device, job_id, 0, done=True, png_b64="A" * (relay._MAX_PNG_B64 + 4)
    )
    assert reply.status == 413
    out = body(local(hub, "local_output", {"job_id": job_id, "after": 0, "wait": 0}))
    assert (out["returncode"], out["error"]) == (255, messages.SCREENSHOT_TOO_LARGE)


def test_abandoned_jobs_are_cancelled(hub, monkeypatch):
    device = Device()
    job_id = body(
        local(hub, "local_exec", {"device": "laptop", "script": "sleep 99", "timeout": 120})
    )["job_id"]
    device_call(hub, device, "device_poll", "/device/poll", {"wait": 0})
    monkeypatch.setattr(relay, "_ABANDONED_AFTER", -1)
    hub._reap()
    reply, path, _ = device_call(hub, device, "device_poll", "/device/poll", {"wait": 0})
    assert verified(hub, reply, path)["target_job_id"] == job_id
    assert local(hub, "local_output", {"job_id": job_id, "after": 0, "wait": 0}).status == 404


def pair_request(device_private, device_public, code, **overrides):
    fields = {
        "name": "laptop",
        "os": "Windows-11",
        "home": "C:\\Users\\r",
        "public_key": protocol.b64u(device_public),
        "policy_hash": "p0",
        "protocol": protocol.PROTOCOL_VERSION,
        **overrides,
    }
    raw = protocol.canonical_json(
        {
            **fields,
            "sig": protocol.b64u(protocol.sign(device_private, protocol.canonical_json(fields))),
        }
    )
    return (
        Principal("pairing:laptop", scopes=(relay.SCOPE_PAIR,)),
        {"authorization": f"Bearer {code}"},
        relay.API + "/pair",
        raw,
    )


def test_pair_happy_path_and_refusals(hub):
    private, public = protocol.generate_keypair()
    code = store.mint_pairing_code("laptop")
    reply = run(hub.pair(*pair_request(private, public, code, protocol="hands/0")))
    assert (
        reply.status == 426
        and verified(hub, reply, relay.API + "/pair")["upgrade"] == messages.UPGRADE
    )
    principal, headers, path, raw = pair_request(private, public, code)
    forged = raw.replace(b'"p0"', b'"p9"')
    assert run(hub.pair(principal, headers, path, forged)).status == 401
    assert (
        run(hub.pair(Principal("x", scopes=(relay.SCOPE_DEVICE,)), headers, path, raw)).status
        == 401
    )
    reply = run(hub.pair(principal, headers, path, raw))
    paired = verified(hub, reply, path)
    assert (
        reply.status == 200
        and protocol.unb64u(paired["relay_public_key"]) == hub._keys.relay_public
    )
    assert store.device_by_id(paired["device_id"])["public_key"] == public
    reused = run(hub.pair(principal, headers, path, raw))
    assert (reused.status, body(reused)["error"]) == (401, "bad_code")
    second = store.mint_pairing_code("laptop")
    taken = run(hub.pair(*pair_request(private, public, second)))
    assert (taken.status, body(taken)["detail"]) == (409, messages.pair_name_taken("laptop"))
    assert run(hub.pair(principal, headers, path, b"not json")).status == 400


def test_state_lists_devices(hub):
    Device()
    state = body(local(hub, "local_state", {}))
    assert state["relay"] is True and [d["name"] for d in state["devices"]] == ["laptop"]


def test_serve_refuses_without_seed_or_outside_dashboard(hands_db, monkeypatch):
    monkeypatch.setattr(relay, "_relay", None)
    started = []
    monkeypatch.setattr(relay.Relay, "start", lambda self: started.append(self))
    assert body(relay.serve(None, here=True))["detail"] == messages.NOT_SET_UP
    assert (
        body(relay.serve(relay.derive_keys(SEED), here=False))["detail"] == messages.RELAY_ELSEWHERE
    )
    assert started == []
    first = relay.serve(relay.derive_keys(SEED), here=True)
    assert relay.serve(relay.derive_keys(SEED), here=True) is first and started == [first]


def test_start_runs_the_heartbeat_on_a_daemon_thread(hands_db, monkeypatch):
    threads = []

    class FakeThread:
        def __init__(self, *, target, name, daemon):
            threads.append((target, name, daemon))

        def start(self):
            pass

    monkeypatch.setattr(relay.threading, "Thread", FakeThread)
    hub = relay.Relay(relay.derive_keys(SEED))
    hub.start()
    assert threads == [(hub._heartbeat_loop, "hermes-hands-relay", True)]
    assert store.relay_alive() is False
    hub._beat()
    assert store.relay_alive() is True
