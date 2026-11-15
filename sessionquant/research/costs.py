"""수신 원장의 토큰 기반 추정과 실제 청구 대사를 구분한다."""
from collections import Counter
from decimal import Decimal

from ..portfolio.models import decimal


def summarize_llm_costs(rows):
    latest={};anonymous=[]
    for row in rows:
        key=row.get('llm_call_id') or row.get('id')
        if key:latest[key]=row
        else:anonymous.append(row)
    records=[*latest.values(),*anonymous]
    estimates=[];billed=[]
    statuses=Counter()
    for row in records:
        statuses[row.get('cost_status') or 'LEGACY_UNVERIFIED']+=1
        try:estimates.append(decimal(row['cost_usd'],nonnegative=True))
        except (ValueError,TypeError,KeyError):pass
        # pricing_verified와 구 cost_is_estimate=False는 단가/토큰 확인이며 청구 확인이 아니다.
        if (row.get('cost_status')=='PROVIDER_BILLED_VERIFIED'
            and row.get('provider_billed_cost_verified') is True and row.get('provider_billed_currency')=='USD'
            and row.get('provider_billed_cost_source')):
            try:billed.append(decimal(row['provider_billed_cost_usd'],nonnegative=True))
            except (ValueError,TypeError,KeyError):pass
    actual=sum(billed,Decimal(0)) if records and len(billed)==len(records) else None
    estimated=sum(estimates,Decimal(0)) if records and len(estimates)==len(records) else None
    return {'llm_recorded_verified_usd':actual,'llm_recorded_estimated_usd':estimated,
        'llm_billing_verified':actual is not None,'llm_usage_records':len(records),
        'llm_superseded_usage_versions':len(rows)-len(records),'llm_unmapped_usage_records':len(anonymous),
        'llm_cost_status_counts':dict(statuses),'llm_billing_basis':'EXPLICIT_PROVIDER_BILL_RECONCILIATION_ONLY'}
