"""Bounded, task-local timings. Never retain arguments, SQL, URLs or response bodies."""
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
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

    def snapshot(self):
        return {'elapsed_seconds':round(perf_counter()-self.started,6),
            'process_cpu_seconds':round(process_time()-self.cpu_started,6),
            'phases':[{'phase':name,'calls':row[0],'seconds':round(row[1],6),
                       'max_seconds':round(row[2],6)} for name,row in sorted(self.rows.items())]}


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
