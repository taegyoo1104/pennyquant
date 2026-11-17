"""Bound repeated cap reads without persisting freshness or operator controls."""
from contextlib import nullcontext
from copy import deepcopy
from datetime import timedelta

import pytest

from sessionquant.watchlist.capitalization import ATTEMPT, HISTORY
from watchlist_fixtures import NOW, context, observe


async def test_publish_matches_uncached_output_with_two_control_reads(tmp_path, monkeypatch):
    repo, _, _, _, manager = context(tmp_path, size=50)
    try:
        await manager.refresh(NOW)
        observe(manager)
        now = NOW + timedelta(seconds=60)
        queries = []
        repo.db.set_trace_callback(queries.append)
        original_scope = manager.read_scope
        monkeypatch.setattr(manager, 'read_scope', nullcontext)
        manager.publish(now)
        before = sum(q.startswith('SELECT') and 'FROM controls' in q for q in queries)
        expected = deepcopy(repo.get_control('watchlist_dashboard_v1'))
        monkeypatch.setattr(manager, 'read_scope', original_scope)
        queries.clear()
        manager.publish(now)
        after = sum(q.startswith('SELECT') and 'FROM controls' in q for q in queries)
        assert repo.get_control('watchlist_dashboard_v1') == expected
        assert before >= 400 and after == 2
        assert repo.all('orders') == []
    finally:
        repo.close()


async def test_read_scope_does_not_cache_expiry_membership_or_mutable_return_values(tmp_path):
    repo, universe, _, _, manager = context(tmp_path, size=50)
    try:
        await manager.refresh(NOW)
        universe.max_age_days = 30
        with manager.read_scope():
            status = manager.store.status(NOW, allow_synthetic=True)
            assert status['status'] == 'VALID'
            status['snapshot']['data_status'] = 'INVALID'
            assert manager.store.status(NOW, allow_synthetic=True)['status'] == 'VALID'
            assert manager.store.status(NOW + timedelta(days=8), allow_synthetic=True)['status'] == 'EXPIRED'
            universe.refresh(['T499'], source='SYNTHETIC_FIXTURE', observed_at=NOW)
            assert manager.store.status(NOW, allow_synthetic=True)['status'] == 'MEMBERSHIP_CHANGED'
    finally:
        repo.close()


async def test_scope_exits_on_error_and_next_scope_sees_external_updates(tmp_path):
    repo, _, _, _, manager = context(tmp_path)
    try:
        await manager.refresh(NOW)
        with pytest.raises(RuntimeError):
            with manager.read_scope():
                initial = manager.store.status(NOW, allow_synthetic=True)
                repo.set_control(ATTEMPT, {'message': 'new refresh'})
                repo.set_control('manual_halt', True)
                # Trading controls are never part of the cap cache.
                assert repo.get_control('manual_halt') is True
                assert manager.store.status(NOW, allow_synthetic=True) == initial
                raise RuntimeError('abort')
        with manager.read_scope():
            assert manager.store.status(NOW, allow_synthetic=True)['refresh'] == {'message': 'new refresh'}
        rows = repo.get_control(HISTORY)
        rows[-1]['data_status'] = 'PARTIAL'
        repo.set_control(HISTORY, rows)
        assert manager.light_symbols(NOW) == []
    finally:
        repo.close()


async def test_adding_snapshot_inside_scope_invalidates_only_cap_history(tmp_path):
    repo, _, _, _, manager = context(tmp_path)
    try:
        await manager.refresh(NOW)
        with manager.read_scope():
            old = manager.store.at(NOW)
            newer = deepcopy(old)
            newer.update(id='replacement', known_at=(NOW+timedelta(seconds=1)).isoformat(), data_status='PARTIAL')
            manager.store.add(newer)
            assert manager.store.at(NOW)['id'] == old['id']
            assert manager.store.at(NOW+timedelta(seconds=1))['id'] == 'replacement'
    finally:
        repo.close()
