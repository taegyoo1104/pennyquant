"""뉴스의 매수 차단과 별개로, 검증된 기업 사건의 제한된 가점만 만든다."""
from datetime import timedelta
import math

from ..events import parse_event
from ..llm.evidence import prepare_article, evidence_span, ValidationFailure
from ..models import utc
from .analysis import event_digest
from .intake import company_reason


def company_score(rows, symbol, now):
    now=utc(now)
    result={'status':'UNKNOWN','value':None,'symbol':symbol,'observed_at':now.isoformat(),
        'reason':'verified_company_facts_unavailable','analysis_ids':[],'llm_call_ids':[],
        'macro_score_applied':False,'policy_version':'company-rank-v1'}
    latest={}
    for row in rows:
        if not isinstance(row,dict):continue
        if row.get('category')!='company' or row.get('ticker')!=symbol:continue
        try:
            completed=utc(row['analyzed_at'])
            if completed>now:continue
            key=row.get('news_id') or row.get('event',{}).get('id')
            if key and (key not in latest or utc(latest[key]['analyzed_at'])<=completed):latest[key]=row
        except (KeyError,ValueError,TypeError,AttributeError):continue
    verified=[]
    for row in latest.values():
        try:
            event=parse_event({'kind':'news','data':row['event']}).data
            confidence=row.get('confidence')
            if (row.get('error') or row.get('validation_error_code') or row.get('primary_verified') is not True
                or row.get('requires_verification') is not False or row.get('missing_information')!=[]
                or row.get('sentiment') not in {'POSITIVE','NEGATIVE','NEUTRAL','MIXED'}
                or type(confidence) not in (int,float) or not math.isfinite(confidence) or not .75<=confidence<=1
                or not row.get('model') or not row.get('prompt_version') or not row.get('adopted_llm_call_id')
                or not row.get('analysis_id') or row.get('input_digest')!=event_digest(event,symbol)
                or company_reason(event,symbol,row.get('source_kind'),True,now)
                or not event.received_at<=utc(row['analyzed_at'])<=now):continue
            items=row.get('events') or []
            spans=row.get('evidence_source_spans') or []
            if not items or len(items)!=len(spans):continue
            # 07에서 실제 전송·검증한 인용 해시를 원문과 다시 대조한다.
            prepared=prepare_article(event.title,event.body,100000)
            for item,span in zip(items,spans):
                checked=evidence_span(prepared,item['evidence'],item['status'])
                if checked['quote_hash']!=span.get('quote_hash'):raise ValidationFailure('EVIDENCE_QUOTE_MISMATCH','quote_hash')
            if any(item.get('status')!='CONFIRMED' for item in items):continue
            adverse=bool(row.get('dilution_risk') or any(item.get('dilution_risk') or item.get('shareholder_impact')=='NEGATIVE' for item in items))
            positive=row.get('sentiment')=='POSITIVE' and any(item.get('shareholder_impact')=='POSITIVE' for item in items)
            verified.append((0. if adverse else 1. if positive else .5,row,event))
        except (ValueError,TypeError,KeyError,AttributeError,ValidationFailure):continue
    if not verified:return result
    adverse=any(item[0]==0 for item in verified)
    score=0. if adverse else max(item[0] for item in verified)
    selected=[item for item in verified if item[0]==score]
    return {**result,'status':'VERIFIED','value':score,'reason':'verified_adverse_no_bonus' if adverse else
        'verified_positive_company_facts' if score>.5 else 'verified_neutral_company_facts',
        'valid_until':min(item[2].published_at+timedelta(hours=24) for item in selected).isoformat(),
        'analysis_ids':[item[1]['analysis_id'] for item in selected],
        'llm_call_ids':[item[1]['adopted_llm_call_id'] for item in selected]}


def score_value(signal, symbol, now):
    """부족한 뉴스는 0점 기여다. 검증된 중립 0.5와 구분해 상태를 보존한다."""
    try:
        value=signal['value']
        if (signal['status']=='VERIFIED' and signal['symbol']==symbol and signal.get('policy_version')=='company-rank-v1'
            and signal.get('macro_score_applied') is False and signal.get('analysis_ids') and signal.get('llm_call_ids')
            and utc(signal['observed_at'])<=now<utc(signal['valid_until'])
            and type(value) in (int,float) and math.isfinite(value) and 0<=value<=1):return value
    except (TypeError,KeyError,ValueError,AttributeError):pass
    return None
