import subprocess

from hermeshands_device import service


def test_install_service_command_escapes_executable_with_spaces(monkeypatch):
    monkeypatch.setattr(service.sys, "platform", "win32")
    monkeypatch.setattr(service.shutil, "which", lambda name: r"C:\Users\A User\pipx\bin\hermes-hands.exe")
    calls = []
    monkeypatch.setattr(service.subprocess, "run", lambda args, check: calls.append((args, check)) or subprocess.CompletedProcess(args, 0))
    assert service.install() == 0
    assert calls[0][0] == ["schtasks", "/Create", "/SC", "ONLOGON", "/RL", "LIMITED", "/TN", "HermesHands", "/TR", '"C:\\Users\\A User\\pipx\\bin\\hermes-hands.exe" serve', "/F"]
    assert calls[0][1] is False


def test_install_service_dry_run_prints_command_without_running(monkeypatch, capsys):
    monkeypatch.setattr(service.sys, "platform", "win32")
    monkeypatch.setattr(service.shutil, "which", lambda name: r"C:\pipx\A User\hermes-hands.exe")
    monkeypatch.setattr(service.subprocess, "run", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("must not run")))
    assert service.install(dry_run=True) == 0
    expected = subprocess.list2cmdline(service.command(executable=r"C:\pipx\A User\hermes-hands.exe"))
    assert capsys.readouterr().out == expected + "\n"


def test_remove_service_command(monkeypatch):
    monkeypatch.setattr(service.sys, "platform", "win32")
    calls = []
    monkeypatch.setattr(service.subprocess, "run", lambda args, check: calls.append(args) or subprocess.CompletedProcess(args, 0))
    assert service.install(remove=True) == 0
    assert calls == [["schtasks", "/Delete", "/TN", "HermesHands", "/F"]]


def test_install_service_refuses_non_windows(monkeypatch, capsys):
    monkeypatch.setattr(service.sys, "platform", "linux")
    assert service.install() == 1
    assert "not supported in v0.1" in capsys.readouterr().err
