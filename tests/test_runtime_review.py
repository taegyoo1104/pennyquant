"""Regression checks for durable identity, resumption marks and live causality."""
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from sessionquant.config.settings import Settings
from sessionquant.database.repository import Repository
from sessionquant.engine import EventEngine
from sessionquant.events import Event, write_events
from sessionquant.models import Account, OrderStatus, Quote, Signal, StockSnapshot
from sessionquant.runtime import follow, replay
from sessionquant.trading.order_manager import PaperOrderManager

T = datetime(2026, 11, 13, 15, 0, tzinfo=timezone.utc)


def q(t=T, bid=1.998, ask=2.002, symbol="DEMO"):
    return Quote(symbol, t, t, bid, ask, 100, 100)


def one_tape(tmp_path):
    tape = tmp_path / "recorded.jsonl"
    write_events(tape, [Event("quote", q())])
    return tape, tmp_path / "paper.db"


def test_replay_resume_requires_same_file_content(tmp_path):
    tape, db = one_tape(tmp_path)
    replay(tape, db, Settings(), "pullback")
    # Same path and event count; changing old prices must invalidate the cursor.
    write_events(tape, [Event("quote", q(bid=2.098, ask=2.102))])
    with pytest.raises(ValueError, match="(?i)(changed|content|hash|identity|differs)"):
        replay(tape, db, Settings(), "pullback", resume=True)


def test_identical_replay_resume_is_noop(tmp_path):
    tape, db = one_tape(tmp_path)
    first = replay(tape, db, Settings(), "pullback")
    state = first.manager.export_state()
    again = replay(tape, db, Settings(), "pullback", resume=True)
    assert again.manager.export_state() == state
    repo = Repository(db)
    assert repo.get_control("replay_cursor") == 1
    repo.close()


def test_follow_refuses_synthetic_replay_state(tmp_path, monkeypatch):
    tape, db = one_tape(tmp_path)
    replay(tape, db, Settings(), "pullback", synthetic=True)
    empty = tmp_path / "live.jsonl"
    empty.touch()

    def stop_unexpected_loop(_seconds):
        raise RuntimeError("follow incorrectly entered loop using replay state")

    monkeypatch.setattr("sessionquant.runtime.time.sleep", stop_unexpected_loop)
    with pytest.raises(ValueError, match="(?i)(mode|runtime|replay|source)"):
        follow(empty, db, Settings(), "pullback")


def test_idle_manual_halt_persists_cancelled_entry(tmp_path, monkeypatch):
    db = tmp_path / "idle.db"
    tape = tmp_path / "live.jsonl"
    tape.touch()
    initial = Repository(db)
    initial.set_control("manual_halt", True)
    initial.close()
    m = PaperOrderManager(Settings(), Account())
    order = m.submit(Signal("pending", "DEMO", "pullback", T, 2.0, "fixture"), q(), T)
    assert order

    class FakeEngine:
        def __init__(self, settings, strategy, repository, **kwargs):
            self.manager = m
            self.repository = repository
            self.quotes = {}
            repository.save_state(m.export_state())

        def persist(self):
            self.repository.save_state(self.manager.export_state())

    count = 0

    def interrupted_poll(_seconds):
        nonlocal count
        count += 1
        if count > 1:
            raise KeyboardInterrupt

    monkeypatch.setattr("sessionquant.runtime.EventEngine", FakeEngine)
    monkeypatch.setattr("sessionquant.runtime.time.sleep", interrupted_poll)
    follow(tape, db, Settings(), "pullback")
    repo = Repository(db)
    state = repo.load_state()
    repo.close()
    assert state["orders"][0]["status"] == OrderStatus.CANCELLED
    assert "manual" in state["halt_reasons"]
    assert state["account"]["reserved_cash"] == 0


def test_engine_restores_latest_quote_not_old_snapshot(tmp_path):
    repo = Repository(tmp_path / "marks.db")
    engine = EventEngine(Settings(), "pullback", repo)
    snapshot = StockSnapshot(q(), 1.75, 1000000, 2500000, 4.0, 2.0)
    engine.process(Event("snapshot", snapshot))
    latest = q(T + timedelta(seconds=1), bid=2.298, ask=2.302)
    engine.process(Event("quote", latest))
    restored = EventEngine(Settings(), "pullback", repo, restore=True)
    assert restored.quotes["DEMO"] == latest
    repo.close()


def test_live_engine_does_not_fill_historical_backlog():
    settings = Settings(regular_hours_only=False)
    engine = EventEngine(settings, "pullback", live=True)
    old = datetime(2020, 1, 2, 15, 0, tzinfo=timezone.utc)
    signal = Signal("old", "DEMO", "pullback", old, 2.0, "old queued signal")
    order = engine.manager.submit(signal, q(old), old)
    assert order
    old_tick = old + timedelta(seconds=1)
    engine.process(Event("quote", q(old_tick)))
    assert engine.manager.fills == []
    assert not engine.manager.account.positions


def test_follow_resume_requires_unchanged_consumed_prefix(tmp_path, monkeypatch):
    import hashlib
    tape, db = one_tape(tmp_path)
    consumed = tape.read_bytes()
    repo = Repository(db)
    engine = EventEngine(Settings(), "pullback", repo, live=True)
    engine.process(Event("quote", q()))
    repo.set_control("runtime_kind", "follow")
    repo.set_control("follow_path", str(tape.resolve()))
    repo.set_control("follow_offset", len(consumed))
    repo.set_control("follow_prefix_sha256", hashlib.sha256(consumed).hexdigest())
    repo.close()
    # Same filename, same byte length: inode/size checks alone cannot detect it.
    write_events(tape, [Event("quote", q(bid=2.098, ask=2.102))])
    assert tape.stat().st_size == len(consumed)

    def stop_unexpected_loop(_seconds):
        raise RuntimeError("follow resumed into modified consumed history")

    monkeypatch.setattr("sessionquant.runtime.time.sleep", stop_unexpected_loop)
    with pytest.raises(ValueError, match="(?i)(prefix|changed|content|identity|differs)"):
        follow(tape, db, Settings(), "pullback")
