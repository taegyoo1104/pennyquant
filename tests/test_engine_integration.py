from dataclasses import replace
from datetime import timedelta
import json

import pytest

from sessionquant.backtest.engine import compare, write_report
from sessionquant.config.settings import Settings, load_settings
from sessionquant.database.repository import Repository
from sessionquant.demo import demo_events
from sessionquant.engine import EventEngine
from sessionquant.events import read_events, write_events
from sessionquant.models import Quote
from sessionquant.strategies import STRATEGIES


def test_synthetic_end_to_end_all_strategies_and_report(tmp_path):
    events = demo_events()
    path = tmp_path / "events.jsonl"
    write_events(path, events)
    parsed = read_events(path)
    report = compare(parsed, Settings(), synthetic=True)
    assert set(report["strategies"]) == set(STRATEGIES)
    assert report["live_readiness"]["approved"] is False
    assert report["split"]["validation_sessions"] == 4
    for metrics in report["strategies"].values():
        assert metrics["trades"] > 0
        assert metrics["fees"] > 0
        assert metrics["cash"] >= 0
        assert metrics["validation"]["trades"] > 0
    # Invented paths deliberately cover winning and losing strategies.
    assert any(m["net_pnl"] < 0 for m in report["strategies"].values())
    assert any(m["net_pnl"] > 0 for m in report["strategies"].values())
    write_report(report, tmp_path / "report.json")
    assert json.loads((tmp_path / "report.json").read_text(encoding="utf-8"))["synthetic"] is True
    assert "합성 데이터" in (tmp_path / "report.md").read_text(encoding="utf-8")


def test_news_received_late_cannot_create_historical_entries():
    events = demo_events()[:81]  # one session
    news = events[0]
    late = replace(news.data, received_at=events[-1].available_at + timedelta(seconds=1))
    from sessionquant.events import Event
    ordered = sorted([Event("news", late), *events[1:]], key=lambda e: e.available_at)
    for name in STRATEGIES:
        engine = EventEngine(Settings(), name)
        for event in ordered:
            engine.process(event)
        assert engine.signal_count == 0
        assert not engine.manager.orders


def test_engine_snapshot_and_journal_are_rolled_back_together(tmp_path):
    repo = Repository(tmp_path / "test.db")
    try:
        repo.save_state({"cash": 70})
        with pytest.raises(RuntimeError):
            with repo.transaction():
                repo.save_state({"cash": 5})
                repo.record("orders", {"client_id": "attempt", "qty": 3})
                raise RuntimeError("simulated crash before commit")
        assert repo.load_state() == {"cash": 70}
        assert repo.all("orders") == []
    finally:
        repo.close()


def test_tape_rejects_out_of_order_and_future_market_event(tmp_path):
    events = demo_events()
    path = tmp_path / "bad.jsonl"
    write_events(path, [events[1], events[0]])
    with pytest.raises(ValueError, match="sorted"):
        read_events(path)
    quote = events[1].data.quote
    from sessionquant.events import Event
    write_events(path, [Event("quote", replace(quote, timestamp=quote.received_at + timedelta(seconds=1)))])
    with pytest.raises(ValueError, match="after receipt"):
        read_events(path)


@pytest.mark.parametrize("name,value", [("max_entries", 2.5), ("require_realtime_news", "false"), ("initial_cash", True), ("mode", "live")])
def test_unsafe_yaml_types_and_live_mode_are_rejected(tmp_path, name, value):
    import yaml
    path = tmp_path / "settings.yaml"
    path.write_text(yaml.safe_dump({name: value}))
    with pytest.raises(ValueError):
        load_settings(path)


def test_quote_rejects_truthy_string_eligibility_and_fractional_depth():
    quote = demo_events()[1].data.quote
    with pytest.raises(ValueError, match="boolean"):
        replace(quote, tradable="false")
    with pytest.raises(ValueError, match="integer"):
        replace(quote, ask_size=1.5)


def test_external_quote_missing_status_never_inherits_tradable_defaults():
    from sessionquant.events import parse_event
    from sessionquant.models import jsonable
    quote = jsonable(demo_events()[1].data.quote)
    del quote["halted"]
    parsed = parse_event({"kind": "quote", "data": quote})
    assert parsed.data.tradable is False


def test_news_revisions_remain_available_for_audit(tmp_path):
    event = demo_events()[0].data
    repo = Repository(tmp_path / "news.db")
    try:
        repo.record("news_events", event)
        repo.record("news_events", replace(event, title="Correction: FDA approval denied",
            received_at=event.received_at + timedelta(seconds=30)))
        assert len(repo.all("news_events")) == 2
    finally:
        repo.close()
