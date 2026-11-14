"""Local notifications: durable alerts are stored by the engine separately."""
from __future__ import annotations

import base64
import os
import subprocess
import sys


def dashboard_environment(*, database: bool = False) -> dict[str, str]:
    """화면 서버에는 필요한 DB 비밀번호만 전달하고 거래·뉴스 키는 제외한다."""
    result = {name: value for name, value in os.environ.items()
            if not any(part in name.upper() for part in ("SECRET", "TOKEN", "PASSWORD", "CREDENTIAL"))
            and not name.upper().endswith("_KEY")
            and name.upper() not in {"TOSS_CLIENT_ID", "TOSS_ACCOUNT_SEQ", "APCA_API_KEY_ID", "SSH_AUTH_SOCK"}}

    if database and os.getenv("SQ_DB_BACKEND", os.getenv("PQ_DB_BACKEND", "sqlite")).lower() == "mysql":
        for name in ("SQ_MYSQL_PASSWORD", "PQ_MYSQL_PASSWORD"):
            if name in os.environ:
                result[name] = os.environ[name]
    return result


class DesktopNotifier:
    def __init__(self, *, enabled: bool = True):
        self.enabled = enabled
        self._seen: set[str] = set()

    def __call__(self, alert: dict) -> None:
        identity = str(alert.get("id", ""))
        if identity and identity in self._seen:
            return
        if identity:
            self._seen.add(identity)
        message = str(alert.get("message") or "매매 상태를 확인하세요.")[:500]
        print(f"SessionQuant 알림: {message}", flush=True)
        if not self.enabled or sys.platform != "win32":
            return
        # Encode a fixed script; single-quote escaping keeps message text data.
        quoted = message.replace("'", "''").replace("\x00", "")
        script = (
            "Add-Type -AssemblyName System.Windows.Forms;"
            "$pqIcon=New-Object System.Windows.Forms.NotifyIcon;"
            "$pqIcon.Icon=[System.Drawing.SystemIcons]::Warning;"
            "$pqIcon.Visible=$true;"
            f"$pqIcon.ShowBalloonTip(10000,'SessionQuant','{quoted}',"
            "[System.Windows.Forms.ToolTipIcon]::Warning);"
            "Start-Sleep -Seconds 4;$pqIcon.Dispose()"
        )
        encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
        try:
            subprocess.Popen(["powershell.exe", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             env=dashboard_environment(), creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except OSError:
            # Desktop policy can suppress a balloon. Console and DB alerts remain.
            print("SessionQuant: 바탕화면 알림을 표시하지 못했습니다. 대시보드 알림을 확인하세요.", flush=True)
