"""호출 메타데이터는 기본 기록하고, 허용한 Paper 원문만 제한 암호화한다."""
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import stat
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

from ..models import utc, jsonable

CALL_SCOPE = ContextVar('sessionquant_llm_call_scope', default=None)
VERSION = 'llm-audit-v1'
ERROR_STATUS = {'PROVIDER_REFUSAL': 'REFUSED', 'INCOMPLETE_FINISH': 'INCOMPLETE',
    'JSON_PARSE_ERROR': 'JSON_PARSE_FAILED', 'SCHEMA_MISMATCH': 'SCHEMA_VALIDATION_FAILED',
    'EVIDENCE_QUOTE_MISMATCH': 'EVIDENCE_MISMATCH', 'SEMANTIC_CONFLICT': 'BUSINESS_RULE_REJECTED',
    'TIMEOUT': 'OUTCOME_UNKNOWN', 'TRANSPORT_ERROR': 'OUTCOME_UNKNOWN'}


class AuditUnavailable(RuntimeError):
    pass


def hash_json(value):
    return sha256(json.dumps(jsonable(value),sort_keys=True,ensure_ascii=False,allow_nan=False).encode()).hexdigest()


def redact(value, secrets=()):
    if isinstance(value, dict):
        return {key: '[비밀정보 제거]' if re.search(
            r'(?i)^(authorization|cookie|set.cookie|.*password.*|.*secret.*|.*api.?key.*|access_token|account_(?:seq|id|number))$', str(key))
            else redact(item,secrets) for key,item in value.items()}
    if isinstance(value,(tuple,list)): return [redact(item,secrets) for item in value]
    if isinstance(value,str):
        for secret in secrets:
            if secret: value=value.replace(secret,'[비밀정보 제거]')
        value=re.sub(r'(?i)(bearer\s+|(?:api.?key|password|secret|account.?seq|account.?number)\s*[:=]\s*)[^\s,;"}]+',
                     r'\1[비밀정보 제거]',value)
        value=re.sub(r'\bsk-[A-Za-z0-9_-]{8,}', '[비밀정보 제거]', value)
        value=re.sub(r'\b(?:tsck|tscs)_(?:live|test)_[A-Za-z0-9_-]+','[비밀정보 제거]',value)
        if value.startswith(('https://','http://')):
            parsed=urlsplit(value)
            return urlunsplit((parsed.scheme,parsed.hostname or '',parsed.path,'',''))
    return value


@dataclass(frozen=True)
class AuditSettings:
    enabled: bool = False
    retention_days: int = 7
    key_file: str = 'data/llm-audit.key'
    publishers: dict = field(default_factory=dict)
    max_body_bytes: int = 128000

    def __post_init__(self):
        if (type(self.enabled) is not bool or type(self.retention_days) is not int or not 1<=self.retention_days<=30
                or type(self.max_body_bytes) is not int or not 1000<=self.max_body_bytes<=256000
                or not isinstance(self.key_file,str) or not self.key_file.strip() or not isinstance(self.publishers,dict)):
            raise ValueError('LLM 감사 보관 설정을 확인하세요.')
        for host,terms in self.publishers.items():
            if not isinstance(host,str) or not re.fullmatch(r'[a-z0-9.-]+',host) or urlsplit(terms).scheme!='https':
                raise ValueError('본문 보관을 허용한 발행사와 이용 조건의 HTTPS 근거가 필요합니다.')


class AuditTrail:
    def __init__(self,repo,settings=None,*,environment='PAPER_LOCAL',clock=None,secrets=()):
        self.repo=repo;self.settings=AuditSettings(**(settings or {})) if isinstance(settings,(dict,type(None))) else settings
        self.environment=environment;self.clock=clock or (lambda:datetime.now(timezone.utc));self.secrets=secrets
        self.cipher=None;self._bodies={};self._last_cleanup=None
        if self.settings.enabled:
            if environment!='PAPER_LOCAL':raise ValueError('원문 감사는 PAPER_LOCAL에서만 허용합니다.')
            from cryptography.fernet import Fernet
            path=Path(self.settings.key_file)
            path.parent.mkdir(parents=True,exist_ok=True)
            if not path.exists():
                try:
                    fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
                    with os.fdopen(fd,'wb') as handle:handle.write(Fernet.generate_key())
                except FileExistsError:pass
            if path.is_symlink() or (os.name!='nt' and stat.S_IMODE(path.stat().st_mode)&0o077):
                raise ValueError('감사 키 파일은 소유자만 읽을 수 있어야 합니다.')
            self.cipher=Fernet(path.read_bytes().strip())
        self.repo.set_control('llm_audit_policy_v1',{**asdict(self.settings),'environment':environment})
        self.cleanup()

    def cleanup(self):
        now=utc(self.clock())
        if self._last_cleanup is None or now-self._last_cleanup>=timedelta(hours=1):
            self.repo.expire_llm_bodies(now);self._last_cleanup=now

    @contextmanager
    def scope(self,metadata):
        trace={'metadata':dict(metadata),'call_ids':[],'precheck_id':None}
        token=CALL_SCOPE.set(trace)
        try:yield trace
        finally:CALL_SCOPE.reset(token)

    def _store(self,row):
        try:self.repo.record('llm_calls',redact(row,self.secrets))
        except Exception as exc:
            raise AuditUnavailable('LLM 감사 저장을 확인할 수 없습니다.') from exc

    def _get(self,call_id):
        try:return self.repo.get_record('llm_calls',call_id)
        except Exception as exc:raise AuditUnavailable('LLM 감사 연결 조회 미확인') from exc

    def skip(self,metadata,status,reason,*,cached_ids=()):
        now=utc(self.clock()).isoformat();identity=uuid4().hex
        self._store({**metadata,'id':identity,'precheck_id':identity,'llm_call_id':None,
            'status':status,'skipped_precheck_reason':reason,'network_attempted':False,'cached':status=='CACHE_HIT',
            'source_call_ids':list(cached_ids),'input_tokens':0,'output_tokens':0,'cost_usd':'0',
            'cost_status':'NO_REQUEST','request_started_at':None,'recorded_at':now,'environment':self.environment,
            'audit_version':VERSION,'body_retention':'NOT_RETAINED'})
        return identity

    def requested(self,call_id,payload,metadata,now,reserved_amount):
        system=[m.get('content') for m in payload.get('messages',[]) if m.get('role')=='system']
        options={key:value for key,value in payload.items() if key!='messages'}
        row={**metadata,'id':call_id,'llm_call_id':call_id,'precheck_id':metadata.get('precheck_id'),
            'status':'REQUESTED','network_attempted':True,'cached':False,'request_started_at':utc(now).isoformat(),
            'recorded_at':utc(now).isoformat(),'environment':self.environment,'audit_version':VERSION,
            'prompt_hash':hash_json(payload.get('messages',[])),'input_hash':hash_json(payload),
            'request_options':options,'system_prompt':system,'reserved_cost_usd':str(reserved_amount),
            'body_retention':'NOT_RETAINED','transitions':[{'status':'REQUESTED','at':utc(now).isoformat()}]}
        urls=metadata.get('article_urls') or [metadata.get('article_url','')]
        if self.cipher and urls and all(urlsplit(url).hostname in self.settings.publishers for url in urls):
            self._bodies[call_id]={'request':redact(payload,self.secrets)}
            row['body_retention']='ENCRYPTED_EXPIRING' if self._save_body(call_id) else 'NOT_RETAINED_SIZE_LIMIT'
        self._store(row)

    def _save_body(self,call_id):
        raw=json.dumps(self._bodies[call_id],ensure_ascii=False,allow_nan=False).encode()
        if len(raw)>self.settings.max_body_bytes:
            self._bodies.pop(call_id,None);return False
        now=utc(self.clock())
        self.repo.record('llm_audit_bodies',{'id':call_id,'llm_call_id':call_id,'algorithm':'FERNET',
            'expires_at':(now+timedelta(days=self.settings.retention_days)).isoformat(),
            'ciphertext':self.cipher.encrypt(raw).decode(),'environment':self.environment})
        return True

    def received(self,call_id,response,now,usage_row):
        row=self._get(call_id)
        if row is None:raise AuditUnavailable('요청 감사 연결이 없습니다.')
        try:
            envelope=response.json();choice=(envelope.get('choices') or [{}])[0]
            finish=choice.get('finish_reason')
        except (ValueError,TypeError,AttributeError,IndexError):finish=None
        status='RESPONSE_RECEIVED' if response.is_success else 'RATE_LIMITED' if response.status_code==429 else 'HTTP_FAILED'
        row.update(response_received_at=utc(now).isoformat(),http_status=response.status_code,finish_reason=finish,
            provider_request_id=response.headers.get('x-request-id'),response_hash=sha256(response.content).hexdigest(),
            response_bytes=len(response.content),status=status,
            latency_ms=max(0,round((utc(now)-utc(row['request_started_at'])).total_seconds()*1000)),
            input_tokens=usage_row['input_tokens'],output_tokens=usage_row['output_tokens'],
            cost_usd=usage_row['cost_usd'],cost_status=usage_row['cost_status'],usage_reported=usage_row['usage_reported'])
        if call_id in self._bodies:
            if len(response.content)<=100000:
                try:body=response.json()
                except ValueError:body=response.text
                self._bodies[call_id]['response']=redact(body,self.secrets)
                if not self._save_body(call_id):row['body_retention']='NOT_RETAINED_SIZE_LIMIT'
            else:row['body_retention']='REQUEST_ONLY_RESPONSE_TOO_LARGE'
        row['transitions'].append({'status':status,'at':utc(now).isoformat()});self._store(row)

    def unknown(self,call_id,code,now):
        row=self._get(call_id)
        if row:
            usage=self.repo.get_record('llm_usage',call_id) or {}
            row.update({key:usage.get(key) for key in ('input_tokens','output_tokens','cost_usd','cost_status','usage_reported')})
            row.update(status='OUTCOME_UNKNOWN',validation_error_code=code,failed_field='transport',
                       validation_finished_at=utc(now).isoformat())
            row['transitions'].append({'status':'OUTCOME_UNKNOWN','at':utc(now).isoformat()});self._store(row)
            self._bodies.pop(call_id,None)

    def finish(self,call_id,result):
        row=self._get(call_id)
        if row is None:raise AuditUnavailable('응답 감사 연결이 없습니다.')
        code=result.get('validation_error_code','')
        if row['status'] in {'HTTP_FAILED','RATE_LIMITED','OUTCOME_UNKNOWN'}:status=row['status']
        elif code:status=ERROR_STATUS.get(code,'SCHEMA_VALIDATION_FAILED')
        elif result.get('error'):status='BUSINESS_RULE_REJECTED'
        else:status='SUCCEEDED'
        result={key:value for key,value in result.items() if key not in {'event','body','article_text'}}
        now=utc(self.clock()).isoformat()
        row.update(status=status,validation_finished_at=now,validation_error_code=code,
                   failed_field=result.get('failed_field',''),normalized_result=result)
        row['transitions'].append({'status':status,'at':now});self._store(row);self._bodies.pop(call_id,None)

    def read_body(self,call_id,*,allow_read=False):
        if not allow_read or not self.cipher or self.environment!='PAPER_LOCAL':return None
        row=self.repo.get_record('llm_audit_bodies',call_id)
        if not row or utc(row['expires_at'])<=utc(self.clock()):return None
        return json.loads(self.cipher.decrypt(row['ciphertext'].encode()))


def article_metadata(article,*,ticker=None,source_kind=None,primary=False,logical_id=None):
    from ..news.analysis import event_digest
    from ..macro.sources import official_url
    macro=hasattr(article,'content_hash')
    is_primary=bool(article.official and official_url(article.url) and article.published_at is not None and article.body.strip()) if macro else bool(primary)
    publisher_verified=bool(is_primary or getattr(article,'publisher_verified',article.verified if not macro else False))
    return {'article_id':article.id,'content_hash':article.content_hash if macro else event_digest(article,ticker),
        'ticker':ticker,'macro_topic':getattr(article,'topic',None),'source_kind':source_kind or getattr(article,'discovery_source','unknown'),
        'article_url':article.url,'publisher':article.source,'publisher_verified':publisher_verified,
        'primary_official_verified':is_primary,
        'independent_confirmation_count':1 if is_primary else 0,
        'body_quality':('BODY_MISSING' if not article.body.strip() else 'OFFICIAL_SHORT_BODY_VERIFIED'
                        if is_primary and len(article.body)<300 else 'VERIFIED_SOURCE_BODY' if publisher_verified
                        else 'BODY_PRESENT_UNVERIFIED'),
        'published_at':utc(article.published_at).isoformat() if article.published_at else None,
        'received_at':utc(article.received_at).isoformat(),'first_available_at':utc(getattr(article,'first_available_at',None) or article.received_at).isoformat(),
        'enrichment_version':getattr(article,'enrichment_version',''),'document_version':getattr(article,'document_version',''),
        'published_at_source':getattr(article,'published_at_source',''),'logical_analysis_id':logical_id,
        'provenance':{'discovery_source':getattr(article,'discovery_source',source_kind),'publisher':article.source}}
