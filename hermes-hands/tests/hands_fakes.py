"""Test doubles for the gateway-half tests: a fake ``plugins.plugin_storage``, a signing device, a
Relay wired to ``client`` through an httpx MockTransport playing the dashboard token seam, and a fake
device agent thread."""

from __future__ import annotations

import asyncio
import base64
import functools
import json
import sqlite3
import sys
import threading
import time
import types
from dataclasses import dataclass

import httpx
import pytest

from hands import client, protocol, relay, store

SEED = "ab" * 32
_LOCAL_ROUTES = {
    "/local/exec": "local_exec",
    "/local/output": "local_output",
    "/local/cancel": "local_cancel",
    "/local/state": "local_state",
}


@pytest.fixture
def hands_db(tmp_path, monkeypatch):
    """``plugins.plugin_storage.plugin_db`` opening plain SQLite files under ``tmp_path``."""
    storage = types.ModuleType("plugins.plugin_storage")

    def plugin_db(name, filename="data.db"):
        assert name == "hermes-hands"
        return sqlite3.connect(tmp_path / filename, timeout=5, check_same_thread=False)

    storage.plugin_db = plugin_db
    monkeypatch.setitem(sys.modules, "plugins.plugin_storage", storage)
    monkeypatch.setattr(store, "OFFLINE_AFTER", 20)
    return tmp_path


@dataclass(frozen=True)
class Principal:
    """Shape of ``hermes_cli.dashboard_auth.TokenPrincipal``."""

    principal: str
    provider: str = relay.PROVIDER
    scopes: tuple[str, ...] = ()


LOCAL = Principal("local", scopes=(relay.SCOPE_LOCAL,))


class Device:
    """A paired device that signs its requests exactly as the device agent does."""

    def __init__(
        self,
        name: str = "laptop",
        policy_hash: str = "p0",
        home: str = "C:\\Users\\ridwan",
        device_os: str = "Windows-11-10.0.26300-SP0",
    ):
        self.name = name
        self.policy_hash = policy_hash
        self.private, self.public = protocol.generate_keypair()
        code = store.mint_pairing_code(name)
        self.device_id = store.complete_pairing(
            code,
            name=name,
            public_key=protocol.b64u(self.public),
            os=device_os,
            home=home,
            policy_hash=policy_hash,
        )

    def request(self, route: str, body: dict, *, tamper: bool = False):
        """``(principal, headers, path, body)`` for a signed device request to ``route``."""
        path = relay.API + route
        raw = protocol.canonical_json(body)
        ts, nonce = int(time.time()), protocol.new_nonce()
        signature = protocol.sign_request(self.private, protocol.D2R, ts, nonce, "POST", path, raw)
        headers = {
            "authorization": "Bearer "
            + protocol.make_bearer(self.device_id, self.private, ts=ts, nonce=nonce),
            "x-hands-signature": signature,
        }
        if tamper:
            raw += b" "
        return Principal(self.device_id, scopes=(relay.SCOPE_DEVICE,)), headers, path, raw


@pytest.fixture
def wired(hands_db, monkeypatch):
    """A Relay reachable through ``client``; returns ``(hub, keys)``."""
    monkeypatch.setattr(relay, "_TICK", 0.01)
    keys = relay.derive_keys(SEED)
    hub = relay.Relay(keys)

    def seam(request: httpx.Request) -> httpx.Response:
        token = request.headers["authorization"].split(" ", 1)[1]
        principal = LOCAL if token == keys.local_bearer else None
        handler = getattr(hub, _LOCAL_ROUTES[request.url.path.removeprefix(relay.API)])
        reply = asyncio.run(handler(principal, request.headers, request.url.path, request.content))
        return httpx.Response(reply.status, content=reply.body, headers=reply.headers)

    transport = httpx.MockTransport(seam)
    monkeypatch.setattr(
        client.httpx, "Client", functools.partial(httpx.Client, transport=transport)
    )
    client.configure("http://127.0.0.1:9119/", keys.local_bearer)
    yield hub, keys
    client.configure("http://127.0.0.1:9119", None)


class Agent:
    """Polls the relay like the device agent. ``run(job) -> (output, returncode)`` executes exec
    jobs; screenshots answer with ``png``. Use as a context manager so the thread is joined."""

    def __init__(self, hub, device, run, png=b"\x89PNG"):
        self.hub, self.device, self.run, self.png = hub, device, run, png
        self.jobs: list[dict] = []
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self._loop, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.stop.set()
        self.thread.join(5)

    def _call(self, handler, route, payload):
        principal, headers, path, raw = self.device.request(route, payload)
        return asyncio.run(getattr(self.hub, handler)(principal, headers, path, raw, "10.0.0.2"))

    def _result(self, job_id, seq, chunk="", done=False, **extra):
        payload = {"job_id": job_id, "seq": seq, "chunk": chunk, "done": done}
        self._call("device_result", "/device/result", {**payload, "policy_hash": "p0", **extra})

    def _loop(self):
        while not self.stop.is_set():
            reply = self._call("device_poll", "/device/poll", {"wait": 1})
            if reply.status != 200:
                continue
            job = json.loads(reply.body)
            self.jobs.append(job)
            if job["type"] == "screenshot":
                png = base64.b64encode(self.png).decode()
                self._result(job["job_id"], 0, done=True, png_b64=png)
            elif job["type"] == "exec":
                output, returncode = self.run(job)
                self._result(job["job_id"], 0, output)
                self._result(job["job_id"], 1, done=True, returncode=returncode)
