from __future__ import annotations

import base64
import json
import logging
import platform
import random
import re
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
    r"([^\n;&|$()<>`]*/hermes_bg_[\w-]+\.pid)(?:'\\'')? 2>/dev/null\)\" "
    r"2>/dev/null; echo \$\?(?:\n\n__hermes_rc=\$\? __hermes_ps=\(\"\$\{PIPESTATUS\[@\]\}\"\); "
    r"\(exit \$__hermes_rc\) && :)?'$"
)


class DeviceLoop:
    def __init__(self, *, allow_screen: bool = False, transport=None):
        self.allow_screen = allow_screen
        self.private, self.public = get_or_create()
        self.running: dict[str, threading.Thread] = {}
        self.processes: dict[str, tuple[object, float]] = {}
        self.stop_event = threading.Event()
        self.fatal: list[Exception] = []
        self.policy_change_logged = False
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
        if response.status_code == 401:
            if response.json().get("error") == "unknown_device":
                self._cancel_all()
                self.fatal.append(PermissionError("unknown_device"))
                raise PermissionError("unknown_device")
            raise PermissionError("device authentication failed")
        if response.status_code == 204:
            return {}
        if response.status_code == 409:
            if response.json().get("error") == "policy_changed":
                self._cancel_running_jobs()
                if not self.policy_change_logged:
                    logger.warning("Relay policy changed; cancelling running jobs and backing off")
                    self.policy_change_logged = True
                raise PermissionError("policy_changed")
            raise ValueError(f"unexpected relay status {response.status_code}")
        if response.status_code != 200:
            raise ValueError(f"unexpected relay status {response.status_code}")
        try:
            ts = int(response.headers["X-Hands-Ts"])
            nonce = response.headers["X-Hands-Nonce"]
            request_nonce = response.request.headers["X-Hands-Nonce"]
            signature = response.headers["X-Hands-Signature"]
            path = urlparse(config.gateway_url()).path.rstrip("/") + endpoint
            valid = nonce == request_nonce and fresh(ts, int(time.time())) and verify_request(
                config.relay_public_key(), signature, R2D, ts, nonce, "POST", path, response.content
            )
        except (KeyError, ValueError, httpx.RequestNotRead):
            valid = False
        if not valid:
            logger.warning("Dropping invalid relay reply for %s", endpoint)
            raise PermissionError("invalid relay response")
        payload = response.json()
        return payload

    def _cancel_all(self) -> None:
        self.stop_event.set()
        self._cancel_running_jobs()

    def _cancel_running_jobs(self) -> None:
        for job_id in list(self.processes):
            proc, started = self.processes[job_id]
            if proc.poll() is None:
                from .executor import _terminate

                _terminate(proc, started)

    def _result(self, job_id: str, seq: int, *, chunk: str = "", done: bool = False, returncode: int | None = None, error: str | None = None, png: bytes | None = None) -> None:
        result = {"job_id": job_id, "seq": seq, "chunk": chunk, "done": done, "policy_hash": policy.digest()}
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
                self._verify_response(response, "/device/result")
                return
            except httpx.HTTPError:
                self.stop_event.wait(2)
        logger.debug("Dropping result for %s because the device is stopping", job_id)

    def _run_job(self, job: dict) -> None:
        job_id, seq = job["job_id"], 0
        try:
            if job["type"] == "exec":
                script = job["script"]
                policy_script = _BACKGROUND_PID_PROBE.sub("", script)
                refused = policy.refusal(policy_script)
                if refused:
                    self._result(job_id, seq, chunk=f"Policy refused command matching: {refused}\n", done=True, returncode=126)
                    return
                exit_code = [0]
                for chunk in executor.run(job["script"], login=job.get("login", False), timeout=job.get("timeout", 120), job_id=job_id, on_start=lambda proc, started: self.processes.__setitem__(job_id, (proc, started)), on_exit=lambda code: exit_code.__setitem__(0, code)):
                    self._result(job_id, seq, chunk=chunk.decode("utf-8", errors="replace"))
                    seq += 1
                self._result(job_id, seq, done=True, returncode=exit_code[0])
            elif job["type"] == "screenshot":
                image = screen.capture(allow_screen=self.allow_screen)
                self._result(job_id, seq, done=True, png=image)
            elif job["type"] == "cancel":
                target = self.processes.get(job["target_job_id"])
                if target and target[0].poll() is None:
                    from .executor import _terminate

                    _terminate(target[0], target[1])
                self._result(job_id, seq, done=True, returncode=0)
        except PermissionError as exc:
            if self.stop_event.is_set():
                logger.debug("Dropping result for %s because the device is stopping", job_id)
                return
            if str(exc) == "unknown_device":
                return
            if str(exc) == "policy_changed":
                self.stop_event.wait(30)
                return
            try:
                self._result(job_id, seq, done=True, returncode=1, error=str(exc))
            except Exception:
                logger.debug("Could not deliver job failure for %s", job_id, exc_info=True)
        except Exception as exc:
            if self.stop_event.is_set():
                logger.debug("Dropping result for %s because the device is stopping", job_id)
                return
            try:
                self._result(job_id, seq, done=True, returncode=1, error=str(exc))
            except Exception:
                logger.debug("Could not deliver job failure for %s", job_id, exc_info=True)
        finally:
            self.processes.pop(job_id, None)
            self.running.pop(job_id, None)

    def _heartbeat_loop(self) -> None:
        while not self.stop_event.is_set():
            try:
                response = self._post("/device/heartbeat", {"policy_hash": policy.digest(), "os": platform.platform(), "home": str(Path.home()), "agent_version": __version__})
                self._verify_response(response, "/device/heartbeat")
                self.stop_event.wait(5)
            except PermissionError as exc:
                if str(exc) == "unknown_device":
                    self.stop_event.set()
                    return
                if str(exc) == "policy_changed":
                    self.stop_event.wait(30)
                    continue
                self.stop_event.wait(2)
            except (httpx.HTTPError, KeyError, ValueError, json.JSONDecodeError):
                self.stop_event.wait(2)

    def run_forever(self) -> None:
        heartbeat = threading.Thread(target=self._heartbeat_loop, daemon=True)
        heartbeat.start()
        while not self.stop_event.is_set():
            try:
                response = self._post("/device/poll", {"wait": 25})
                job = self._verify_response(response, "/device/poll")
                if self.fatal:
                    raise self.fatal[0]
                if job:
                    thread = threading.Thread(target=self._run_job, args=(job,), daemon=True)
                    self.running[job["job_id"]] = thread
                    thread.start()
                if self.fatal:
                    raise self.fatal[0]
            except PermissionError as exc:
                if str(exc) == "unknown_device":
                    raise
                if str(exc) == "policy_changed":
                    self.stop_event.wait(30)
                    continue
                time.sleep(random.uniform(1, 3))
            except (httpx.HTTPError, KeyError, ValueError, json.JSONDecodeError):
                logger.warning("Device poll failed; retrying")
                time.sleep(random.uniform(1, 3))
