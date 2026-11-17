"""Offline synthetic benchmark. Uses only a disposable SQLite DB; never loads .env."""
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from pathlib import Path
import argparse
import json
import tempfile
import time

from sessionquant.database.repository import Repository
from sessionquant.database.serialization import dumps
from sessionquant.market.bars import session_bounds
from sessionquant.market.history import HistoryStore, history_plan, SOURCE
from sessionquant.market.source import MarketBatch
from sessionquant.market.universe import UniverseStore
from sessionquant.models import Bar, Quote
from sessionquant.research.journal import start
from sessionquant.session_engine import SessionEngine
from sessionquant.session_runtime import batch_events


def benchmark(cycles=5):
    if type(cycles) is not int or not 2 <= cycles <= 20:
        raise ValueError('cycles must be between 2 and 20')
    now = datetime(2026,11,16,18,0,2,tzinfo=timezone.utc)
    def minutes(symbol,opened,count):
        return [Bar(symbol,opened+timedelta(minutes=i),now,100,101,99,100,100,
                    source=SOURCE,price_adjustment='raw') for i in range(1,count+1)]
    with tempfile.TemporaryDirectory(prefix='sq-synthetic-benchmark-') as directory:
        repo = Repository(Path(directory)/'benchmark.db',backend='sqlite')
        try:
            store = HistoryStore(repo)
            for day,opened,_ in history_plan(now):
                store.save('AAPL',day,minutes('AAPL',opened,390))
            universe = UniverseStore()
            universe.refresh(['AAPL'],source='SYNTHETIC_FIXTURE',observed_at=now-timedelta(days=1))
            engine = SessionEngine(repo,universe,strict_market_data=True,synthetic=True,record_inputs=True)
            engine.market.history_provider = lambda symbol,stamp:store.load(symbol,stamp,record=True)
            start(repo,engine,source='synthetic_benchmark')
            current = {s:minutes(s,session_bounds(now)[0],210) for s in ('SPY','QQQ','AAPL')}
            writes = 0
            def count_write(sql):
                nonlocal writes
                if sql.startswith('INSERT INTO market_history_sessions('):writes += 1
            repo.db.set_trace_callback(count_write)
            elapsed = []
            for i in range(cycles):
                stamp = now+timedelta(seconds=i)
                began = time.perf_counter()
                for rows in current.values():store.ingest(rows[-200:])
                batch = MarketBatch(bars=[b for rows in current.values() for b in rows] if i==0 else [],
                    quotes=[Quote(s,stamp,stamp,100,100.01,1000,1000,False,False,'NASDAQ') for s in current])
                engine.apply_batch(batch_events(batch),stamp)
                store.publish(['AAPL'],stamp,updated=['AAPL'])
                elapsed.append(time.perf_counter()-began)
            evidence = {'quality':engine.market.quality_rows,'candidates':repo.all('candidates'),
                        'rvol':store.report('AAPL',stamp),'orders':repo.all('orders')}
            return {'synthetic':True,'backend':'sqlite','provider_calls':0,'order_writes':len(evidence['orders']),
                'cycles':cycles,'historical_minutes':len(store.load('AAPL',stamp)),
                'history_writes_during_cycles':writes,
                'cold_cycle_seconds':round(elapsed[0],6),
                'steady_cycle_seconds':[round(t,6) for t in elapsed[1:]],
                'total_seconds':round(sum(elapsed),6),
                'result_sha256':sha256(dumps(evidence).encode()).hexdigest()}
        finally:
            repo.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cycles',type=int,default=5)
    parser.add_argument('--output',type=Path)
    args = parser.parse_args()
    result = benchmark(args.cycles)
    text = json.dumps(result,ensure_ascii=False,indent=2)
    if args.output:
        with args.output.open('x',encoding='utf-8') as stream:stream.write(text+'\n')
    print(text)


if __name__ == '__main__':main()
