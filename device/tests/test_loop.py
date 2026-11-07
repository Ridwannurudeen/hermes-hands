import time
import threading

import httpx

from hermeshands_device import loop, config
from hermeshands_device.protocol import R2D, generate_keypair, sign_request


def test_loop_rejects_bad_signature(monkeypatch):
    private, public = generate_keypair()
    monkeypatch.setattr(loop, "get_or_create", lambda: (private, public))
    monkeypatch.setattr(config, "gateway_url", lambda: "https://example.test/api/plugins/hermes-hands")
    monkeypatch.setattr(config, "device_id", lambda: "device-1")
    monkeypatch.setattr(config, "relay_public_key", lambda: public)
    response = httpx.Response(200, content=b'{"job_id":"x","type":"exec","script":"echo bad"}', headers={"X-Hands-Ts": str(int(time.time())), "X-Hands-Nonce": "n", "X-Hands-Signature": "bad"})
    agent = loop.DeviceLoop(transport=httpx.MockTransport(lambda request: response))
    try:
        try:
            signed_response = agent._post("/device/poll", {"wait": 25})
            agent._verify_response(signed_response, "/device/poll")
        except PermissionError:
            pass
        else:
            raise AssertionError("bad signature accepted")
    finally:
        agent.client.close()


def test_loop_requires_reply_nonce_to_match_request(monkeypatch):
    private, public = generate_keypair()
    monkeypatch.setattr(loop, "get_or_create", lambda: (private, public))
    monkeypatch.setattr(config, "gateway_url", lambda: "https://example.test/api/plugins/hermes-hands")
    monkeypatch.setattr(config, "device_id", lambda: "device-1")
    monkeypatch.setattr(config, "relay_public_key", lambda: public)
    body = b'{"job_id":"x","type":"exec","script":"echo bad"}'
    ts = int(time.time())

    def handler(request):
        path = "/api/plugins/hermes-hands/device/poll"
        return httpx.Response(200, content=body, request=request, headers={
            "X-Hands-Ts": str(ts), "X-Hands-Nonce": "different",
            "X-Hands-Signature": sign_request(private, R2D, ts, "different", "POST", path, body),
        })

    agent = loop.DeviceLoop(transport=httpx.MockTransport(handler))
    try:
        try:
            agent._verify_response(agent._post("/device/poll", {"wait": 25}), "/device/poll")
        except PermissionError:
            pass
        else:
            raise AssertionError("reply with an unrelated nonce accepted")
    finally:
        agent.client.close()


def test_loop_204_is_no_job_and_non_200_is_not_parsed():
    agent = object.__new__(loop.DeviceLoop)
    assert agent._verify_response(httpx.Response(204), "/device/poll") == {}
    try:
        agent._verify_response(httpx.Response(202, json={"job_id": "x", "type": "exec"}), "/device/poll")
    except ValueError:
        pass
    else:
        raise AssertionError("non-200 reply accepted as a job")


def test_loop_posts_seq_ordered_chunks(monkeypatch):
    posted = []
    agent = object.__new__(loop.DeviceLoop)
    agent.processes = {}
    agent.running = {}
    monkeypatch.setattr(loop.policy, "refusal", lambda script: None)
    monkeypatch.setattr(loop.policy, "digest", lambda: "hash")
    monkeypatch.setattr(loop.executor, "run", lambda *a, **kw: iter((b"one", b"two")))
    monkeypatch.setattr(agent, "_result", lambda job_id, seq, **kwargs: posted.append((seq, kwargs)))
    agent._run_job({"job_id": "job", "type": "exec", "script": "echo hi"})
    assert [seq for seq, _ in posted] == [0, 1, 2]
    assert posted[-1][1]["done"] is True


def test_loop_retries_result_after_network_error(monkeypatch):
    agent = object.__new__(loop.DeviceLoop)
    agent.stop_event = threading.Event()
    attempts = []
    monkeypatch.setattr(loop.policy, "digest", lambda: "hash")

    def post(endpoint, body):
        attempts.append(body)
        if len(attempts) == 1:
            raise httpx.ConnectError("offline")
        return httpx.Response(200, json={"ok": True})

    monkeypatch.setattr(agent, "_post", post)
    monkeypatch.setattr(agent, "_verify_response", lambda response, endpoint: {})
    monkeypatch.setattr(agent.stop_event, "wait", lambda delay: False)
    agent._result("job", 0, done=True)
    assert len(attempts) == 2
