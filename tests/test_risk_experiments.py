"""비교 가설은 별도 임시 장부에서 실행하고 운영 상수로 자동 반영하지 않는다."""
from dataclasses import replace
from decimal import Decimal
import json

import pytest

from sessionquant.portfolio.exit_policy import ExitPolicy
from sessionquant.portfolio.models import PortfolioSettings
from sessionquant.risk_experiments import compare_risk_parameters, experiment_plan
from sessionquant.session_demo import synthetic_tape


def test_independent_hypotheses_preserve_baseline_settings_and_decimal_stops():
    settings = PortfolioSettings()
    plans = experiment_plan(settings)
    assert len(plans) == 10 and len({p[0] for p in plans}) == 10
    assert settings.target_return == Decimal('.10')
    assert {config.target_return for _, config, _ in plans} == {Decimal('.02'), Decimal('.05'), Decimal('.10')}
    assert ExitPolicy(stop_mode='atr').stop_price('100', '98', None) is None
    assert ExitPolicy(stop_mode='atr', atr_multiple='2').stop_price('100', '98', '1.5') == Decimal('97')
    assert ExitPolicy(stop_mode='fixed_percent').stop_price('100', '98', None) == Decimal('99')


def test_all_four_single_strategy_runs_report_costs_unfilled_and_no_recommendation(tmp_path):
    # 외부 연결이나 신규 데이터 기록 기능을 사용하지 않고 기존 합성 테이프만 재생한다.
    tape, universe = tmp_path/'tape.jsonl', tmp_path/'universe.json'
    from sessionquant.market.universe import UniverseStore
    rows, opened = synthetic_tape()
    UniverseStore(universe).refresh(['AAPL'], source='SYNTHETIC_FIXTURE', observed_at=opened)
    tape.write_text(''.join(json.dumps(row)+'\n' for row in rows))
    report = tmp_path/'comparison.json'
    result = compare_risk_parameters(tape, universe, report, synthetic=True)
    assert len(result['results']) == 40 and result['recommended_parameters'] is None
    assert not result['profitability_validated'] and result['live_order_writes'] == 0
    assert result['broker_api_calls'] == result['llm_api_calls'] == 0
    assert json.loads(report.read_text()) == result and report.with_suffix('.md').exists()
    assert {r['active_strategy'] for r in result['results'].values()} == {'momentum','pullback','gap_breakout','vwap'}
    for row in result['results'].values():
        metrics = row['results']
        assert metrics['fill_model_version'] == 'market_v2' and metrics['risk_policy_version'] == 2
        assert Decimal(metrics['fees_usd']) >= 0 and metrics['open_positions'] >= 0
        assert metrics['cost_scope']['spread_slippage_and_fill_fees_included']
    before = report.read_bytes()
    with pytest.raises(ValueError): compare_risk_parameters(tape, universe, report, synthetic=True)
    assert report.read_bytes() == before


@pytest.mark.parametrize('policy', [{'fixed_stop_pct': '1'}, {'atr_multiple': '-1'},
    {'max_holding_minutes': True}, {'profit_protection': 'yes'}, {'stop_mode': 'auto_optimize'}])
def test_invalid_research_policy_rejected(policy):
    with pytest.raises(ValueError): ExitPolicy(**policy)


def test_research_policy_change_cannot_silently_resume_existing_ledger(tmp_path):
    from sessionquant.database.repository import Repository
    from sessionquant.market.universe import UniverseStore
    from sessionquant.session_engine import SessionEngine
    from test_paper_market_fills import NOW
    repo = Repository(tmp_path/'policy.db', backend='sqlite')
    try:
        engine = SessionEngine(repo, UniverseStore(), exit_policy=ExitPolicy())
        engine.tick(NOW)
        with pytest.raises(ValueError):
            SessionEngine(repo, UniverseStore(), restore=True, exit_policy=replace(ExitPolicy(), max_holding_minutes=30))
    finally: repo.close()
