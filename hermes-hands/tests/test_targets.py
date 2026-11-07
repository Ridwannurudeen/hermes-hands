from hands import targets


def test_target_resolution(monkeypatch):
    monkeypatch.setattr(targets.store, "get_session_target", lambda key: "laptop")
    assert targets.resolve_target(session_key="chat", in_cron=True, default_target="laptop") == "server"
    assert targets.resolve_target(session_key="chat", in_cron=False, default_target="server") == "laptop"
    assert targets.resolve_target(session_key="", in_cron=False, default_target="server") == "server"


def test_target_resolution_falls_back(monkeypatch):
    monkeypatch.setattr(targets.store, "get_session_target", lambda key: None)
    assert targets.resolve_target(session_key="chat", in_cron=False, default_target="server") == "server"
