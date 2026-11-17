"""Bounded, task-local timings. Never retain arguments, SQL, URLs or response bodies."""
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
import re
from time import perf_counter, process_time

ACTIVE = ContextVar('sessionquant_performance', default=None)
PHASES = frozenset({'api_pacing','api_http','api_retry_wait','db_query','db_commit',
    'json_encode','engine_batch','engine_persist','history_save','history_load',
    'indicators','rvol','bar_quality','warmup','watchlist_publish'})


class Timings:
    def __init__(self):
        self.started = perf_counter()
        self.cpu_started = process_time()
        self.rows = {}
        self.database = {}

    def snapshot(self):
        return {'elapsed_seconds':round(perf_counter()-self.started,6),
            'process_cpu_seconds':round(process_time()-self.cpu_started,6),
            'phases':[{'phase':name,'calls':row[0],'seconds':round(row[1],6),
                       'max_seconds':round(row[2],6)} for name,row in sorted(self.rows.items())],
            'database':[{'operation':key[0],'table':key[1],'calls':row[0],
                         'seconds':round(row[1],6),'max_seconds':round(row[2],6)}
                        for key,row in sorted(self.database.items())]}


@contextmanager
def capture():
    timings = Timings()
    token = ACTIVE.set(timings)
    try:
        yield timings
    finally:
        ACTIVE.reset(token)


@contextmanager
def measure(name):
    active = ACTIVE.get()
    if active is None:
        yield
        return
    if name not in PHASES:
        raise ValueError('Unknown timing phase')
    began = perf_counter()
    try:
        yield
    finally:
        elapsed = perf_counter()-began
        row = active.rows.setdefault(name,[0,0.0,0.0])
        row[0] += 1
        row[1] += elapsed
        row[2] = max(row[2],elapsed)


@contextmanager
def database_query(sql, kinds):
    """Retain only allowlisted operation/table categories, never SQL or values."""
    active = ACTIVE.get()
    if active is None:
        yield
        return
    operation = sql.lstrip().split(None,1)[0].upper() if sql.strip() else 'OTHER'
    if operation not in {'SELECT','INSERT','UPDATE','DELETE','CREATE','ALTER','SET','START'}:
        operation = 'OTHER'
    allowed = {'pq_'+kind for kind in kinds} | {
        'pq_controls','pq_engine_state','sq_financial_ledger','sq_schema_versions'}
    match = re.search(r'\b(?:FROM|INTO|UPDATE|TABLE(?:\s+IF\s+NOT\s+EXISTS)?)\s+`?([a-zA-Z0-9_]+)',sql,re.I)
    table = match.group(1).lower() if match and match.group(1).lower() in allowed else 'other'
    key = (operation,table)
    if key not in active.database and len(active.database) >= 64:
        key = ('OTHER','other')
    began = perf_counter()
    try:
        yield
    finally:
        elapsed = perf_counter()-began
        row = active.database.setdefault(key,[0,0.0,0.0])
        row[0] += 1
        row[1] += elapsed
        row[2] = max(row[2],elapsed)


def timed(name):
    def decorate(function):
        @wraps(function)
        def wrapped(*args, **kwargs):
            if ACTIVE.get() is None:
                return function(*args, **kwargs)
            with measure(name):
                return function(*args, **kwargs)
        return wrapped
    return decorate
