"""실제 전략에서 모의 체결·청산·복구까지의 연결과 장애 경계를 검증한다."""
from datetime import timedelta
import json

import pytest

from sessionquant.cli import main
from sessionquant.database.repository import Repository
from sessionquant.market.universe import UniverseStore
from sessionquant.session_config import SessionConfig, load_session_config


def fixture_files(tmp_path):
    from sessionquant.session_demo import synthetic_tape
    rows, opened = synthetic_tape()
    universe = tmp_path/"universe.json"
    UniverseStore(universe).refresh(["AAPL"], source="SYNTHETIC_FIXTURE", observed_at=opened-timedelta(days=1))
    tape = tmp_path/"tape.jsonl"
    tape.write_text("".join(json.dumps(x)+"\n" for x in rows), encoding="utf-8")
    return rows, opened, universe, tape


def test_real_pullback_fractional_fill_and_session_exit(tmp_path):
    from decimal import Decimal
    from sessionquant.session_runtime import replay_session
    _, _, universe, tape = fixture_files(tmp_path)
    result = replay_session(tape, tmp_path/"paper.db", universe, synthetic=True, backend="sqlite")
    assert [x["side"] for x in result["fills"]] == ["buy", "sell"]
    assert Decimal(result["fills"][0]["qty"]) < 1
    assert result["state"]["positions"] == {}
    assert result["state"]["mode"] == "paper"
    assert result["context"]["live_orders_enabled"] is False
    assert any(o["intent"]["reason"] == "session_exit" for o in result["orders"])


def test_resume_does_not_reissue_orders_and_rejects_changed_universe(tmp_path):
    from sessionquant.session_runtime import replay_session
    _, _, universe, tape = fixture_files(tmp_path)
    db = tmp_path/"paper.db"
    first = replay_session(tape, db, universe, synthetic=True, backend="sqlite")
    second = replay_session(tape, db, universe, synthetic=True, backend="sqlite", resume=True)
    assert first["fills"] == second["fills"] and first["orders"] == second["orders"]
    with pytest.raises(ValueError, match="복구"):
        replay_session(tape, db, universe, synthetic=True, backend="sqlite")
    universe.write_text(universe.read_text()+"\n")
    with pytest.raises(ValueError, match="자료"):
        replay_session(tape, db, universe, synthetic=True, backend="sqlite", resume=True)


def test_db_write_failure_rolls_back_cursor_and_engine_cannot_continue(tmp_path, monkeypatch):
    from sessionquant.session_engine import SessionEngine, event_time
    rows, _, universe, _ = fixture_files(tmp_path)
    repo = Repository(tmp_path/"paper.db", backend="sqlite")
    try:
        engine = SessionEngine(repo, UniverseStore(universe), synthetic=True)
        engine.apply_batch(rows[:3], event_time(rows[0]), cursor=3)
        saved = repo.load_state()
        monkeypatch.setattr(repo, "save_state", lambda state: (_ for _ in ()).throw(RuntimeError("DB 저장 실패")))
        with pytest.raises(RuntimeError, match="저장 실패"):
            engine.apply_batch(rows[3:6], event_time(rows[3]), cursor=6)
        assert repo.load_state() == saved
        assert len(repo.all("market_bars")) == 3
        with pytest.raises(RuntimeError, match="재시작"):
            engine.tick(event_time(rows[6]))
    finally:
        repo.close()


def test_current_universe_cannot_be_applied_to_earlier_tape(tmp_path):
    from sessionquant.session_runtime import replay_session
    _, opened, universe, tape = fixture_files(tmp_path)
    universe.unlink()
    UniverseStore(universe).refresh(["AAPL"], source="current_only", observed_at=opened+timedelta(days=1))
    with pytest.raises(ValueError, match="구성 종목"):
        replay_session(tape, tmp_path/"paper.db", universe, synthetic=True, backend="sqlite")


def test_unverified_eligibility_produces_no_orders(tmp_path):
    from sessionquant.session_runtime import replay_session
    rows, _, universe, tape = fixture_files(tmp_path)
    for row in rows:
        if row["kind"] == "eligibility":
            row["data"]["fractional"] = None
    tape.write_text("".join(json.dumps(x)+"\n" for x in rows))
    result = replay_session(tape, tmp_path/"paper.db", universe, synthetic=True, backend="sqlite")
    assert result["orders"] == []


def test_actual_order_flags_blocked_before_any_legacy_adapter(capsys):
    assert main(["legacy", "run-system", "--live", "--acknowledge-live-orders"]) == 2
    assert "실제 주문" in capsys.readouterr().err


def test_config_paths_and_non_paper_environment(tmp_path):
    file = tmp_path/"config"/"session.yaml"
    file.parent.mkdir()
    file.write_text("database_backend: mysql\ndb_path: ../data/local.db\nuniverse_path: ../data/list.json\n")
    settings = load_session_config(file)
    assert settings.db_path == tmp_path/"data"/"local.db"
    with pytest.raises(ValueError, match="PAPER_LOCAL"):
        SessionConfig(environment="LIVE_LOCAL")
    with pytest.raises(ValueError):
        SessionConfig(poll_seconds=float("nan"))
