import os
import shlex
import threading
import time

import httpx

from hermeshands_device import cli, loop, config
from hermeshands_device.protocol import R2D, b64u, canonical_json, generate_keypair, sign_request


def _core_wrapped(command):
    try:
        from tools.environments.base_session_env import _wrap_command_script
    except ImportError:
        escaped = command.replace("'", "'\\''")
        escaped += '\n\n__hermes_rc=$? __hermes_ps=("${PIPESTATUS[@]}"); (exit $__hermes_rc) && :'
        return f"eval '{escaped}'"
    return _wrap_command_script(
        command,
        quoted_cwd="/tmp",
        quoted_snap="/tmp/snapshot",
        snap_tmp_template="/tmp/snapshot.tmp.XXXXXXXXXX",
        passthrough_names=(),
        snapshot_ready=False,
        cwd_marker="__CWD__",
        pipe_marker="__PIPE__",
    )


def _signed_poll(monkeypatch, *, body=None, signature_body=None, response_ts=None, response_nonce=None, signing_key=None):
    private, public = generate_keypair()
    monkeypatch.setattr(loop, "get_or_create", lambda: (private, public))
    monkeypatch.setattr(config, "gateway_url", lambda: "https://example.test/api/plugins/hermes-hands")
    monkeypatch.setattr(config, "device_id", lambda: "device-1")
    monkeypatch.setattr(config, "relay_public_key", lambda: public)
    payload = {"job_id": "job-1", "type": "exec", "script": "echo signed"}
    body = canonical_json(payload) if body is None else body
    signature_body = body if signature_body is None else signature_body
    ts = int(time.time()) if response_ts is None else response_ts
    key = private if signing_key is None else signing_key

    def handler(request):
        nonce = request.headers["X-Hands-Nonce"] if response_nonce is None else response_nonce
        path = "/api/plugins/hermes-hands/device/poll"
        return httpx.Response(200, content=body, request=request, headers={
            "X-Hands-Ts": str(ts), "X-Hands-Nonce": nonce,
            "X-Hands-Signature": sign_request(key, R2D, ts, nonce, "POST", path, signature_body),
        })

    return payload, loop.DeviceLoop(transport=httpx.MockTransport(handler))


def test_loop_accepts_correctly_signed_poll_reply(monkeypatch):
    job, agent = _signed_poll(monkeypatch)
    try:
        assert agent._verify_response(agent._post("/device/poll", {"wait": 25}), "/device/poll") == job
    finally:
        agent.client.close()


def test_loop_rejects_tampered_poll_body(monkeypatch):
    body = canonical_json({"job_id": "job-1", "type": "exec", "script": "echo altered"})
    signature_body = canonical_json({"job_id": "job-1", "type": "exec", "script": "echo signed"})
    _, agent = _signed_poll(monkeypatch, body=body, signature_body=signature_body)
    try:
        try:
            agent._verify_response(agent._post("/device/poll", {"wait": 25}), "/device/poll")
        except PermissionError:
            pass
        else:
            raise AssertionError("tampered body accepted")
    finally:
        agent.client.close()


def test_loop_rejects_stale_poll_reply(monkeypatch):
    _, agent = _signed_poll(monkeypatch, response_ts=int(time.time()) - 301)
    try:
        try:
            agent._verify_response(agent._post("/device/poll", {"wait": 25}), "/device/poll")
        except PermissionError:
            pass
        else:
            raise AssertionError("stale reply accepted")
    finally:
        agent.client.close()


def test_loop_rejects_poll_reply_signed_by_wrong_key(monkeypatch):
    wrong_private, _ = generate_keypair()
    _, agent = _signed_poll(monkeypatch, signing_key=wrong_private)
    try:
        try:
            agent._verify_response(agent._post("/device/poll", {"wait": 25}), "/device/poll")
        except PermissionError:
            pass
        else:
            raise AssertionError("reply signed by the wrong key accepted")
    finally:
        agent.client.close()


def test_pair_accepts_correctly_signed_reply(monkeypatch):
    private, public = generate_keypair()
    _, device_public = generate_keypair()
    monkeypatch.setattr(cli, "get_or_create", lambda: (private, device_public))
    monkeypatch.setattr(cli.policy, "digest", lambda: "policy-hash")
    saved = []
    monkeypatch.setattr(config, "save", lambda data: saved.append(data))
    body = canonical_json({"device_id": "device-1", "relay_public_key": b64u(public)})
    ts = int(time.time())
    path = "/api/plugins/hermes-hands/pair"

    def post(url, **kwargs):
        request = httpx.Request("POST", url, headers=kwargs["headers"], content=kwargs["content"])
        return httpx.Response(200, content=body, request=request, headers={
            "X-Hands-Ts": str(ts), "X-Hands-Nonce": "relay-nonce",
            "X-Hands-Signature": sign_request(private, R2D, ts, "relay-nonce", "POST", path, body),
        })

    monkeypatch.setattr(cli.httpx, "post", post)
    assert cli._pair("https://example.test", "test device", "pair-code") == 0
    assert saved == [{"gateway_url": "https://example.test/api/plugins/hermes-hands", "device_id": "device-1", "relay_public_key": b64u(public)}]


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


def test_loop_allows_core_background_pid_liveness_probe(monkeypatch):
    agent = object.__new__(loop.DeviceLoop)
    agent.stop_event = threading.Event()
    agent.processes = {}
    agent.running = {}
    command = 'kill -0 "$(cat /tmp/hermes_bg_proc-1.pid 2>/dev/null)" 2>/dev/null; echo $?'
    script = _core_wrapped(command)
    assert "eval 'kill -0" in script
    assert loop.policy.refusal(command)
    posted = []
    monkeypatch.setattr(loop.policy, "digest", lambda: "hash")
    monkeypatch.setattr(loop.executor, "run", lambda *args, **kwargs: iter((b"0\n",)))
    monkeypatch.setattr(agent, "_result", lambda job_id, seq, **kwargs: posted.append(kwargs))
    agent._run_job({"job_id": "probe", "type": "exec", "script": script})
    assert posted == [{"chunk": "0\n"}, {"done": True, "returncode": 0}]


def test_loop_refuses_denied_commands_even_when_preceded_by_background_probe(monkeypatch):
    probe = 'kill -0 "$(cat /tmp/hermes_bg_proc-1.pid 2>/dev/null)" 2>/dev/null; echo $?'
    commands = (
        probe + "; kill -9 $PPID",
        probe + f"; taskkill /PID {os.getpid()} /F",
        f'echo "{probe}"; hermes-hands unpair',
        'kill -0 "$(cat ~/.hermes-hands/agent.pid 2>/dev/null)" 2>/dev/null; echo $?; kill -9 $PPID',
        probe + "; shutdown now",
    )
    for command in commands:
        script = _core_wrapped(command)
        agent = object.__new__(loop.DeviceLoop)
        agent.processes = {}
        agent.running = {}
        posted = []
        monkeypatch.setattr(agent, "_result", lambda job_id, seq, **kwargs: posted.append(kwargs))
        monkeypatch.setattr(loop.executor, "run", lambda *args, **kwargs: iter(()))
        agent._run_job({"job_id": "denied", "type": "exec", "script": script})
        assert posted == [{"chunk": f"Policy refused command matching: {loop.policy.refusal(script)}\n", "done": True, "returncode": 126}]


def test_probe_wrapper_matches_core_output_when_core_is_importable():
    try:
        from tools.environments.base_session_env import _wrap_command_script
    except ImportError:
        return
    pid_path = shlex.quote("/tmp/a'b folder/hermes_bg_proc-1.pid")
    command = f'kill -0 "$(cat {pid_path} 2>/dev/null)" 2>/dev/null; echo $?'
    expected = _wrap_command_script(
        command,
        quoted_cwd="/tmp",
        quoted_snap="/tmp/snapshot",
        snap_tmp_template="/tmp/snapshot.tmp.XXXXXXXXXX",
        passthrough_names=(),
        snapshot_ready=False,
        cwd_marker="__CWD__",
        pipe_marker="__PIPE__",
    )
    assert _core_wrapped(command) == expected
    probe_line = next(line for line in expected.splitlines() if line.startswith("eval '"))
    assert loop._BACKGROUND_PID_PROBE.sub("", probe_line) == ""


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


def test_policy_change_cancels_jobs_without_stopping_agent(monkeypatch):
    agent = object.__new__(loop.DeviceLoop)
    agent.stop_event = threading.Event()
    agent.processes = {}
    agent.policy_change_logged = False
    response = httpx.Response(409, json={"error": "policy_changed"})

    try:
        agent._verify_response(response, "/device/poll")
    except PermissionError as exc:
        assert str(exc) == "policy_changed"
    else:
        raise AssertionError("policy change response was accepted")

    assert not agent.stop_event.is_set()
    posted = []
    agent.running = {}
    monkeypatch.setattr(loop.policy, "refusal", lambda script: None)
    monkeypatch.setattr(loop.policy, "digest", lambda: "hash")
    monkeypatch.setattr(loop.executor, "run", lambda *a, **kw: iter(()))
    monkeypatch.setattr(agent, "_result", lambda job_id, seq, **kwargs: posted.append(kwargs))
    agent._run_job({"job_id": "later", "type": "exec", "script": "echo hi"})
    assert posted == [{"done": True, "returncode": 0}]


def test_stopping_during_result_delivery_does_not_escape_job_thread(monkeypatch):
    agent = object.__new__(loop.DeviceLoop)
    agent.stop_event = threading.Event()
    agent.processes = {}
    agent.running = {}
    monkeypatch.setattr(loop.policy, "refusal", lambda script: None)
    monkeypatch.setattr(loop.policy, "digest", lambda: "hash")

    def post(*args, **kwargs):
        agent.stop_event.set()
        raise httpx.ConnectError("device stopped")

    monkeypatch.setattr(agent, "_post", post)
    monkeypatch.setattr(loop.executor, "run", lambda *a, **kw: iter((b"output",)))
    errors = []

    def run():
        try:
            agent._run_job({"job_id": "job", "type": "exec", "script": "echo hi"})
        except Exception as exc:
            errors.append(exc)

    thread = threading.Thread(target=run)
    thread.start()
    thread.join(2)
    assert not thread.is_alive()
    assert errors == []
