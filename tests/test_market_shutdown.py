"""자동 종료 달력과 실제 프로세스 종료 시 장부 저장을 확인한다."""
import asyncio
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import signal
import sqlite3
import subprocess
import sys
import time

import pytest
import yaml

from sessionquant.market_shutdown import shutdown_delay, with_graceful_sigterm


@pytest.mark.parametrize('now,end', [
    ('2026-11-16T13:00:00+00:00','2026-11-16T21:05:00+00:00'),
    ('2026-07-06T13:00:00+00:00','2026-07-06T20:05:00+00:00'),
    ('2026-11-27T13:00:00+00:00','2026-11-27T18:05:00+00:00'),
])
def test_close_deadline_accounts_for_dst_and_early_close(now,end):
    now,end=map(datetime.fromisoformat,(now,end))
    assert shutdown_delay(now)==int((end-now).total_seconds())


@pytest.mark.parametrize('now',[
    '2026-11-15T13:00:00+00:00',  # Sunday
    '2026-11-26T13:00:00+00:00',  # Thanksgiving
    '2026-11-16T21:05:00+00:00',
    '2026-11-17T01:00:00+00:00',  # UTC next day, still Monday evening in New York
])
def test_closed_or_nontrading_day_does_not_wait_until_next_session(now):
    assert shutdown_delay(datetime.fromisoformat(now))==0


def test_calendar_error_is_not_reported_as_holiday(monkeypatch):
    def failed(_):raise RuntimeError('calendar unavailable')
    monkeypatch.setattr('sessionquant.market_shutdown.calendar',failed)
    with pytest.raises(RuntimeError):shutdown_delay(datetime.now(timezone.utc))


async def test_unrelated_cancellation_still_propagates_and_restores_handler():
    previous=signal.getsignal(signal.SIGTERM)
    started=asyncio.Event()
    async def work():
        started.set()
        await asyncio.Event().wait()
    task=asyncio.create_task(with_graceful_sigterm(work))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):await task
    assert signal.getsignal(signal.SIGTERM)==previous


@pytest.mark.skipif(sys.platform=='win32',reason='GCE Linux SIGTERM semantics')
def test_sigterm_persists_runtime_and_releases_database_lease(tmp_path):
    from sessionquant.market.universe import UniverseStore
    from sessionquant.database.repository import Repository
    from sessionquant.session_runtime import _open
    now=datetime.now(timezone.utc)
    universe=tmp_path/'universe.json'
    UniverseStore(universe).refresh(['AAPL'],source='SYNTHETIC_FIXTURE',observed_at=now)
    feed=tmp_path/'feed.jsonl';feed.touch()
    db=tmp_path/'cloud-paper.db'
    config=tmp_path/'config.yaml'
    config.write_text(yaml.safe_dump({'source':'file','input_path':str(feed),'db_path':str(db),
        'universe_path':str(universe),'database_backend':'sqlite','news_enabled':False,
        'watchlist_enabled':False,'recording_enabled':True}))
    environment={**os.environ,'SQ_DB_BACKEND':'sqlite','SQ_ENVIRONMENT':'PAPER_LOCAL'}
    with (tmp_path/'runtime.log').open('w+') as log:
        child=subprocess.Popen([sys.executable,'-m','sessionquant','run','--config',str(config),
            '--no-dashboard','--no-desktop-notifications'],env=environment,stdout=log,stderr=log)
        try:
            deadline=time.monotonic()+15
            while time.monotonic()<deadline:
                assert child.poll() is None
                try:
                    with sqlite3.connect(db,timeout=.2) as connection:
                        row=connection.execute("SELECT payload FROM controls WHERE name='trade_control_state'").fetchone()
                        saved=connection.execute('SELECT payload FROM engine_state WHERE id=1').fetchone()
                    if row and saved and json.loads(row[0]).get('heartbeat'):break
                except sqlite3.OperationalError:
                    pass
                time.sleep(.05)
            else:pytest.fail('Paper runtime did not become ready')
            child.send_signal(signal.SIGTERM)
            assert child.wait(timeout=15)==0
        finally:
            if child.poll() is None:
                child.kill();child.wait()
        repo=Repository(db,backend='sqlite')
        try:
            assert repo.get_control('trade_control_state')['armed'] is False
            assert repo.get_control('trade_control_state')['status']=='OFFLINE'
            assert '정지됨' in repo.get_control('engine_status')
            assert repo.load_state() and not repo.all('orders')
        finally:repo.close()
        stack,reopened=_open(db,'sqlite')
        with stack:assert reopened.load_state()
