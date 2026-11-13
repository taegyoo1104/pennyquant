from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from sessionquant.config.settings import Settings
from sessionquant.models import NewsEvent, Quote, StockSnapshot
from sessionquant.news.classifier import classify
from sessionquant.news.event_filter import eligible_events, positive_event
from sessionquant.scanners.stock_scanner import rank_candidates, scan


NOW = datetime(2026, 11, 13, 15, 0, tzinfo=timezone.utc)


def event(title="Company receives FDA approval", **kwargs):
    return NewsEvent("n1", ("DEMO",), title, NOW - timedelta(seconds=5), NOW, "Alpaca", real_time=True, **kwargs)


def snapshot(**kwargs):
    quote = Quote("DEMO", NOW, NOW, 2.40, 2.405, 100, 100)
    return StockSnapshot(quote, 2.0, 1_000_000, 2_400_000, 4.0, 2.45, **kwargs)


@pytest.mark.parametrize("title,sentiment,kind", [
    ("Company receives FDA approval", "positive", "fda_approval"),
    ("FDA approves new therapy", "positive", "fda_approval"),
    ("Company has not received FDA approval", "uncertain", "negated_claim"),
    ("FDA approval was not granted", "uncertain", "negated_claim"),
    ("FDA approval denied", "uncertain", "negated_claim"),
    ("FDA approval has not been granted", "uncertain", "negated_claim"),
    ("Positive clinical results were not achieved", "uncertain", "negated_claim"),
    ("Trial results are not positive", "uncertain", "negated_claim"),
    ("Company expects FDA approval next year", "uncertain", "prospective_claim"),
    ("FDA approval is expected next year", "uncertain", "prospective_claim"),
    ("FDA approval may be granted next year", "uncertain", "prospective_claim"),
    ("Positive clinical results but company launches public offering", "negative", "dilution"),
    ("FDA rejects application", "negative", "regulatory"),
    ("Clinical trial failed to meet primary endpoint", "negative", "clinical_failure"),
    ("Company won a major contract", "positive", "contract"),
    ("Stock surges 50%", "neutral", "price_only"),
    ("Rumor of FDA approval", "uncertain", "rumor"),
    ("Company files Form 8-K", "neutral", "unclassified"),
    ("Company files Form 424B5", "neutral", "unclassified"),
    ("Company announces acquisition", "uncertain", "merger"),
])
def test_news_rules(title, sentiment, kind):
    result = classify(event(title))
    assert (result.sentiment, result.event_type) == (sentiment, kind)


def test_unverified_and_negative_news_block_entries():
    assert classify(replace(event(), verified=False)).exclude
    negative = replace(event("Company receives delisting notice"), id="n2")
    assert positive_event([event(), negative], "DEMO", NOW, Settings()) is None


def test_delayed_supplemental_bad_news_still_blocks():
    negative = replace(event("Company receives delisting notice"), id="late", published_at=NOW-timedelta(hours=1), real_time=False)
    assert positive_event([event(), negative], "DEMO", NOW, Settings()) is None
    neutral = replace(event("Company files Form 8-K"), id="form", real_time=False)
    assert positive_event([event(), neutral], "DEMO", NOW, Settings()) == event()


def test_news_gates_future_delayed_old_and_non_realtime():
    valid = event()
    settings = Settings()
    bad = [
        replace(valid, id="future", published_at=NOW+timedelta(seconds=1)),
        replace(valid, id="receipt", received_at=NOW+timedelta(seconds=1)),
        replace(valid, id="delayed", published_at=NOW-timedelta(minutes=3)),
        replace(valid, id="old", published_at=NOW-timedelta(days=2), received_at=NOW-timedelta(days=2)),
        replace(valid, id="historical", real_time=False),
        replace(valid, id="wrong", symbols=("OTHER",)),
        replace(valid, id="outoforder", received_at=valid.published_at-timedelta(seconds=1)),
    ]
    assert eligible_events([*bad, valid], "DEMO", NOW, settings) == [valid]
    assert positive_event([valid], "DEMO", NOW, settings) == valid


def test_first_receipt_wins_and_duplicate_is_not_refreshed():
    first = event()
    refreshed = replace(first, received_at=NOW+timedelta(seconds=1))
    assert eligible_events([refreshed, first], "DEMO", NOW+timedelta(seconds=1), Settings()) == [first]


def test_valid_snapshot_passes():
    assert scan(snapshot(), NOW, Settings()) == (True, [])


@pytest.mark.parametrize("update,reason", [
    ({"relative_volume":None}, "unknown_relative_volume"),
    ({"relative_volume":float("nan")}, "unknown_relative_volume"),
    ({"relative_volume":2}, "insufficient_relative_volume"),
    ({"dollar_volume":100}, "insufficient_dollar_volume"),
    ({"cumulative_volume":float("nan")}, "insufficient_volume"),
    ({"prev_close":0}, "unknown_previous_close"),
    ({"day_high":3}, "falling_from_high"),
    ({"corporate_action":True}, "corporate_action"),
    ({"dilution_risk":True}, "dilution_risk"),
])
def test_missing_data_and_risks_fail_closed(update, reason):
    approved, reasons = scan(replace(snapshot(), **update), NOW, Settings())
    assert not approved and reason in reasons


@pytest.mark.parametrize("update,reason", [
    ({"exchange":"OTC"}, "unapproved_exchange"),
    ({"halted":True}, "not_tradable"),
    ({"ask":2.5}, "wide_spread"),
    ({"bid_size":0}, "insufficient_depth"),
    ({"timestamp":NOW-timedelta(seconds=6)}, "stale_quote"),
    ({"timestamp":NOW+timedelta(seconds=1)}, "invalid_quote_time"),
])
def test_quote_risks(update, reason):
    item = snapshot()
    approved, reasons = scan(replace(item, quote=replace(item.quote, **update)), NOW, Settings())
    assert not approved and reason in reasons


def test_candidate_limit_is_deterministic():
    sample = snapshot()
    values = [replace(sample, quote=replace(sample.quote, symbol=f"X{i:02}")) for i in range(25)]
    result = rank_candidates(values[::-1], NOW, Settings())
    assert len(result) == 20
    assert [item.quote.symbol for item in result] == [f"X{i:02}" for i in range(20)]
