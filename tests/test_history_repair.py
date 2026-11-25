from dataclasses import replace
from datetime import timedelta

import httpx
import pytest

from sessionquant.database.repository import Repository
from sessionquant.market.history import HistoryStore, KIND, history_plan
from sessionquant.market.history_commands import backfill
from sessionquant.market.history_repair import repair_conflict
from sessionquant.watchlist.budget import ReadCallBudget
from test_watchlist_history import NOW, Pages, minute_rows


@pytest.fixture
def history(tmp_path):
    repo = Repository(tmp_path/'repair.db', backend='sqlite')
    day, opened, closed = history_plan(NOW)[-1]
    bars = minute_rows('AAPL', opened)
    store = HistoryStore(repo)
    store.save('AAPL', day, bars+[replace(bars[0], volume=999)])
    yield repo, store, day, opened, closed, bars
    repo.close()


async def repair(h, client, budget=None):
    _, store, day, opened, closed, _ = h
    return await repair_conflict(store, 'AAPL', day, opened, closed, client,
                                 budget or ReadCallBudget(20), now=NOW)


async def test_matching_complete_reads_archive_replace_and_do_not_backdate(history):
    repo, store, day, opened, closed, bars = history
    old = store.get('AAPL', day)[0]
    client = Pages(bars)
    result = await repair(history, client)
    assert result['status'] == 'REPAIRED'
    assert len(client.calls) == 4  # each independent read starts at the close
    assert client.calls[0][1] == client.calls[2][1] == closed
    row, corrected = store.get('AAPL', day)
    assert row['quality']['complete'] and len(corrected) == 390
    audit = repo.get_control(result['audit_control'])
    assert audit['before'] == old and audit['after'] == row
    assert all(b.received_at > NOW for b in corrected)
    assert not [b for b in store.load('AAPL', NOW) if opened < b.timestamp <= closed]
    assert not repo.all('orders') and not repo.all('llm_calls')
    assert HistoryStore(repo).get('AAPL', day)[0] == row


@pytest.mark.parametrize('mode', ['missing', 'conflicting', 'different', 'stalled', 'above', 'wrong_source'])
async def test_unverified_reads_leave_original_unchanged(history, mode):
    repo, store, day, _, _, bars = history
    old = store.get('AAPL', day)[0]

    class Unreliable(Pages):
        async def candles(self, *args, **kwargs):
            if mode == 'different' and len(self.calls) == 2:
                self.rows = [replace(b, volume=123) for b in bars]
            rows, cursor = await super().candles(*args, **kwargs)
            if mode == 'conflicting' and rows:
                rows.append(replace(rows[0], volume=456))
            if mode == 'stalled':
                cursor = kwargs['before'].isoformat()
            if mode == 'above':
                rows.append(replace(bars[-1], timestamp=kwargs['before']+timedelta(minutes=1)))
            if mode == 'wrong_source':
                rows = [replace(b, source='unknown') for b in rows]
            return rows, cursor

    result = await repair(history, Unreliable(bars[1:] if mode=='missing' else bars))
    assert result['status'] == 'UNRESOLVED'
    assert store.get('AAPL', day)[0] == old
    assert repo.get_record(KIND, old['id']) == old


async def test_second_read_failure_preserves_original_and_retry_backoff(history, monkeypatch):
    import sessionquant.market.history_commands as commands
    repo, store, day, opened, closed, bars = history
    monkeypatch.setattr(commands, 'history_plan', lambda *a, **kw: [(day, opened, closed)])
    old = store.get('AAPL', day)[0]

    class Limited(Pages):
        async def candles(self, *a, **kw):
            if len(self.calls) == 2:
                httpx.Response(429, headers={'Retry-After':'120'},
                               request=httpx.Request('GET','https://example.invalid')).raise_for_status()
            return await super().candles(*a, **kw)

    report = await backfill(repo, ['AAPL'], Limited(bars), now=NOW, repair_conflicts=True)
    assert report['status']=='INTERRUPTED' and report['retry_after']
    assert repo.get_record(KIND, old['id']) == old


async def test_shared_budget_interrupts_without_partial_replacement(history, monkeypatch):
    import sessionquant.market.history_commands as commands
    repo, store, day, opened, closed, bars = history
    monkeypatch.setattr(commands, 'history_plan', lambda *a, **kw: [(day, opened, closed)])
    old = store.get('AAPL', day)[0]
    report = await backfill(repo, ['AAPL'], Pages(bars), now=NOW, max_requests=3, repair_conflicts=True)
    assert report['status']=='INTERRUPTED' and report['read_requests']==3
    assert repo.get_record(KIND, old['id']) == old
    report = await backfill(repo, ['AAPL'], Pages(bars), now=NOW, max_requests=4, repair_conflicts=True)
    assert report['status']=='COMPLETE' and report['complete_symbol_days']==1
    assert report['conflict_repairs'][0]['status']=='REPAIRED'


async def test_failed_write_rolls_back_archive_and_preserves_cache(history, monkeypatch):
    repo, store, day, _, _, bars = history
    old = store.get('AAPL', day)[0]
    controls_before = repo.db.execute('SELECT count(*) FROM controls').fetchone()[0]
    def fail(*a, **kw):
        raise RuntimeError('write failed')
    monkeypatch.setattr(repo, 'record', fail)
    with pytest.raises(RuntimeError):
        await repair(history, Pages(bars))
    assert repo.db.execute('SELECT count(*) FROM controls').fetchone()[0] == controls_before
    assert repo.get_record(KIND, old['id']) == store.get('AAPL', day)[0] == old


async def test_mid_repair_change_is_not_overwritten(history):
    repo, store, day, _, _, bars = history
    old = store.get('AAPL', day)[0]
    changed = {**old, 'external_writer':True}
    class Concurrent(Pages):
        async def candles(self, *a, **kw):
            repo.record(KIND, changed)
            return await super().candles(*a, **kw)
    result = await repair(history, Concurrent(bars))
    assert result['reason']=='history_changed_during_repair'
    assert repo.get_record(KIND, old['id']) == changed


def test_exclusion_text_distinguishes_counts_and_preserves_other_reasons():
    from sessionquant.dashboard.watchlist_panel import history_problem_text
    text = history_problem_text([{'session':'2026-11-17', 'missing_minutes':0,
        'conflicting_minutes':1, 'reasons':['history_minutes_missing_or_conflicting']}])
    assert text=='2026-11-17: 누락 0분 · 값 충돌 1분'
    assert 'history_session_missing' in history_problem_text([
        {'session':'2026-11-17', 'reasons':['history_session_missing']}])


async def test_open_session_is_never_reconciled(history):
    _, store, day, opened, closed, bars = history
    client = Pages(bars)
    result = await repair_conflict(store, 'AAPL', day, opened, closed, client,
                                   ReadCallBudget(20), now=closed)
    assert result['reason']=='session_not_closed' and not client.calls


async def test_early_close_uses_calendar_length(tmp_path):
    from datetime import datetime, timezone
    from sessionquant.market.bars import session_bounds
    now = datetime(2026, 11, 30, 14, 0, tzinfo=timezone.utc)
    day = '2026-11-27'
    opened, closed = session_bounds(datetime(2026, 11, 27, 17, 0, tzinfo=timezone.utc))
    repo = Repository(tmp_path/'short.db', backend='sqlite')
    try:
        store = HistoryStore(repo)
        bars = minute_rows('AAPL', opened, count=210, known=now)
        store.save('AAPL', day, bars+[replace(bars[0], volume=999)])
        result = await repair_conflict(store, 'AAPL', day, opened, closed, Pages(bars),
                                      ReadCallBudget(4), now=now)
        assert result['status']=='REPAIRED'
        assert store.get('AAPL', day)[0]['quality']['expected_minutes']==210
    finally:
        repo.close()
