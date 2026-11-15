"""실데이터 Paper 준비·뉴스 진단·읽기 전용 운영 보고. 실제 주문 경로는 없다."""
from collections import Counter
from contextlib import ExitStack, redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
import json
import os
import sys

from .database.repository import Repository
from .market.paper_policy import OBSERVED
from .models import jsonable, symbol_valid, utc
from .session_runtime import _open


def _environment(config):
    if config.environment != 'PAPER_LOCAL' or config.source != 'toss':
        raise ValueError('토스 실데이터 PAPER_LOCAL 설정이 필요합니다.')
    os.environ['SQ_ENVIRONMENT'] = config.environment
    os.environ['SQ_DB_BACKEND'] = config.database_backend


def _identity(config):
    if config.database_backend == 'mysql':
        from .database.config import MySQLConfig, namespace_for
        db = MySQLConfig.from_env()
        return ('mysql', db.host, db.port, db.database,
                namespace_for(config.db_path, db.namespace, 'PAPER_LOCAL'))
    return ('sqlite', str(Path(config.db_path).resolve()))


def prepare_paper(source, target):
    """비어 있는 별도 원장에 수집 자료만 복사한다. 원본 관측 시각을 유지한다."""
    from .market.history import KIND
    from .watchlist.capitalization import HISTORY, ATTEMPT
    from .watchlist.toss_capitalization import STATE
    _environment(source); _environment(target)
    if target.paper_data_policy != OBSERVED:
        raise ValueError('대상은 observed_paper_v1 설정이어야 합니다.')
    if _identity(source) == _identity(target):
        raise ValueError('기존 원장과 다른 DB/네임스페이스가 필요합니다. SQ_DB_NAMESPACE도 확인하세요.')
    if Path(source.universe_path).resolve() != Path(target.universe_path).resolve():
        raise ValueError('원본과 대상은 같은 시점 보존 universe_path를 사용해야 합니다.')
    with ExitStack() as stack:
        source_stack, src = _open(source.db_path, source.database_backend)
        stack.enter_context(source_stack)
        target_stack, dst = _open(target.db_path, target.database_backend)
        stack.enter_context(target_stack)
        if dst.namespace_has_data():
            raise ValueError('대상 원장이 이미 있습니다. 준비를 반복하지 말고 상태 조회/실행하거나 새 DB를 지정하세요.')
        count = 0
        with dst.transaction():
            for _, row in src.iter_records(KIND, batch_size=20):
                dst.record(KIND, row)
                count += 1
            if not count:
                raise ValueError('원본에 저장된 과거 분봉이 없습니다. 기존 설정으로 history-backfill을 먼저 실행하세요.')
            for key in (HISTORY, ATTEMPT, STATE):
                value = src.get_control(key)
                if value is not None:dst.set_control(key, value)
            result = {'status':'PREPARED', 'prepared_at':datetime.now(timezone.utc).isoformat(),
                'paper_data_policy':OBSERVED, 'copied_symbol_days':count,
                'source_db':str(source.db_path), 'target_db':str(target.db_path),
                'source_namespace':getattr(src,'namespace',None),
                'target_namespace':getattr(dst,'namespace',None),
                'copied_trades':0, 'order_writes':0, 'llm_generation_calls':0}
            dst.set_control('paper_preparation_v1', result)
        return result


def paper_report(repo, config, now=None):
    """전체 원장 누계와 현재 관측을 구분해 짧게 보고한다."""
    from .market.quality import QUALITY_KEY
    from .market.reporting import current_report
    from .watchlist.selection import VIEW
    from .watchlist.history_selection import CONTROL
    from .trading_controls import KEY
    now = utc(now or datetime.now(timezone.utc))
    quality = current_report(repo.get_control(QUALITY_KEY, {}), now)
    control = repo.get_control(KEY, {})
    context = repo.get_control('sessionquant_context', {})
    watchlist = repo.get_control(VIEW, {})
    history = repo.get_control(CONTROL, {})
    fresh = bool(control.get('heartbeat') and 0 <= (now-utc(control['heartbeat'])).total_seconds() <= 60)
    calls = Counter(); call_attempts = 0
    for _, row in repo.iter_records('llm_calls'):
        calls[row.get('status','UNKNOWN')] += 1
        call_attempts += bool(row.get('network_attempted'))
    fills = Counter(row.get('side','unknown') for _,row in repo.iter_records('executions'))
    candidates = repo.all('candidates')
    items = quality['items']
    return {'observed_at':now.isoformat(), 'read_only':True, 'live_orders_enabled':False,
        'source':config.source, 'configured_policy':config.paper_data_policy,
        'recorded_policy':context.get('paper_data_policy'),
        'engine':{'status':control.get('status','NOT_STARTED'), 'heartbeat_fresh':fresh,
                  'heartbeat':control.get('heartbeat'), 'run_id':control.get('run_id'),
                  'data_ready':control.get('data_ready')},
        'warmup':repo.get_control('startup_data_warmup_v1',{}),
        'watchlist':{'status':watchlist.get('status','UNKNOWN'), 'base_count':history.get('base_count'),
            'operating_count':history.get('included_count'), 'excluded_history_count':history.get('excluded_count'),
            'selection_session':history.get('session'), 'checked_at':history.get('checked_at')},
        'market':{'captured_at':quality.get('captured_at'), 'evaluated_symbols':len(items),
            'quote_fresh_count':sum(bool(r['quote_fresh']) for r in items),
            'paper_eligible_count':sum(bool((r.get('paper_eligibility') or {}).get('allowed')) for r in items),
            'strategy_rvol_count':sum(r.get('strategy_rvol') is not None for r in items),
            'actual_evidence_coverage':quality['required_data_coverage'], 'blocker_counts':quality['blocker_counts']},
        'latest_candidates':[{'symbol':r.get('symbol'),'timestamp':r.get('timestamp'),
            'eligible_at_evaluation':r.get('eligible'), 'blockers_at_evaluation':r.get('blockers'),
            'has_signal':bool(r.get('signal')),
            'rvol_at_evaluation':r.get('indicators',{}).get('relative_volume')} for r in candidates],
        'namespace_totals':{'paper_fills_by_side':dict(fills), 'llm_network_attempts':call_attempts,
                            'llm_records_by_status':dict(calls)},
        'news':repo.get_control('session_news_status',{}),
        'llm_health':{k:v for k,v in repo.get_control('llm_provider_health',{}).items() if k!='credential_fingerprint'},
        'last_news_probe':repo.get_control('paper_news_probe_v1'),
        'order_writes':0, 'llm_generation_calls':0}


async def probe_news(repo, config, symbol, *, analyzer=None, company_service=None, clock=None):
    """기업 한 곳의 실제 수집 경로를 실행한다. 생성 시도는 실패 포함 최대 1회."""
    from .macro.analysis import BudgetedAnalyzer
    from .macro.models import LLMSettings
    from .macro.service import NewsPipeline
    symbol_valid(symbol)
    clock = clock or (lambda:datetime.now(timezone.utc))
    analyzer = analyzer or BudgetedAnalyzer(repo, LLMSettings.from_dict(config.llm),
        clock=clock, generation_call_limit=1, audit_settings=config.llm_debug_audit)
    if analyzer.generation_call_limit != 1:
        raise ValueError('뉴스 진단은 생성 시도 1회 제한이 필요합니다.')
    pipeline = NewsPipeline(repo, companies=config.companies, analyzer=analyzer,
        company_service=company_service, include_alpaca=config.include_alpaca, clock=clock,
        company_max_age_hours=config.company_new_analysis_max_age_hours)
    try:
        if not analyzer.api_key:
            result = {'status':'NO_OPENAI_KEY','symbol':symbol,'checked_at':utc(clock()).isoformat(),
                      'llm_generation_calls':0,'order_writes':0,'intraday_trading_verified':False}
        else:
            results = await pipeline.refresh_company([symbol], clock())
            context = pipeline.risk_context(clock(),symbol)
            signal = context['candidate_score_signal']
            calls = [r for r in repo.recent('llm_calls',1000) if r.get('run_id')==analyzer.run_id]
            valid = [r for r in results if not r.error and r.adopted_llm_call_id]
            status = ('VALID_RESPONSE_AND_STRATEGY_CONTEXT' if valid and signal['status']=='VERIFIED'
                      else 'VALID_RESPONSE_REVIEW_CONTEXT' if valid
                      else 'LLM_ATTEMPT_FAILED_OR_INVALID' if analyzer.generation_calls
                      else 'NO_ELIGIBLE_NEWS_OR_BUDGET_BLOCKED')
            result = {'status':status,'symbol':symbol,'checked_at':utc(clock()).isoformat(),
                'collected_events':len(pipeline.company.events), 'analyses':len(results),
                'valid_responses':len(valid), 'llm_generation_calls':analyzer.generation_calls,
                'issues':pipeline.company.status,'strategy_context':context,
                'call_status_counts':dict(Counter(r.get('status','UNKNOWN') for r in calls)),
                'analysis_errors':dict(Counter(r.error or 'VALID' for r in results)),
                'order_writes':0, 'intraday_trading_verified':False}
        repo.set_control('paper_news_probe_v1',jsonable(result))
        return jsonable(result)
    finally:
        await pipeline.aclose()
        await analyzer.aclose()


async def command(config, operation, *, source=None, symbol='AAPL', output=None):
    _environment(config)
    if output is not None and Path(output).exists():
        raise ValueError('출력 파일이 이미 있습니다. 새 이름을 지정하세요.')
    if operation == 'prepare-paper':
        result = prepare_paper(source,config)
    elif operation == 'paper-news-probe':
        stack, repo = _open(config.db_path,config.database_backend)
        with stack, redirect_stdout(sys.stderr):result = await probe_news(repo,config,symbol)
    else:
        repo = Repository(config.db_path,backend=config.database_backend)
        try:
            if operation=='news-status':
                from .news.diagnostics import read_news_status
                result=read_news_status(repo)
            else:result = paper_report(repo,config)
        finally:repo.close()
    if output is not None:
        with Path(output).open('x',encoding='utf-8') as stream:
            json.dump(result,stream,ensure_ascii=False,indent=2)
        return {'status':'SAVED','output':str(output),'order_writes':0,
                'llm_generation_calls':result.get('llm_generation_calls',0),
                **({'message':result['message'],'counts':result['counts']} if operation=='news-status' else {})}
    return result
