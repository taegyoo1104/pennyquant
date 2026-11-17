"""Quote checkpoints avoid rewriting history while retaining atomic recovery."""
from datetime import datetime, timedelta, timezone
from dataclasses import replace
import json

import pytest

from sessionquant.database.repository import Repository
from sessionquant.market.universe import UniverseStore
from sessionquant.models import Bar, Quote, jsonable
from sessionquant.session_engine import SessionEngine

NOW = datetime(2026, 11, 17, 15, 40, tzinfo=timezone.utc)


def setup_engine(tmp_path, count=10):
    repo = Repository(tmp_path/'checkpoint.db', backend='sqlite')
    universe = UniverseStore(tmp_path/'universe.json')
    universe.refresh(['AAPL'], source='SYNTHETIC_FIXTURE', observed_at=NOW-timedelta(days=30))
    engine = SessionEngine(repo, universe, synthetic=True)
    engine.market.bars['AAPL'] = [
        Bar('AAPL', NOW-timedelta(minutes=count-i), NOW-timedelta(minutes=count-i),
            100, 101, 99, 100, 1000) for i in range(count)]
    engine.dirty_symbols.add('AAPL')
    engine.dirty_bar_symbols.add('AAPL')
    engine.apply_batch([], NOW)
    return repo, universe, engine


def quote_event(now):
    return {'kind':'quote', 'data':jsonable(Quote('AAPL',now,now,100,100.01,1000,1000,False,False,'NASDAQ'))}


def test_quote_checkpoint_bytes_drop_and_restart_retains_full_state(tmp_path,monkeypatch):
    repo, universe, engine = setup_engine(tmp_path, count=7800)
    try:
        previous_bars = repo.get_control('sq_market_AAPL')
        writes = []
        original = repo.set_control
        def write(name,value):
            writes.append((name,len(json.dumps(jsonable(value)).encode())))
            original(name,value)
        monkeypatch.setattr(repo,'set_control',write)
        now = NOW+timedelta(seconds=1)
        engine.apply_batch([quote_event(now)], now)
        assert 'sq_market_AAPL' not in [name for name,_ in writes]
        new_bytes = sum(size for name,size in writes if name.startswith('sq_market_'))
        old_bytes = len(json.dumps({**previous_bars,**repo.get_control('sq_market_meta_AAPL')}).encode())
        assert new_bytes < old_bytes / 100
        assert repo.get_control('sq_market_AAPL') == previous_bars
        restored = SessionEngine(repo,universe,synthetic=True,restore=True)
        assert restored.market.to_state() == engine.market.to_state()
        assert restored.broker.to_state() == engine.broker.to_state()
        assert restored.last_decision_at == engine.last_decision_at
        print(f'quote market checkpoint bytes: {old_bytes} -> {new_bytes}')
    finally:
        repo.close()


def test_version2_migrates_without_rewriting_bars_and_missing_overlay_fails(tmp_path,monkeypatch):
    repo, universe, engine = setup_engine(tmp_path)
    try:
        old_chunk = {**repo.get_control('sq_market_AAPL'), **repo.get_control('sq_market_meta_AAPL')}
        repo.set_control('sq_market_AAPL',old_chunk)
        repo.set_control('sq_market_meta_AAPL',None)
        state=repo.load_state();state['schema_version']=2;repo.save_state(state)
        restored=SessionEngine(repo,universe,synthetic=True,restore=True)
        restored.tick(NOW+timedelta(seconds=1))
        assert repo.load_state()['schema_version']==3
        assert repo.get_control('sq_market_AAPL')==old_chunk
        assert SessionEngine(repo,universe,synthetic=True,restore=True).market.to_state()==restored.market.to_state()
        repo.set_control('sq_market_meta_AAPL',None)
        with pytest.raises(ValueError,match='메타데이터'):
            SessionEngine(repo,universe,synthetic=True,restore=True)
    finally:
        repo.close()


def test_corrected_bar_checkpoint_preserves_versions_and_receipts(tmp_path):
    repo, universe, engine=setup_engine(tmp_path)
    try:
        before=engine.market.bars['AAPL'][-1]
        now=NOW+timedelta(seconds=1)
        correction=replace(before,close=100.5,received_at=now)
        engine.apply_batch([{'kind':'bar','data':jsonable(correction)}],now)
        restored=SessionEngine(repo,universe,synthetic=True,restore=True)
        assert restored.market.bars['AAPL']==engine.market.bars['AAPL']
        assert before in restored.market.bars['AAPL'] and correction in restored.market.bars['AAPL']
    finally:
        repo.close()


def test_failed_manifest_write_rolls_back_split_checkpoint_and_poison_engine(tmp_path,monkeypatch):
    repo, universe, engine=setup_engine(tmp_path)
    try:
        saved=repo.load_state()
        before=engine.market.to_state()
        def fail(_):raise RuntimeError('simulated commit failure')
        monkeypatch.setattr(repo,'save_state',fail)
        now=NOW+timedelta(seconds=1)
        with pytest.raises(RuntimeError,match='simulated'):
            engine.apply_batch([quote_event(now)],now)
        assert repo.load_state()==saved
        assert SessionEngine(repo,universe,synthetic=True,restore=True).market.to_state()==before
        with pytest.raises(RuntimeError,match='재시작'):
            engine.tick(now+timedelta(seconds=1))
    finally:
        repo.close()


def test_ranked_candidates_saved_once_with_final_allocation_state(tmp_path,monkeypatch):
    from test_stage08_selection import session, batch_for, NOW as decision_at, VIEW
    repo,engine,market=session(tmp_path)
    try:
        writes=[]
        original=repo.record
        def record(kind,value):
            if kind=='candidates':writes.append(jsonable(value))
            return original(kind,value)
        monkeypatch.setattr(repo,'record',record)
        engine.apply_batch(batch_for(market),decision_at)
        frame=repo.get_control(VIEW)
        assert len(writes)==frame['evaluated_count']==4
        assert {r['id']:r['selection']['portfolio_status'] for r in writes}=={
            r['symbol']:r['portfolio_status'] for r in frame['rows']}
        assert len(engine.broker.get_open_orders())==frame['approved_new_count']==2
    finally:
        repo.close()
