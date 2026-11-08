import queue
import re
import sys
import threading
import time
import types

import pytest

from hermeshands_device import console as console_module
from hermeshands_device.console import ConsoleOwner


class FakeConsole:
    def __init__(self, *lines):
        self.lines = queue.Queue()
        self.outputs = []
        self.prompts = []
        self.reader_thread_ids = []
        self.read_started = threading.Event()
        self.lock = threading.Lock()
        for line in lines:
            self.lines.put(line)

    def write(self, text):
        with self.lock:
            self.outputs.append(text)

    def read_line(self, prompt):
        with self.lock:
            self.prompts.append(prompt)
            self.reader_thread_ids.append(threading.get_ident())
        self.read_started.set()
        return self.lines.get()

    def feed(self, line):
        self.lines.put(line)

    def output(self):
        with self.lock:
            return "".join(self.outputs)

    def prompt_text(self):
        with self.lock:
            return "".join(self.prompts)


def _wait_for(predicate, timeout=1.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return predicate()


def test_console_owner_composes_help_and_lowercase_s_is_text():
    console = FakeConsole("H", "Please help me find the file.", "s")
    stopped = threading.Event()
    help_requests = []
    owner = ConsoleOwner(console, on_stop=stopped.set, on_help=help_requests.append)

    owner.start()

    assert stopped.wait(1)
    owner.join(1)
    assert help_requests == ["Please help me find the file."]


def test_console_owner_timestamps_interactive_prompts():
    console = FakeConsole("S")
    stopped = threading.Event()
    owner = ConsoleOwner(console, on_stop=stopped.set, on_help=lambda text: None)

    owner.start()

    assert stopped.wait(1)
    owner.join(1)
    assert re.match(r"^\[\d{4}-\d{2}-\d{2}T[^\]]+Z\] Press S to stop or H to ask for help: $", console.prompts[0])


def test_startup_questions_and_interactive_input_have_one_owner_thread():
    console = FakeConsole("Y", "Y", "S")
    stopped = threading.Event()
    owner = ConsoleOwner(console, on_stop=stopped.set, on_help=lambda text: None)

    assert owner.startup_consent("Start? [Y/N]: ") == "Y"
    assert owner.request("session", "Allow screenshots?", timeout=1) == "Y"
    assert stopped.wait(1)

    owner.close()
    owner.join(1)
    assert len(set(console.reader_thread_ids)) == 1
    assert re.match(r"^\[\d{4}-\d{2}-\d{2}T[^\]]+Z\] Start\? \[Y/N\]: $", console.prompts[0])


def test_terminal_console_returns_eof_when_stdin_closes(monkeypatch):
    reads = []
    restored = []

    class ClosedInput:
        def fileno(self):
            return 7

        def read(self, count):
            reads.append(count)
            if len(reads) > 1:
                raise RuntimeError("read repeated after EOF")
            return ""

    class Output:
        def __init__(self):
            self.text = []

        def write(self, text):
            self.text.append(text)

        def flush(self):
            return None

    termios = types.ModuleType("termios")
    termios.TCSADRAIN = 1
    termios.tcgetattr = lambda descriptor: ["saved"]
    termios.tcsetattr = lambda descriptor, when, state: restored.append((descriptor, when, state))
    tty = types.ModuleType("tty")
    tty.setraw = lambda descriptor: None
    output = Output()
    monkeypatch.setattr(console_module.os, "name", "posix")
    monkeypatch.setattr(console_module.sys, "stdin", ClosedInput())
    monkeypatch.setattr(console_module.sys, "stdout", output)
    monkeypatch.setitem(sys.modules, "termios", termios)
    monkeypatch.setitem(sys.modules, "tty", tty)

    assert console_module.TerminalConsole().read_line("Prompt: ") is None
    assert output.text == ["Prompt: "]
    assert restored == [(7, 1, ["saved"])]


def test_startup_console_read_error_reaches_caller():
    class BrokenConsole:
        def write(self, text):
            return None

        def read_line(self, prompt):
            raise OSError("console read failed")

    owner = ConsoleOwner(BrokenConsole(), on_stop=lambda: None, on_help=lambda text: None)

    with pytest.raises(OSError, match="console read failed"):
        owner.startup_consent("Start?")

    owner.join(1)


def test_uppercase_s_stops_from_help_editor():
    console = FakeConsole("H", "S")
    stopped = threading.Event()
    help_requests = []
    owner = ConsoleOwner(console, on_stop=stopped.set, on_help=help_requests.append)

    owner.start()

    assert stopped.wait(1)
    owner.join(1)
    assert help_requests == []


def test_escape_cancels_help_editor_without_stopping():
    console = FakeConsole("H", "\x1b", "S")
    stopped = threading.Event()
    help_requests = []
    owner = ConsoleOwner(console, on_stop=stopped.set, on_help=help_requests.append)

    owner.start()

    assert stopped.wait(1)
    owner.join(1)
    assert help_requests == []


def test_help_text_cannot_grant_a_screenshot_prompt():
    console = FakeConsole("H")
    stopped = threading.Event()
    help_requests = []
    answers = []
    owner = ConsoleOwner(console, on_stop=stopped.set, on_help=help_requests.append)
    owner.start()
    assert _wait_for(lambda: "Help message" in console.prompt_text())
    prompt = threading.Thread(
        target=lambda: answers.append(
            owner.request("screen-request", "Allow screenshot?", timeout=1)
        )
    )
    prompt.start()
    assert _wait_for(lambda: "screen-request" in console.output())
    assert re.search(r"\[\d{4}-\d{2}-\d{2}T[^\]]+Z\] \[screen-request\] Allow screenshot\?", console.output())
    console.feed("Y")
    prompt.join(1)
    assert answers == [""]
    assert help_requests == ["Y"]

    console.feed("S")
    assert stopped.wait(1)
    owner.join(1)


def test_h_starts_help_composer_during_a_screenshot_prompt():
    console = FakeConsole()
    stopped = threading.Event()
    help_requests = []
    answers = []
    owner = ConsoleOwner(console, on_stop=stopped.set, on_help=help_requests.append)
    owner.start()
    assert console.read_started.wait(1)
    prompt = threading.Thread(
        target=lambda: answers.append(owner.request("screen-request", "Allow screenshot?", timeout=1))
    )
    prompt.start()
    assert _wait_for(lambda: "screen-request" in console.output())

    console.feed("H")
    assert _wait_for(lambda: "Help message" in console.prompt_text())
    console.feed("Please open the app.")
    prompt.join(1)
    assert answers == [""]
    assert _wait_for(lambda: help_requests)
    assert help_requests == ["Please open the app."]

    console.feed("S")
    assert stopped.wait(1)
    owner.join(1)


def test_late_answer_is_bound_to_expired_prompt_id():
    console = FakeConsole()
    stopped = threading.Event()
    owner = ConsoleOwner(console, on_stop=stopped.set, on_help=lambda text: None)
    owner.start()
    assert console.read_started.wait(1)

    assert owner.request("old-request", "Allow screenshot?", timeout=0.02) is None
    answers = []
    later = threading.Thread(target=lambda: answers.append(owner.request("new-request", "Allow this screenshot?", timeout=1)))
    later.start()
    console.feed("Y")
    assert _wait_for(lambda: "new-request" in console.prompt_text())
    console.feed("N")
    later.join(1)

    owner.close()
    console.feed(None)
    owner.join(1)
    assert answers == ["N"]
    assert "old-request" in console.output()
    assert "new-request" in console.prompt_text()


def test_expired_queued_prompt_is_not_shown_later():
    console = FakeConsole()
    stopped = threading.Event()
    owner = ConsoleOwner(console, on_stop=stopped.set, on_help=lambda text: None)
    owner.start()
    assert console.read_started.wait(1)
    first_answers = []
    first = threading.Thread(
        target=lambda: first_answers.append(
            owner.request("first-request", "First question?", timeout=1)
        )
    )
    first.start()
    assert _wait_for(lambda: "first-request" in console.output())

    assert owner.request("expired-request", "Expired question?", timeout=0.02) is None
    console.feed("Y")
    first.join(1)
    assert first_answers == ["Y"]
    assert _wait_for(lambda: len(console.reader_thread_ids) > 1)
    assert "expired-request" not in console.prompt_text()

    console.feed("S")
    assert stopped.wait(1)
    owner.join(1)
