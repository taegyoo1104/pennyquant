"""Exercise rendered dashboard states and persistent entry controls."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from streamlit.testing.v1 import AppTest

from pennyquant.database.repository import Repository
from pennyquant.dashboard.app import event_health

APP = str(Path(__file__).resolve().parents[1] / "pennyquant/dashboard/app.py")
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
