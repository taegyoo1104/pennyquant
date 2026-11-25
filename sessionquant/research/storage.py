"""Bounded diagnostic copies only; financial/recovery records never use this budget."""
from dataclasses import asdict, dataclass
from datetime import date
import json
from zoneinfo import ZoneInfo

from ..models import utc
from ..portfolio.models import serialize

BUDGET = 'auxiliary_storage_budget_v1'
NY = ZoneInfo('America/New_York')


@dataclass(frozen=True)
class StoragePolicy:
    mode: str = 'full'
    sample_seconds: int = 60
    daily_bytes: int = 128 * 1024 * 1024
    daily_records: int = 5000
    max_record_bytes: int = 512 * 1024

    def __post_init__(self):
        if self.mode not in {'full', 'bounded_diagnostics'}:
            raise ValueError('보조 기록 방식은 full 또는 bounded_diagnostics입니다.')
        for name, low, high in (('sample_seconds',1,3600), ('daily_bytes',1024,1024**3),
                               ('daily_records',1,100000), ('max_record_bytes',512,4*1024**2)):
            value = getattr(self,name)
            if type(value) is not int or not low <= value <= high:
                raise ValueError(f'보조 기록 {name}: {low}~{high} 정수를 사용하세요.')

    @property
    def bounded(self):
        return self.mode == 'bounded_diagnostics'


def day_at(now):
    return utc(now).astimezone(NY).date().isoformat()


def budget(repo, now):
    day = day_at(now)
    saved = repo.get_control(BUDGET, None)
    if saved is None:
        return {'version':1,'day':day,'bytes':0,'records':0,'last_capture':{},'limited':False}
    invalid = not isinstance(saved,dict)
    if not invalid:
        try:
            date.fromisoformat(saved.get('day',''))
            for stamp in saved.get('last_capture',{}).values(): utc(stamp)
        except (ValueError,TypeError,AttributeError):
            invalid = True
    if (invalid or saved.get('version') != 1 or not isinstance(saved.get('day'),str)
            or any(type(saved.get(k)) is not int or saved[k] < 0 for k in ('bytes','records'))
            or not isinstance(saved.get('last_capture'),dict)):
        return {'version':1,'day':day,'bytes':0,'records':0,'last_capture':{},
                'limited':True,'last_skip_reason':'invalid_budget'}
    if saved['day'] > day:
        return {**saved,'limited':True,'last_skip_reason':'clock_rollback'}
    if saved['day'] < day:
        return {'version':1,'day':day,'bytes':0,'records':0,'last_capture':{},'limited':False}
    return dict(saved)


def remember(repo, state):
    repo._auxiliary_storage_gate = {**state, 'last_capture':dict(state['last_capture'])}


def due(repo, policy, stream, now, *, urgent=False):
    if not policy.bounded:
        return True
    cached = getattr(repo, '_auxiliary_storage_gate', None)
    state = cached if cached and cached.get('day') == day_at(now) else budget(repo,now)
    remember(repo,state)
    if state['bytes'] >= policy.daily_bytes or state['records'] >= policy.daily_records or state.get('limited'):
        return False
    stamp = state['last_capture'].get(stream)
    # Cache is only a conservative fast path. Actual admission rereads the
    # transactional budget, so restarts cannot grant a fresh daily quota.
    return urgent or not stamp or (utc(now)-utc(stamp)).total_seconds() >= policy.sample_seconds


def store(repo, policy, table, row, stream, now, *, urgent=False):
    if not policy.bounded:
        repo.record(table,row)
        return True
    encoded = json.dumps(serialize(row), ensure_ascii=True, allow_nan=False).encode('utf-8')
    with repo.transaction():
        state = budget(repo,now)
        stamp = state['last_capture'].get(stream)
        if not urgent and stamp and (utc(now)-utc(stamp)).total_seconds() < policy.sample_seconds:
            remember(repo,state)
            return False
        if state.get('limited') or state['bytes'] >= policy.daily_bytes or state['records'] >= policy.daily_records:
            remember(repo,state)
            return False
        reason = ('record_size' if len(encoded)>policy.max_record_bytes else
                  'daily_bytes' if state['bytes']+len(encoded)>policy.daily_bytes else None)
        state['last_capture'] = {**state['last_capture'], stream:utc(now).isoformat()}
        state.update(policy=asdict(policy), updated_at=utc(now).isoformat())
        if reason:
            state.update(last_skip_reason=reason, limited=reason=='daily_bytes')
        else:
            repo.record(table,row)
            state['bytes'] += len(encoded)
            state['records'] += 1
            state['limited'] = state['bytes'] >= policy.daily_bytes or state['records'] >= policy.daily_records
            state['last_skip_reason'] = 'daily_limit' if state['limited'] else None
        repo.set_control(BUDGET,state)
        remember(repo,state)
    return reason is None


def archive_selection(engine, frame, now):
    policy = engine.auxiliary_storage
    # An order references this exact ID. Never sample or quota-limit its audit.
    if not policy.bounded or frame.get('approved_count',0)>0 or any(row.get('order_intent_id') for row in frame.get('rows',[])):
        engine.repo.record('pipeline_status',frame)
    elif due(engine.repo,policy,'selection',now):
        store(engine.repo,policy,'pipeline_status',frame,'selection',now)
