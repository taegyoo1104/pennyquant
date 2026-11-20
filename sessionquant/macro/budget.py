"""독립 실행자도 공유하는 LLM 예산 예약·멱등 정산 원장."""
import asyncio
from datetime import datetime, timezone
from decimal import Decimal
import json
from uuid import uuid4

from ..models import utc

CONTROL='session_llm_budget_v1'


class BudgetBlocked(RuntimeError):
    def __init__(self, message='', *, code='UNKNOWN_LIMIT'):
        super().__init__(message)
        self.code = code


class BudgetLedger:
    def __init__(self,repository,settings):
        self.repo,self.settings=repository,settings
        self.lock=asyncio.Lock()
        self.semaphore=asyncio.Semaphore(settings.max_concurrent_calls)

    def snapshot(self,now=None):
        now=utc(now or datetime.now(timezone.utc));state=self.repo.get_control(CONTROL,{})
        month=state.get(now.strftime('%Y-%m'),{})
        return {'month':now.strftime('%Y-%m'),'spent_usd':month.get('spent','0'),
            'reserved_usd':str(sum((Decimal(item['amount']) for item in month.get('reservations',{}).values()),Decimal(0))),
            'daily_calls':month.get('days',{}).get(now.date().isoformat(),{'primary':0,'escalation':0}),
            'hourly_calls':month.get('hours',{}).get(now.strftime('%Y-%m-%dT%H'),{'primary':0,'escalation':0}),
            'daily_primary_cap':self.settings.daily_primary_call_cap,
            'hourly_primary_cap':self.settings.hourly_primary_call_cap,
            'monthly_cap_usd':str(self.settings.monthly_hard_budget_usd),
            'pricing_verified':self.settings.pricing_verified,'pricing_basis':'configured_token_rates','currency':'USD'}

    async def reserve(self,stage,payload,now,*,call_id=None,metadata=None,before_commit=None):
        rate_in,rate_out=self.settings.rates(stage)
        input_bound=len(json.dumps(payload.get('messages',[]),ensure_ascii=False).encode())+4096
        output_bound=payload.get('max_completion_tokens',self.settings.max_output_tokens)
        amount=(Decimal(input_bound)*rate_in+Decimal(output_bound)*rate_out)/Decimal(1000000)
        now=utc(now);month_key=now.strftime('%Y-%m');day=now.date().isoformat();hour=now.strftime('%Y-%m-%dT%H')
        token=call_id or uuid4().hex
        briefing = stage == 'primary' and (metadata or {}).get('source_kind') == 'strategy_snapshot'
        def update(state):
            state=state or {};month=state.setdefault(month_key,{'spent':'0','days':{},'reservations':{}})
            counts=month['days'].setdefault(day,{'primary':0,'escalation':0})
            hourly=month.setdefault('hours',{}).setdefault(hour,{'primary':0,'escalation':0})
            if counts[stage]>=getattr(self.settings,'daily_'+stage+'_call_cap'):
                raise BudgetBlocked('일일 LLM 호출 한도에 도달했습니다', code='DAILY_CALL_CAP')
            if hourly[stage]>=getattr(self.settings,'hourly_'+stage+'_call_cap'):
                raise BudgetBlocked('시간당 LLM 호출 한도에 도달했습니다', code='HOURLY_CALL_CAP')
            briefing_days = month.setdefault('briefing_days', {})
            briefing_hours = month.setdefault('briefing_hours', {})
            if stage == 'primary' and not briefing:
                # The existing cap includes both news and briefing. Reserve a
                # slice for regular-session narration, without increasing caps
                # or clearing calls made before this version was deployed.
                if (counts[stage] - briefing_days.get(day, 0) >=
                        self.settings.daily_primary_call_cap - self.settings.daily_briefing_reserved_calls):
                    raise BudgetBlocked('남은 일일 호출은 AI 브리핑용입니다', code='BRIEFING_DAILY_RESERVE')
                if (hourly[stage] - briefing_hours.get(hour, 0) >=
                        self.settings.hourly_primary_call_cap - self.settings.hourly_briefing_reserved_calls):
                    raise BudgetBlocked('남은 시간당 호출은 AI 브리핑용입니다', code='BRIEFING_HOURLY_RESERVE')
            total=Decimal(month['spent'])+sum((Decimal(row['amount']) for row in month['reservations'].values()),Decimal(0))
            cap=self.settings.monthly_soft_budget_usd if stage=='primary' else self.settings.monthly_hard_budget_usd
            if total+amount>min(cap,self.settings.monthly_hard_budget_usd):raise BudgetBlocked('월간 LLM 비용 한도에 도달했습니다', code='MONTHLY_COST_CAP')
            if token in month['reservations']:raise ValueError('LLM 예약 식별자가 중복됩니다.')
            month['reservations'][token]={'amount':str(amount),'stage':stage,'created_at':now.isoformat(),**(metadata or {})}
            counts[stage]+=1;hourly[stage]+=1
            if briefing:
                briefing_days[day] = briefing_days.get(day, 0) + 1
                briefing_hours[hour] = briefing_hours.get(hour, 0) + 1
            if before_commit:before_commit(token,amount)
            return state
        async with self.lock:self.repo.update_control(CONTROL,update,{})
        return month_key,token

    async def settle(self,reservation,stage,model,*,input_tokens=None,output_tokens=None,cached_tokens=0,
                     rejected=False,error='',now,metadata=None,after_settle=None):
        month_key,token=reservation
        def update(state):
            month=state[month_key]
            existing=self.repo.get_record('llm_usage',token)
            if existing:return state
            pending=month['reservations'].pop(token)
            measured=(type(input_tokens) is int and type(output_tokens) is int and input_tokens>=0 and output_tokens>=0)
            cache_known=type(cached_tokens) is int and 0<=cached_tokens<=(input_tokens if measured else 0)
            if measured:
                rate_in,rate_out=self.settings.rates(stage)
                cache_rate=self.settings.cached_rate(stage)
                cached=cached_tokens if cache_known else 0
                cost=(Decimal(input_tokens-cached)*rate_in+Decimal(cached)*cache_rate+Decimal(output_tokens)*rate_out)/Decimal(1000000)
            else:
                cost=Decimal(pending['amount'])
            month['spent']=str(Decimal(month['spent'])+cost)
            row={'id':token,'llm_call_id':token,'timestamp':utc(now).isoformat(),'model':model,'stage':stage,
                'prompt_version':self.settings.prompt_version,'input_tokens':input_tokens if measured else None,
                'output_tokens':output_tokens if measured else None,'cached_input_tokens':cached_tokens if measured and cache_known else None,
                'usage_reported':measured,'cost_usd':str(cost),'cost_is_estimate':not measured,
                'cost_status':'TOKEN_BASED_ESTIMATE' if measured else 'UNCONFIRMED_RESERVED_ESTIMATE',
                'provider_billed_cost_usd':None,'pricing_verified':self.settings.pricing_verified,
                'pricing_basis':'provider_tokens_configured_rates' if measured else 'reserved_upper_bound',
                'response_rejected':bool(rejected),'error':error,**(metadata or {})}
            self.repo.record('llm_usage',row)
            if after_settle:after_settle(row)
            return state
        async with self.lock:self.repo.update_control(CONTROL,update,{})
        return self.repo.get_record('llm_usage',token)
