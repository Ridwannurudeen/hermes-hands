from __future__ import annotations

import base64
import codecs
import json
import logging
import platform
import re
import secrets
import threading
import time
from pathlib import Path
from urllib.parse import urlparse

import httpx

from . import __version__, config, executor, policy, screen
from .keys import get_or_create
from .protocol import D2R, R2D, canonical_json, fresh, make_bearer, new_nonce, sign_request, verify_request

logger = logging.getLogger(__name__)

_BACKGROUND_PID_PROBE = re.compile(
    r"(?ms)^eval 'kill -0 \"\$\(cat "
    r"(('\\'')?[^\n;&|$()<>`']*/hermes_bg_[\w-]+\.pid(?:'\\'')?) 2>/dev/null\)\" "
    r"2>/dev/null; echo \$\?(?:\n\n__hermes_rc=\$\? __hermes_ps=\(\"\$\{PIPESTATUS\[@\]\}\"\); "
    r"\(exit \$__hermes_rc\) && :)?'$"
)
_CORE_EVAL_COMMAND = re.compile(
    r"(?ms)^eval '((?:[^']|'\\'')*)\n\n__hermes_rc=\$\? __hermes_ps=\(\"\$\{PIPESTATUS\[@\]\}\"\); \(exit \$__hermes_rc\) && :'$"
)


class DeviceLoop:
    def __init__(self, *, allow_screen: bool = False, transport=None):
        self.allow_screen = allow_screen
        self.private, self.public = get_or_create()
        self.running: dict[str, threading.Thread] = {}
        self.processes: dict[str, tuple[object, float]] = {}
        self.cancelled_jobs: set[str] = set()
        self.process_lock = threading.Lock()
        self.job_deadlines: dict[str, float] = {}
        self.stop_event = threading.Event()
        self.fatal: list[Exception] = []
        self.policy_change_logged = False
        self.instance = secrets.token_hex(8)
        self.poll_retry_delay = 1
        self.heartbeat_retry_delay = 1
        self.last_auth_warning = 0.0
        self.accepted_policy_hash = None
        self.client = httpx.Client(transport=transport, timeout=35)

    def _post(self, endpoint: str, body_obj: dict) -> httpx.Response:
        body = canonical_json(body_obj)
        path = urlparse(config.gateway_url()).path.rstrip("/") + endpoint
        ts, nonce = int(time.time()), new_nonce()
        headers = {
            "Content-Type": "application/json",
            "Authorization": "Bearer " + make_bearer(config.device_id(), self.private, ts=ts, nonce=nonce),
            "X-Hands-Ts": str(ts), "X-Hands-Nonce": nonce,
            "X-Hands-Signature": sign_request(self.private, D2R, ts, nonce, "POST", path, body),
        }
        return self.client.post(config.gateway_url() + endpoint, content=body, headers=headers)

    def _verify_response(self, response: httpx.Response, endpoint: str) -> dict:
        if response.status_code in (401, 409):
            try:
                payload = response.json()
            except (ValueError, json.JSONDecodeError):
                payload = {}
            expected = "unknown_device" if response.status_code == 401 else "policy_changed"
            if payload.get("error") == expected and self._error_signature_valid(response, endpoint):
                if expected == "unknown_device":
                    self._cancel_all()
                    self.fatal.append(PermissionError("unknown_device"))
                    raise PermissionError("unknown_device")
                self._cancel_running_jobs()
                if not self.policy_change_logged:
                    logger.warning("Relay policy changed; cancelling running jobs and backing off")
                    self.policy_change_logged = True
                raise PermissionError("policy_changed")
            self._transient_status(response.status_code)
            raise ValueError(f"transient relay status {response.status_code}")
        if response.status_code == 429:
            self._transient_status(response.status_code)
            raise ValueError("transient relay status 429")
        if response.status_code == 204:
            return {}
        if response.status_code != 200:
            raise ValueError(f"unexpected relay status {response.status_code}")
        if not self._response_signature_valid(response, endpoint):
            logger.warning("Dropping invalid relay reply for %s", endpoint)
            raise PermissionError("invalid relay response")
        payload = response.json()
        if endpoint in ("/device/heartbeat", "/device/result") or (endpoint == "/device/poll" and payload):
            request = json.loads(response.request.content)
            self.accepted_policy_hash = request["policy_hash"]
        return payload

    def _response_signature_valid(self, response: httpx.Response, endpoint: str) -> bool:
        try:
            ts = int(response.headers["X-Hands-Ts"])
            nonce = response.headers["X-Hands-Nonce"]
            request_nonce = response.request.headers["X-Hands-Nonce"]
            signature = response.headers["X-Hands-Signature"]
            path = urlparse(config.gateway_url()).path.rstrip("/") + endpoint
            return nonce == request_nonce and fresh(ts, int(time.time())) and verify_request(
                config.relay_public_key(), signature, R2D, ts, nonce, "POST", path, response.content
            )
        except (KeyError, ValueError, httpx.RequestNotRead):
            return False

    def _error_signature_valid(self, response: httpx.Response, endpoint: str) -> bool:
        return self._response_signature_valid(response, endpoint)

    def _transient_status(self, status: int) -> None:
        now = time.monotonic()
        if now - self.last_auth_warning >= 60:
            logger.warning(
                "Relay returned HTTP %s; retrying. Confirm this device is still paired and that the dashboard is using the same relay data directory.",
                status,
            )
            self.last_auth_warning = now

    def _retry(self, counter: str = "poll_retry_delay", *, cap: int = 3) -> None:
        if not hasattr(self, counter):
            setattr(self, counter, 1)
        delay = min(cap, getattr(self, counter))
        setattr(self, counter, min(cap, delay * 2))
        self.stop_event.wait(delay)

    def _poll_body(self) -> dict:
        return {"wait": 25, "policy_hash": policy.digest(), "instance": self.instance}

    def _heartbeat_body(self) -> dict:
        return {"policy_hash": policy.digest(), "os": platform.platform(), "home": str(Path.home()), "agent_version": __version__, "instance": self.instance}

    def _cancel_all(self) -> None:
        self.stop_event.set()
        self._cancel_running_jobs()

    def _cancel_running_jobs(self) -> None:
        if not hasattr(self, "process_lock"):
            self.process_lock = threading.Lock()
        with self.process_lock:
            processes = list(self.processes.values())
        for proc, started in processes:
            if proc.poll() is None:
                from .executor import _terminate

                _terminate(proc, started)

    def _result(self, job_id: str, seq: int, *, chunk: str = "", done: bool = False, returncode: int | None = None, error: str | None = None, png: bytes | None = None, policy_hash: str | None = None) -> None:
        if not hasattr(self, "process_lock"):
            self.process_lock = threading.Lock()
        result = {"job_id": job_id, "seq": seq, "chunk": chunk, "done": done, "policy_hash": policy.digest() if policy_hash is None else policy_hash}
        if returncode is not None:
            result["returncode"] = returncode
        if error:
            result["error"] = error
        if png is not None:
            result["png_b64"] = base64.b64encode(png).decode("ascii")
        while not self.stop_event.is_set():
            try:
                response = self._post("/device/result", result)
                if response.status_code >= 500:
                    response.raise_for_status()
                payload = self._verify_response(response, "/device/result")
                if payload.get("cancel"):
                    with self.process_lock:
                        target = self.processes.get(job_id)
                        if target is None:
                            self.cancelled_jobs.add(job_id)
                            if len(self.cancelled_jobs) > 1024:
                                self.cancelled_jobs.pop()
                    if target is not None and target[0].poll() is None:
                        executor._terminate(target[0], target[1])
                    raise InterruptedError("relay cancelled the job")
                return
            except (httpx.HTTPError, ValueError):
                deadline = getattr(self, "job_deadlines", {}).get(job_id)
                if deadline is not None and time.monotonic() >= deadline:
                    logger.warning("Stopping result retries for %s after its job deadline", job_id)
                    return
                self.stop_event.wait(1)
        logger.debug("Dropping result for %s because the device is stopping", job_id)

    def _run_job(self, job: dict) -> None:
        job_id, seq = job["job_id"], 0
        if not hasattr(self, "cancelled_jobs"):
            self.cancelled_jobs = set()
        if not hasattr(self, "process_lock"):
            self.process_lock = threading.Lock()
        if not hasattr(self, "job_deadlines"):
            self.job_deadlines = {}
        if not hasattr(self, "instance"):
            self.instance = secrets.token_hex(8)
        tail = b""
        try:
            if job["type"] == "exec":
                current_policy_hash, policy_patterns = policy.snapshot()
                if current_policy_hash != getattr(self, "accepted_policy_hash", None):
                    self._result(
                        job_id,
                        seq,
                        chunk="Device policy changed. The owner must run /hands accept-policy <device>.\n",
                        done=True,
                        returncode=126,
                        policy_hash=current_policy_hash,
                    )
                    return
                script = job["script"]
                policy_script = _BACKGROUND_PID_PROBE.sub("", script)
                wrapped_command = _CORE_EVAL_COMMAND.search(policy_script)
                if wrapped_command:
                    policy_script = wrapped_command.group(1).replace("'\\''", "'")
                refused = policy.refusal(policy_script, policy_patterns)
                if refused:
                    self._result(job_id, seq, chunk=f"Policy refused command matching: {refused}\n", done=True, returncode=126)
                    return
                exit_code = [0]
                capped = threading.Event()
                timeout = job.get("timeout", 120)
                self.job_deadlines[job_id] = time.monotonic() + timeout
                decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
                def on_start(proc, started):
                    with self.process_lock:
                        self.processes[job_id] = (proc, started)
                        cancelled = job_id in self.cancelled_jobs
                        self.cancelled_jobs.discard(job_id)
                    if cancelled:
                        executor._terminate(proc, started)
                for chunk in executor.run(job["script"], login=job.get("login", False), timeout=timeout, job_id=job_id, stdin=job.get("stdin"), on_start=on_start, on_exit=lambda code: exit_code.__setitem__(0, code), on_capped=capped.set):
                    with self.process_lock:
                        process = self.processes.get(job_id)
                    if process is not None and process[0].poll() is not None:
                        tail += chunk  # read after bash exited: it goes with done, in one result
                        if len(tail) < executor.CHUNK_SIZE:
                            continue
                        chunk, tail = tail, b""
                    self._result(job_id, seq, chunk=decoder.decode(chunk))
                    seq += 1
                final = decoder.decode(tail, final=True)
                if capped.is_set():
                    self._result(job_id, seq, chunk=final, done=True, returncode=1, error="Output exceeded the 64 MiB device limit")
                else:
                    self._result(job_id, seq, chunk=final, done=True, returncode=exit_code[0])
            elif job["type"] == "screenshot":
                image = screen.capture(allow_screen=self.allow_screen)
                self._result(job_id, seq, done=True, png=image)
            elif job["type"] == "cancel":
                with self.process_lock:
                    target = self.processes.get(job["target_job_id"])
                    if target is None:
                        self.cancelled_jobs.add(job["target_job_id"])
                        if len(self.cancelled_jobs) > 1024:
                            self.cancelled_jobs.pop()
                if target and target[0].poll() is None:
                    executor._terminate(target[0], target[1])
                self._result(job_id, seq, done=True, returncode=0)
        except InterruptedError:
            return
        except PermissionError as exc:
            if self.stop_event.is_set():
                logger.debug("Dropping result for %s because the device is stopping", job_id)
                return
            if str(exc) == "unknown_device":
                return
            if str(exc) == "policy_changed":
                self._retry("poll_retry_delay", cap=5)
                return
            try:
                if tail:
                    self._result(job_id, seq, chunk=tail.decode("utf-8", errors="replace"))
                    seq += 1
                self._result(job_id, seq, done=True, returncode=1, error=str(exc))
            except Exception:
                logger.debug("Could not deliver job failure for %s", job_id, exc_info=True)
        except Exception as exc:
            if self.stop_event.is_set():
                logger.debug("Dropping result for %s because the device is stopping", job_id)
                return
            try:
                if tail:
                    self._result(job_id, seq, chunk=tail.decode("utf-8", errors="replace"))
                    seq += 1
                self._result(job_id, seq, done=True, returncode=1, error=str(exc))
            except Exception:
                logger.debug("Could not deliver job failure for %s", job_id, exc_info=True)
        finally:
            with self.process_lock:
                self.processes.pop(job_id, None)
                self.cancelled_jobs.discard(job_id)
            self.running.pop(job_id, None)
            self.job_deadlines.pop(job_id, None)

    def _heartbeat_loop(self) -> None:
        if not hasattr(self, "heartbeat_retry_delay"):
            self.heartbeat_retry_delay = 1
        while not self.stop_event.is_set():
            try:
                response = self._post("/device/heartbeat", self._heartbeat_body())
                self._verify_response(response, "/device/heartbeat")
                self.heartbeat_retry_delay = 1
                self.stop_event.wait(5)
            except PermissionError as exc:
                if str(exc) == "unknown_device":
                    self.stop_event.set()
                    return
                if str(exc) == "policy_changed":
                    self._retry("heartbeat_retry_delay", cap=5)
                    continue
                self._retry("heartbeat_retry_delay")
            except (httpx.HTTPError, KeyError, ValueError, json.JSONDecodeError):
                self._retry("heartbeat_retry_delay")

    def run_forever(self) -> None:
        if not hasattr(self, "poll_retry_delay"):
            self.poll_retry_delay = 1
        executor.reap_recorded_processes()
        heartbeat = threading.Thread(target=self._heartbeat_loop, daemon=True)
        heartbeat.start()
        try:
            while not self.stop_event.is_set():
                try:
                    response = self._post("/device/poll", self._poll_body())
                    job = self._verify_response(response, "/device/poll")
                    if self.fatal:
                        raise self.fatal[0]
                    if job:
                        thread = threading.Thread(target=self._run_job, args=(job,), daemon=True)
                        self.running[job["job_id"]] = thread
                        thread.start()
                    self.poll_retry_delay = 1
                    if self.fatal:
                        raise self.fatal[0]
                except PermissionError as exc:
                    if str(exc) == "unknown_device":
                        raise
                    if str(exc) == "policy_changed":
                        self._retry("poll_retry_delay", cap=5)
                        continue
                    self._retry("poll_retry_delay")
                except (httpx.HTTPError, KeyError, ValueError, json.JSONDecodeError):
                    logger.warning("Device poll failed; retrying")
                    self._retry("poll_retry_delay")
        finally:
            self.stop_event.set()
            self._cancel_running_jobs()


