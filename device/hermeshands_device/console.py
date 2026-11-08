from __future__ import annotations

import os
import sys
import threading
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Protocol


WITNESS_CAPABILITIES = ("witness-v1", "checkup-v1", "help-v1", "receipt-v1")
WITNESS_START_PROMPT = "Start witness mode? [Y/N]: "
SCREENSHOT_SESSION_PROMPT = "Allow screenshot requests this session?"
WITNESS_CONSOLE_CLOSED = "Witness mode declined because the console closed.\n"
WITNESS_DECLINED = "Witness mode declined. No session was started.\n"
WITNESS_STOPPED_BEFORE_START = "Witness mode stopped before the session started.\n"
SCREENSHOTS_DISABLED = "Screenshot requests are disabled for this session.\n"


def _timestamped(text: str) -> str:
    timestamp = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    return f"[{timestamp}] {text}"


def witness_consent_copy(owner_label: str, gateway_host: str) -> str:
    return (
        f"Witness session for paired owner: {owner_label}\n"
        f"Gateway: {gateway_host}\n"
        "Session limit: 30 minutes\n"
        f"Capabilities: {', '.join(WITNESS_CAPABILITIES)}\n"
        "No mouse or keyboard control.\n"
        "Commands and outcomes are recorded.\n"
        "Screenshots require separate session and request consent.\n"
        "Press S or s to stop.\n"
    )


class ConsoleInput(Protocol):
    def write(self, text: str) -> None: ...

    def read_line(self, prompt: str) -> str | None: ...


class TerminalConsole:
    def __init__(self):
        self._write_lock = threading.Lock()

    def write(self, text: str) -> None:
        with self._write_lock:
            sys.stdout.write(text)
            sys.stdout.flush()

    def read_line(self, prompt: str) -> str | None:
        self.write(prompt)
        characters = []
        if os.name == "nt":
            import msvcrt

            while True:
                character = msvcrt.getwch()
                if character in ("\x00", "\xe0"):
                    msvcrt.getwch()
                    continue
                if character == "\x1a":
                    return None
                if character in ("\r", "\n"):
                    self.write("\n")
                    return "".join(characters)
                if character == "\x1b":
                    self.write("\n")
                    return character
                if character == "\x03":
                    raise KeyboardInterrupt
                if character in ("\b", "\x7f"):
                    if characters:
                        characters.pop()
                        self.write("\b \b")
                    continue
                characters.append(character)
                self.write(character)

        import termios
        import tty

        descriptor = sys.stdin.fileno()
        previous = termios.tcgetattr(descriptor)
        try:
            tty.setraw(descriptor)
            while True:
                character = sys.stdin.read(1)
                if character in ("", "\x04"):
                    return None
                if character in ("\r", "\n"):
                    self.write("\n")
                    return "".join(characters)
                if character == "\x1b":
                    self.write("\n")
                    return character
                if character == "\x03":
                    raise KeyboardInterrupt
                if character in ("\b", "\x7f"):
                    if characters:
                        characters.pop()
                        self.write("\b \b")
                    continue
                characters.append(character)
                self.write(character)
        finally:
            termios.tcsetattr(descriptor, termios.TCSADRAIN, previous)


@dataclass
class _Prompt:
    prompt_id: str
    text: str
    ready: threading.Event = field(default_factory=threading.Event)
    answer: str | None = None
    expired: bool = False


class ConsoleOwner:
    def __init__(self, console: ConsoleInput, *, on_stop: Callable[[], None], on_help: Callable[[str], None]):
        self.console = console
        self.on_stop = on_stop
        self.on_help = on_help
        self._lock = threading.Lock()
        self._queued: deque[_Prompt] = deque()
        self._active: _Prompt | None = None
        self._reading = False
        self._read_prompt: _Prompt | None = None
        self._thread: threading.Thread | None = None
        self._closed = False
        self._startup_ready = threading.Event()
        self._startup_resume = threading.Event()
        self._startup_answer: str | None = None
        self._startup_error: Exception | None = None

    def start(self) -> None:
        with self._lock:
            if self._thread is not None or self._closed:
                return
            self._thread = threading.Thread(target=self._run, name="hermes-hands-console", daemon=True)
            self._thread.start()

    def startup_consent(self, consent_prompt: str) -> str | None:
        with self._lock:
            if self._thread is not None or self._closed:
                raise RuntimeError("console owner has already started")
            self._thread = threading.Thread(
                target=self._run_startup,
                args=(consent_prompt,),
                name="hermes-hands-console",
                daemon=True,
            )
            self._thread.start()
        self._startup_ready.wait()
        if self._startup_error is not None:
            raise self._startup_error
        return self._startup_answer

    def request(self, prompt_id: str, text: str, *, timeout: float | None = None) -> str | None:
        prompt = _Prompt(prompt_id, text)
        dynamic_prompt = False
        with self._lock:
            if self._closed:
                return None
            active_ids = {item.prompt_id for item in self._queued}
            if self._active is not None:
                active_ids.add(self._active.prompt_id)
            if prompt_id in active_ids:
                raise ValueError(f"prompt ID is already active: {prompt_id}")
            self._queued.append(prompt)
            self._promote_locked()
            if self._active is prompt and self._reading and self._read_prompt is None:
                self._read_prompt = prompt
                dynamic_prompt = True
        if dynamic_prompt:
            self.console.write(f"\n{_timestamped(f'[{prompt_id}] {text}')}\n")
        self._startup_resume.set()
        self.start()
        if not prompt.ready.wait(timeout):
            with self._lock:
                prompt.expired = True
                if self._active is prompt and self._read_prompt is not prompt:
                    self._active = None
                    self._promote_locked()
                elif prompt in self._queued:
                    self._queued.remove(prompt)
            return None
        return prompt.answer

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            pending = list(self._queued)
            self._queued.clear()
            if self._active is not None:
                pending.append(self._active)
                self._active = None
            for prompt in pending:
                prompt.answer = None
                prompt.ready.set()
            self._startup_resume.set()

    def join(self, timeout: float | None = None) -> None:
        with self._lock:
            thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout)

    def _promote_locked(self) -> None:
        if self._active is None and self._queued:
            self._active = self._queued.popleft()

    def _deliver_prompt(self, prompt: _Prompt, answer: str | None) -> None:
        with self._lock:
            if self._closed:
                return
            if self._active is prompt:
                self._active = None
            if not prompt.expired:
                prompt.answer = answer
                prompt.ready.set()
            self._promote_locked()

    def _read_line(self, command_prompt: str) -> tuple[str | None, _Prompt | None]:
        with self._lock:
            if self._closed:
                return None, None
            self._promote_locked()
            prompt = self._active
            self._read_prompt = prompt
            self._reading = True
            text = f"[{prompt.prompt_id}] {prompt.text} [Y/N]: " if prompt else command_prompt
        try:
            answer = self.console.read_line(_timestamped(text))
        except KeyboardInterrupt:
            with self._lock:
                self._reading = False
                self._read_prompt = None
            self.on_stop()
            self.close()
            return None, prompt
        except Exception:
            with self._lock:
                self._reading = False
                self._read_prompt = None
            self.on_stop()
            self.close()
            raise
        with self._lock:
            prompt = self._read_prompt
            self._read_prompt = None
            self._reading = False
            closed = self._closed
        if closed:
            return None, prompt
        if answer is None:
            if prompt is not None:
                self._deliver_prompt(prompt, None)
            self.on_stop()
            self.close()
        return answer, prompt

    def _stop(self) -> None:
        self.on_stop()
        self.close()

    def _run_startup(self, consent_prompt: str) -> None:
        consent = None
        try:
            consent = self.console.read_line(_timestamped(consent_prompt))
        except KeyboardInterrupt:
            consent = None
        except Exception as exc:
            self._startup_error = exc
            self._startup_ready.set()
            self.close()
            return
        if consent is None:
            self.on_stop()
        self._startup_answer = consent
        self._startup_ready.set()
        self._startup_resume.wait()
        if not self._closed:
            self._run()

    def _edit_help(self) -> None:
        while not self._closed:
            text, prompt = self._read_line("Help message, or Escape to cancel: ")
            if prompt is not None:
                if text == "S":
                    self._deliver_prompt(prompt, "")
                    self._stop()
                    return
                if text == "\x1b":
                    self._deliver_prompt(prompt, "")
                    self.console.write("Help request cancelled.\n")
                    return
                self._deliver_prompt(prompt, "")
            if text is None:
                return
            if text == "\x1b":
                self.console.write("Help request cancelled.\n")
                return
            if text == "S":
                self._stop()
                return
            if not text:
                self.console.write("Help message cannot be empty.\n")
                continue
            self.on_help(text)
            self.console.write("Help request ready.\n")
            return

    def _run(self) -> None:
        while not self._closed:
            line, prompt = self._read_line("Press S to stop or H to ask for help: ")
            if prompt is not None:
                if line in ("S", "s"):
                    self._deliver_prompt(prompt, line)
                    self._stop()
                    return
                self._deliver_prompt(prompt, "" if line == "H" else line)
                if line == "H":
                    self._edit_help()
                continue
            if line is None:
                return
            if line in ("S", "s"):
                self._stop()
                return
            if line == "H":
                self._edit_help()
