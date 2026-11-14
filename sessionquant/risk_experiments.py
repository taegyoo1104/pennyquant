"""수익 최적값을 선택하지 않는 단일 전략별 손절·익절·시간 청산 비교."""
from dataclasses import replace
from decimal import Decimal
import json
from pathlib import Path
from tempfile import TemporaryDirectory

from .portfolio.exit_policy import ExitPolicy
from .portfolio.models import PortfolioSettings, serialize
from .session_runtime import replay_session
from .session_backtest import summarize


def experiment_plan(settings):
    """아래 수치는 검증되지 않은 비교 가설이다. 운영 설정에 자동 반영하지 않는다."""
    baseline = ExitPolicy()
    rows = [(f'target_{pct}',replace(settings,target_return=Decimal(pct)),baseline) for pct in ('0.02','0.05','0.10')]
    target = replace(settings,target_return=Decimal('0.02'))
    rows += [('atr_1',target,ExitPolicy(stop_mode='atr',atr_multiple=Decimal('1'))),
             ('atr_2',target,ExitPolicy(stop_mode='atr',atr_multiple=Decimal('2'))),
             ('fixed_01',target,ExitPolicy(stop_mode='fixed_percent',fixed_stop_pct=Decimal('0.01'))),
             ('fixed_02',target,ExitPolicy(stop_mode='fixed_percent',fixed_stop_pct=Decimal('0.02'))),
             ('protection_off',target,ExitPolicy(profit_protection=False)),
             ('holding_30',target,ExitPolicy(max_holding_minutes=30)),
             ('holding_60',target,ExitPolicy(max_holding_minutes=60))]
    return rows


def compare_risk_parameters(input_path, universe_path, report_path, *, strategy='all', settings=None, synthetic=False):
    """한 번에 한 전략·한 장부만 실행한다. 시세·LLM·실주문 API를 호출하지 않는다."""
    strategies = ['momentum','pullback','gap_breakout','vwap'] if strategy == 'all' else [strategy]
    if any(name not in {'momentum','pullback','gap_breakout','vwap'} for name in strategies):
        raise ValueError('지원하지 않는 비교 전략입니다.')
    settings = settings or PortfolioSettings()
    report_path = Path(report_path)
    if report_path.suffix.lower() != '.json':
        raise ValueError('비교 보고서 경로는 .json 파일이어야 합니다.')
    if report_path.exists() or report_path.with_suffix('.md').exists():
        raise ValueError('기존 비교 보고서를 덮어쓰지 않습니다. 새 경로를 지정하세요.')
    results = {}
    with TemporaryDirectory(prefix='sessionquant-risk-') as directory:
        for active in strategies:
            for label,config,policy in experiment_plan(settings):
                name = active+':'+label
                run = replay_session(Path(input_path),Path(directory)/(active+'-'+label+'.db'),Path(universe_path),
                    strategy=active,synthetic=synthetic,settings=config,exit_policy=policy,backend='sqlite')
                results[name] = {'active_strategy':active,'target_return':str(config.target_return),
                    'exit_policy':serialize(policy),'results':summarize(run,config)}
    payload = {'report_version':1,'risk_policy_version':2,'scope':'offline_rule_comparison','synthetic':synthetic,
        'input':str(input_path),'universe':str(universe_path),'dataset_validation':'SYNTHETIC' if synthetic else 'UNKNOWN',
        'live_orders_enabled':False,'live_order_writes':0,'llm_api_calls':0,'broker_api_calls':0,
        'recommended_parameters':None,'profitability_validated':False,'results':results,
        'limitations':['비교 수치는 미검증 가설이며 최적값·수익 보장이 아닙니다.',
            '각 전략은 별도 장부에서 단독 실행합니다. 동시 다중 전략 엔진이 아닙니다.',
            '시장가·수수료·슬리피지 가정은 동일하며 주문 금액은 같은 위험 규칙으로 재계산되어 달라질 수 있습니다.',
            '입력에 없는 시각의 호가·체결은 생성하지 않습니다. 촘촘한 시세가 없으면 시간 청산 비교 근거도 부족합니다.',
            '미청산은 잔여 수량으로 보고하며 미래 청산·LLM·인프라 비용은 순손익에서 제외됩니다.',
            '일일 2%는 신규 진입 중단 기준입니다. 갭·지연·장애로 이를 넘는 손실이 가능합니다.']}
    report_path.parent.mkdir(parents=True,exist_ok=True)
    with report_path.open('x',encoding='utf-8') as output:
        json.dump(payload,output,ensure_ascii=False,indent=2,allow_nan=False)
    lines = ['# SessionQuant 위험 매개변수 비교','합성 동작 검증이며 실제 수익률이 아닙니다.' if synthetic else '입력 기록 재생이며 출처·완전성·수익성 검증은 UNKNOWN입니다.',
        '| 단독 전략·가설 | 비용 차감 손익 USD | 수익률 % | 수수료 USD | 미청산 | 체결된 청산 사유 |',
        '|---|---:|---:|---:|---:|---|']
    for name,row in results.items():
        value = row['results']
        lines.append(f"| {name} | {value['net_pnl']} | {value['return_pct']:.4f} | {value['fees_usd']} | {value['open_positions']} | {value['exit_reason_counts']} |")
    lines += payload['limitations']
    with report_path.with_suffix('.md').open('x',encoding='utf-8') as output:
        output.write('\n\n'.join(lines))
    return payload
