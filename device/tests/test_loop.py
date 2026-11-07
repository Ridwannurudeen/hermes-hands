import os
import shlex
import threading
import time

import httpx
import pytest

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
        assert agent._verify_response(agent._post("/device/poll", {"wait": 25, "policy_hash": "hash"}), "/device/poll") == job
    finally:
        agent.client.close()


def test_verified_poll_accepts_sent_policy_hash_before_job(monkeypatch):
    job, agent = _signed_poll(monkeypatch)
    posted = []
    monkeypatch.setattr(loop.policy, "digest", lambda: "accepted-hash")
    monkeypatch.setattr(loop.policy, "refusal", lambda script: None)
    monkeypatch.setattr(loop.executor, "run", lambda *args, **kwargs: iter(()))
    monkeypatch.setattr(agent, "_result", lambda job_id, seq, **kwargs: posted.append(kwargs))
    try:
        poll_body = agent._poll_body()
        assert poll_body["policy_hash"] == "accepted-hash"
        assert agent._verify_response(agent._post("/device/poll", poll_body), "/device/poll") == job
        assert agent.accepted_policy_hash == "accepted-hash"
        agent._run_job(job)
        assert posted == [{"chunk": "", "done": True, "returncode": 0}]
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


@pytest.mark.parametrize("status,error", [(401, "unknown_device"), (409, "policy_changed")])
def test_auth_control_replies_require_valid_signature(monkeypatch, status, error):
    private, public = generate_keypair()
    monkeypatch.setattr(loop, "get_or_create", lambda: (private, public))
    monkeypatch.setattr(config, "gateway_url", lambda: "https://example.test/api/plugins/hermes-hands")
    monkeypatch.setattr(config, "device_id", lambda: "device-1")
    monkeypatch.setattr(config, "relay_public_key", lambda: public)
    agent = loop.DeviceLoop(transport=httpx.MockTransport(lambda request: httpx.Response(204, request=request)))
    request = httpx.Request("POST", "https://example.test/device/poll", headers={"X-Hands-Nonce": "request-nonce"})
    response = httpx.Response(status, json={"error": error}, request=request)
    try:
        with pytest.raises(ValueError, match="transient relay status"):
            agent._verify_response(response, "/device/poll")
    finally:
        agent.client.close()


def test_signed_unknown_device_stops_agent(monkeypatch):
    private, public = generate_keypair()
    monkeypatch.setattr(loop, "get_or_create", lambda: (private, public))
    monkeypatch.setattr(config, "gateway_url", lambda: "https://example.test/api/plugins/hermes-hands")
    monkeypatch.setattr(config, "device_id", lambda: "device-1")
    monkeypatch.setattr(config, "relay_public_key", lambda: public)
    agent = loop.DeviceLoop(transport=httpx.MockTransport(lambda request: httpx.Response(204, request=request)))
    request = httpx.Request("POST", "https://example.test/device/poll", headers={"X-Hands-Nonce": "request-nonce"})
    body = canonical_json({"error": "unknown_device"})
    nonce = request.headers["X-Hands-Nonce"]
    path = "/api/plugins/hermes-hands/device/poll"
    ts = int(time.time())
    response = httpx.Response(401, content=body, request=request, headers={
        "X-Hands-Ts": str(ts), "X-Hands-Nonce": nonce,
        "X-Hands-Signature": sign_request(private, R2D, ts, nonce, "POST", path, body),
    })
    try:
        with pytest.raises(PermissionError, match="unknown_device"):
            agent._verify_response(response, "/device/poll")
        assert agent.stop_event.is_set()
    finally:
        agent.client.close()


def test_retry_backoff_caps_at_30_seconds(monkeypatch):
    agent = object.__new__(loop.DeviceLoop)
    agent.retry_delay = 1
    delays = []
    agent.stop_event = type("Stop", (), {"wait": lambda self, delay: delays.append(delay)})()
    for _ in range(8):
        agent._retry()
    assert delays == [1, 2, 4, 8, 16, 30, 30, 30]


def test_policy_changed_retry_backoff_caps_at_5_seconds(monkeypatch):
    agent = object.__new__(loop.DeviceLoop)
    agent.retry_delay = 16
    delays = []
    agent.stop_event = type("Stop", (), {"wait": lambda self, delay: delays.append(delay)})()
    agent._retry(cap=5)
    assert delays[0] == 5
    assert agent.retry_delay == 5


def test_heartbeat_retries_within_5_seconds_after_policy_changed(monkeypatch):
    agent = object.__new__(loop.DeviceLoop)
    agent.stop_event = threading.Event()
    agent.retry_delay = 30
    agent.policy_change_logged = False
    agent.last_auth_warning = 0
    attempts, delays = [], []
    monkeypatch.setattr(agent, "_heartbeat_body", lambda: {})
    monkeypatch.setattr(agent, "_post", lambda *args: attempts.append(args) or httpx.Response(200))

    def verify(response, endpoint):
        if len(attempts) == 1:
            raise PermissionError("policy_changed")
        agent.stop_event.set()
        return {}

    monkeypatch.setattr(agent, "_verify_response", verify)
    monkeypatch.setattr(agent.stop_event, "wait", lambda delay: delays.append(delay) or False)
    agent._heartbeat_loop()
    assert len(attempts) == 2
    assert delays[0] == 5


def test_poll_and_heartbeat_share_instance(monkeypatch):
    agent = object.__new__(loop.DeviceLoop)
    agent.instance = "1234567890abcdef"
    monkeypatch.setattr(loop.policy, "digest", lambda: "hash")
    agent.accepted_policy_hash = "hash"
    assert agent._poll_body()["instance"] == agent._heartbeat_body()["instance"] == agent.instance


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
    agent.stop_event = threading.Event()
    monkeypatch.setattr(loop.policy, "refusal", lambda script: None)
    monkeypatch.setattr(loop.policy, "digest", lambda: "hash")
    agent.accepted_policy_hash = "hash"
    monkeypatch.setattr(loop.executor, "run", lambda *a, **kw: iter((b"one", b"two")))
    monkeypatch.setattr(agent, "_result", lambda job_id, seq, **kwargs: posted.append((seq, kwargs)))
    agent._run_job({"job_id": "job", "type": "exec", "script": "echo hi"})
    assert [seq for seq, _ in posted] == [0, 1, 2]
    assert posted[-1][1]["done"] is True


def test_exec_refuses_changed_policy_and_posts_current_hash(monkeypatch):
    agent = object.__new__(loop.DeviceLoop)
    agent.processes, agent.running = {}, {}
    posted = []
    monkeypatch.setattr(loop.policy, "digest", lambda: "new-hash")
    monkeypatch.setattr(loop.executor, "run", lambda *a, **kw: pytest.fail("job ran under unaccepted policy"))
    monkeypatch.setattr(agent, "_result", lambda job_id, seq, **kwargs: posted.append(kwargs))
    agent.accepted_policy_hash = "old-hash"
    agent._run_job({"job_id": "changed", "type": "exec", "script": "echo hi"})
    assert posted == [{"chunk": "Device policy changed. The owner must run /hands accept-policy <device>.\n", "done": True, "returncode": 126, "policy_hash": "new-hash"}]


def test_exec_runs_when_policy_matches_accepted_hash(monkeypatch):
    agent = object.__new__(loop.DeviceLoop)
    agent.processes, agent.running = {}, {}
    posted = []
    monkeypatch.setattr(loop.policy, "digest", lambda: "same-hash")
    monkeypatch.setattr(loop.policy, "refusal", lambda script: None)
    monkeypatch.setattr(loop.executor, "run", lambda *a, **kw: iter(()))
    monkeypatch.setattr(agent, "_result", lambda job_id, seq, **kwargs: posted.append(kwargs))
    agent.accepted_policy_hash = "same-hash"
    agent._run_job({"job_id": "same", "type": "exec", "script": "echo hi"})
    assert posted == [{"chunk": "", "done": True, "returncode": 0}]


def test_loop_incrementally_decodes_split_utf8(monkeypatch):
    posted = []
    agent = object.__new__(loop.DeviceLoop)
    agent.processes, agent.running = {}, {}
    agent.cancelled_jobs = set()
    agent.job_deadlines = {}
    monkeypatch.setattr(loop.policy, "refusal", lambda script: None)
    monkeypatch.setattr(loop.policy, "digest", lambda: "hash")
    agent.accepted_policy_hash = "hash"
    monkeypatch.setattr(loop.executor, "run", lambda *a, **kw: iter((b"\xe4\xb8", b"\xad")))
    monkeypatch.setattr(agent, "_result", lambda job_id, seq, **kwargs: posted.append(kwargs))
    agent._run_job({"job_id": "utf8", "type": "exec", "script": "echo hi"})
    assert "".join(item["chunk"] for item in posted) == "\u4e2d"


def test_loop_drains_all_output_when_result_posts_are_slow(monkeypatch, tmp_path):
    bash = loop.executor.resolve_bash()
    if not bash:
        import pytest

        pytest.skip("bash is unavailable")
    monkeypatch.setattr(loop.executor, "resolve_bash", lambda: bash)
    monkeypatch.setattr(loop.executor.Path, "home", lambda: tmp_path)
    monkeypatch.setattr(loop.policy, "refusal", lambda script: None)
    monkeypatch.setattr(loop.policy, "digest", lambda: "hash")
    agent = object.__new__(loop.DeviceLoop)
    agent.accepted_policy_hash = "hash"
    agent.processes, agent.running = {}, {}
    agent.cancelled_jobs, agent.job_deadlines = set(), {}
    output = []

    def slow_result(job_id, seq, **kwargs):
        time.sleep(0.005)
        output.append(kwargs.get("chunk", ""))

    agent._result = slow_result
    agent._run_job({"job_id": "slow-output", "type": "exec", "script": "head -c 1500000 /dev/zero | tr '\\000' x", "timeout": 30})
    assert "".join(output).encode() == b"x" * 1_500_000


def test_result_reply_cancel_terminates_registered_job(monkeypatch):
    agent = object.__new__(loop.DeviceLoop)
    agent.stop_event = threading.Event()
    agent.last_auth_warning = 0
    process = type("Proc", (), {"poll": lambda self: None})()
    agent.processes = {"job": (process, 1.0)}
    agent.job_deadlines = {}
    terminated = []
    monkeypatch.setattr(loop.executor, "_terminate", lambda proc, started: terminated.append((proc, started)))
    monkeypatch.setattr(agent, "_post", lambda *args: httpx.Response(200))
    monkeypatch.setattr(agent, "_verify_response", lambda *args: {"ok": True, "cancel": True})
    try:
        agent._result("job", 0)
    except InterruptedError:
        pass
    else:
        raise AssertionError("cancel reply did not stop result posting")
    assert terminated == [(process, 1.0)]


def test_result_reply_cancel_before_registration_is_remembered(monkeypatch):
    agent = object.__new__(loop.DeviceLoop)
    agent.stop_event = threading.Event()
    agent.processes = {}
    agent.cancelled_jobs = set()
    agent.job_deadlines = {}
    monkeypatch.setattr(agent, "_post", lambda *args: httpx.Response(200))
    monkeypatch.setattr(agent, "_verify_response", lambda *args: {"cancel": True})
    try:
        agent._result("job", 0)
    except InterruptedError:
        pass
    assert "job" in agent.cancelled_jobs


def test_loop_sends_output_read_after_exit_with_done(monkeypatch):
    posted = []
    agent = object.__new__(loop.DeviceLoop)
    agent.processes = {}
    agent.running = {}
    exited = []

    class Process:
        def poll(self):
            return 3 if exited else None

    def run(*args, on_start, on_exit, **kwargs):
        on_start(Process(), 0.0)
        yield b"running "
        exited.append(True)
        yield b"after "
        yield b"exit"
        on_exit(3)

    monkeypatch.setattr(loop.policy, "refusal", lambda script: None)
    monkeypatch.setattr(loop.policy, "digest", lambda: "hash")
    agent.accepted_policy_hash = "hash"
    monkeypatch.setattr(loop.executor, "run", run)
    monkeypatch.setattr(agent, "_result", lambda job_id, seq, **kwargs: posted.append((seq, kwargs)))
    agent._run_job({"job_id": "job", "type": "exec", "script": "echo hi"})
    assert posted == [(0, {"chunk": "running "}), (1, {"chunk": "after exit", "done": True, "returncode": 3})]


def test_loop_flushes_held_output_before_executor_error(monkeypatch):
    posted = []
    agent = object.__new__(loop.DeviceLoop)
    agent.processes = {}
    agent.running = {}
    agent.stop_event = threading.Event()

    class Process:
        def poll(self):
            return 3

    def run(*args, on_start, on_exit, **kwargs):
        on_start(Process(), 0.0)
        yield b"held output"
        raise RuntimeError("executor failed")

    monkeypatch.setattr(loop.policy, "refusal", lambda script: None)
    monkeypatch.setattr(loop.policy, "digest", lambda: "hash")
    agent.accepted_policy_hash = "hash"
    monkeypatch.setattr(loop.executor, "run", run)
    monkeypatch.setattr(agent, "_result", lambda job_id, seq, **kwargs: posted.append((seq, kwargs)))
    agent._run_job({"job_id": "job", "type": "exec", "script": "echo hi"})
    assert posted == [
        (0, {"chunk": "held output"}),
        (1, {"done": True, "returncode": 1, "error": "executor failed"}),
    ]


def test_loop_allows_core_background_pid_liveness_probe(monkeypatch):
    agent = object.__new__(loop.DeviceLoop)
    agent.stop_event = threading.Event()
    agent.processes = {}
    agent.running = {}
    command = 'kill -0 "$(cat /tmp/hermes_bg_proc-1.pid 2>/dev/null)" 2>/dev/null; echo $?'
    script = _core_wrapped(command)
    assert "eval 'kill -0" in script
    assert loop.policy.refusal(command) is None
    posted = []
    monkeypatch.setattr(loop.policy, "digest", lambda: "hash")
    agent.accepted_policy_hash = "hash"
    monkeypatch.setattr(loop.executor, "run", lambda *args, **kwargs: iter((b"0\n",)))
    monkeypatch.setattr(agent, "_result", lambda job_id, seq, **kwargs: posted.append(kwargs))
    agent._run_job({"job_id": "probe", "type": "exec", "script": script})
    assert posted == [{"chunk": "0\n"}, {"chunk": "", "done": True, "returncode": 0}]


def test_loop_allows_core_background_pid_probe_with_quoted_space_path(monkeypatch):
    agent = object.__new__(loop.DeviceLoop)
    agent.stop_event = threading.Event()
    agent.processes = {}
    agent.running = {}
    pid_path = shlex.quote("/tmp/path with space/hermes_bg_proc-1.pid")
    command = f'kill -0 "$(cat {pid_path} 2>/dev/null)" 2>/dev/null; echo $?'
    script = _core_wrapped(command)
    assert loop.policy.refusal(command) is None
    posted = []
    monkeypatch.setattr(loop.policy, "digest", lambda: "hash")
    agent.accepted_policy_hash = "hash"
    monkeypatch.setattr(loop.executor, "run", lambda *args, **kwargs: iter((b"0\n",)))
    monkeypatch.setattr(agent, "_result", lambda job_id, seq, **kwargs: posted.append(kwargs))
    agent._run_job({"job_id": "probe-space", "type": "exec", "script": script})
    assert posted == [{"chunk": "0\n"}, {"chunk": "", "done": True, "returncode": 0}]


@pytest.mark.parametrize("path", [
    "/tmp/hermes_bg_proc-1.pid",
    "/tmp/path with space/hermes_bg_proc-1.pid",
])
def test_loop_allows_core_background_probe_with_old_policy(monkeypatch, tmp_path, path):
    monkeypatch.setattr(config, "POLICY_PATH", tmp_path / "policy.json")
    config.POLICY_PATH.write_text(
        canonical_json({"deny": [r"\b(?:taskkill|Stop-Process|kill)\b.*(?:hermes-hands|\\1|\b(?:PID|pid)\b)"]}).decode(),
        encoding="utf-8",
    )
    command = f'kill -0 "$(cat {shlex.quote(path)} 2>/dev/null)" 2>/dev/null; echo $?'
    script = _core_wrapped(command)
    assert loop.policy.refusal(command)
    agent = object.__new__(loop.DeviceLoop)
    agent.stop_event = threading.Event()
    agent.processes = {}
    agent.running = {}
    posted = []
    monkeypatch.setattr(loop.policy, "digest", lambda: "hash")
    agent.accepted_policy_hash = "hash"
    monkeypatch.setattr(loop.executor, "run", lambda *args, **kwargs: iter((b"0\n",)))
    monkeypatch.setattr(agent, "_result", lambda job_id, seq, **kwargs: posted.append(kwargs))
    agent._run_job({"job_id": "old-policy-probe", "type": "exec", "script": script})
    assert posted == [{"chunk": "0\n"}, {"chunk": "", "done": True, "returncode": 0}]


def test_loop_does_not_strip_injected_shell_from_quoted_pid_path(monkeypatch):
    command = "kill -0 \"$(cat " + shlex.quote("/tmp/'; kill -9 $PPID; '/hermes_bg_proc-1.pid") + " 2>/dev/null)\" 2>/dev/null; echo $?"
    script = _core_wrapped(command)
    agent = object.__new__(loop.DeviceLoop)
    agent.processes = {}
    agent.running = {}
    posted = []
    monkeypatch.setattr(agent, "_result", lambda job_id, seq, **kwargs: posted.append(kwargs))
    monkeypatch.setattr(loop.executor, "run", lambda *args, **kwargs: iter(()))
    monkeypatch.setattr(loop.policy, "digest", lambda: "hash")
    agent.accepted_policy_hash = "hash"
    agent._run_job({"job_id": "injected-path", "type": "exec", "script": script})
    assert posted == [{"chunk": f"Policy refused command matching: {loop.policy.refusal(script)}\n", "done": True, "returncode": 126}]


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
        monkeypatch.setattr(loop.policy, "digest", lambda: "hash")
        agent.accepted_policy_hash = "hash"
        agent._run_job({"job_id": "denied", "type": "exec", "script": script})
        assert posted == [{"chunk": f"Policy refused command matching: {loop.policy.refusal(script)}\n", "done": True, "returncode": 126}]


def test_probe_wrapper_matches_core_output_when_core_is_importable():
    try:
        from tools.environments.base_session_env import _wrap_command_script
    except ImportError:
        return
    pid_path = shlex.quote("/tmp/a b folder/hermes_bg_proc-1.pid")
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
    assert "eval 'kill -0" in expected


def test_loop_retries_result_after_network_error(monkeypatch):
    agent = object.__new__(loop.DeviceLoop)
    agent.stop_event = threading.Event()
    attempts = []
    monkeypatch.setattr(loop.policy, "digest", lambda: "hash")
    agent.accepted_policy_hash = "hash"

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
    agent.last_auth_warning = 0
    private, public = generate_keypair()
    monkeypatch.setattr(config, "gateway_url", lambda: "https://example.test/api/plugins/hermes-hands")
    monkeypatch.setattr(config, "relay_public_key", lambda: public)
    request = httpx.Request("POST", "https://example.test/device/poll", headers={"X-Hands-Nonce": "nonce"})
    body = canonical_json({"error": "policy_changed"})
    ts = int(time.time())
    response = httpx.Response(409, content=body, request=request, headers={
        "X-Hands-Ts": str(ts), "X-Hands-Nonce": "nonce",
        "X-Hands-Signature": sign_request(private, R2D, ts, "nonce", "POST", "/api/plugins/hermes-hands/device/poll", body),
    })

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
    agent.accepted_policy_hash = "hash"
    monkeypatch.setattr(loop.executor, "run", lambda *a, **kw: iter(()))
    monkeypatch.setattr(agent, "_result", lambda job_id, seq, **kwargs: posted.append(kwargs))
    agent._run_job({"job_id": "later", "type": "exec", "script": "echo hi"})
    assert posted == [{"chunk": "", "done": True, "returncode": 0}]


def test_stopping_during_result_delivery_does_not_escape_job_thread(monkeypatch):
    agent = object.__new__(loop.DeviceLoop)
    agent.stop_event = threading.Event()
    agent.processes = {}
    agent.running = {}
    monkeypatch.setattr(loop.policy, "refusal", lambda script: None)
    monkeypatch.setattr(loop.policy, "digest", lambda: "hash")
    agent.accepted_policy_hash = "hash"

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


def test_run_forever_terminates_jobs_when_loop_exits(monkeypatch):
    agent = object.__new__(loop.DeviceLoop)
    agent.stop_event = threading.Event()
    agent.fatal = []
    agent.running = {}
    agent.processes = {"job": (object(), 1.0)}
    agent.instance = "1234567890abcdef"
    calls = []
    monkeypatch.setattr(loop.executor, "reap_recorded_processes", lambda: None)
    monkeypatch.setattr(loop.threading, "Thread", lambda **kwargs: type("Thread", (), {"start": lambda self: None})())
    monkeypatch.setattr(agent, "_cancel_running_jobs", lambda: calls.append("cancel"))
    monkeypatch.setattr(agent, "_post", lambda *args: (_ for _ in ()).throw(KeyboardInterrupt()))
    with pytest.raises(KeyboardInterrupt):
        agent.run_forever()
    assert calls == ["cancel"]



