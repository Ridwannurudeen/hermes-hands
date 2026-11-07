"""The relay: per-device job queues, long-polls, ordered results and signed replies.

It lives in the ``hermes dashboard`` process only: ``dashboard/plugin_api.py`` calls :func:`serve` on
the first request, and only when ``app.state.gateway_runner`` is None. Replies to an authenticated
device are signed R2D with the relay key; the R2D nonce is the request's bearer nonce, which binds a
reply to the request it answers. Refusals to callers that proved no key are unsigned. ``/local/*``
serves the gateway's loopback client (``client.py``). Framework-free: handlers take ``(principal,
headers, path, body)``, device routes also the client address ``peer``, and return a :class:`Reply`.
"""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
import sqlite3
import threading
import time
from collections import deque
from dataclasses import dataclass, field

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from . import messages, protocol, store

PROVIDER = "hermes-hands"
API = "/api/plugins/hermes-hands"
TOKEN_ROUTES = tuple(
    API + route
    for route in (
        "/pair",
        "/device/poll",
        "/device/result",
        "/device/heartbeat",
        "/local/exec",
        "/local/output",
        "/local/cancel",
        "/local/state",
    )
)
SCOPE_PAIR = "hands:pair"
SCOPE_DEVICE = "hands:device"
SCOPE_UNKNOWN = "hands:unknown"  # a well-formed device bearer whose device_id is not paired
SCOPE_LOCAL = "hands:local"

UNREACHABLE = 255  # nothing ran: device offline, revoked or refused, relay down
CANCELLED = 137
MAX_POLL_WAIT = 25
MAX_OUTPUT_WAIT = 20
MAX_PNG_BYTES = 2 * 1024 * 1024
_MAX_PNG_B64 = 4 * -(-MAX_PNG_BYTES // 3)
_TICK = 0.05
_LIVENESS_EVERY = 1.0
_ABANDONED_AFTER = 60  # seconds without a gateway fetch: the caller is gone, cancel the job
_HEARTBEAT_EVERY = 5
_UNKNOWN_LIMIT = 10  # unknown_device replies per client address per window
_UNKNOWN_WINDOW = 60.0
_UNKNOWN_TRACKED = 10_000  # addresses remembered before expired windows are dropped

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Keys:
    """Everything derived from ``HANDS_RELAY_SEED``: the relay's Ed25519 key pair and the local bearer."""

    relay_private: bytes
    relay_public: bytes
    local_bearer: str


def derive_keys(seed: str) -> Keys:
    """HKDF-SHA256 over the seed, one label per key, so both processes derive the same keys."""

    def _hkdf(label: bytes) -> bytes:
        return HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=label).derive(
            seed.encode("utf-8")
        )

    private = _hkdf(b"hermes-hands/relay-signing-key")
    public = (
        Ed25519PrivateKey.from_private_bytes(private)
        .public_key()
        .public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    )
    return Keys(private, public, protocol.b64u(_hkdf(b"hermes-hands/local-bearer")))


@dataclass
class Reply:
    status: int
    body: bytes = b""
    headers: dict[str, str] = field(default_factory=dict)
    # Set when the device list changed (paired, newly refused): plugin_api.py broadcasts it as
    # ``plugin.hermes-hands.devices.changed`` so the Desktop pane refreshes before its next poll.
    devices_changed: dict | None = None

    @classmethod
    def json(cls, status: int, obj: dict) -> "Reply":
        return cls(status, json.dumps(obj, ensure_ascii=False).encode("utf-8"))


@dataclass
class _Job:
    job_id: str
    device_id: str
    device_name: str
    chunks: dict[int, str] = field(default_factory=dict)  # by device seq, not yet handed out
    received: int = 0  # device seqs 0..received-1 have all arrived
    final_seq: int | None = None
    returncode: int | None = None
    error: str | None = None
    png_b64: str | None = None
    done: bool = False
    picked: bool = False
    last_fetch: float = field(default_factory=time.monotonic)


def _has(principal, scope: str) -> bool:
    return (
        principal is not None
        and getattr(principal, "provider", None) == PROVIDER
        and scope in getattr(principal, "scopes", ())
    )


def _bearer(headers) -> str:
    scheme, _, token = headers.get("authorization", "").partition(" ")
    return token.strip() if scheme.strip().lower() == "bearer" else ""


def _pairing_detail(exc: store.PairingError) -> str:
    if exc.error == "name_taken":
        return messages.pair_name_taken(exc.name)
    if exc.error == "name_mismatch":
        return messages.pair_name_mismatch(exc.name)
    return messages.PAIR_BAD_CODE


class Relay:
    def __init__(self, keys: Keys):
        self._keys = keys
        self._lock = threading.Lock()
        self._jobs: dict[str, _Job] = {}
        self._queues: dict[str, deque] = {}
        self._nonces = protocol.NonceCache()
        self._unknown_hits: dict[
            str, tuple[float, int]
        ] = {}  # client address -> (window start, count)
        self._refused: set[str] = set()  # devices currently refused for a changed deny-list

    # --- lifecycle -----------------------------------------------------------------------------
    def start(self) -> None:
        threading.Thread(
            target=self._heartbeat_loop, name="hermes-hands-relay", daemon=True
        ).start()

    def _heartbeat_loop(self) -> None:
        while True:
            self._beat()
            time.sleep(_HEARTBEAT_EVERY)

    def _beat(self) -> None:
        """Write the liveness row the gateway reads (``store.relay_alive``) and reap abandoned jobs."""
        try:
            store.write_relay_heartbeat()
        except sqlite3.Error:
            logger.warning("hermes-hands: could not write the relay heartbeat", exc_info=True)
        self._reap()

    def _reap(self) -> None:
        """Cancel and drop jobs nobody has fetched for ``_ABANDONED_AFTER`` seconds."""
        cutoff = time.monotonic() - _ABANDONED_AFTER
        with self._lock:
            for job in [j for j in self._jobs.values() if j.last_fetch < cutoff]:
                self._cancel_locked(job)
                del self._jobs[job.job_id]

    # --- signing and job bookkeeping (caller holds no lock unless named *_locked) ----------------
    def _signed(self, status: int, obj: dict, path: str, nonce: str | None = None) -> Reply:
        body = protocol.canonical_json(obj)
        ts, nonce = int(time.time()), nonce or protocol.new_nonce()
        signature = protocol.sign_request(
            self._keys.relay_private, protocol.R2D, ts, nonce, "POST", path, body
        )
        return Reply(
            status,
            body,
            {"X-Hands-Ts": str(ts), "X-Hands-Nonce": nonce, "X-Hands-Signature": signature},
        )

    @staticmethod
    def _finish_locked(job: _Job, returncode: int, error: str) -> None:
        job.done, job.returncode, job.error = True, returncode, error

    def _cancel_locked(self, job: _Job) -> None:
        if job.done:
            return
        self._finish_locked(job, CANCELLED, messages.cancelled(job.device_name))
        if job.picked:
            cancel = {"job_id": secrets.token_hex(8), "type": "cancel", "target_job_id": job.job_id}
            self._queues.setdefault(job.device_id, deque()).append(cancel)

    def _fail_device(self, device_id: str, error: str) -> None:
        with self._lock:
            self._queues.pop(device_id, None)
            for job in self._jobs.values():
                if job.device_id == device_id and not job.done:
                    self._finish_locked(job, UNREACHABLE, error)

    def _pop(self, device_id: str) -> dict | None:
        with self._lock:
            queue = self._queues.get(device_id)
            while queue:
                payload = queue.popleft()
                if payload["type"] == "cancel":
                    return payload
                job = self._jobs.get(payload["job_id"])
                if job is not None and not job.done:
                    job.picked = True
                    return payload
        return None

    def _take_locked(self, job: _Job, after: int) -> dict:
        """Chunks from seq ``after`` on (older ones are freed); the job is dropped once done is handed out."""
        for seq in [s for s in job.chunks if s < after]:
            del job.chunks[seq]
        out = {
            "chunks": [job.chunks[s] for s in range(after, job.received)],
            "next": max(after, job.received),
            "done": job.done,
            "returncode": job.returncode if job.done else None,
            "error": job.error if job.done else None,
            "png_b64": job.png_b64 if job.done else None,
        }
        if job.done:
            self._jobs.pop(job.job_id, None)
        return out

    def _check_device(self, job: _Job) -> None:
        """Fail a running job whose device was revoked, went offline or changed its deny-list."""
        device = store.device_by_id(job.device_id)
        if device is None:
            error = messages.revoked(job.device_name)
        elif not device["policy_ok"]:
            error = messages.policy_changed(job.device_name)
        elif not device["online"]:
            error = messages.went_offline(job.device_name)
        else:
            return
        with self._lock:
            if not job.done:
                self._finish_locked(job, UNREACHABLE, error)

    # --- device routes -------------------------------------------------------------------------
    def _device(self, principal, headers, path: str, body: bytes, peer: str):
        """``(device, bearer nonce, None)`` for a verified device request, else ``(None, None, reply)``.
        The token provider checked the bearer's signature and freshness; this checks the request
        signature (X-Hands-Signature) over the same ts and nonce, and only then burns the nonce, so a
        caller without the device key cannot burn one. ``NonceCache.check_and_add`` is atomic, so of
        two concurrent copies of one request exactly one gets through. Refusals here are unsigned: a
        caller that has not proven a device key never gets a relay signature."""
        bearer = protocol.parse_bearer(_bearer(headers))
        if _has(principal, SCOPE_UNKNOWN):
            return None, None, self._unknown(peer)
        if (
            not _has(principal, SCOPE_DEVICE)
            or bearer is None
            or bearer.device_id != principal.principal
        ):
            return (
                None,
                None,
                Reply.json(401, {"error": "unauthenticated", "detail": messages.UNAUTHENTICATED}),
            )
        device = store.device_by_id(bearer.device_id)
        if device is None:
            return None, None, self._unknown(peer)
        if not protocol.verify_request(
            device["public_key"],
            headers.get("x-hands-signature", ""),
            protocol.D2R,
            bearer.ts,
            bearer.nonce,
            "POST",
            path,
            body,
        ):
            return (
                None,
                None,
                Reply.json(401, {"error": "bad_signature", "detail": messages.UNAUTHENTICATED}),
            )
        if not self._nonces.check_and_add(bearer.nonce, int(time.time())):
            return None, None, Reply.json(401, {"error": "replayed", "detail": messages.REPLAYED})
        return device, bearer.nonce, None

    def _unknown(self, peer: str) -> Reply:
        """``401 unknown_device`` (the device stops on it), unsigned, at most ``_UNKNOWN_LIMIT`` per
        client address per window; logged here because a forged bearer leaves no token-auth audit row."""
        now = time.monotonic()
        with self._lock:
            start, count = self._unknown_hits.get(peer, (now, 0))
            if now - start > _UNKNOWN_WINDOW:
                start, count = now, 0
            self._unknown_hits[peer] = (start, count + 1)
            if len(self._unknown_hits) > _UNKNOWN_TRACKED:
                self._unknown_hits = {
                    ip: hit
                    for ip, hit in self._unknown_hits.items()
                    if now - hit[0] <= _UNKNOWN_WINDOW
                }
                if len(self._unknown_hits) > _UNKNOWN_TRACKED:
                    self._unknown_hits.clear()
        if count >= _UNKNOWN_LIMIT:
            if count == _UNKNOWN_LIMIT:
                logger.warning("hermes-hands: rate-limiting unknown_device replies to %s", peer)
            return Reply.json(429, {"error": "rate_limited", "detail": messages.RATE_LIMITED})
        logger.warning(
            "hermes-hands: unknown_device from %s (unpaired or revoked device bearer)", peer
        )
        return Reply.json(401, {"error": "unknown_device", "detail": messages.UNKNOWN_DEVICE_WIRE})

    def _seen_refusal(
        self, ok: bool | None, device: dict, path: str, nonce: str, peer: str
    ) -> Reply | None:
        """The reply for a device whose ``record_seen`` came back unknown or policy-refused, else None."""
        if ok is None:
            return self._unknown(peer)
        if not ok:
            self._fail_device(device["device_id"], messages.policy_changed(device["name"]))
            reply = self._signed(
                409,
                {"error": "policy_changed", "detail": messages.POLICY_CHANGED_WIRE},
                path,
                nonce,
            )
            with self._lock:
                newly = device["device_id"] not in self._refused
                self._refused.add(device["device_id"])
            if newly:
                reply.devices_changed = {"reason": "policy_changed", "device": device["name"]}
            return reply
        with self._lock:
            self._refused.discard(device["device_id"])
        return None

    async def pair(self, principal, headers, path: str, body: bytes) -> Reply:
        if not _has(principal, SCOPE_PAIR):
            return Reply.json(401, {"error": "bad_code", "detail": messages.PAIR_BAD_CODE})
        try:
            request = json.loads(body)
            fields = {k: v for k, v in request.items() if k != "sig"}
            public_key = protocol.unb64u(request["public_key"])
            signature = protocol.unb64u(request["sig"])
            name, device_os, home, policy_hash, version = (
                str(request[k]) for k in ("name", "os", "home", "policy_hash", "protocol")
            )
        except (ValueError, KeyError, TypeError, AttributeError):
            return self._signed(400, {"error": "bad_request", "detail": messages.BAD_REQUEST}, path)
        if version != protocol.PROTOCOL_VERSION:
            return self._signed(
                426,
                {
                    "error": "protocol_mismatch",
                    "detail": messages.protocol_mismatch(version),
                    "upgrade": messages.UPGRADE,
                },
                path,
            )
        if not protocol.verify(public_key, signature, protocol.canonical_json(fields)):
            return self._signed(
                401, {"error": "bad_signature", "detail": messages.PAIR_BAD_SIGNATURE}, path
            )
        try:
            device_id = await asyncio.to_thread(
                store.complete_pairing,
                _bearer(headers),
                name=name,
                public_key=request["public_key"],
                os=device_os,
                home=home,
                policy_hash=policy_hash,
            )
        except store.PairingError as exc:
            return self._signed(
                exc.status, {"error": exc.error, "detail": _pairing_detail(exc)}, path
            )
        reply = self._signed(
            200,
            {"device_id": device_id, "relay_public_key": protocol.b64u(self._keys.relay_public)},
            path,
        )
        reply.devices_changed = {"reason": "paired", "device": name}
        return reply

    async def device_poll(self, principal, headers, path: str, body: bytes, peer: str) -> Reply:
        device, nonce, refusal = await asyncio.to_thread(
            self._device, principal, headers, path, body, peer
        )
        if refusal is not None:
            return refusal
        try:
            wait = min(max(int(json.loads(body).get("wait", MAX_POLL_WAIT)), 0), MAX_POLL_WAIT)
        except (ValueError, TypeError, AttributeError):
            return self._signed(
                400, {"error": "bad_request", "detail": messages.BAD_REQUEST}, path, nonce
            )
        ok = await asyncio.to_thread(store.record_seen, device["device_id"])
        refusal = self._seen_refusal(ok, device, path, nonce, peer)
        if refusal is not None:
            return refusal
        deadline = time.monotonic() + wait
        while True:
            job = self._pop(device["device_id"])
            if job is not None:
                return self._signed(200, job, path, nonce)
            if time.monotonic() >= deadline:
                return Reply(204)
            await asyncio.sleep(_TICK)

    async def device_result(self, principal, headers, path: str, body: bytes, peer: str) -> Reply:
        device, nonce, refusal = await asyncio.to_thread(
            self._device, principal, headers, path, body, peer
        )
        if refusal is not None:
            return refusal
        try:
            result = json.loads(body)
            job_id, seq, chunk = (
                str(result["job_id"]),
                int(result["seq"]),
                str(result.get("chunk", "")),
            )
            done, policy_hash = bool(result["done"]), str(result["policy_hash"])
            returncode = None if result.get("returncode") is None else int(result["returncode"])
            error, png_b64 = result.get("error"), result.get("png_b64")
            if seq < 0 or (png_b64 is not None and not isinstance(png_b64, str)):
                raise ValueError("bad seq or png_b64")
        except (ValueError, KeyError, TypeError, AttributeError):
            return self._signed(
                400, {"error": "bad_request", "detail": messages.BAD_REQUEST}, path, nonce
            )
        ok = await asyncio.to_thread(
            store.record_seen, device["device_id"], policy_hash=policy_hash
        )
        refusal = self._seen_refusal(ok, device, path, nonce, peer)
        if refusal is not None:
            return refusal
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None or job.device_id != device["device_id"] or job.done:
                return self._signed(200, {"ok": True, "cancel": True}, path, nonce)
            if png_b64 is not None and len(png_b64) > _MAX_PNG_B64:
                self._finish_locked(job, UNREACHABLE, messages.SCREENSHOT_TOO_LARGE)
                return self._signed(
                    413,
                    {"error": "too_large", "detail": messages.SCREENSHOT_TOO_LARGE},
                    path,
                    nonce,
                )
            if seq >= job.received and seq not in job.chunks:
                job.chunks[seq] = chunk
                if done:
                    job.final_seq, job.returncode = seq, returncode
                    job.error, job.png_b64 = (str(error) if error else None), png_b64
                while job.received in job.chunks:
                    job.received += 1
                if job.final_seq is not None and job.received > job.final_seq:
                    job.done = True
        return self._signed(200, {"ok": True, "cancel": False}, path, nonce)

    async def device_heartbeat(
        self, principal, headers, path: str, body: bytes, peer: str
    ) -> Reply:
        device, nonce, refusal = await asyncio.to_thread(
            self._device, principal, headers, path, body, peer
        )
        if refusal is not None:
            return refusal
        try:
            beat = json.loads(body)
            policy_hash, device_os, home = (str(beat[k]) for k in ("policy_hash", "os", "home"))
        except (ValueError, KeyError, TypeError, AttributeError):
            return self._signed(
                400, {"error": "bad_request", "detail": messages.BAD_REQUEST}, path, nonce
            )
        ok = await asyncio.to_thread(
            store.record_seen, device["device_id"], policy_hash=policy_hash, os=device_os, home=home
        )
        refusal = self._seen_refusal(ok, device, path, nonce, peer)
        if refusal is not None:
            return refusal
        return self._signed(200, {"ok": True}, path, nonce)

    # --- local routes (gateway loopback client) ------------------------------------------------
    @staticmethod
    def _local_refusal(principal, headers) -> Reply | None:
        if "x-forwarded-for" in headers or "forwarded" in headers:
            return Reply.json(403, {"error": "forwarded", "detail": messages.FORWARDED})
        if not _has(principal, SCOPE_LOCAL):
            return Reply.json(401, {"error": "unauthenticated", "detail": messages.UNAUTHENTICATED})
        return None

    async def local_exec(self, principal, headers, path: str, body: bytes) -> Reply:
        refusal = self._local_refusal(principal, headers)
        if refusal is not None:
            return refusal
        try:
            request = json.loads(body)
            name, kind = str(request["device"]), str(request.get("type", "exec"))
            if kind == "exec":
                extra = {
                    "script": str(request["script"]),
                    "login": bool(request.get("login", False)),
                    "timeout": int(request["timeout"]),
                }
            elif kind == "screenshot":
                extra = {}
            else:
                raise ValueError(f"unknown job type {kind!r}")
        except (ValueError, KeyError, TypeError, AttributeError):
            return Reply.json(400, {"error": "bad_request", "detail": messages.BAD_REQUEST})
        device = await asyncio.to_thread(store.get_device, name)
        if device is None:
            return Reply.json(
                404, {"error": "unknown_device", "detail": messages.unknown_device(name)}
            )
        if not device["policy_ok"]:
            return Reply.json(
                409, {"error": "policy_changed", "detail": messages.policy_changed(name)}
            )
        if not device["online"]:
            return Reply.json(409, {"error": "offline", "detail": messages.offline(name)})
        job_id = secrets.token_hex(8)
        payload = {"job_id": job_id, "type": kind, **extra}
        with self._lock:
            self._jobs[job_id] = _Job(job_id, device["device_id"], name)
            self._queues.setdefault(device["device_id"], deque()).append(payload)
        return Reply.json(200, {"job_id": job_id})

    async def local_output(self, principal, headers, path: str, body: bytes) -> Reply:
        refusal = self._local_refusal(principal, headers)
        if refusal is not None:
            return refusal
        try:
            request = json.loads(body)
            job_id, after = str(request["job_id"]), max(int(request.get("after", 0)), 0)
            wait = min(max(float(request.get("wait", MAX_OUTPUT_WAIT)), 0.0), MAX_OUTPUT_WAIT)
        except (ValueError, KeyError, TypeError, AttributeError):
            return Reply.json(400, {"error": "bad_request", "detail": messages.BAD_REQUEST})
        now = time.monotonic()
        deadline, next_check = now + wait, now
        while True:
            with self._lock:
                job = self._jobs.get(job_id)
                if job is None:
                    return Reply.json(404, {"error": "unknown_job", "detail": messages.RELAY_LOST})
                job.last_fetch = time.monotonic()
                if job.done or job.received > after or job.last_fetch >= deadline:
                    return Reply.json(200, self._take_locked(job, after))
            if time.monotonic() >= next_check:
                await asyncio.to_thread(self._check_device, job)
                next_check = time.monotonic() + _LIVENESS_EVERY
                continue
            await asyncio.sleep(_TICK)

    async def local_cancel(self, principal, headers, path: str, body: bytes) -> Reply:
        refusal = self._local_refusal(principal, headers)
        if refusal is not None:
            return refusal
        try:
            job_id = str(json.loads(body)["job_id"])
        except (ValueError, KeyError, TypeError):
            return Reply.json(400, {"error": "bad_request", "detail": messages.BAD_REQUEST})
        with self._lock:
            job = self._jobs.get(job_id)
            if job is not None:
                self._cancel_locked(job)
        return Reply.json(200, {"ok": True})

    async def local_state(self, principal, headers, path: str, body: bytes) -> Reply:
        refusal = self._local_refusal(principal, headers)
        if refusal is not None:
            return refusal
        return Reply.json(200, await self.state())

    async def state(self) -> dict:
        """What ``/local/state`` and ``/ui/state`` show: the relay is up, every paired device, and an
        alert for each device refused for a changed deny-list."""
        devices = await asyncio.to_thread(store.list_devices)
        return {"relay": True, "devices": devices, "alerts": messages.alerts(devices)}


_relay: Relay | None = None
_relay_lock = threading.Lock()


def serve(keys: Keys | None, *, here: bool) -> Relay | Reply:
    """This process's relay, created and started on first use; a 503 :class:`Reply` when this process
    cannot be the relay (no seed yet, or it is the gateway, whose copy of the routes stays inert)."""
    global _relay
    if keys is None:
        return Reply.json(503, {"error": "not_set_up", "detail": messages.NOT_SET_UP})
    if not here:
        return Reply.json(503, {"error": "relay_elsewhere", "detail": messages.RELAY_ELSEWHERE})
    with _relay_lock:
        if _relay is None:
            _relay = Relay(keys)
            _relay.start()
        return _relay
