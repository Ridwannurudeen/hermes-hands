"""The gateway client and RemoteProcessHandle against a real Relay, through the MockTransport seam,
with a fake device agent on a thread."""

import functools
import os
import threading

import httpx
import pytest

from hands import client, messages, relay, store
from hands.handle import RemoteProcessHandle
from hands_fakes import Agent, Device, hands_db, wired  # noqa: F401  (fixtures)


def echo(job):
    return job["script"].split(" ", 1)[1] + "\n", 0


def read_all(proc) -> bytes:
    fd, data = proc.stdout.fileno(), []
    while chunk := os.read(fd, 4096):
        data.append(chunk)
    return b"".join(data)


def test_not_set_up_refuses_without_network(hands_db):
    client.configure("http://127.0.0.1:9119", None)
    with pytest.raises(client.HandsError) as refused:
        client.start_job("laptop", script="true", login=False, timeout=5)
    assert str(refused.value) == messages.NOT_SET_UP


def test_relay_down_is_reported(monkeypatch):
    def refuse(request):
        raise httpx.ConnectError("refused", request=request)

    transport = httpx.MockTransport(refuse)
    monkeypatch.setattr(
        client.httpx, "Client", functools.partial(httpx.Client, transport=transport)
    )
    client.configure("http://127.0.0.1:9119", "bearer")
    try:
        with pytest.raises(client.HandsError) as down:
            client.state()
        assert str(down.value) == messages.RELAY_DOWN
    finally:
        client.configure("http://127.0.0.1:9119", None)


def test_the_loopback_client_ignores_proxy_settings(monkeypatch):
    """V48: with a proxy in the environment, the local bearer still goes straight to the relay."""
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "all_proxy"):
        monkeypatch.setenv(name, "http://127.0.0.1:1")
    monkeypatch.delenv("NO_PROXY", raising=False)
    monkeypatch.delenv("no_proxy", raising=False)
    client.configure("http://127.0.0.1:9119", "bearer")  # the real transport, as in production
    try:
        assert client._http.trust_env is False
        assert not client._http._mounts  # httpx mounts one proxy transport per env proxy
    finally:
        client.configure("http://127.0.0.1:9119", None)


def test_state_gives_up_quickly_on_a_hung_dashboard(monkeypatch):
    timeouts = []

    def answer(request):
        timeouts.append(request.extensions["timeout"]["read"])
        return httpx.Response(200, json={"relay": True, "devices": [], "alerts": []})

    transport = httpx.MockTransport(answer)
    monkeypatch.setattr(
        client.httpx, "Client", functools.partial(httpx.Client, transport=transport)
    )
    client.configure("http://127.0.0.1:9119", "bearer")
    try:
        assert client.state()["relay"] is True
        assert timeouts == [5]  # not the 35 s output long-poll timeout
    finally:
        client.configure("http://127.0.0.1:9119", None)


@pytest.mark.parametrize(
    "status, reply",
    [
        (401, {"json": {"error": "unauthenticated", "detail": "Unauthorized"}}),  # token_auth.py:96
        (404, {"json": {"detail": "Not Found"}}),
        (502, {"text": "<html>bad gateway</html>"}),
    ],
)
def test_foreign_refusals_name_the_likely_causes(monkeypatch, status, reply):
    transport = httpx.MockTransport(lambda request: httpx.Response(status, **reply))
    monkeypatch.setattr(
        client.httpx, "Client", functools.partial(httpx.Client, transport=transport)
    )
    client.configure("http://127.0.0.1:9119", "bearer")
    try:
        with pytest.raises(client.HandsError) as refused:
            client.state()
        assert str(refused.value) == messages.relay_refused(status)
    finally:
        client.configure("http://127.0.0.1:9119", None)


def test_offline_device_refused_with_reason(wired, monkeypatch):
    Device()
    monkeypatch.setattr(store, "OFFLINE_AFTER", -1)
    with pytest.raises(client.HandsError) as offline:
        client.start_job("laptop", script="true", login=False, timeout=5)
    assert str(offline.value) == messages.offline("laptop")


def test_wrong_bearer_is_refused(wired):
    client.configure("http://127.0.0.1:9119", "not-the-local-bearer")
    with pytest.raises(client.HandsError) as refused:
        client.state()
    assert str(refused.value) == messages.UNAUTHENTICATED


def test_handle_runs_a_command_on_the_fake_device(wired):
    hub, _ = wired
    with Agent(hub, Device(), echo):
        proc = RemoteProcessHandle("laptop", "echo hello from the laptop", login=False, timeout=10)
        assert read_all(proc) == b"hello from the laptop\n"
        assert proc.wait(5) == 0


def failing_once(monkeypatch, route, error):
    """Make the next ``route`` request fail with ``error`` (an exception, or a status to answer)."""
    real = client._http.post
    left = [error]

    def post(path, **kwargs):
        if path == route and left:
            failure = left.pop()
            if isinstance(failure, Exception):
                raise failure
            return httpx.Response(failure, text="<html>bad gateway</html>")
        return real(path, **kwargs)

    monkeypatch.setattr(client._http, "post", post)
    monkeypatch.setattr(client, "_RETRY_DELAY", 0)


@pytest.mark.parametrize("error", [httpx.ConnectError("refused"), 502])
def test_a_lost_output_fetch_is_tried_again(wired, monkeypatch, error):
    """C23: one transport error or proxy 5xx mid-command no longer ends the command."""
    hub, _ = wired
    failing_once(monkeypatch, "/local/output", error)
    with Agent(hub, Device(), echo):
        proc = RemoteProcessHandle("laptop", "echo still here", login=False, timeout=10)
        assert read_all(proc) == b"still here\n"
        assert proc.wait(5) == 0


def test_starting_is_never_retried(wired, monkeypatch):
    Device()
    failing_once(monkeypatch, "/local/exec", httpx.ConnectError("refused"))
    with pytest.raises(client.RelayUnavailable) as down:
        client.start_job("laptop", script="true", login=False, timeout=5, stdin="x")
    assert str(down.value) == messages.RELAY_DOWN


@pytest.mark.parametrize(
    "error", [httpx.ReadError("reset"), httpx.ReadTimeout("slow"), httpx.RemoteProtocolError("cut")]
)
def test_a_start_whose_reply_was_lost_may_have_been_queued(wired, monkeypatch, error):
    """R56: only a request that never reached the relay (no connection) is "nothing ran"."""
    Device()
    failing_once(monkeypatch, "/local/exec", error)
    with pytest.raises(client.HandsError) as unknown:
        client.start_job("laptop", script="true", login=False, timeout=5)
    assert str(unknown.value) == messages.queue_unknown("laptop")
    assert "nothing ran" not in str(unknown.value)


@pytest.mark.parametrize("error", [httpx.ConnectTimeout("slow"), httpx.PoolTimeout("busy")])
def test_a_start_timeout_does_not_prove_the_request_was_unsent(wired, monkeypatch, error):
    Device()
    failing_once(monkeypatch, "/local/exec", error)
    with pytest.raises(client.HandsError) as unknown:
        client.start_job("laptop", script="true", login=False, timeout=5)
    assert str(unknown.value) == messages.queue_unknown("laptop")


@pytest.mark.parametrize("failure", [502, 200])
def test_a_start_without_a_usable_http_reply_may_have_been_queued(wired, monkeypatch, failure):
    Device()
    if failure == 200:
        real = client._http.post

        def post(path, **kwargs):
            if path == "/local/exec":
                return httpx.Response(200, json={"not": "a job id"})
            return real(path, **kwargs)

        monkeypatch.setattr(client._http, "post", post)
    else:
        failing_once(monkeypatch, "/local/exec", failure)
    with pytest.raises(client.HandsError) as unknown:
        client.start_job("laptop", script="true", login=False, timeout=5)
    assert str(unknown.value) == messages.queue_unknown("laptop")


def test_relay_gone_after_the_start_says_the_command_may_still_run(wired, monkeypatch):
    hub, _ = wired
    Device()
    monkeypatch.setattr(client, "_RETRY_DELAY", 0)
    real = client._http.post

    def post(path, **kwargs):
        if path == "/local/output":
            raise httpx.ConnectError("refused")
        return real(path, **kwargs)

    monkeypatch.setattr(client._http, "post", post)
    proc = RemoteProcessHandle("laptop", "make", login=False, timeout=10)
    assert read_all(proc).decode("utf-8") == messages.contact_lost("laptop")
    assert proc.wait(5) == 255
    [job] = hub._jobs.values()
    assert (job.done, job.returncode) == (True, relay.CANCELLED)  # the handle cancelled it


def test_request_screenshot_returns_png_bytes(wired):
    hub, _ = wired
    with Agent(hub, Device(), echo, png=b"\x89PNG\r\n\x1a\nfake"):
        assert client.request_screenshot("laptop") == b"\x89PNG\r\n\x1a\nfake"
        assert client.state()["devices"][0]["name"] == "laptop"


def test_unencodable_script_returns_a_hands_error_instead_of_silent_255(wired, monkeypatch):
    """A9: a lone surrogate in script text cannot be UTF-8 JSON-encoded; it must fail with a
    clear hands: error rather than killing the worker thread with silent exit 255."""
    hub, _ = wired
    unhandled = []
    monkeypatch.setattr(threading, "excepthook", unhandled.append)
    with Agent(hub, Device(), lambda job: ("ok\n", 0)) as agent:
        proc = RemoteProcessHandle("laptop", "echo \ud800", login=False, timeout=5)
        output = read_all(proc).decode("utf-8")
        assert proc.wait(5) == 255
        assert output.strip() == messages.BAD_SCRIPT_ENCODING
        assert unhandled == []
        assert agent.jobs == []

        # Surrogateescaped bytes in stdin still travel as stdin_b64 when the script itself is valid.
        proc_stdin = RemoteProcessHandle(
            "laptop", "cat > out", login=False, timeout=5, stdin="\udcff"
        )
        assert read_all(proc_stdin) == b"ok\n"
        assert proc_stdin.wait(5) == 0
        assert agent.jobs[0]["stdin_b64"] == "/w=="
        assert "stdin" not in agent.jobs[0]
