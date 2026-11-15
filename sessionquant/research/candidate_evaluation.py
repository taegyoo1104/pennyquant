"""같은 기록의 뉴스·업종 점수 기여를 격리 Paper에서 비교한다."""
from dataclasses import asdict
from pathlib import Path
from tempfile import TemporaryDirectory

from .bundle import load_bundle,write_json
from .replay import replay_recording
from .quality import quality_report
from .journal import identity
from ..portfolio.selection import CandidateSettings
from ..portfolio.models import PortfolioSettings
from ..session_backtest import summarize


def evaluate_candidates(bundle,report):
    bundle,report=Path(bundle),Path(report)
    if report.suffix!='.json' or report.exists():raise ValueError('새 .json 보고서 경로가 필요합니다.')
    manifest=load_bundle(bundle)
    starts=manifest['recording_starts']
    if not starts or not starts[0]['data'].get('candidate_selection',{}).get('enabled'):
        raise ValueError('08 후보 선정 입력을 기록한 묶음이 필요합니다. 과거 점수를 소급 생성하지 않습니다.')
    base=CandidateSettings(**starts[0]['data']['candidate_selection'])
    if any(row['data'].get('candidate_selection')!=asdict(base) for row in starts):
        raise ValueError('선정 정책이 변경된 기록은 분리해서 비교하세요.')
    quality=quality_report(bundle/'events.jsonl');results={}
    with TemporaryDirectory(prefix='sessionquant-candidates-') as directory:
        for label,news,sector in [('technical_liquidity',False,False),('company_news',True,False),('sector_strength',False,True),('combined',True,True)]:
            options={**asdict(base),'use_news_score':news,'use_sector_score':sector}
            result=replay_recording(bundle,Path(directory)/(label+'.db'),candidate_settings=options)
            frames=result['portfolio_selections'];rows=[row for frame in frames for row in frame['rows']]
            results[label]={'settings':options,'diagnostic_simulation':summarize(result,PortfolioSettings(**starts[0]['data']['settings'])),
                'selection_frames':len(frames),'ranked_rows':len(rows),'approved_new':sum(frame['approved_new_count'] for frame in frames),
                'approved_add':sum(frame['approved_add_count'] for frame in frames),
                'verified_news_rows':sum(row['indicators']['score_component_status']['news']=='VERIFIED' for row in rows),
                'verified_sector_rows':sum(row['indicators']['score_component_status']['sector']=='VERIFIED' for row in rows),
                'order_intent_ids':[row['client_id'] for row in result['entry_intents']],
                'selection_ids':[frame['id'] for frame in frames],'performance':None}
    payload={'report_version':1,'policy_version':'candidate-ablation-v1','bundle_id':manifest['bundle_id'],
        'evaluation_id':identity([manifest['bundle_id'],asdict(base),'candidate-ablation-v1']),
        'quality':quality,'results':results,'actual_out_of_sample_validated':False,'operating_parameters_changed':False,
        'live_order_writes':0,'llm_generation_calls':0,'limitations':[
            '동일 원본 기록·비용·현금·전략·시간창·위험 차단을 사용하며 뉴스·업종 가점만 비교합니다.',
            '누락된 뉴스/업종 자료는 UNKNOWN으로 유지하고 생성·최신 자료로 보충하지 않습니다.',
            '합성·단기 표본의 진단값으로 실제 전략 효과·수익성을 확정하지 않습니다.']}
    report.parent.mkdir(parents=True,exist_ok=True);write_json(report,payload)
    return payload
