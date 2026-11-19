"""토스 섹터의 보고 시총을 수집해 같은 통화의 S&P 500 종목 순위를 만든다."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from hashlib import sha256
import json
import re
from zoneinfo import ZoneInfo

from ..models import symbol_valid
from .capitalization import utc
from ..portfolio.models import decimal, serialize
from .capitalization import WatchlistSnapshot, symbols_digest, cap_selection_metadata, EXCLUDABLE_CAP_ISSUES

STATE = 'toss_market_cap_collection_v1'
SOURCE = 'toss:/api/v1/sectors/{sectorId}/stocks:v1.2.28'
CONTRACT = 'toss_reported_security_krw_v1'


class CollectionPending(ValueError):
    """호출 예산에 맞춰 다음 주기에 이어서 수집한다."""


class TossMarketCapProvider:
    def __init__(self, repo, universe, *, clock=None):
        self.repo, self.universe = repo, universe
        self.clock = clock or (lambda:datetime.now(timezone.utc))
        self.state = repo.get_control(STATE,{}) or {}

    def _save(self): self.repo.set_control(STATE,self.state)

    async def advance(self, read, *, limit=1):
        """외부 토스 클라이언트/읽기 예산을 공유한다. 새 인증·주문을 생성하지 않는다."""
        now = utc(self.clock())
        members = self.universe.members(now)
        digest = symbols_digest(members)
        started = self.state.get('started_at')
        if (not started or self.state.get('membership_hash') != digest
                or (self.state.get('complete') and now-utc(started)>=timedelta(days=1))
                or (not self.state.get('complete') and now-utc(started)>timedelta(minutes=15))):
            self.state = {'started_at':now.isoformat(),'membership_hash':digest,'roots':None,'pages':{},'complete':False}
        if self.state.get('complete'):return True
        for _ in range(limit):
            roots = self.state['roots']
            if roots is None:
                response = await read('sectors',None)
                if response is None:break
                values = response.get('sectors') if isinstance(response,dict) else None
                if not isinstance(values,list) or not values:raise ValueError('토스 섹터 목록을 확인할 수 없습니다.')
                ids = [r.get('sectorId') for r in values if isinstance(r,dict)]
                if len(ids)!=len(values) or len(set(ids))!=len(ids) or any(not isinstance(s,str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,40}',s) for s in ids):
                    raise ValueError('토스 섹터 ID 중복·형식을 확인하세요.')
                if any(type(r.get('depth')) is not int or r['depth'] not in {0,1,2} or (r['depth']!=0 and not any(p['sectorId']==r.get('parentSectorId') and p.get('depth')==r['depth']-1 for p in values)) for r in values):
                    raise ValueError('토스 섹터 계층 연결이 불완전합니다.')
                roots = sorted(r['sectorId'] for r in values if r.get('depth')==0)
                if not roots:raise ValueError('토스 대분류 섹터가 없습니다.')
                self.state['roots'] = roots
            else:
                pending = [s for s in roots if s not in self.state['pages']]
                if not pending:break
                sector = pending[0]
                response = await read('sector_stocks',sector)
                if response is None:break
                rows = response.get('stocks') if isinstance(response,dict) else None
                if not isinstance(rows,list):raise ValueError('토스 섹터 종목 응답이 배열이 아닙니다.')
                self.state['pages'][sector] = {'received_at':utc(self.clock()).isoformat(),'stocks':rows}
            self._save()
        if self.state['roots'] and len(self.state['pages'])==len(self.state['roots']):
            if utc(self.state['started_at']).astimezone(ZoneInfo('America/New_York')).date()!=utc(self.clock()).astimezone(ZoneInfo('America/New_York')).date():
                self.state={};self._save();return False
            self.state['complete']=True;self.state['completed_at']=utc(self.clock()).isoformat()
        self._save()
        return self.state.get('complete',False)

    async def fetch(self):
        if not self.state.get('complete'):raise CollectionPending('토스 섹터 시총을 호출 예산 안에서 수집 중입니다.')
        return {'schema_version':2,'contract':CONTRACT,'source':SOURCE,'source_documentation':
            'https://openapi.tossinvest.com/openapi-docs/latest/openapi.json',
            'source_verified':True,'currency':'KRW','market_cap_basis':'provider_reported_security',
            'provider_as_of_at':None,'as_of_semantics':'collection_started_at_not_valuation_time',
            'update_frequency':'daily','started_at':self.state['started_at'],'received_at':self.state['completed_at'],
            'root_sectors':self.state['roots'],'pages':self.state['pages'],'membership_hash':self.state['membership_hash'],'synthetic':False}


def select_toss(batch, universe, now, *, size=100, allow_synthetic=False, missing_cap_policy='block'):
    """달러·발행사 총액을 추정하지 않고 공급자 보고 종목 시총을 정렬한다."""
    now = utc(now);members=universe.members(now)
    if (batch.get('contract')!=CONTRACT or batch.get('source')!=SOURCE or batch.get('currency')!='KRW'
            or batch.get('market_cap_basis')!='provider_reported_security' or batch.get('source_verified') is not True
            or batch.get('provider_as_of_at') is not None):raise ValueError('토스 보고 시총 계약을 확인하세요.')
    if type(batch.get('synthetic')) is not bool or (batch['synthetic'] and not allow_synthetic):
        raise ValueError('합성 토스 시총은 운영에 사용할 수 없습니다.')
    begin,end = utc(batch['started_at']),utc(batch['received_at'])
    if not begin<=end<=now or end-begin>timedelta(minutes=15) or now-begin>=timedelta(days=1):
        raise ValueError('토스 시총 수집 시각이 역전·미래·만료 상태입니다.')
    if not members or batch.get('membership_hash')!=symbols_digest(members):raise ValueError('수집 당시 편입 목록이 다릅니다.')
    pages=batch.get('pages');observations={};issues=[]
    roots=batch.get('root_sectors')
    if not isinstance(roots,list) or set(roots)!=set(pages or {}):raise ValueError('대분류 수집 범위가 다릅니다.')
    if begin.astimezone(ZoneInfo('America/New_York')).date()!=end.astimezone(ZoneInfo('America/New_York')).date():
        raise ValueError('서로 다른 날짜의 일별 시총을 합치지 않습니다.')
    if not isinstance(pages,dict) or not pages:raise ValueError('시총 수집 페이지가 없습니다.')
    for sector,page in pages.items():
        if not begin<=utc(page['received_at'])<=end:raise ValueError('시총 페이지 수신 시각이 다릅니다.')
        seen=set()
        for row in page['stocks']:
            symbol=row.get('symbol') if isinstance(row,dict) else None
            if symbol not in members:continue
            symbol_valid(symbol)
            if symbol in seen:issues.append({'ticker':symbol,'code':'duplicate_in_sector'})
            seen.add(symbol)
            try:
                if row.get('currency')!='KRW':raise ValueError('시총 통화가 다릅니다.')
                if row.get('marketCapitalization') is None or row.get('marketCapitalization') == '':
                    issues.append({'ticker':symbol,'code':'market_cap_value_missing'});continue
                amount=decimal(row.get('marketCapitalization'),positive=True)
            except (ValueError,TypeError):
                issues.append({'ticker':symbol,'code':'reported_cap_unknown'});continue
            prior=observations.get(symbol)
            if prior and Decimal(prior['market_cap_value'])!=amount:
                issues.append({'ticker':symbol,'code':'sector_cap_conflict'});continue
            observations[symbol]={'ticker':symbol,'name':row.get('name') if isinstance(row.get('name'),str) else None,'market_cap_value':str(amount),'market_cap_usd':None,
                'currency':'KRW','sector_ids':sorted(set((prior or {}).get('sector_ids',[])+[sector])),
                'issuer_id':None,'issuer_verified':False,'provider_as_of_at':None}
    issues += [{'ticker':s,'code':'market_cap_missing'} for s in sorted(set(members)-set(observations))]
    # ETF는 유니버스 오류가 있어도 거래 후보로 승격하지 않는다.
    from ..market_reference.contracts import REFERENCE_SYMBOLS
    for symbol in set(observations)&REFERENCE_SYMBOLS:
        observations.pop(symbol);issues.append({'ticker':symbol,'code':'reference_etf_not_a_trading_member'})
    if missing_cap_policy=='exclude':
        for symbol in {r['ticker'] for r in issues if r['code'] in EXCLUDABLE_CAP_ISSUES}:
            observations.pop(symbol,None)
    selected=sorted(observations.values(),key=lambda r:(-Decimal(r['market_cap_value']),r['ticker']))[:size]
    for rank,row in enumerate(selected,1):
        row.update(rank=rank,source=SOURCE,as_of_at=begin.isoformat(),received_at=end.isoformat(),known_at=now.isoformat(),
            data_status='VERIFIED' if not issues else 'PARTIAL',selection_reason='토스 보고 종목 시총 KRW 내림차순·동률 티커순; 발행사 합산 없음')
    payload={'schema_version':2,'contract':CONTRACT,'created_at':now.isoformat(),'known_at':now.isoformat(),
        'as_of_at':begin.isoformat(),'as_of_semantics':'collection_started_at_not_valuation_time','provider_as_of_at':None,
        'received_at':end.isoformat(),'valid_until':(begin+timedelta(days=1)).isoformat(),'source':SOURCE,
        'source_documentation':batch['source_documentation'],'currency':'KRW','market_cap_basis':'provider_reported_security',
        'share_class_policy':'separate_exact_tickers_no_inferred_issuer_total','synthetic':batch['synthetic'],
        'data_status':'VALID' if not issues and selected else 'PARTIAL','membership_hash':symbols_digest(members),
        'membership_count':len(members),'membership_known_at':universe.snapshot(now)['known_at'],'items':selected,
        'issues':issues,'valid_observations':len(observations),'size_limit':size,
        'raw_payload_hash':sha256(json.dumps(batch['pages'],sort_keys=True,ensure_ascii=False).encode()).hexdigest()}
    payload.update(cap_selection_metadata(issues,selected,missing_cap_policy))
    for row in selected:
        row['data_status'] = 'VERIFIED' if payload['data_status']=='VALID' else 'PARTIAL'
        row['rank_scope'] = payload['ranking_scope']
    payload=serialize(payload);payload['id']=sha256(json.dumps(payload,sort_keys=True,ensure_ascii=False).encode()).hexdigest()
    return WatchlistSnapshot(payload)


def bind_provider(manager, client):
    """기존 파일·테스트 주입을 보존하고 토스 런타임의 인증 클라이언트를 재사용한다."""
    config=manager.config
    use_toss=(config.market_cap_provider=='toss' or (config.market_cap_provider=='auto'
        and config.source=='toss' and not config.market_cap_input.is_file()))
    if not manager.provider_explicit and use_toss and all(hasattr(client,name) for name in ('sectors','sector_stocks')):
        manager.provider=TossMarketCapProvider(manager.repo,manager.universe,clock=manager.clock)
        return True
    return False
