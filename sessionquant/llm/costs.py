"""프로그램 예산·공식 조직 비용·수동 확인 잔액을 구별한다."""
from datetime import datetime,timezone
from decimal import Decimal

from ..models import utc
from ..portfolio.models import decimal


def set_manual_credit(repo,balance,checked_at,*,now=None):
    now=utc(now or datetime.now(timezone.utc));checked=utc(checked_at)
    if checked>now:raise ValueError('수동 잔액 확인 시각은 미래일 수 없습니다.')
    amount=decimal(balance,nonnegative=True)
    value={'balance_usd':str(amount),'checked_at':checked.isoformat(),'source':'USER_MANUAL',
           'currency':'USD','provider_balance_verified':False}
    repo.set_control('openai_manual_credit_snapshot',value)
    return value


def credit_summary(repo,now=None):
    now=utc(now or datetime.now(timezone.utc))
    manual=repo.get_control('openai_manual_credit_snapshot',{}) or {}
    limits=repo.get_control('session_llm_limits_v1',{}) or {}
    months=repo.get_control('session_llm_budget_v1',{}) or {};month=months.get(now.strftime('%Y-%m'),{})
    reserved=sum((decimal(row['amount'],nonnegative=True) for row in month.get('reservations',{}).values()),Decimal(0))
    hard=decimal(limits['monthly_hard_budget_usd'],nonnegative=True) if limits.get('monthly_hard_budget_usd') is not None else None
    result={'program_budget_remaining_usd':max(Decimal(0),hard-decimal(month.get('spent','0'))-reserved) if hard is not None else None,
        'program_reserved_usd':reserved,'manual_balance_usd':None,'manual_checked_at':manual.get('checked_at'),
        'local_cost_since_check_usd':None,'estimated_credit_usd':None,'provider_balance_verified':False,
        'official_organization_cost_usd':None,'official_cost_scope':'NOT_CONNECTED','credit_uncertainty':[]}
    official=repo.get_control('openai_official_cost_snapshot',{}) or {}
    if official.get('source')=='OPENAI_COSTS_API' and official.get('currency')=='USD' and official.get('amount_usd') is not None:
        result.update(official_organization_cost_usd=decimal(official['amount_usd'],nonnegative=True),
            official_cost_scope=official.get('scope','UNKNOWN'),official_cost_retrieved_at=official.get('retrieved_at'))
    if not manual:return result
    checked=utc(manual['checked_at']);balance=decimal(manual['balance_usd'],nonnegative=True)
    result['manual_balance_usd']=balance
    if checked>now:
        result['credit_uncertainty'].append('manual_check_in_future');return result
    cost=Decimal(0);unknown=False
    for _,row in repo.iter_records('llm_usage'):
        try:
            settled=utc(row['timestamp'])
            if settled>now:continue
            started=utc(row.get('request_started_at') or row['timestamp'])
            if started<checked<=settled:unknown=True;continue
            if started>=checked:
                cost+=decimal(row['cost_usd'],nonnegative=True)
                if row.get('cost_status')=='UNCONFIRMED_RESERVED_ESTIMATE':result['credit_uncertainty'].append('unconfirmed_call_cost')
        except (ValueError,TypeError,KeyError,AttributeError):unknown=True
    pending=Decimal(0)
    for month in months.values():
        for row in month.get('reservations',{}).values():
            try:
                if checked<=utc(row['created_at'])<=now:pending+=decimal(row['amount'],nonnegative=True)
                elif utc(row['created_at'])<checked:unknown=True
            except (ValueError,TypeError,KeyError):unknown=True
    result['local_cost_since_check_usd']=cost
    result['credit_uncertainty'].append('other_apps_and_provider_adjustments_not_observed')
    if pending:result['credit_uncertainty'].append('pending_reservations')
    if unknown:result['credit_uncertainty'].append('request_crossed_manual_check_or_legacy_time_unknown')
    else:result['estimated_credit_usd']=max(Decimal(0),balance-cost-pending)
    return result
