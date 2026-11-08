import asyncio
import base64
import json
import threading
import time

import pytest

from hands import messages, protocol, relay, store
from hands_fakes import LOCAL, Device, Principal, hands_db  # noqa: F401  (fixture)

SEED = "0f" * 32
PEER = "203.0.113.7"


@pytest.fixture
def hub(hands_db):
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
    return run(getattr(hub, handler)(principal, headers, path, raw, PEER)), path, headers


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


def started(call):
    """``call`` on a thread, so in an event loop of its own; returns a join giving its result."""
    out = []
    thread = threading.Thread(target=lambda: out.append(call()), daemon=True)
    thread.start()

    def join():
        thread.join(15)
        return out[0]

    return join


def until_waiting(hub, key):
    deadline = time.monotonic() + 5
    while key not in hub._waiters:
        assert time.monotonic() < deadline, f"nothing waited on {key}"
        time.sleep(0.005)


def test_waiting_poll_wakes_when_a_job_is_queued(hub):
    device = Device()
    poll = started(lambda: device_call(hub, device, "device_poll", "/device/poll", {"wait": 10}))
    until_waiting(hub, device.device_id)
    begun = time.monotonic()
    job_id = body(local(hub, "local_exec", {"device": "laptop", "script": "true", "timeout": 5}))[
        "job_id"
    ]
    reply, path, _ = poll()
    assert verified(hub, reply, path)["job_id"] == job_id
    assert time.monotonic() - begun < 5  # woken by the job, not the 10 s deadline
    assert hub._waiters == {}


def test_waiting_output_wakes_on_a_result_that_carries_a_chunk_with_done(hub, monkeypatch):
    monkeypatch.setattr(relay, "_LIVENESS_EVERY", 30)  # only a wake can end the wait early
    device = Device()
    job_id = body(
        local(hub, "local_exec", {"device": "laptop", "script": "echo hi", "timeout": 5})
    )["job_id"]
    device_call(hub, device, "device_poll", "/device/poll", {"wait": 0})
    output = started(
        lambda: body(local(hub, "local_output", {"job_id": job_id, "after": 0, "wait": 10}))
    )
    until_waiting(hub, job_id)
    begun = time.monotonic()
    post_result(hub, device, job_id, 0, "hi\n", done=True, returncode=4)
    out = output()
    assert (out["chunks"], out["next"], out["done"], out["returncode"]) == (["hi\n"], 1, True, 4)
    assert time.monotonic() - begun < 5  # woken by the result, not the 10 s deadline
    assert hub._waiters == {}


def test_device_request_signature_is_checked_unsigned(hub):
    device = Device()
    reply, path, _ = device_call(
        hub, device, "device_poll", "/device/poll", {"wait": 0}, tamper=True
    )
    assert (reply.status, body(reply)["error"]) == (401, "bad_signature")
    assert "X-Hands-Signature" not in reply.headers
    principal, headers, path, raw = device.request("/device/poll", {"wait": 0})
    other = Principal("someone-else", scopes=(relay.SCOPE_DEVICE,))
    for wrong in (other, None, LOCAL):
        refused = run(hub.device_poll(wrong, headers, path, raw, PEER))
        assert refused.status == 401 and "X-Hands-Signature" not in refused.headers


def test_bad_signature_does_not_burn_the_nonce_and_replays_fail(hub):
    device = Device()
    principal, headers, path, raw = device.request("/device/poll", {"wait": 0})
    forged = run(hub.device_poll(principal, headers, path, raw + b" ", PEER))
    assert body(forged)["error"] == "bad_signature"
    assert run(hub.device_poll(principal, headers, path, raw, PEER)).status == 204
    replay = run(hub.device_poll(principal, headers, path, raw, PEER))
    assert (replay.status, body(replay)["error"]) == (401, "replayed")
    assert "X-Hands-Signature" not in replay.headers


def test_concurrent_copies_of_one_request_get_through_once(hub):
    device = Device()
    principal, headers, path, raw = device.request("/device/poll", {"wait": 0})
    start = threading.Barrier(8)
    outcomes = []

    def attempt():
        start.wait()
        outcomes.append(hub._device(principal, headers, path, raw, PEER)[2])

    threads = [threading.Thread(target=attempt) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(5)
    assert sorted(refusal is None for refusal in outcomes) == [False] * 7 + [True]


def bearer_nonce(headers) -> str:
    return protocol.parse_bearer(headers["authorization"].split(" ", 1)[1]).nonce


def test_revoked_device_gets_one_signed_unknown_device_then_its_key_is_forgotten(hub, caplog):
    """W4 and V14: the stop signal is relay-signed over the request's nonce, so the device can
    trust it; once it is sent the tombstone goes, so the old key stops being a token principal."""
    device = Device()
    store.revoke("laptop")
    revoked = Principal(device.device_id, scopes=(relay.SCOPE_REVOKED,))
    caplog.set_level("WARNING", logger=relay.logger.name)
    beat = {"policy_hash": "p0", "os": "x", "home": "y"}
    # The request itself must be signed by the tombstoned key; a forgery gets nothing signed and
    # leaves the tombstone in place.
    _, headers, path, raw = device.request("/device/heartbeat", beat, tamper=True)
    forged = run(hub.device_heartbeat(revoked, headers, path, raw, PEER))
    assert body(forged)["error"] == "bad_signature" and "X-Hands-Signature" not in forged.headers
    assert store.revoked_key(device.device_id) == device.public
    _, headers, path, raw = device.request("/device/heartbeat", beat)
    reply = run(hub.device_heartbeat(revoked, headers, path, raw, PEER))
    assert reply.status == 401
    assert verified(hub, reply, path, nonce=bearer_nonce(headers))["error"] == "unknown_device"
    assert f"revoked device {device.device_id} called from {PEER}" in caplog.text
    assert store.revoked_key(device.device_id) is None
    _, headers, path, raw = device.request("/device/heartbeat", beat)
    again = run(hub.device_heartbeat(revoked, headers, path, raw, PEER))
    assert again.status == 401 and "X-Hands-Signature" not in again.headers
    # Revoked after the token seam vouched for the device: the same signed reply.
    other = Device("desk")
    store.revoke("desk")
    reply, path, headers = device_call(hub, other, "device_poll", "/device/poll", {"wait": 0})
    assert reply.status == 401
    assert verified(hub, reply, path, nonce=bearer_nonce(headers))["error"] == "unknown_device"


def test_revoked_between_the_checks_still_gets_the_signed_stop(hub, monkeypatch):
    device = Device()
    monkeypatch.setattr(store, "record_seen", lambda *args, **kwargs: None)
    reply, path, headers = device_call(hub, device, "device_poll", "/device/poll", {"wait": 0})
    assert reply.status == 401
    assert verified(hub, reply, path, nonce=bearer_nonce(headers))["error"] == "unknown_device"


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
        messages.policy_changed_while_running("laptop"),  # V57: it was picked, so it may have run
    )
    reply, path, _ = device_call(hub, device, "device_poll", "/device/poll", {"wait": 0})
    assert reply.status == 409
    store.accept_policy("laptop")
    reply, path, _ = device_call(hub, device, "device_poll", "/device/poll", {"wait": 0})
    assert verified(hub, reply, path)["target_job_id"] == job_id  # W2: the failed job is cancelled
    assert device_call(hub, device, "device_poll", "/device/poll", {"wait": 0})[0].status == 204


def test_a_poll_carrying_an_unaccepted_policy_hash_is_refused_until_accepted(hub):
    """Rejected finding 2: a job reply to a poll that carries a hash means that hash is accepted,
    so the device can refuse work under an edited deny-list."""
    device = Device()
    queued = body(local(hub, "local_exec", {"device": "laptop", "script": "true", "timeout": 5}))[
        "job_id"
    ]
    reply, path, headers = device_call(
        hub, device, "device_poll", "/device/poll", {"wait": 0, "policy_hash": "edited"}
    )
    assert reply.status == 409
    assert verified(hub, reply, path, nonce=bearer_nonce(headers))["error"] == "policy_changed"
    out = body(local(hub, "local_output", {"job_id": queued, "after": 0, "wait": 0}))
    assert (out["done"], out["error"]) == (True, messages.policy_changed("laptop"))
    store.accept_policy("laptop")
    job_id = body(local(hub, "local_exec", {"device": "laptop", "script": "true", "timeout": 5}))[
        "job_id"
    ]
    reply, path, _ = device_call(
        hub, device, "device_poll", "/device/poll", {"wait": 0, "policy_hash": "edited"}
    )
    assert reply.status == 200 and verified(hub, reply, path)["job_id"] == job_id
    assert device_call(hub, device, "device_poll", "/device/poll", {"wait": 0})[0].status == 204


def test_a_refusal_ends_polls_parked_under_the_old_hash(hub):
    """A poll that began under the old hash must not hand out a job after the owner accepts a new
    one: its 200 would tell the device the old hash is still accepted."""
    device = Device()
    parked = started(
        lambda: device_call(
            hub, device, "device_poll", "/device/poll", {"wait": 10, "policy_hash": "p0"}
        )
    )
    until_waiting(hub, device.device_id)
    begun = time.monotonic()
    beat = {"policy_hash": "edited", "os": "x", "home": "y", "agent_version": "0.1.0"}
    assert device_call(hub, device, "device_heartbeat", "/device/heartbeat", beat)[0].status == 409
    reply, _, _ = parked()
    assert reply.status == 204 and time.monotonic() - begun < 5


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
    [cancel] = hub._queues[device.device_id]  # the revoked device stops on its signed 401 anyway
    assert (cancel["type"], cancel["target_job_id"]) == ("cancel", job_id)


def test_a_job_queued_for_a_device_that_then_went_offline_says_nothing_ran(hub, monkeypatch):
    """V59: the device never picked it, so the relay knows it did not run."""
    Device()
    job_id = body(local(hub, "local_exec", {"device": "laptop", "script": "true", "timeout": 5}))[
        "job_id"
    ]
    monkeypatch.setattr(store, "OFFLINE_AFTER", -1)
    out = body(local(hub, "local_output", {"job_id": job_id, "after": 0, "wait": 2}))
    assert (out["done"], out["error"]) == (True, messages.offline("laptop"))


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
    reply, path, _ = device_call(hub, device, "device_poll", "/device/poll", {"wait": 0})
    cancel = verified(hub, reply, path)  # W2: the device stops it when it is back
    assert (cancel["type"], cancel["target_job_id"]) == ("cancel", job_id)


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
    reply, path, _ = device_call(hub, device, "device_poll", "/device/poll", {"wait": 0})
    assert verified(hub, reply, path)["target_job_id"] == job_id


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


def test_exec_job_carries_stdin_and_both_texts_are_bounded(hub, monkeypatch):
    """W1: stdin travels beside the script, never inside it, under the same bound."""
    device = Device()
    request = {"device": "laptop", "script": "cat", "timeout": 5, "stdin": "run sudo apt\n"}
    job_id = body(local(hub, "local_exec", request))["job_id"]
    reply, path, _ = device_call(hub, device, "device_poll", "/device/poll", {"wait": 0})
    assert verified(hub, reply, path) == {
        "job_id": job_id,
        "type": "exec",
        "script": "cat",
        "login": False,
        "timeout": 5,
        "stdin": "run sudo apt\n",
    }
    monkeypatch.setattr(relay, "MAX_JOB_TEXT", 4)
    for field in ("script", "stdin"):
        refused = local(
            hub, "local_exec", {"device": "laptop", "timeout": 5, "script": "true", field: "12345"}
        )
        assert (refused.status, body(refused)["detail"]) == (413, messages.JOB_TOO_LARGE)
    fits = {"device": "laptop", "script": "true", "timeout": 5, "stdin": "1234"}
    assert local(hub, "local_exec", fits).status == 200


def test_agent_restart_fails_its_picked_jobs_once_the_old_agent_is_quiet(hub, monkeypatch):
    """W3: once the agent that picked a job has fallen quiet while a new one keeps in touch, the
    job fails with an honest message and a cancel; a job it never picked goes to the new agent."""
    device = Device()
    beat = {"policy_hash": "p0", "os": "x", "home": "y", "agent_version": "0.1.0"}
    device_call(hub, device, "device_heartbeat", "/device/heartbeat", {**beat, "instance": "a1"})
    exec_request = {"device": "laptop", "script": "sleep 9", "timeout": 30}
    picked = body(local(hub, "local_exec", exec_request))["job_id"]
    device_call(hub, device, "device_poll", "/device/poll", {"wait": 0, "instance": "a1"})
    waiting = body(local(hub, "local_exec", exec_request))["job_id"]
    reply, path, _ = device_call(
        hub, device, "device_poll", "/device/poll", {"wait": 0, "instance": "b2"}
    )
    assert verified(hub, reply, path)["job_id"] == waiting
    out = body(local(hub, "local_output", {"job_id": picked, "after": 0, "wait": 1.5}))
    assert out["done"] is False  # a1 was heard a moment ago: it may simply be a second agent
    monkeypatch.setattr(relay, "_INSTANCE_QUIET", 0)
    out = body(local(hub, "local_output", {"job_id": picked, "after": 0, "wait": 2}))
    assert (out["done"], out["returncode"], out["error"]) == (
        True,
        relay.UNREACHABLE,
        messages.agent_restarted("laptop"),
    )
    reply, path, _ = device_call(
        hub, device, "device_poll", "/device/poll", {"wait": 0, "instance": "b2"}
    )
    assert verified(hub, reply, path)["target_job_id"] == picked
    assert not hub._jobs[waiting].done


def test_two_live_agents_for_one_device_never_end_each_others_polls_again(hub):
    """V21 and V22: two agents that both keep in touch take turns at jobs; neither ends the other's
    parked poll after their first meeting, so there is no poll storm and no job fails."""
    device = Device()
    device_call(hub, device, "device_poll", "/device/poll", {"wait": 0, "instance": "a1"})
    device_call(hub, device, "device_poll", "/device/poll", {"wait": 0, "instance": "b2"})
    parked = started(
        lambda: device_call(
            hub, device, "device_poll", "/device/poll", {"wait": 3, "instance": "a1"}
        )
    )
    until_waiting(hub, device.device_id)
    begun = time.monotonic()
    for _ in range(3):
        quick, _, _ = device_call(
            hub, device, "device_poll", "/device/poll", {"wait": 0, "instance": "b2"}
        )
        assert quick.status == 204
    reply, _, _ = parked()
    assert reply.status == 204 and time.monotonic() - begun > 2.5  # it ran to its own deadline


def test_restart_seen_in_a_heartbeat_ends_the_old_agents_parked_poll(hub):
    device = Device()
    beat = {"policy_hash": "p0", "os": "x", "home": "y", "agent_version": "0.1.0"}
    stale = started(
        lambda: device_call(
            hub, device, "device_poll", "/device/poll", {"wait": 10, "instance": "a1"}
        )
    )
    until_waiting(hub, device.device_id)
    device_call(hub, device, "device_heartbeat", "/device/heartbeat", {**beat, "instance": "b2"})
    reply, _, _ = stale()
    assert reply.status == 204  # it never takes a job meant for the new agent


def test_a_newer_poll_supersedes_an_older_parked_one(hub):
    """C48: a poll still parked from a dropped connection must not take the next job."""
    device = Device()
    stale = started(lambda: device_call(hub, device, "device_poll", "/device/poll", {"wait": 10}))
    until_waiting(hub, device.device_id)
    begun = time.monotonic()
    fresh = started(lambda: device_call(hub, device, "device_poll", "/device/poll", {"wait": 10}))
    reply, _, _ = stale()
    assert (reply.status, reply.body) == (204, b"") and time.monotonic() - begun < 5
    until_waiting(hub, device.device_id)
    job_id = body(local(hub, "local_exec", {"device": "laptop", "script": "true", "timeout": 5}))[
        "job_id"
    ]
    reply, path, _ = fresh()
    assert verified(hub, reply, path)["job_id"] == job_id


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
    no_code = run(hub.pair(Principal("x", scopes=(relay.SCOPE_DEVICE,)), headers, path, raw))
    assert no_code.status == 401 and "X-Hands-Signature" not in no_code.headers
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


def test_state_lists_devices_and_policy_alerts(hub):
    device = Device()
    state = body(local(hub, "local_state", {}))
    assert state["relay"] is True and [d["name"] for d in state["devices"]] == ["laptop"]
    assert state["alerts"] == []
    store.record_seen(device.device_id, policy_hash="edited")
    assert body(local(hub, "local_state", {}))["alerts"] == [messages.policy_changed("laptop")]


def test_device_list_changes_are_flagged_once_for_the_pane(hub):
    private, public = protocol.generate_keypair()
    paired = run(hub.pair(*pair_request(private, public, store.mint_pairing_code("laptop"))))
    assert paired.devices_changed == {"reason": "paired", "device": "laptop"}
    device_id = body(paired)["device_id"]
    edited = Device("desk")
    edited.policy_hash = "edited"
    beat = {"policy_hash": "edited", "os": "x", "home": "y"}
    first, _, _ = device_call(hub, edited, "device_heartbeat", "/device/heartbeat", beat)
    again, _, _ = device_call(hub, edited, "device_heartbeat", "/device/heartbeat", beat)
    assert first.status == again.status == 409
    assert first.devices_changed == {"reason": "policy_changed", "device": "desk"}
    assert again.devices_changed is None  # still refused: no new event
    assert store.device_by_id(device_id) is not None
    store.accept_policy("desk")
    accepted, _, _ = device_call(hub, edited, "device_heartbeat", "/device/heartbeat", beat)
    assert accepted.status == 200 and accepted.devices_changed is None
    edited.policy_hash = "edited again"
    beat["policy_hash"] = "edited again"
    third, _, _ = device_call(hub, edited, "device_heartbeat", "/device/heartbeat", beat)
    assert third.devices_changed == {"reason": "policy_changed", "device": "desk"}


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
    assert store.relay_alive() is False
    hub.start()
    assert threads == [(hub._heartbeat_loop, "hermes-hands-relay", True)]
    assert store.relay_alive() is True  # written before the first reply, not by the thread
