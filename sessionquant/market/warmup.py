"""Startup catch-up under the collector's existing read budget; never certifies missing bars."""
import asyncio
from collections import deque
from datetime import datetime, timedelta, timezone
from time import monotonic

from ..models import utc
from ..watchlist.history_selection import operating_session
from .bars import session_bounds
from .history import history_plan, session_quality
from .history_inspections import HistoryInspections

CONTROL = 'startup_data_warmup_v1'


class StartupWarmup:
    def __init__(self, repo, store, selection, control_state, *, clock=None):
        self.repo, self.store, self.selection = repo, store, selection
        self.control = control_state
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.inspections = HistoryInspections(repo)
        self.selection.inspections = self.inspections
        self.session = None
        self.jobs = deque()
        self.symbols = ()
        self.report = {}
        self.reset()

    def reset(self):
        self.control['data_ready'] = False
        self.selection.startup_pending = True
        self.jobs.clear()
        self.current_planned = False
        self.history_failed = set()
        self.symbols = ()
        self.report = {'status':'WAITING_WATCHLIST','started_at':utc(self.clock()).isoformat(),
            'completed_tasks':0,'complete_tasks':0,'required_tasks':0,'received_pages':0,
            'reused_history_tasks':0,'history_total_tasks':0,
            'deferred_history_tasks':0,
            'issues':[], 'message':'감시 목록을 확인하고 있습니다.'}
        self.publish()

    def publish(self):
        self.inspections.flush(utc(self.clock()))
        self.report['updated_at'] = utc(self.clock()).isoformat()
        self.report['session'] = self.session
        self.repo.set_control(CONTROL, self.report)

    def _plan(self, symbols, now):
        self.symbols = tuple(symbols)
        _, opened, _ = operating_session(now)
        self.report.update(status='COLLECTING',symbols=list(symbols),cutoff_at=now.isoformat(),
                           phase='HISTORY',
                           message='과거 비교 자료와 당일 분봉을 준비하고 있습니다.')
        # Recent days first: closing the program early most often leaves yesterday incomplete.
        plan = history_plan(opened)
        self.inspections.refresh(symbols,plan)
        self.report['history_total_tasks'] = len(plan)*len(symbols)
        for date, start, end in reversed(plan):
            for symbol in symbols:
                if self.inspections.reusable(symbol,date,start,end,now) is not None:
                    self.report['reused_history_tasks'] += 1
                    continue
                deferred = self.inspections.deferred(symbol,date,start,end,now)
                if deferred is not None:
                    self.report['deferred_history_tasks'] += 1
                    self.history_failed.add(symbol)
                    self.report['issues'].append({'symbol':symbol,'session':date,'current_session':False,
                        'reason':deferred['reason'],**deferred['quality'],
                        'retry_deferred':True,'checked_at':deferred['checked_at'],
                        'retry_after':deferred['retry_after']})
                    continue
                self.jobs.append({'symbol':symbol,'day':date,'opened':start,'end':end,
                                  'cursor':end,'pages':0,'current':False})
        self.report['required_tasks'] = len(self.jobs)

    def _current_plan(self, priority, now):
        self.current_planned = True
        self.report.update(phase='CURRENT_SESSION',cutoff_at=now.isoformat())
        day, _, _ = operating_session(now)
        bounds = session_bounds(now)
        if bounds and bounds[0] < now < bounds[1]:
            end = now.replace(second=0,microsecond=0)
            if end > bounds[0]:
                current = [{'symbol':s,'day':day,'opened':bounds[0],'end':end,
                            'cursor':end,'pages':0,'current':True}
                           for s in dict.fromkeys([*priority,'SPY','QQQ',
                               *(s for s in self.symbols if s not in self.history_failed)])]
                self.jobs.extend(current)
                self.report['required_tasks'] += len(current)

    def _visible(self, job, now):
        _, bars = self.store.get(job['symbol'],job['day'])
        return [b for b in bars if b.received_at <= now and job['opened'] < b.timestamp <= job['end']]

    def _missing(self, job, now):
        expected = {job['opened']+timedelta(minutes=i)
                    for i in range(1,int((job['end']-job['opened']).total_seconds()//60)+1)}
        return expected-{b.timestamp for b in self._visible(job,now)}

    def _quality(self, job, now, known):
        if not job['current']:
            quality,_ = self.inspections.inspect(self.store,job['symbol'],job['day'],job['opened'],job['end'],now)
            return quality
        visible = self._visible(job,now)
        quality = session_quality(visible,job['opened'],job['end'])
        if job['current']:
            # DB completeness alone does not prove the running engine received the bars.
            expected = {job['opened']+timedelta(minutes=i) for i in range(1,quality['expected_minutes']+1)}
            quality['engine_minutes_missing'] = len(expected-set(known(job['symbol'])))
            quality['complete'] &= quality['engine_minutes_missing'] == 0
        return quality

    def _finish_job(self, job, quality, reason=None, *, defer_retry=False):
        self.jobs.popleft()
        self.report['completed_tasks'] += 1
        if quality.get('complete'):
            self.report['complete_tasks'] += 1
        else:
            if not job['current']:
                self.history_failed.add(job['symbol'])
                if defer_retry:
                    self.inspections.defer_retry(job['symbol'],job['day'])
            self.report['issues'].append({'symbol':job['symbol'],'session':job['day'],
                'current_session':job['current'],'reason':reason or 'history_minutes_missing_or_conflicting',
                **quality})

    async def advance(self, manager, fetch, accept, known, *, priority=(), max_pages=4, hydrate=None):
        now = utc(self.clock())
        session = operating_session(now)[0]
        if self.session != session:
            self.session = session
            self.reset()
        if self.control.get('data_ready'):
            return
        status = manager.store.status(now,allow_synthetic=manager.allow_synthetic)
        if status['status'] != 'VALID':
            self.report.update(status='WAITING_WATCHLIST',message='사용 가능한 감시 목록을 기다리고 있습니다.')
            self.publish()
            return
        symbols = manager.base_symbols(now)
        if not symbols:
            self.report.update(status='WAITING_WATCHLIST',message='감시 후보가 없어 자료 준비를 기다립니다.')
            self.publish()
            return
        if self.symbols and self.symbols != tuple(symbols):
            self.reset()
        if not self.symbols:
            self._plan(symbols,now)
        began, pages = monotonic(), 0
        while self.jobs and pages < max_pages and monotonic()-began < 3:
            await asyncio.sleep(0)  # Keep the engine's risk/exit clock alive between DB checks.
            job = self.jobs[0]
            self.report.update(current_symbol=job['symbol'],current_session=job['day'])
            try:
                if job['current'] and not job.get('hydrated'):
                    # Loading DB observations is not a new provider receipt. The source
                    # records their availability in this run separately from received_at.
                    (hydrate or accept)(job['symbol'],self._visible(job,utc(self.clock())))
                    job['hydrated'] = True
                quality = self._quality(job,utc(self.clock()),known)
                if quality['complete']:
                    self._finish_job(job,quality)
                    continue
                missing = self._missing(job,utc(self.clock()))
                pending = [stamp for stamp in missing if stamp <= job['cursor']]
                if not pending:
                    # Already queried holes and conflicting versions cannot be repaired
                    # by repeatedly downloading the same pages. Keep the quality failure.
                    self._finish_job(job,quality,defer_retry=True)
                    continue
                job['cursor'] = max(pending)
                response = await fetch(job['symbol'],job['cursor'])
                if response is None:
                    self.report.update(status='RETRY_WAIT',message='조회 제한·오류 또는 이번 회차 예산으로 다음 조회를 기다립니다.')
                    break
                fetched, following = response
                pages += 1
                self.report['received_pages'] += 1
                job['pages'] += 1
                if any(b.timestamp > job['cursor'] or b.received_at > utc(self.clock()) for b in fetched):
                    raise ValueError('future_page')
                selected = [b for b in fetched if job['opened'] < b.timestamp <= job['end']]
                self.store.save(job['symbol'],job['day'],selected,
                    progress={'status':'WARMUP','cursor':following,'pages':job['pages']})
                if job['current']:
                    accept(job['symbol'],selected)
                quality = self._quality(job,utc(self.clock()),known)
                stalled = bool(following and utc(following) >= job['cursor'])
                reached = bool(fetched and min(b.timestamp for b in fetched) <= job['opened'])
                if quality['complete'] or not fetched or not following or stalled or reached or job['pages'] >= 10:
                    exhausted = not fetched or not following or reached
                    self._finish_job(job,quality,'cursor_stalled' if stalled else None,
                                     defer_retry=exhausted and not stalled)
                else:
                    job['cursor'] = utc(following)
                self.report.update(status='COLLECTING',message='누락된 분봉을 보충하고 있습니다.')
            except (ValueError,TypeError,KeyError,AttributeError):
                self._finish_job(job,{},'history_record_invalid')
        if not self.jobs and not self.current_planned:
            self._current_plan(priority,utc(self.clock()))
        if not self.jobs:
            if operating_session(utc(self.clock()))[0] != self.session:
                self.session = operating_session(utc(self.clock()))[0]
                self.reset()
                return
            # A fresh snapshot is selected only after startup repair, never before it.
            self.selection.startup_pending = False
            self.selection.frame = {}
            frame = self.selection.ensure(list(self.symbols),utc(self.clock()))
            self.control['data_ready'] = True
            self.report.update(status='PARTIAL' if self.report['issues'] or frame['excluded_count'] else 'READY',
                finished_at=utc(self.clock()).isoformat(),history_ready_symbols=frame['included_symbols'],
                history_ready_count=frame['included_count'],history_excluded_count=frame['excluded_count'],
                message='자료 준비 검사가 끝났습니다. 최신 시세와 전략 조건은 별도로 확인합니다.')
        self.publish()
