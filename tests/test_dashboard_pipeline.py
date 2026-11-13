"""Unified candidate/evidence UI never invents account readiness or clears a loss latch."""
from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

from streamlit.testing.v1 import AppTest

from pennyquant.config.settings import Settings
from pennyquant.database.repository import Repository
from pennyquant.dashboard.app import alert_sound, pipeline_candidate_rows, pipeline_mode


APP = str(Path(__file__).resolve().parents[1] / "pennyquant/dashboard/app.py")


def system_db(tmp_path, monkeypatch, *, mode="paper", daily_halt=False):
    path = tmp_path / "system.db"
    monkeypatch.setenv("PQ_DB", str(path))
    report = tmp_path / "unrelated.json"
    report.write_text("invalid unrelated report", encoding="utf-8")
    monkeypatch.setenv("PQ_REPORT", str(report))
    now = datetime.now(timezone.utc).isoformat()
    repo = Repository(path)
    repo.set_control("runtime_kind", "market_pipeline")
    repo.set_control("pipeline_status", {"mode": mode, "status": "running", "last_cycle_at": now,
                     "max_positions": 5, "daily_loss_pct": .02, "live_orders_enabled": False,
                     "halt_reasons": ["daily_loss"] if daily_halt else []})
    return path, repo, now


def test_pipeline_empty_forced_mode_has_no_fabricated_balance_or_performance(tmp_path, monkeypatch):
    monkeypatch.setenv("PQ_DB", str(tmp_path / "absent.db"))
    monkeypatch.setenv("PQ_REPORT", str(tmp_path / "absent.json"))
    monkeypatch.setattr("sys.argv", [APP, "--mode", "system"])
    app = AppTest.from_file(APP, default_timeout=30).run()
    assert not app.exception
    assert [tab.label for tab in app.tabs] == ["후보 종목", "뉴스 · AI 분석", "주문 · 포지션", "운영 · 알림"]
    assert next(metric for metric in app.metric if metric.label == "총자산").value == "—"
    assert next(metric for metric in app.metric if metric.label == "보유 / 최대 종목").value == "— / 5"
    assert not app.get("download_button")
    assert next(toggle for toggle in app.toggle if toggle.label == "새 위험 알림 소리").value is False
    assert next(button for button in app.button if button.label == "신규 진입 중지").disabled


def test_pipeline_candidates_and_primary_vs_auxiliary_analysis_are_distinct(tmp_path, monkeypatch):
    _, repo, now = system_db(tmp_path, monkeypatch)
    repo.record("candidates", {"id": "DEMO", "symbol": "DEMO", "last_price": 2.1, "gain_pct": .12,
                "triggers": ["price_rise", "high_market_volume"], "trading_volume": 2000000,
                "trading_amount": 4200000, "ranked_at": now, "received_at": now,
                "readiness": "WATCH", "blockers": ["verified_market_snapshot_required"]})
    repo.record("news_analysis", {"id": "analysis-1", "ticker": "DEMO", "source_kind": "sec",
                "primary_verified": True, "sentiment": "POSITIVE", "entry_eligible": True,
                "reason": "원문에서 확정 계약 확인", "analyzed_at": now,
                "event": {"title": "Company signs a contract <script>", "published_at": now, "received_at": now},
                "events": [{"evidence": "signed a contract"}]})
    repo.record("news_analysis", {"id": "analysis-2", "ticker": "DEMO", "source_kind": "alpaca",
                "primary_verified": False, "sentiment": "UNKNOWN", "entry_eligible": False,
                "reason": "보조 기사", "error": "llm_unavailable", "analyzed_at": now,
                "missing_information": ["공식 원문 필요"]})
    repo.close()
    app = AppTest.from_file(APP, default_timeout=30).run()
    assert not app.exception and not app.error
    candidates = next(item.value for item in app.dataframe if "선정 이유" in item.value.columns)
    assert candidates.iloc[0]["종목"] == "DEMO"
    assert candidates.iloc[0]["등락률 (%)"] == 12.0
    assert candidates.iloc[0]["선정 이유"] == "급등 · 거래량 상위"
    assert candidates.iloc[0]["판단 상태"] == "관찰"
    assert candidates.iloc[0]["관찰·차단 사유"] == "매매용 시장 데이터 검증 대기"
    markup = "\n".join(item.value for item in app.markdown)
    assert "SEC · 원문 확인" in markup and "Alpaca · 지연 보조 자료" in markup
    assert "Company signs a contract &lt;script&gt;" in markup
    assert "Company signs a contract <script>" not in markup
    assert any("llm_unavailable" in item.value for item in app.warning)
    assert not app.get("download_button")


def test_pipeline_daily_loss_alert_and_manual_resume_preserve_day_halt(tmp_path, monkeypatch):
    path, repo, now = system_db(tmp_path, monkeypatch, daily_halt=True)
    repo.save_state({"account": {"equity": 97, "day_start_equity": 100, "positions": {}, "cash": 97},
                     "halt_reasons": ["daily_loss"]})
    repo.record("alerts", {"id": "loss-1", "timestamp": now, "level": "critical", "code": "daily_loss",
                            "message": "오늘 손실 한도에 도달하여 신규 매수를 중단했습니다."})
    repo.set_control("manual_halt", True)
    repo.close()
    app = AppTest.from_file(APP, default_timeout=30).run()
    assert not app.exception
    assert next(metric for metric in app.metric if metric.label == "당일 순손익").value == "$-3.00"
    assert next(metric for metric in app.metric if metric.label == "신규 진입").value == "당일 손실로 중지"
    assert any("오늘 손실 한도" in item.value for item in app.error)
    next(button for button in app.button if button.label == "신규 진입 재개").click().run()
    assert not app.exception
    repo = Repository(path)
    assert repo.get_control("manual_halt") is False
    assert repo.load_state()["halt_reasons"] == ["daily_loss"]
    assert repo.get_control("pipeline_status")["halt_reasons"] == ["daily_loss"]
    repo.close()


def test_live_system_does_not_display_old_paper_balance_as_live_cash(tmp_path, monkeypatch):
    _, repo, now = system_db(tmp_path, monkeypatch, mode="live")
    repo.save_state({"account": {"equity": 999999, "day_start_equity": 999999, "positions": {"PAPER": {"qty": 1}}}})
    repo.set_control("broker_state", {"mode": "live", "reconciled": True, "positions": {"LIVE": 2},
                     "cash_buying_power": "1000.00", "observed_at": now})
    repo.close()
    app = AppTest.from_file(APP, default_timeout=30).run()
    assert not app.exception
    assert next(metric for metric in app.metric if metric.label == "총자산").value == "—"
    assert next(metric for metric in app.metric if metric.label == "보유 / 최대 종목").value == "1 / 5"
    assert next(metric for metric in app.metric if metric.label == "당일 순손익").value == "—"
    holdings = next(item.value for item in app.dataframe if "보유 수량" in item.value.columns)
    assert holdings.iloc[0]["종목"] == "LIVE" and holdings.iloc[0]["보유 수량"] == 2
    assert not any("실거래 활성화" in button.label for button in app.button)
    assert any("실주문 차단" in item.value for item in app.markdown)


def test_pipeline_missing_price_and_timestamp_remain_unknown():
    rows = pipeline_candidate_rows([{"symbol": "DEMO", "last_price": None, "gain_pct": None,
                                    "triggers": ["price_fall_watch"], "readiness": "BLOCKED"}])
    assert rows[0]["현재가 ($)"] is None and rows[0]["등락률 (%)"] is None
    assert rows[0]["제공 시각 상태"] == "제공 시각 미확인"
    assert rows[0]["선정 이유"] == "급락 관찰"
    assert rows[0]["판단 상태"] == "신규 매수 차단"
    assert pipeline_mode({}, {}, "system")
    assert pipeline_mode({}, {}, runtime_kind="market_pipeline")
    assert not pipeline_mode({}, {}, "auto", "toss_monitor")
    assert alert_sound().startswith(b"RIFF")


def test_system_new_alert_sound_requires_opt_in_and_a_new_alert(tmp_path, monkeypatch):
    path, repo, now = system_db(tmp_path, monkeypatch)
    repo.record("alerts", {"id": "old", "level": "warning", "message": "기존 알림", "timestamp": now})
    repo.close()
    app = AppTest.from_file(APP, default_timeout=30).run()
    assert not app.exception and not app.get("audio")
    next(toggle for toggle in app.toggle if toggle.label == "새 위험 알림 소리").set_value(True).run()
    assert not app.exception and not app.get("audio")
    repo = Repository(path)
    repo.record("alerts", {"id": "new", "level": "critical", "message": "새 위험 알림",
                            "timestamp": (datetime.now(timezone.utc) + timedelta(seconds=1)).isoformat()})
    repo.close()
    next(button for button in app.button if button.label == "데이터 새로고침").click().run()
    assert not app.exception and app.get("audio")


def test_pipeline_expired_candidates_are_not_shown_as_current_ready_entries(tmp_path, monkeypatch):
    _, repo, now = system_db(tmp_path, monkeypatch)
    status = repo.get_control("pipeline_status")
    repo.set_control("pipeline_status", {**status, "active_symbols": []})
    repo.record("candidates", {"id": "OLD", "symbol": "OLD", "last_price": 2, "gain_pct": .2,
                               "readiness": "READY", "triggers": ["price_rise"], "received_at": now})
    repo.close()
    app = AppTest.from_file(APP, default_timeout=30).run()
    assert not app.exception
    assert next(metric for metric in app.metric if metric.label == "후보 종목").value == "0개"
    assert not any("선정 이유" in item.value.columns for item in app.dataframe)


def test_pipeline_position_shows_estimated_net_liquidation_and_breakeven(tmp_path, monkeypatch):
    _, repo, now = system_db(tmp_path, monkeypatch)
    settings = Settings(fee_model="toss_us", exit_price_policy="net", slippage_bps=10,
                        commission_sell_bps=10, sell_regulatory_bps=.206, sell_regulatory_min=.01)
    repo.save_state({"account": {"cash": 0, "equity": 201.578, "day_start_equity": 200.2,
                     "positions": {"DEMO": {"qty": 100, "avg_price": 2, "entry_fee": .2,
                                            "stop_price": 1.91, "target_price": 2.22}}},
                     "settings": asdict(settings)})
    repo.record("quote_snapshots", {"symbol": "DEMO", "bid": 2.02, "ask": 2.025,
                                    "timestamp": now, "received_at": now})
    repo.close()
    app = AppTest.from_file(APP, default_timeout=30).run()
    assert not app.exception
    positions = next(item.value for item in app.dataframe if "평균 매수가 ($)" in item.value.columns)
    assert positions.iloc[0]["평가손익 ($)"] == 1.378
    assert positions.iloc[0]["예상 손익분기가 ($)"] == 2.01
