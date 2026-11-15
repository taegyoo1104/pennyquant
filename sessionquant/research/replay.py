"""저장된 처리 시계와 당시 분석만 사용하는 격리 Paper 재생."""
from collections import Counter
from pathlib import Path

from ..market.universe import UniverseStore
from ..market_reference.risk import MacroReferenceRiskMonitor, filter_settings
from ..market_reference.store import VIEW
from ..models import utc, jsonable
from ..portfolio.models import PortfolioSettings, serialize
from ..portfolio.exit_policy import ExitPolicy
from ..session_engine import SessionEngine
from ..session_runtime import _open, bind_runtime
from .bundle import events, load_bundle
from .journal import identity

POLICIES = ('baseline','shadow','reference','fail_closed')


class RecordedNews:
    """누락된 분석 시각·모델·프롬프트를 소급해서 생성하지 않는다."""
    def __init__(self): self.contexts = {}; self.missing = 0; self.last_verified = {}

    def risk_context(self, now, symbol):
        row = self.contexts.get(symbol) or {}
        empty = {'status':'UNKNOWN','block_entries':False,'major_adverse':False,'blackout':False,'reasons':[]}
        context, evidence = row.get('context') or {}, row.get('evidence') or {}
        try:
            if utc(row['available_at']) > now: raise ValueError('미래 뉴스 상태')
            versions = evidence.get('analyses',[])
            for item in versions:
                if not item.get('model') or not item.get('prompt_version'):raise ValueError('분석 버전 미확인')
                received = utc(item['received_at']) if item.get('received_at') else None
                if received is None or not utc(item['published_at']) <= received <= utc(item['analyzed_at']) <= now:
                    raise ValueError('분석 시점 미확인')
            calendars = evidence.get('economic_events',[])
            if context.get('status') not in {'UNKNOWN','DISABLED'} and not versions and not calendars:
                raise ValueError('뉴스 결론의 당시 분석 근거 없음')
            if any(utc(item['known_at']) > now for item in calendars):raise ValueError('미래 경제 일정')
            if evidence.get('error_type'):raise ValueError('분석 근거 수집 실패')
            result = empty | context
            self.last_verified[symbol] = result
            return result
        except (KeyError, TypeError, ValueError, AttributeError):
            self.missing += 1
            prior = self.last_verified.get(symbol,{})
            return empty | {'block_entries':prior.get('block_entries') is True,'blackout':prior.get('blackout') is True}


class RecordedScope:
    def __init__(self): self.blockers = None;self.symbols=[];self.expiries={}

    def candidate_symbols(self,now):return self.symbols

    def candidate_expiry(self,symbol,now):return self.expiries.get(symbol)

    def entry_blockers(self,symbol,now):
        return self.blockers.get(symbol,['scope_not_observed']) if self.blockers is not None else []


def replay_recording(bundle, db_path, *, strategy=None, policy='baseline', use_news=True, use_market_filter=True,
                     allow_adds=None, resume=False, entry_start=None, entry_end=None, research_settings=None,candidate_settings=None):
    """매매 적용은 임시/신규 SQLite Paper에만 허용한다. 운영 설정을 활성화하지 않는다."""
    from dataclasses import replace
    bundle, db_path = Path(bundle), Path(db_path)
    manifest = load_bundle(bundle)
    if policy not in POLICIES:raise ValueError('지원하지 않는 기록 재생 정책입니다.')
    starts = manifest['recording_starts']
    if not starts or not starts[0]['data']['initial_flat']:
        raise ValueError('초기 포지션·주문이 없는 시점부터 기록한 묶음이 필요합니다.')
    first = starts[0]['data']
    from ..portfolio.selection import CandidateSettings
    candidate_options=CandidateSettings(**(candidate_settings if candidate_settings is not None else first.get('candidate_selection',{'enabled':False})))
    if len({identity(row['data']['settings']) for row in starts}) != 1:
        raise ValueError('비용·위험 설정이 바뀐 기록은 분리해서 평가해야 합니다.')
    settings = PortfolioSettings(**first['settings'])
    if allow_adds is not None:settings = replace(settings,allow_adds=allow_adds)
    research_settings = research_settings or {}
    # ON은 이 오프라인 경로에서만 허용된다. SessionConfig의 운영 활성화 제한은 유지한다.
    risk_options = {**research_settings,'enabled':policy in {'reference','fail_closed'},
        'paper_shadow_only':policy=='shadow','fail_closed':policy=='fail_closed'}
    risk_options = filter_settings(risk_options,research_only=True)
    selected = strategy or first['strategy']
    binding = identity({'policy_contract_version':'recorded_policy_v1','bundle':manifest['bundle_id'],'strategy':selected,'policy':policy,'use_news':use_news,
        'use_market_filter':use_market_filter,'settings':serialize(settings),'risk_options':risk_options,
        'entry_start':entry_start,'entry_end':entry_end})
    if candidate_options.enabled:binding=identity([binding,serialize(candidate_options)])
    stack, repo = _open(db_path,'sqlite')
    with stack:
        if repo.namespace_has_data() and not resume:raise ValueError('재생 장부가 이미 있습니다. 새 DB 또는 --resume을 지정하세요.')
        bind_runtime(repo,source='recorded-replay',identity=binding,synthetic=manifest['synthetic'])
        universe = UniverseStore(bundle/'universe.json')
        news, scope = RecordedNews(), RecordedScope()
        monitor = MacroReferenceRiskMonitor(repo,risk_options,research_only=True) if policy!='baseline' else None
        engine = SessionEngine(repo,universe,strategy=selected,settings=settings,restore=resume,
            synthetic=manifest['synthetic'],strict_market_data=first['strict_market_data'],
            use_news=use_news,use_market_filter=use_market_filter,risk_provider=news,watchlist_policy=scope,
            exit_policy=ExitPolicy(**first['exit_policy']),macro_reference_risk=monitor,research_replay=True,candidate_settings=candidate_options)
        news.last_verified = repo.get_control('recorded_news_verified_contexts',{})
        news.missing = repo.get_control('recorded_news_missing_count',0)
        cursor = repo.get_control('recording_cursor',0)
        seen, counts = set(), Counter(repo.get_control('recording_risk_counts',{}))
        decisions = Counter(repo.get_control('recording_decision_counts',{}))
        reference_state_counts = Counter(repo.get_control('recording_reference_state_counts',{}))
        frames = 0
        for index,row in enumerate(events(bundle/'events.jsonl'),1):
            if row['id'] in seen:continue
            seen.add(row['id'])
            if index <= cursor:continue
            stamp = utc(row['available_at']); data = row['data']
            if entry_end is not None and stamp >= utc(entry_end):break
            with repo.transaction():
                if row['kind'] == 'market_reference_view_v1':repo.set_control(VIEW,data)
                if row['kind'] == 'engine_batch':
                    if not universe.members(stamp):raise ValueError('처리 당시 확인 가능한 유니버스 이력이 없습니다.')
                    news.contexts = data.get('news_contexts') or {}
                    scope.blockers = data.get('scope_blockers')
                    scope.symbols=data.get('candidate_scope_symbols') or []
                    scope.expiries={row['symbol']:row.get('scope_valid_until') for row in (data.get('portfolio_selection') or {}).get('rows',[])}
                    engine.selection_source_id=(data.get('portfolio_selection') or {}).get('id')
                    in_window = (entry_start is None or stamp >= utc(entry_start)) and (entry_end is None or stamp < utc(entry_end))
                    repo.set_control('manual_halt',data.get('manual_halt') is True or not in_window)
                    control = data.get('control_state') or {}
                    for symbol,reason in data.get('operator_exits',{}).items():
                        position = engine.broker.positions.get(symbol)
                        if position:engine.broker.exit_intents.setdefault(symbol,{'reason':reason,
                            'opened_at':position.opened_at.isoformat(),'requested_at':stamp.isoformat(),'attempt':0})
                    if control.get('draining'):
                        for symbol,position in engine.broker.positions.items():
                            engine.broker.exit_intents.setdefault(symbol,{'reason':'operator_stop_all',
                                'opened_at':position.opened_at.isoformat(),'requested_at':stamp.isoformat(),'attempt':0})
                    engine.apply_batch(data['events'],stamp,cursor=index)
                    frames += 1
                    if in_window:
                        key = engine.reference_risk_status.get('status','NOT_EVALUATED')
                        counts[key] += 1
                        for candidate in engine._batch_decisions:
                            decisions['samples'] += 1
                            decisions['eligible'] += int(candidate['eligible'])
                            decisions['missing_or_delayed_reference'] += int(key=='UNKNOWN')
                            reference_view = repo.get_control(VIEW,{}) or {}
                            items = list((reference_view.get('items') or {}).values())
                            observation = 'NO_OBSERVATION' if not items or any(not item.get('event_at') for item in items) else 'DELAYED' if any(item.get('latency_class')=='DELAYED' for item in items) else 'VERIFIED' if all(item.get('quality_status')=='GOOD' and utc(item['valid_until'])>=stamp for item in items) else 'UNKNOWN'
                            reference_state_counts[observation+':samples'] += 1
                            reference_state_counts[observation+':eligible'] += int(candidate['eligible'])
                repo.set_control('recorded_news_verified_contexts',news.last_verified)
                repo.set_control('recorded_news_missing_count',news.missing)
                repo.set_control('recording_cursor',index)
                repo.set_control('recording_risk_counts',dict(counts))
                repo.set_control('recording_decision_counts',dict(decisions))
                repo.set_control('recording_reference_state_counts',dict(reference_state_counts))
        return {'state':engine.broker.to_state(),'context':repo.get_control('sessionquant_context'),
            'fills':jsonable(engine.broker.fills),'orders':jsonable(list(engine.broker.orders.values())),
            'curve':repo.all('account_snapshots'),'regimes':repo.all('market_regimes'),
            'risk_decisions':[r for r in repo.all('pipeline_status') if r.get('kind')=='risk_decision'],
            'reference_blocks':[r for r in repo.all('pipeline_status') if r.get('kind')=='reference_entry_block'],
            'entry_intents':repo.all('order_intents'),'risk_state_counts':dict(counts),'decision_counts':dict(decisions),
            'portfolio_selections':[row for row in repo.all('pipeline_status') if row.get('kind')=='portfolio_selection'],
            'candidate_settings':serialize(candidate_options),
            'reference_state_decisions':dict(reference_state_counts),'missing_news_contexts':news.missing,
            'bundle_id':manifest['bundle_id'],'policy':policy,'policy_contract_version':'recorded_policy_v1',
            'risk_settings':serialize(risk_options),'frames_this_execution':frames,'llm_api_calls':0,
            'live_order_writes':0,'source_contract':'decision_time_recording_v1'}
