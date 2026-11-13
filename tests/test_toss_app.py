from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from sessionquant.database.repository import Repository
from sessionquant.toss_app import dashboard_command, run_toss_app


def options(tmp_path, **updates):
    return SimpleNamespace(**{"symbols": "aapl, MSFT", "interval": 3.0, "db": tmp_path / "monitor.db",
                              "port": 18509, "no_dashboard": False, "max_polls": 1, **updates})


def test_dashboard_command_uses_local_readonly_mode_and_distinct_report(tmp_path):
    path = tmp_path / "한글 경로" / "monitor.db"
    command = dashboard_command(path, 18509)
    assert command[:3][1:] == ["-m", "streamlit"]
    assert command[command.index("--server.address") + 1] == "127.0.0.1"
    assert command[command.index("--db") + 1] == str(path)
    assert command[command.index("--mode") + 1] == "monitor"
    assert "no-performance-report" in command[command.index("--report") + 1]


async def test_no_dashboard_monitor_starts_with_normalized_symbols(tmp_path, monkeypatch):
    calls = []
    async def monitor(symbols, db, **kwargs):
        calls.append((symbols, db, kwargs["interval_seconds"], kwargs["max_cycles"]))
        kwargs["on_ready"]()
        return 0
    monkeypatch.setattr("sessionquant.toss_app.monitor_toss", monitor)
    def forbidden(*args, **kwargs):
        pytest.fail("collector-only mode must not start Streamlit or reserve a dashboard port")
    monkeypatch.setattr("sessionquant.toss_app.subprocess.Popen", forbidden)
    monkeypatch.setattr("sessionquant.toss_app._port_available", forbidden)
    assert await run_toss_app(options(tmp_path, no_dashboard=True)) == 0
    assert calls == [(["AAPL", "MSFT"], tmp_path / "monitor.db", 3.0, 1)]


async def test_dashboard_ends_when_finite_monitor_ends(tmp_path, monkeypatch):
    class Child:
        stopped = False
        def poll(self): return 0 if self.stopped else None
        def terminate(self): self.stopped = True
        def wait(self, timeout): return 0
    child = Child()
    monkeypatch.setenv("TOSS_CLIENT_SECRET", "test-local-secret")
    monkeypatch.setenv("GH_TOKEN", "test-repo-token")
    async def monitor(*args, **kwargs):
        kwargs["on_ready"]()
        return 0
    monkeypatch.setattr("sessionquant.toss_app.monitor_toss", monitor)
    monkeypatch.setattr("sessionquant.toss_app._port_available", lambda port: None)
    def start(*args, **kwargs):
        assert "TOSS_CLIENT_SECRET" not in kwargs["env"]
        assert "GH_TOKEN" not in kwargs["env"]
        return child
    monkeypatch.setattr("sessionquant.toss_app.subprocess.Popen", start)
    assert await run_toss_app(options(tmp_path)) == 0
    assert child.stopped


async def test_cancel_stops_collector_and_dashboard(tmp_path, monkeypatch):
    ready = asyncio.Event()
    collector_stopped = asyncio.Event()
    class Child:
        stopped = False
        def poll(self): return 0 if self.stopped else None
        def terminate(self): self.stopped = True
        def wait(self, timeout): return 0
    child = Child()
    async def monitor(*args, **kwargs):
        try:
            kwargs["on_ready"]()
            ready.set()
            await asyncio.Future()
        finally:
            collector_stopped.set()
    monkeypatch.setattr("sessionquant.toss_app.monitor_toss", monitor)
    monkeypatch.setattr("sessionquant.toss_app._port_available", lambda port: None)
    monkeypatch.setattr("sessionquant.toss_app.subprocess.Popen", lambda *args, **kwargs: child)
    task = asyncio.create_task(run_toss_app(options(tmp_path)))
    await asyncio.wait_for(ready.wait(), 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert child.stopped and collector_stopped.is_set()


async def test_dashboard_failure_stops_collector(tmp_path, monkeypatch):
    stopped = asyncio.Event()
    class Child:
        def poll(self): return 1
    async def monitor(*args, **kwargs):
        try:
            kwargs["on_ready"]()
            await asyncio.Future()
        finally:
            stopped.set()
    monkeypatch.setattr("sessionquant.toss_app.monitor_toss", monitor)
    monkeypatch.setattr("sessionquant.toss_app._port_available", lambda port: None)
    monkeypatch.setattr("sessionquant.toss_app.subprocess.Popen", lambda *args, **kwargs: Child())
    with pytest.raises(RuntimeError, match="Dashboard exited"):
        await run_toss_app(options(tmp_path))
    assert stopped.is_set()


def test_recent_price_samples_are_bounded_and_chronological(tmp_path):
    repo = Repository(tmp_path / "prices.db")
    try:
        for i in range(5):
            repo.record("price_samples", {"id": str(i), "symbol": "AAPL", "sample": i})
        assert [row["sample"] for row in repo.recent("price_samples", 2)] == [3, 4]
        assert len(repo.all("price_samples")) == 5
    finally:
        repo.close()


@pytest.mark.parametrize("mode", ["replay", "follow"])
def test_paper_runtime_cannot_take_over_price_monitor_database(tmp_path, mode):
    from sessionquant.config.settings import Settings
    from sessionquant.demo import demo_events
    from sessionquant.events import write_events
    from sessionquant.runtime import follow, replay
    db = tmp_path / "monitor.db"
    repo = Repository(db)
    repo.set_control("runtime_kind", "toss_monitor")
    repo.set_control("source", {"synthetic": False, "label": "original monitor source"})
    repo.close()
    tape = tmp_path / "tape.jsonl"
    write_events(tape, [demo_events()[0]])
    with pytest.raises(ValueError, match="runtime mode differs"):
        if mode == "replay":
            replay(tape, db, Settings(), "pullback")
        else:
            follow(tape, db, Settings(), "pullback")
    repo = Repository(db)
    assert repo.get_control("runtime_kind") == "toss_monitor"
    assert repo.get_control("source")["label"] == "original monitor source"
    assert repo.load_state() is None
    repo.close()
