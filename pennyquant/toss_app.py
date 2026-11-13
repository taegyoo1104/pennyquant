"""One-command launcher for an observed-price monitor and local dashboard."""
from __future__ import annotations

import asyncio
from contextlib import suppress
import json
import os
from pathlib import Path
import socket
import subprocess
import sys

from .database.repository import Repository
from .monitor import monitor_toss


def dashboard_command(db_path: Path, port: int) -> list[str]:
    app = Path(__file__).resolve().parent / "dashboard" / "app.py"
    report = db_path.with_name(db_path.stem + "-no-performance-report.json")
    return [sys.executable, "-m", "streamlit", "run", str(app),
            "--server.address", "127.0.0.1", "--server.port", str(port),
            "--server.headless", "true", "--browser.gatherUsageStats", "false",
            "--", "--db", str(db_path), "--report", str(report), "--mode", "monitor"]


def _port_available(port: int) -> None:
    if not 1 <= port <= 65535:
        raise ValueError("--port must be between 1 and 65535")
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as candidate:
            candidate.bind(("127.0.0.1", port))
    except OSError as exc:
        raise RuntimeError(f"Dashboard port {port} is already in use; choose another --port, for example --port 8502") from exc


async def _stop_dashboard(child: subprocess.Popen | None) -> None:
    if child is None or child.poll() is not None:
        return
    child.terminate()
    try:
        await asyncio.to_thread(child.wait, timeout=5)
    except subprocess.TimeoutExpired:
        child.kill()
        await asyncio.to_thread(child.wait, timeout=5)


async def run_toss_app(args) -> int:
    symbols = [part.strip().upper() for part in args.symbols.split(",") if part.strip()]
    db = Path(args.db).resolve()
    child: subprocess.Popen | None = None
    collector: asyncio.Task | None = None
    if not args.no_dashboard:
        _port_available(args.port)

    def start_dashboard():
        nonlocal child
        if not args.no_dashboard:
            dashboard_env = dict(os.environ)
            for name in ("TOSS_CLIENT_ID", "TOSS_CLIENT_SECRET", "TOSS_ACCOUNT_SEQ",
                         "APCA_API_KEY_ID", "APCA_API_SECRET_KEY", "GH_TOKEN", "GITHUB_TOKEN"):
                dashboard_env.pop(name, None)
            child = subprocess.Popen(dashboard_command(db, args.port), cwd=Path(__file__).resolve().parents[1],
                                     env=dashboard_env)
            print(f"Toss observed-price dashboard: http://localhost:{args.port}", flush=True)
        print(f"Read-only monitoring: {', '.join(symbols)}. Stop with Ctrl+C. No paper or live orders.", flush=True)

    try:
        collector = asyncio.create_task(monitor_toss(symbols, db, interval_seconds=args.interval,
            max_cycles=args.max_polls, on_ready=start_dashboard))
        while not collector.done():
            if child is not None and child.poll() is not None:
                collector.cancel()
                with suppress(asyncio.CancelledError):
                    await collector
                raise RuntimeError("Dashboard exited; price collection stopped. Check the dashboard output and port")
            await asyncio.sleep(0.2)
        code = await collector
        if code:
            repo = Repository(db)
            try:
                state = repo.get_control("monitor", {})
                print(json.dumps({"monitor_status": state.get("status"), "error": state.get("error")}, ensure_ascii=False))
            finally:
                repo.close()
        return code
    finally:
        if collector is not None and not collector.done():
            collector.cancel()
            with suppress(asyncio.CancelledError):
                await collector
        await _stop_dashboard(child)
