"""예산 예약·요청 감사·실제 HTTP 한 번·정산을 연결하는 공통 전송 경계."""
import asyncio
from uuid import uuid4

import httpx

from .audit import CALL_SCOPE,AuditUnavailable
from ..models import utc
from ..llm_health import response_health,set_health
from ..macro.budget import BudgetBlocked


class BudgetClient:
    def __init__(self,owner,stage):self.owner,self.stage=owner,stage

    async def post(self,url,**kwargs):
        owner=self.owner;owner._check_health()
        payload=kwargs['json'];payload['max_completion_tokens']=owner.settings.max_output_tokens
        trace=CALL_SCOPE.get()
        if trace is None:raise AuditUnavailable('호출과 원본 기사의 연결이 없습니다.')
        metadata={**trace['metadata'],'stage':self.stage,'model':payload['model'],
                  'prompt_version':trace['metadata'].get('prompt_version',owner.settings.prompt_version),'run_id':owner.run_id}
        now=utc(owner._clock());call_id=uuid4().hex
        async with owner.ledger.semaphore:
            owner._check_health()
            now=utc(owner._clock())
            metadata['request_started_at']=now.isoformat()
            try:
                reservation=await owner.ledger.reserve(self.stage,payload,now,call_id=call_id,metadata={
                    key:metadata.get(key) for key in ('article_id','content_hash','logical_analysis_id','ticker','macro_topic')},
                    before_commit=lambda token,amount:owner.audit.requested(token,payload,metadata,now,amount))
            except BudgetBlocked:raise
            except Exception as exc:
                owner.audit._bodies.pop(call_id,None)
                raise AuditUnavailable('요청 전 예산·감사 기록 미확인') from exc
            trace['call_ids'].append(call_id)
            try:
                response=await owner.client.post(url,**kwargs)
            except (httpx.HTTPError,asyncio.CancelledError) as exc:
                trace['outcome_unknown']=True
                code='TIMEOUT' if isinstance(exc,httpx.TimeoutException) else 'TRANSPORT_ERROR'
                if isinstance(exc,httpx.HTTPError):
                    set_health(owner.repo,'TEMPORARY_ERROR',key=owner.api_key,now=owner._clock(),retry_seconds=60)
                await asyncio.shield(self._settle(reservation,self.stage,payload['model'],
                    error='request_outcome_unknown',now=owner._clock(),metadata=metadata,
                    after_settle=lambda usage:owner.audit.unknown(call_id,code,owner._clock())))
                raise
            response.extensions['llm_call_id']=call_id
            response_health(owner.repo,response,key=owner.api_key,now=owner._clock())
            try:
                envelope=response.json();usage=envelope.get('usage') or {}
                if not isinstance(usage,dict):usage={}
                details=usage.get('prompt_tokens_details') or {}
                cached=details.get('cached_tokens',0) if isinstance(details,dict) else 0
            except (ValueError,AttributeError):usage={};cached=0
            await self._settle(reservation,self.stage,payload['model'],
                input_tokens=usage.get('prompt_tokens'),output_tokens=usage.get('completion_tokens'),cached_tokens=cached,
                rejected=not response.is_success,error='' if response.is_success else 'http_'+str(response.status_code),
                now=owner._clock(),metadata=metadata,
                after_settle=lambda row:owner.audit.received(call_id,response,owner._clock(),row))
            return response

    async def _settle(self,*args,**kwargs):
        try:return await self.owner.ledger.settle(*args,**kwargs)
        except Exception as exc:
            trace=CALL_SCOPE.get()
            if trace:trace['outcome_unknown']=True
            raise AuditUnavailable('응답 후 사용량·감사 정산 미확인') from exc

    async def aclose(self):pass
