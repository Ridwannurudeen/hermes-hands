"""The gateway client and RemoteProcessHandle against a real Relay, through the MockTransport seam,
with a fake device agent on a thread."""

import functools
import os

import httpx
import pytest

from hands import client, messages, store
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


def test_request_screenshot_returns_png_bytes(wired):
    hub, _ = wired
    with Agent(hub, Device(), echo, png=b"\x89PNG\r\n\x1a\nfake"):
        assert client.request_screenshot("laptop") == b"\x89PNG\r\n\x1a\nfake"
        assert client.state()["devices"][0]["name"] == "laptop"
