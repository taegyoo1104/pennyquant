"""Resume only the same stopped Paper deployment backed up recently."""
from datetime import datetime, timedelta, timezone
import importlib.util
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location('resume_guard', Path(__file__).parents[1] / 'deploy/gcp/resume_retention_guard.py')
guard = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(guard)
NOW = datetime(2026, 11, 20, 7, 20, tzinfo=timezone.utc)
NAME = 'sessionquant-20261120T065932Z-fixture.tar.gz'
OFFLINE = {'mode': 'paper', 'status': 'OFFLINE', 'running': False, 'armed': False}


@pytest.mark.parametrize('age', [-1, 3601])
def test_resume_rejects_future_or_expired_backup(age):
    with pytest.raises(ValueError, match='백업'):
        guard.validate_receipt(f'UPLOADED {(NOW-timedelta(seconds=age)).isoformat()} {NAME}', NOW)


@pytest.mark.parametrize('text', ['FAILED yesterday', 'UPLOADED 2026-11-20T07:15:00 unsafe',
                                  f'UPLOADED 2026-11-20T07:15:00 {NAME}'])
def test_resume_rejects_invalid_receipts(text):
    with pytest.raises(ValueError):
        guard.validate_receipt(text, NOW)


@pytest.mark.parametrize('patch', [{'status': 'RUNNING'}, {'mode': 'live'}, {'running': True}, {'armed': True}, {'armed': 0}])
def test_resume_requires_unarmed_offline_paper_state(patch):
    with pytest.raises(ValueError):
        guard.validate_control(({**OFFLINE, **patch}, NOW-timedelta(minutes=1)), NOW)


def test_resume_rejects_missing_state_and_any_postbackup_control_change():
    for row in (None, (OFFLINE, NOW+timedelta(microseconds=1))):
        with pytest.raises(ValueError):
            guard.validate_control(row, NOW)


def test_resume_accepts_only_current_receipt_and_unchanged_offline_heartbeat():
    uploaded = guard.validate_receipt(f'UPLOADED {NOW.isoformat()} {NAME}', NOW+timedelta(minutes=30))
    assert uploaded == NOW
    guard.validate_control((OFFLINE, NOW.replace(tzinfo=None)-timedelta(minutes=32)), uploaded)
