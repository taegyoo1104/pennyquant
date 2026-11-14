"""SessionQuant 화면의 미확인 상태, 금액 표시와 접속 오류 처리를 검증한다."""
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from streamlit.testing.v1 import AppTest

from sessionquant.dashboard.session_app import DashboardData, build_view, decimal_value, load_database

APP = str(Path(__file__).resolve().parents[1] / "sessionquant/dashboard/session_app.py")


def test_empty_dashboard_does_not_invent_account(tmp_path, monkeypatch):
    monkeypatch.setenv("SQ_DB", str(tmp_path / "absent.db"))
    monkeypatch.setenv("SQ_DB_BACKEND", "sqlite")
    monkeypatch.setenv("PQ_DB_BACKEND", "sqlite")
    app = AppTest.from_file(APP, default_timeout=30).run()
    assert not app.exception
    assert app.title[0].value == "SessionQuant"
    assert app.metric[0].value == "—"
    assert app.metric[4].value == "확인 필요"
    assert {"시장 현황", "종목 현황", "포트폴리오", "뉴스 분석", "주문 및 운영"}.issubset({item.label for item in app.tabs})
    assert not any("실거래 활성화" == button.label for button in app.button)
    assert not (tmp_path / "absent.db").exists()


def test_dashboard_decimal_positions_and_unknown_timestamps():
    data = DashboardData(state={"broker": {"account": {"cash": "80.00", "equity": "100.00", "reserved_cash": "0"},
                                                   "positions": {"AAPL": {"qty": "0.1", "avg_price": "200", "entry_fee": "0.01", "stop_price": "196"}}}})
    view = build_view(data, datetime(2026, 11, 13, 17, tzinfo=timezone.utc))
    assert view["positions"][0]["보유 수량"] == "0.1"
    assert view["positions"][0]["비중"] == "20.0%"
    assert view["positions"][0]["평가 손익"] == "$-0.01"
    assert view["lag"] is None
    assert view["regime"] == "UNKNOWN"
    assert decimal_value("NaN") is None
    assert decimal_value("Infinity") is None
    assert decimal_value(True) is None
    assert decimal_value("0.000001") == Decimal("0.000001")


def test_mysql_loader_does_not_require_local_file_and_sanitizes_errors(tmp_path, monkeypatch):
    import sessionquant.database.config as config
    import sessionquant.database.repository as module

    called = []
    monkeypatch.setattr(config, "database_backend", lambda: "mysql")

    def broken_repository(path):
        called.append(path)
        raise RuntimeError("password=PRIVATE_TEST_SECRET host=some-private-server")

    monkeypatch.setattr(module, "Repository", broken_repository)
    data = load_database(tmp_path / "not-a-local-db.db")
    assert called
    assert data.error
    assert "PRIVATE_TEST_SECRET" not in data.error
    assert "some-private-server" not in data.error
    assert not data.connected


def test_loader_bounds_every_journal_and_closes(tmp_path, monkeypatch):
    import sessionquant.database.config as config
    import sessionquant.database.repository as module

    calls = []

    class FakeRepository:
        def __init__(self, path):
            pass
        def load_state(self):
            return {"account": {"equity": "100"}}
        def get_control(self, name, default):
            assert name == "sessionquant_context"
            return {"environment": "PAPER_LOCAL"}
        def recent(self, name, limit):
            calls.append((name, limit))
            return []
        def close(self):
            calls.append("closed")

    monkeypatch.setattr(config, "database_backend", lambda: "mysql")
    monkeypatch.setattr(module, "Repository", FakeRepository)
    data = load_database(tmp_path / "unused.db")
    assert data.connected
    assert calls[-1] == "closed"
    assert all(limit == 2000 for name, limit in calls[:-1])


def test_dashboard_reads_actual_engine_flat_state_and_context():
    data = DashboardData(state={"environment": "PAPER_LOCAL", "broker": {"cash": "80", "equity": "100",
        "positions": {"AAPL": {"qty": "0.1", "avg_price": "200", "initial_amount": "20", "adds": 1}}}},
        context={"updated_at": "2026-11-13T17:00:00+00:00", "synthetic": True,
                 "market_regime": {"state": "BULLISH", "components": {"SPY": {"vwap": 500}}}})
    view = build_view(data, datetime(2026, 11, 13, 17, tzinfo=timezone.utc))
    assert view["account"]["equity"] == "100"
    assert view["positions"][0]["최초 진입 금액"] == "$20.00"
    assert view["positions"][0]["추가매수 횟수"] == 1
    assert view["regime"] == "BULLISH"
    assert view["lag"] == 0
    assert view["synthetic"]
