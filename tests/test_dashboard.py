"""Exercise rendered dashboard states and persistent entry controls."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from sessionquant.database.repository import Repository
from sessionquant.dashboard.app import event_health, monitor_mode, quote_health

APP = str(Path(__file__).resolve().parents[1] / "sessionquant/dashboard/app.py")
T0 = "2026-11-13T14:31:00+00:00"


def test_dashboard_empty_state(tmp_path, monkeypatch):
    monkeypatch.setenv("PQ_DB", str(tmp_path / "absent.db"))
    monkeypatch.setenv("PQ_REPORT", str(tmp_path / "absent.json"))
    app = AppTest.from_file(APP, default_timeout=30).run()
    assert not app.exception
    assert not app.get("help_info")
    assert len(app.tabs) == 4
    assert app.metric[0].value == "—"
    assert next(b for b in app.button if b.label == "신규 진입 중지").disabled
    assert app.toggle[0].label == "자동 갱신 · 3초"
    assert app.toggle[0].value is True
    app.toggle[0].set_value(False).run()
    assert not app.exception
    assert app.toggle[0].value is False


def test_dashboard_report_data_and_controls(tmp_path, monkeypatch):
    db_path = tmp_path / "sample.db"
    report_path = tmp_path / "report.json"
    monkeypatch.setenv("PQ_DB", str(db_path))
    monkeypatch.setenv("PQ_REPORT", str(report_path))
    repo = Repository(db_path)
    quote = {"symbol": "DEMO", "timestamp": T0, "received_at": T0, "bid": 2.0, "ask": 2.005, "bid_size": 30, "ask_size": 30, "tradable": True}
    repo.record("quote_snapshots", quote)
    repo.record("stocks", {"symbol": "DEMO", "quote": quote, "prev_close": 1.7, "relative_volume": 3.5, "dollar_volume": 3_000_000})
    repo.record("market_bars", {"symbol": "DEMO", "timestamp": T0, "received_at": T0, "close": 2.0})
    repo.record("news_events", {"id": "news-1", "symbols": ["DEMO"], "title": "FDA approves demo treatment", "published_at": T0, "received_at": T0, "source": "synthetic", "classification": {"sentiment": "positive"}})
    repo.record("signals", {"id": "signal-1", "symbol": "DEMO", "timestamp": T0, "strategy": "pullback", "reason": "test", "reference_price": 2.0})
    repo.save_state({
        "account": {"cash": 60.0, "equity": 70.2, "day_start_equity": 70.0, "reserved_cash": 0.0, "entries_today": 1,
                    "positions": {"DEMO": {"qty": 5, "avg_price": 2.0, "entry_fee": .01, "stop_price": 1.9, "target_price": 2.2, "strategy": "pullback"}}},
        "orders": {"order-1": {"client_id": "order-1", "symbol": "DEMO", "side": "buy", "qty": 5, "filled_qty": 5, "limit_price": 2.005, "avg_fill_price": 2., "created_at": T0, "status": "filled"}},
        "fills": [{"symbol": "DEMO", "side": "buy", "qty": 5, "price": 2., "fee": .01, "timestamp": T0, "client_id": "order-1"}],
        "halt_reasons": ["daily_loss"],
    })
    repo.set_control("source", {"synthetic": True, "label": "test fixture"})
    last_event = (datetime.now(timezone.utc) - timedelta(seconds=30)).isoformat()
    repo.set_control("runtime", {"last_event": last_event, "mode": "paper", "strategy": "pullback"})
    repo.close()
    report_path.write_text(json.dumps({
        "synthetic": True,
        "split": {"train_sessions": 3, "validation_sessions": 2},
        "strategies": {"pullback": {
            "trades": 4, "net_pnl": .2, "return_pct": .286, "win_rate": .75, "profit_factor": 1.5,
            "max_drawdown_pct": .3, "avg_trade_pnl": .05, "max_consecutive_losses": 1, "unfilled_order_rate": .25,
            "validation": {"trades": 2, "net_pnl": -.1},
        }},
        "equity_curve": [{"timestamp": T0, "strategy": "pullback", "equity": 70.2}],
        "trades": [{"symbol": "DEMO", "net_pnl": .2}],
    }), encoding="utf-8")

    app = AppTest.from_file(APP, default_timeout=30).run()
    assert not app.exception
    assert not app.get("help_info")
    assert app.metric[0].value == "$70.20"
    assert app.metric[3].value == "1개"
    compare = next(item.value for item in app.dataframe if "검증 구간 순손익 ($)" in item.value.columns)
    assert compare.iloc[0]["승률 (%)"] == 75.0
    assert compare.iloc[0]["미체결 비율 (%)"] == 25.0
    assert compare.iloc[0]["검증 구간 거래"] == 2
    system = next(item.value for item in app.dataframe if "항목" in item.value.columns)
    assert system.loc[system["항목"] == "마지막 이벤트 (KST)", "상태"].iloc[0].endswith("KST")
    assert system.loc[system["항목"] == "데이터 신선도", "상태"].iloc[0] == "최신 이벤트 없음 · 5초 초과"
    assert any("v0.1.0 MVP" in item.value for item in app.markdown)
    next(b for b in app.button if b.label == "신규 진입 중지").click().run()
    assert not app.exception
    repo = Repository(db_path)
    assert repo.get_control("manual_halt") is True
    assert repo.load_state()["halt_reasons"] == ["daily_loss"]
    repo.close()
    next(b for b in app.button if b.label == "신규 진입 재개").click().run()
    assert not app.exception
    repo = Repository(db_path)
    assert repo.get_control("manual_halt") is False
    assert repo.load_state()["halt_reasons"] == ["daily_loss"]
    repo.close()


def test_event_health_does_not_infer_connection_and_checks_clock():
    now = datetime(2026, 11, 13, 15, 0, tzinfo=timezone.utc)
    current = event_health({"last_event": (now - timedelta(seconds=2)).isoformat()}, now)
    assert current["stale"] is False
    assert current["lag_seconds"] == 2
    assert "연결 여부 별도 확인" in current["status"]
    stale = event_health({"last_event": (now - timedelta(minutes=3)).isoformat()}, now)
    assert stale["stale"] is True
    assert stale["lag_label"] == "3분 0초"
    assert event_health({}, now)["lag_seconds"] is None
    assert event_health({"last_event": "2026-11-13T15:00:00"}, now)["status"] == "이벤트 시각 확인 필요"
    future = event_health({"last_event": (now + timedelta(seconds=10)).isoformat()}, now)
    assert future["stale"] is True
    assert future["status"] == "이벤트 시각이 현재보다 미래임"


def setup_monitor_db(tmp_path, monkeypatch, *, status="connected", error=None):
    db_path = tmp_path / "monitor.db"
    report_path = tmp_path / "unrelated-report.json"
    monkeypatch.setenv("PQ_DB", str(db_path))
    monkeypatch.setenv("PQ_REPORT", str(report_path))
    now = datetime.now(timezone.utc)
    received = now.isoformat()
    repo = Repository(db_path)
    repo.set_control("source", {"synthetic": False, "label": "Toss observed prices, partial venues, not NBBO"})
    repo.set_control("runtime", {"mode": "monitor", "provider": "toss", "last_event": received})
    repo.set_control("monitor", {"mode": "monitor", "provider": "toss", "status": status,
        "symbols": ["AAPL", "MSFT"], "interval_seconds": 3, "last_success": received,
        "error": error, "feed_verified": False, "trading_enabled": False,
        "missing_symbols": ["MSFT"] if error else []})
    aapl = {"id": "AAPL", "symbol": "AAPL", "last_price": "185.70", "currency": "USD",
        "timestamp": (now - timedelta(minutes=5)).isoformat(), "received_at": received, "source": "toss"}
    repo.record("stocks", aapl)
    repo.record("stocks", {"id": "OLD", "symbol": "OLD", "last_price": "999", "currency": "USD",
        "timestamp": received, "received_at": received, "source": "toss"})
    repo.record("price_samples", {**aapl, "id": "AAPL-1"})
    if not error:
        msft = {"id": "MSFT", "symbol": "MSFT", "last_price": "351.24", "currency": "USD",
            "timestamp": None, "received_at": received, "source": "toss"}
        repo.record("stocks", msft)
        repo.record("price_samples", {**msft, "id": "MSFT-1"})
    repo.close()
    # A monitoring DB must not display a coincidental demo/performance file.
    report_path.write_text("not a valid report", encoding="utf-8")
    return db_path


def test_monitor_displays_observed_prices_without_paper_account_or_controls(tmp_path, monkeypatch):
    db_path = setup_monitor_db(tmp_path, monkeypatch)
    app = AppTest.from_file(APP, default_timeout=30).run()
    assert not app.exception
    assert not app.get("help_info")
    assert len(app.tabs) == 4
    assert not any(item.label in {"총자산", "가용 현금", "최근 세션 손익"} for item in app.metric)
    assert not any(button.label in {"신규 진입 중지", "신규 진입 재개"} for button in app.button)
    assert next(item for item in app.metric if item.label == "관심 종목").value == "2개"
    assert next(item for item in app.metric if item.label == "현재가 수신 종목").value == "2개"
    assert not app.error  # unrelated invalid report is not read in monitor mode
    assert not app.get("download_button")
    prices = next(item.value for item in app.dataframe if "현재가 ($)" in item.value.columns)
    assert set(prices["종목"]) == {"AAPL", "MSFT"}
    assert prices.loc[prices["종목"] == "AAPL", "현재가 ($)"].iloc[0] == 185.7
    assert prices.loc[prices["종목"] == "AAPL", "제공 시각 상태"].iloc[0] == "제공 시각이 오래됨"
    assert prices.loc[prices["종목"] == "MSFT", "제공 시각 상태"].iloc[0] == "제공 시각 미확인"
    assert set(prices["거래 가능 여부"]) == {"미확인"}
    assert any("조회 시점별 가격" in item.value for item in app.markdown)
    assert any("체결 틱이나 1분봉 차트가 아닙니다" in item.value for item in app.markdown)
    repo = Repository(db_path)
    assert repo.load_state() is None
    assert repo.get_control("manual_halt") is None
    repo.close()


@pytest.mark.parametrize("status,label", [("error", "조회 오류"), ("stopped", "수집 중지 · 오류")])
def test_monitor_surfaces_errors_and_never_fills_unknown_price_with_zero(tmp_path, monkeypatch, status, label):
    setup_monitor_db(tmp_path, monkeypatch, status=status,
                     error={"code": "http_error", "http_status": 403, "message": "Provider rejected access"})
    app = AppTest.from_file(APP, default_timeout=30).run()
    assert not app.exception
    assert next(item for item in app.metric if item.label == "수집 상태").value == label
    assert next(item for item in app.metric if item.label == "현재가 수신 종목").value == "1개"
    assert any("HTTP 403" in item.value for item in app.error)
    prices = next(item.value for item in app.dataframe if "현재가 ($)" in item.value.columns)
    assert prices.loc[prices["종목"] == "MSFT", "현재가 ($)"].isna().all()
    assert any("MSFT" in item.value for item in app.warning)


def test_monitor_mode_can_be_forced_before_collector_creates_database(tmp_path, monkeypatch):
    monkeypatch.setenv("PQ_DB", str(tmp_path / "absent.db"))
    monkeypatch.setenv("PQ_REPORT", str(tmp_path / "absent.json"))
    monkeypatch.setattr("sys.argv", [APP, "--mode", "monitor"])
    app = AppTest.from_file(APP, default_timeout=30).run()
    assert not app.exception
    assert not any(button.label in {"신규 진입 중지", "신규 진입 재개"} for button in app.button)
    assert any("토스 시세 모니터링" in item.value for item in app.markdown)
    assert next(item for item in app.metric if item.label == "수집 상태").value == "수집 상태 미확인"


def test_quote_health_distinguishes_missing_old_and_fresh_provider_time():
    now = datetime(2026, 11, 13, 15, 0, tzinfo=timezone.utc)
    assert quote_health(None, now)["status"] == "제공 시각 미확인"
    old = quote_health((now - timedelta(minutes=3)).isoformat(), now)
    assert old["stale"] is True and old["status"] == "제공 시각이 오래됨"
    fresh = quote_health((now - timedelta(seconds=1)).isoformat(), now)
    assert fresh["stale"] is False
    assert "실시간 권한 별도 확인" in fresh["status"]
    assert monitor_mode({}, {}, "monitor") is True
    assert monitor_mode({"mode": "monitor"}, {}, "paper") is True
    assert monitor_mode({}, {}, "auto") is False
