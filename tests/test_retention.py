from datetime import datetime, timedelta, timezone

import pytest

from sessionquant.cli import parser
from sessionquant.database.retention import RetentionPolicy, _unused, backup_name

NOW = datetime(2026, 11, 18, 21, 5, tzinfo=timezone.utc)
NAME = "sessionquant-20261118T210400Z-test-fixture.tar.gz"


@pytest.mark.parametrize("settings", [
    {"enabled": 1}, {"diagnostics_days": 6}, {"news_days": 29}, {"max_rows": True},
    {"max_seconds": 91}, {"batch_size": 501}, {"diagnostics_days": 60, "news_days": 30},
])
def test_policy_rejects_unsafe_limits(settings):
    with pytest.raises(ValueError):
        RetentionPolicy(**settings)


@pytest.mark.parametrize("value", [
    "FAILED now", "RUNNING now", f"UPLOADED {(NOW + timedelta(seconds=1)).isoformat()} {NAME}",
    f"UPLOADED {(NOW - timedelta(hours=2)).isoformat()} {NAME}",
    f"UPLOADED {NOW.isoformat()} ../../wrong-file", "",
])
def test_apply_requires_recent_successful_backup_receipt(tmp_path, value):
    receipt = tmp_path / "status"
    receipt.write_text(value)
    with pytest.raises(ValueError):
        backup_name(receipt, NOW)


def test_fresh_receipt_and_preview_cli(tmp_path):
    receipt = tmp_path / "status"
    receipt.write_text(f"UPLOADED {NOW.isoformat()} {NAME}\n")
    assert backup_name(receipt, NOW) == NAME
    with pytest.raises(ValueError):
        backup_name(None, NOW)
    args = parser().parse_args(["data-retention"])
    assert not args.apply and args.backup_receipt is None


def test_unknown_recent_or_referenced_metadata_is_kept():
    protected = {"news": {"catalyst"}, "selection": {"allocation"}}
    assert _unused("news_events", {"id": "expired", "published_at": "2026-01-01T00:00:00Z"}, NOW, protected)
    for metadata in ({"news_id": "catalyst"}, {"event_id": "catalyst"}, {"known_at": NOW.isoformat()},
                     {"valid_until": "2099-01-01T00:00:00Z"}, {"published_at": "unknown"},
                     {"journal_ingested_at": NOW.isoformat()}):
        assert not _unused("news_analysis", metadata, NOW, protected)
    assert not _unused("pipeline_status", {"id": "allocation"}, NOW, protected)


@pytest.mark.parametrize("timestamp,expired", [
    ("2026-01-01T00:00:00Z", True),
    (NOW.isoformat(), False),
    ((NOW + timedelta(days=1)).isoformat(), False),
    ("unverified", False),
    ("2026-01-01T00:00:00", False),
])
def test_imported_timestamp_must_prove_expiry_even_with_old_receipt(timestamp, expired):
    metadata = {"received_at": "2026-01-01T00:00:00Z", "timestamp": timestamp}
    assert _unused("quote_snapshots", metadata, NOW, {"news": set(), "selection": set()}) is expired
