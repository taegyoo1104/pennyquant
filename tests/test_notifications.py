import base64

from pennyquant.notifications import DesktopNotifier, dashboard_environment


def test_dashboard_environment_removes_broker_news_llm_credentials(monkeypatch):
    for name in ("TOSS_CLIENT_ID", "TOSS_CLIENT_SECRET", "TOSS_ACCOUNT_SEQ", "APCA_API_KEY_ID",
                 "APCA_API_SECRET_KEY", "OPENAI_API_KEY", "GH_TOKEN", "CUSTOM_PASSWORD"):
        monkeypatch.setenv(name, "PRIVATE")
    monkeypatch.setenv("PATH", "public-path")
    env = dashboard_environment()
    assert env["PATH"] == "public-path"
    assert "PRIVATE" not in env.values()


def test_notification_is_local_and_deduplicated(capsys):
    notifier = DesktopNotifier(enabled=False)
    alert = {"id": "daily-2026-11-13", "message": "일일 손실 한도에 도달해 신규 매매를 중단했습니다."}
    notifier(alert)
    notifier(alert)
    assert capsys.readouterr().out.count("일일 손실 한도") == 1


def test_windows_notification_message_cannot_escape_script(monkeypatch):
    calls = []
    monkeypatch.setattr("pennyquant.notifications.sys.platform", "win32")
    monkeypatch.setattr("pennyquant.notifications.subprocess.Popen", lambda *args, **kwargs: calls.append((args, kwargs)))
    DesktopNotifier()({"id": "one", "message": "가격';$(PRIVATE)"})
    command = calls[0][0][0]
    script = base64.b64decode(command[-1]).decode("utf-16-le")
    assert "'가격'';$(PRIVATE)'" in script
    assert calls[0][1].get("shell") is None
