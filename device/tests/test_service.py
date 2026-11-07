import subprocess
from xml.etree import ElementTree

from hermeshands_device import service


def test_install_service_command_escapes_executable_with_spaces(monkeypatch):
    monkeypatch.setattr(service.sys, "platform", "win32")
    monkeypatch.setattr(service.shutil, "which", lambda name: r"C:\Users\A User\pipx\bin\hermes-hands.exe")
    monkeypatch.setattr(service.os, "environ", {"USERDOMAIN": "LAPTOP", "USERNAME": "A User"})
    monkeypatch.setattr(service.Path, "home", lambda: service.Path(r"C:\Users\A User"))
    calls = []
    monkeypatch.setattr(service.subprocess, "run", lambda args, check: calls.append((args, check)) or subprocess.CompletedProcess(args, 0))
    assert service.install() == 0
    assert calls[0][0][:3] == ["schtasks", "/Create", "/XML"]
    assert calls[0][0][3].endswith("task.xml")
    assert calls[0][0][-3:] == ["/TN", "HermesHands", "/F"]
    assert calls[0][1] is False


def test_install_service_dry_run_prints_command_without_running(monkeypatch, capsys):
    monkeypatch.setattr(service.sys, "platform", "win32")
    monkeypatch.setattr(service.shutil, "which", lambda name: r"C:\pipx\A User\hermes-hands.exe")
    monkeypatch.setattr(service.os, "environ", {"USERDOMAIN": "LAPTOP", "USERNAME": "A User"})
    monkeypatch.setattr(service.Path, "home", lambda: service.Path(r"C:\Users\A User"))
    monkeypatch.setattr(service.subprocess, "run", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("must not run")))
    assert service.install(dry_run=True) == 0
    output = capsys.readouterr().out
    assert 'schtasks /Create /XML ' in output
    assert "DisallowStartIfOnBatteries" in output and ">false<" in output
    assert "StopIfGoingOnBatteries" in output
    assert "ExecutionTimeLimit" in output and ">PT0S<" in output
    assert "RestartOnFailure" in output and "<Count>3</Count>" in output
    assert "LAPTOP\\A User" in output
    assert "serve" in output


def test_remove_service_command(monkeypatch):
    monkeypatch.setattr(service.sys, "platform", "win32")
    calls = []
    monkeypatch.setattr(service.subprocess, "run", lambda args, check: calls.append(args) or subprocess.CompletedProcess(args, 0))
    assert service.install(remove=True) == 0
    assert calls == [["schtasks", "/Delete", "/TN", "HermesHands", "/F"]]


def test_task_xml_has_current_user_logon_and_unlimited_restartable_settings():
    xml = service.task_xml(executable=r"C:\Program Files\hermes-hands.exe", username="LAPTOP\\A User")
    root = ElementTree.fromstring(xml)
    ns = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}
    assert root.findtext("t:Triggers/t:LogonTrigger/t:UserId", namespaces=ns) == "LAPTOP\\A User"
    assert root.findtext("t:Principals/t:Principal/t:UserId", namespaces=ns) == "LAPTOP\\A User"
    assert root.findtext("t:Settings/t:DisallowStartIfOnBatteries", namespaces=ns) == "false"
    assert root.findtext("t:Settings/t:StopIfGoingOnBatteries", namespaces=ns) == "false"
    assert root.findtext("t:Settings/t:ExecutionTimeLimit", namespaces=ns) == "PT0S"
    assert root.findtext("t:Settings/t:RestartOnFailure/t:Count", namespaces=ns) == "3"
    assert root.findtext("t:Actions/t:Exec/t:Command", namespaces=ns) == r"C:\Program Files\hermes-hands.exe"
    assert root.findtext("t:Actions/t:Exec/t:WorkingDirectory", namespaces=ns) == str(service.Path.home())


def test_task_xml_uses_utf16_bom_and_declaration():
    xml = service.task_xml(executable="hermes-hands.exe", username="LAPTOP\\A User")
    assert xml.startswith(b"\xff\xfe")
    assert '<?xml version="1.0" encoding="UTF-16"?>' in xml.decode("utf-16")


def test_install_service_refuses_non_windows(monkeypatch, capsys):
    monkeypatch.setattr(service.sys, "platform", "linux")
    assert service.install() == 1
    assert "not supported in v0.1" in capsys.readouterr().err
