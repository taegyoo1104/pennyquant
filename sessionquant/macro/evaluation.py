"""합성/실제 라벨을 구분하고 같은 기업·거시 자료의 분류 품질을 비교한다."""
from dataclasses import replace
from datetime import datetime,timedelta,timezone
from decimal import Decimal
import json
import os
from pathlib import Path

from ..database.repository import Repository
from ..events import parse_event
from ..models import utc
from .analysis import BudgetedAnalyzer
from .models import LLMSettings,MacroArticle


def classification_metrics(outcomes):
    labels=sorted({row['expected'] for row in outcomes}|{row['actual'] for row in outcomes})
    per_class={}
    for label in labels:
        tp=sum(row['expected']==label and row['actual']==label for row in outcomes)
        fp=sum(row['expected']!=label and row['actual']==label for row in outcomes)
        fn=sum(row['expected']==label and row['actual']!=label for row in outcomes)
        precision=tp/(tp+fp) if tp+fp else 0.0;recall=tp/(tp+fn) if tp+fn else 0.0
        per_class[label]={'precision':precision,'recall':recall,'f1':2*precision*recall/(precision+recall) if precision+recall else 0.0,
            'true_positive':tp,'false_positive':fp,'false_negative':fn}
    count=len(outcomes)
    failures={}
    for row in outcomes:
        code=row.get('validation_error_code') or row.get('error')
        if code:failures[code]=failures.get(code,0)+1
    rumors=[row for row in outcomes if row.get('expected_status')=='RUMOR']
    attempted=sum(row.get('network_attempts',0)>0 for row in outcomes)
    return {'examples':count,'accuracy':sum(row['expected']==row['actual'] for row in outcomes)/count if count else None,
        'per_class':per_class,'adverse_false_negative':per_class.get('NEGATIVE',{}).get('false_negative',0),
        'rumor_false_positive':sum(row.get('classification_status')=='CONFIRMED' for row in rumors),
        'validation_failure_counts':failures,'cache_hit_ratio':sum(row.get('cached',False) for row in outcomes)/count if count else None,
        'macro_f1':sum(value['f1'] for value in per_class.values())/len(per_class) if per_class else None,
        'model_attempted_cases':attempted,
        'json_parse_failure_rate':failures.get('JSON_PARSE_ERROR',0)/attempted if attempted else None,
        'schema_failure_rate':failures.get('SCHEMA_MISMATCH',0)/attempted if attempted else None,
        'evidence_failure_rate':failures.get('EVIDENCE_QUOTE_MISMATCH',0)/attempted if attempted else None,
        'evidence_match_ratio':sum(row.get('evidence_matched',False) for row in outcomes)/count if count else None,
        'estimated_cost_usd':str(sum((Decimal(row.get('estimated_cost_usd','0')) for row in outcomes),Decimal(0))),
        'latency_ms':sum(row.get('latency_ms',0) for row in outcomes)}


def parse_case(row):
    kind=row.get('kind','company')
    if kind=='company':
        event=parse_event({'kind':'news','data':row['data']}).data
        if row['ticker'] not in event.symbols and row.get('expected_error')!='ticker_not_in_article_symbols':
            raise ValueError('기사와 평가 종목이 일치하지 않습니다')
    elif kind=='macro':
        data=dict(row['data'])
        data['received_at']=utc(data['received_at']);data['published_at']=utc(data['published_at']) if data.get('published_at') else None
        event=MacroArticle(**data)
    else:raise ValueError('평가 유형은 company 또는 macro여야 합니다.')
    expected=row.get('expected_sentiment',row.get('expected_direction'))
    if expected not in {'POSITIVE','NEGATIVE','NEUTRAL','MIXED','UNKNOWN'}:raise ValueError('정답 방향 라벨이 필요합니다.')
    return kind,event,expected


async def evaluate_models(input_path,output,*,models=('gpt-4.1-mini','gpt-6-luna','gpt-6.1-terra'),execute=False,
                          offset=0,limit=20,client=None,api_key=None):
    output=Path(output)
    if output.exists():raise ValueError('기존 평가 보고서를 덮어쓰지 않습니다. 새 경로를 지정하세요.')
    if type(offset) is not int or offset<0 or type(limit) is not int or not 1<=limit<=200:
        raise ValueError('평가 offset과 1~200건 batch 크기를 확인하세요.')
    rows=[json.loads(line) for line in Path(input_path).read_text(encoding='utf-8').splitlines() if line.strip()]
    if not 1<=len(rows)<=2000:raise ValueError('라벨 자료는 1~2000건이어야 합니다.')
    selected=rows[offset:offset+limit]
    if not selected:raise ValueError('선택한 평가 구간이 비어 있습니다.')
    for row in selected:parse_case(row)
    report={'execute':execute,'examples':len(selected),'total_examples':len(rows),'offset':offset,'limit':limit,
        'models':list(models),'results':{},'synthetic_examples':sum(row.get('synthetic') is True for row in selected),
        'source':'USER_LABELED_DATASET','prompt_language':'English','paid_generation_calls':0,
        'korean_prompt_comparison':'미실행: 언어 변경 전 별도 라벨 평가 필요','model_quality_verified':False}
    if execute:
        key=api_key if api_key is not None else os.getenv('OPENAI_API_KEY','')
        if not key:raise ValueError('본인 PC에 OPENAI_API_KEY를 설정하세요')
        audit_path=output.with_suffix(output.suffix+'.audit.db')
        if audit_path.exists():raise ValueError('평가 감사 DB도 새 경로가 필요합니다.')
        audit_path.parent.mkdir(parents=True,exist_ok=True)
        repo=Repository(audit_path,backend='sqlite')
        now=datetime.now(timezone.utc)
        seen_calls=set()
        try:
            for model in models:
                settings=LLMSettings(primary_model=model,enable_escalation=False,daily_primary_call_cap=200,hourly_primary_call_cap=200)
                analyzer=BudgetedAnalyzer(repo,settings,api_key=key,client=client)
                outcomes=[]
                try:
                    for row in selected:
                        kind,event,expected=parse_case(row)
                        rebased=bool(row.get('synthetic') and not row.get('temporal_case'))
                        if rebased:event=replace(event,published_at=now-timedelta(minutes=1),received_at=now)
                        if kind=='company':
                            result=await analyzer.analyze(event,row['ticker'],source_kind=row.get('source_kind','rss'),
                                primary_verified=row.get('primary_verified',True),now=datetime.now(timezone.utc))
                            value=result.to_dict();actual=result.sentiment;status=next((item.status for item in result.events),'UNKNOWN')
                            matched=bool(result.evidence_source_spans)
                        else:
                            value=await analyzer.analyze_macro(event,now=datetime.now(timezone.utc))
                            actual=value['direction'];status=value.get('model_status',value['status']);matched=bool(value.get('evidence_source_span'))
                        call_ids=value.get('llm_call_ids',[]);fresh=[identity for identity in call_ids if identity not in seen_calls]
                        usages=[repo.get_record('llm_usage',identity) for identity in fresh];seen_calls.update(fresh)
                        calls=[repo.get_record('llm_calls',identity) for identity in fresh]
                        outcomes.append({'kind':kind,'case_id':row.get('id'),'expected':expected,'actual':actual,
                            'correct':actual==expected,'expected_status':row.get('expected_status'),'classification_status':status,
                            'error':value.get('error'),'validation_error_code':value.get('validation_error_code'),
                            'cached':value.get('cached',False),'evidence_matched':matched,'synthetic':row.get('synthetic',False),
                            'timestamps_rebased_for_evaluation':rebased,'llm_call_ids':call_ids,'network_attempts':len(fresh),
                            'input_tokens':sum(item.get('input_tokens') or 0 for item in usages if item),
                            'output_tokens':sum(item.get('output_tokens') or 0 for item in usages if item),
                            'estimated_cost_usd':str(sum((Decimal(item['cost_usd']) for item in usages if item),Decimal(0))),
                            'latency_ms':sum(item.get('latency_ms',0) for item in calls if item)})
                finally:await analyzer.aclose()
                report['results'][model]={'company':classification_metrics([r for r in outcomes if r['kind']=='company']),
                    'macro':classification_metrics([r for r in outcomes if r['kind']=='macro']),
                    **classification_metrics(outcomes),'cases':outcomes}
            report['paid_generation_calls']=len(seen_calls)
            report['audit_database']=audit_path.name
        finally:repo.close()
    output.parent.mkdir(parents=True,exist_ok=True)
    with output.open('x',encoding='utf-8') as handle:json.dump(report,handle,ensure_ascii=False,indent=2)
    return report
