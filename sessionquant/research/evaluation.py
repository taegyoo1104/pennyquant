"""고정 정책의 독립 비교와 시간순 개발/검증/최종 홀드아웃 평가."""
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory

from ..portfolio.models import PortfolioSettings, decimal
from ..session_backtest import summarize
from .bundle import load_bundle, events, write_json
from .journal import identity
from .quality import quality_report, NY
from .replay import replay_recording, POLICIES
from .proxies import proxy_report
from .costs import summarize_llm_costs


def split_plan(dates, *, train_days=20, validation_days=5, holdout_days=5):
    if any(type(v) is not int or v<1 for v in (train_days,validation_days,holdout_days)):
        raise ValueError('학습·검증·홀드아웃 거래일 수는 양의 정수여야 합니다.')
    dates=sorted(set(dates)); needed=train_days+validation_days+holdout_days
    if len(dates)<needed:return {'status':'INSUFFICIENT_SESSIONS','required_days':needed,'observed_days':len(dates),'folds':[],'holdout':[]}
    development,holdout=dates[:-holdout_days],dates[-holdout_days:]
    folds=[{'training':development[:end-validation_days], 'validation':development[end-validation_days:end]}
           for end in range(train_days+validation_days,len(development)+1,validation_days)]
    return {'status':'PLANNED','folds':folds,'holdout':holdout,'threshold_selection':'NONE_FIXED_HYPOTHESES',
        'holdout_policy':'최종 구간을 임계값 선택에 사용하지 않습니다. 이 보고서를 보고 재조정하면 새 미래 홀드아웃이 필요합니다.'}


def _bounds(dates):
    start=datetime.fromisoformat(dates[0]).replace(tzinfo=NY)
    end=datetime.fromisoformat(dates[-1]).replace(tzinfo=NY)+timedelta(days=1)
    return start.isoformat(),end.isoformat()


def _conditional_blocks(baseline, filtered):
    """같은 기준선 거래에서 이익이 난 진입을 차단했는지 기술한다. 인과 효과가 아니다."""
    lookup={row['client_id']:row.get('signal_id') for row in baseline['entry_intents']}
    positions,completed={},{}
    for fill in baseline['fills']:
        symbol=fill['symbol'];qty=Decimal(fill['qty'])
        if fill['side']=='buy':
            item=positions.setdefault(symbol,{'qty':Decimal(0),'pnl':Decimal(0),'signals':set()})
            item['qty']+=qty
            if lookup.get(fill['client_id']):item['signals'].add(lookup[fill['client_id']])
        elif symbol in positions:
            item=positions[symbol];item['qty']-=qty;item['pnl']+=Decimal(fill['realized_pnl'])
            if item['qty']==0:
                for signal in item['signals']:completed[signal]=item['pnl']
                del positions[symbol]
    blocked={row['signal_id'] for row in filtered['reference_blocks']}
    matched=blocked & set(completed)
    winners=sum(completed[signal]>0 for signal in matched)
    return {'blocked_signals':len(blocked),'baseline_completed_matches':len(matched),
        'blocked_profitable_baseline_entries':winners,'conditional_false_positive_rate':winners/len(matched) if matched else None,
        'basis':'동일 신호의 완료된 기준선 거래에 한정한 기술 통계. 실제 필터의 인과 효과·미체결 기회 수익은 UNKNOWN.'}


def evaluate_recording(bundle, report, *, strategy='all',train_days=20,validation_days=5,holdout_days=5,
                       min_trades=30, api_cost_usd=None, infrastructure_cost_usd=None):
    bundle,report=Path(bundle),Path(report)
    if report.suffix!='.json' or report.exists() or report.with_suffix('.md').exists():
        raise ValueError('기존 파일과 다른 새 .json 보고서 경로가 필요합니다.')
    if type(min_trades) is not int or min_trades<1:raise ValueError('최소 거래 수는 양의 정수여야 합니다.')
    manifest=load_bundle(bundle)
    quality=quality_report(bundle/'events.jsonl')
    plan=split_plan(quality['trade_dates'],train_days=train_days,validation_days=validation_days,holdout_days=holdout_days)
    strategies=['momentum','pullback','gap_breakout','vwap'] if strategy=='all' else [strategy]
    if any(item not in {'momentum','pullback','gap_breakout','vwap'} for item in strategies):raise ValueError('전략 이름을 확인하세요.')
    usage=[]
    for row in events(bundle/'events.jsonl'):
        if row['kind']=='llm_usage':usage.append(row['data'])
    llm_costs=summarize_llm_costs(usage)
    api=decimal(api_cost_usd,nonnegative=True) if api_cost_usd is not None else None
    infra=decimal(infrastructure_cost_usd,nonnegative=True) if infrastructure_cost_usd is not None else None
    variants=[(name+':'+policy,name,policy,True,True,None) for name in strategies for policy in POLICIES]
    primary='pullback' if 'pullback' in strategies else strategies[0]
    variants += [(primary+':numeric_only',primary,'reference',False,True,None),
                 (primary+':neither_news_nor_reference',primary,'baseline',False,True,None),
                 (primary+':no_add',primary,'baseline',True,True,False),
                 (primary+':technical_only',primary,'baseline',False,False,None)]
    periods={'diagnostic_all':None}
    for index,fold in enumerate(plan['folds']):periods['validation_'+str(index+1)]=fold['validation']
    if plan['holdout']:periods['final_holdout']=plan['holdout']
    results={}
    with TemporaryDirectory(prefix='sessionquant-study-') as directory:
        for period,dates in periods.items():
            raw_results={};rows={}
            for label,active,policy,news,market,adds in variants:
                start,end=_bounds(dates) if dates else (None,None)
                result=replay_recording(bundle,Path(directory)/(period+'-'+label.replace(':','-')+'.db'),
                    strategy=active,policy=policy,use_news=news,use_market_filter=market,allow_adds=adds,entry_start=start,entry_end=end)
                metrics=summarize(result,PortfolioSettings(**manifest['recording_starts'][0]['data']['settings']))
                raw_results[label]=result
                issues=list(quality['blocking_reasons'])
                if quality['synthetic']:issues.append('synthetic_data')
                if metrics['trades']<min_trades:issues.append('insufficient_trades')
                if period!='final_holdout':issues.append('not_final_holdout')
                if result['missing_news_contexts'] and news:issues.append('news_context_unverified')
                issues.append('actual_broker_costs_and_execution_unverified')
                rows[label]={'strategy':active,'policy':policy,'news_enabled':news,
                    'policy_contract_version':result['policy_contract_version'],'risk_settings':result['risk_settings'],
                    'data_validation':quality['data_validation'],'evaluation_status':'UNVERIFIED_DATA' if quality['blocking_reasons'] else 'INSUFFICIENT_EVIDENCE',
                    'performance':None,'diagnostic_simulation':metrics,'blocking_reasons':list(dict.fromkeys(issues)),
                    'risk_state_counts':result['risk_state_counts'],'reference_availability_decisions':result['reference_state_decisions'],
                    'decision_counts':result['decision_counts'],'missing_news_contexts':result['missing_news_contexts']}
            for label,row in rows.items():
                if row['policy'] in {'reference','fail_closed'} and row['news_enabled']:
                    row['entry_block_comparison']=_conditional_blocks(raw_results[row['strategy']+':baseline'],raw_results[label])
            results[period]=rows
    payload={'report_version':1,'bundle_id':manifest['bundle_id'],'evaluation_id':identity({'bundle':manifest['bundle_id'],
        'plan':plan,'variants':variants,'min_trades':min_trades,
        'policy_versions':{label:row['risk_settings'] for label,row in results['diagnostic_all'].items()},
        'api_cost_usd':api,'infrastructure_cost_usd':infra}),'quality':quality,'split_plan':plan,'results':results,
        'proxy_analysis':proxy_report(bundle/'events.jsonl'),'costs':{**llm_costs,
        'api_actual_usd':api,'infrastructure_actual_usd':infra,'included_in_strategy_pnl':False,
        'allocation':'기간·정책 사이에 실비를 자동 배분하지 않습니다. 누락 비용은 0이 아니라 UNKNOWN입니다.'},
        'recommended_parameters':None,'actual_out_of_sample_validated':False,'operating_filter_activated':False,
        'live_order_writes':0,'llm_generation_calls':0,'limitations':[
            '실제 시점·실비·충분한 외표본을 검증하기 전 수치는 진단용 모의 결과이며 실제 운용 성과가 아닙니다.',
            '20거래일은 초기 관측의 기준이며 통계적 수익성의 증명이 아닙니다.',
            '동일 원본 자료·비용·현금·거래 시간창을 사용하고 각 전략을 별도 Paper 장부에서 평가합니다.',
            '뉴스 미관측을 뉴스 없음으로 해석하지 않으며 모델을 재호출하지 않습니다.',
            '최종 홀드아웃을 보고 조정하면 그 구간은 이후 홀드아웃으로 사용할 수 없습니다.']}
    report.parent.mkdir(parents=True,exist_ok=True)
    write_json(report,payload)
    lines=['# SessionQuant 실제 기록 평가','실제 성과 검증은 미완료입니다. 아래 수치는 기록 재생의 진단용 모의 결과입니다.',
        '| 구간·단독 전략·정책 | 완료 거래 | 진단 순손익 USD | MDD % | 수수료 USD | 미청산 |',
        '|---|---:|---:|---:|---:|---:|']
    for period,rows in results.items():
        for name,row in rows.items():
            metric=row['diagnostic_simulation']
            lines.append(f"| {period}/{name} | {metric['trades']} | {metric['net_pnl']} | {metric['max_drawdown_pct']:.3f} | {metric['fees_usd']} | {metric['open_positions']} |")
    lines += ['','시점·표본·실비 검증 전 수익성이나 ETF 위험 필터 효과를 확정하지 않습니다.','\n'.join(payload['limitations'])]
    with report.with_suffix('.md').open('x',encoding='utf-8') as stream:stream.write('\n\n'.join(lines))
    return payload
