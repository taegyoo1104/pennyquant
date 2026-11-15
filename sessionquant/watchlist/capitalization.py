"""검증된 동일 기준일 시총 입력과 당시 알려진 감시목록 이력을 보존한다."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from hashlib import sha256
import json
import re
from urllib.parse import urlsplit
from pathlib import Path
from typing import Protocol

from ..models import symbol_valid, utc as model_utc
from ..portfolio.models import decimal, serialize

HISTORY = 'watchlist_snapshots_v1'
ATTEMPT = 'watchlist_refresh_v1'
EXCLUDABLE_CAP_ISSUES = {'market_cap_missing', 'market_cap_value_missing'}


def cap_selection_metadata(issues, items, missing_cap_policy):
    """A usable known-cap subset is distinct from a verified full-universe ranking."""
    if missing_cap_policy not in {'block','exclude'}:
        raise ValueError('시총 누락 정책은 block 또는 exclude여야 합니다.')
    excluded = sorted({r['ticker'] for r in issues if r['code'] in EXCLUDABLE_CAP_ISSUES}) if missing_cap_policy=='exclude' else []
    usable = bool(items) and (not issues or (missing_cap_policy=='exclude'
                                           and all(r['code'] in EXCLUDABLE_CAP_ISSUES for r in issues)))
    return {'data_status':'VALID' if usable else 'PARTIAL',
            'missing_cap_policy':missing_cap_policy, 'coverage_status':'PARTIAL' if issues else 'COMPLETE',
            'ranking_scope':'available_market_caps' if excluded else 'full_universe',
            'ranking_verified':bool(items) and not issues, 'excluded_market_cap_symbols':excluded,
            'market_cap_exclusions':[{'ticker':s,'reasons':sorted({r['code'] for r in issues if r['ticker']==s})}
                                     for s in excluded]}


def utc(value):
    """공급자의 누락·잘못된 시각을 UNKNOWN 처리할 수 있는 검증 오류로 바꾼다."""
    if not isinstance(value, (str, datetime)):
        raise ValueError('시간대가 포함된 시각이 필요합니다.')
    return model_utc(value)


class MarketCapProvider(Protocol):
    """공급자의 시총 정의·허용 사용·시점 검증을 마친 원본 배치를 반환한다."""
    async def fetch(self) -> dict: ...


class FileMarketCapProvider:
    """라이선스·원본 정의를 확인한 운영자의 JSON 내보내기를 읽는다.

    기존 RSS용 반올림 스냅숏을 자동 변환하거나, 주가×발행주식수로 시총을 만들지 않는다.
    """
    def __init__(self, path):
        self.path = Path(path)

    async def fetch(self):
        if not self.path.is_file():
            raise ValueError('검증된 시가총액 입력 파일이 없습니다.')
        if self.path.stat().st_size > 5_000_000:
            raise ValueError('시가총액 입력 크기를 확인하세요.')
        data = json.loads(self.path.read_text(encoding='utf-8-sig'), parse_float=Decimal)
        if not isinstance(data, dict) or data.get('schema_version') != 1:
            raise ValueError('시가총액 입력 형식을 확인하세요.')
        return data


def symbols_digest(symbols):
    return sha256(json.dumps(sorted(symbols)).encode()).hexdigest()


@dataclass(frozen=True)
class WatchlistSnapshot:
    """원본 정의와 선택 이유를 포함하는 불변 선택 기록이다."""
    payload: dict

    @property
    def id(self):
        return self.payload['id']

    @property
    def symbols(self):
        return [row['ticker'] for row in self.payload['items']]


class WatchlistSelector:
    def __init__(self, size=100, max_age_days=7, *, missing_cap_policy='block'):
        if type(size) is not int or not 1 <= size <= 100:
            raise ValueError('감시목록 크기는 1~100이어야 합니다.')
        self.size, self.max_age = size, timedelta(days=max_age_days)
        if missing_cap_policy not in {'block','exclude'}:
            raise ValueError('시총 누락 정책은 block 또는 exclude여야 합니다.')
        self.missing_cap_policy = missing_cap_policy

    def select(self, batch, universe, now, *, allow_synthetic=False):
        """불완전 입력의 순위를 전체 S&P 500의 확정 순위로 표시하지 않는다."""
        now = utc(now)
        if isinstance(batch,dict) and batch.get('schema_version') == 2:
            from .toss_capitalization import select_toss
            return select_toss(batch,universe,now,size=self.size,allow_synthetic=allow_synthetic,
                               missing_cap_policy=self.missing_cap_policy)
        members = universe.members(now)
        if not members:
            raise ValueError('S&P 500 편입 정보가 없거나 만료됐습니다.')
        if not isinstance(batch, dict) or batch.get('schema_version') != 1 or batch.get('source_verified') is not True:
            raise ValueError('시총 공급자와 허용 사용의 검증이 필요합니다.')
        if batch.get('market_cap_basis') != 'issuer_total' or batch.get('currency') != 'USD':
            raise ValueError('발행사 전체 시총·USD 정의가 필요합니다.')
        if not isinstance(batch.get('source'), str) or not batch['source'].strip():
            raise ValueError('시총 자료 출처가 필요합니다.')
        if not isinstance(batch.get('source_documentation'), str) or not batch['source_documentation'].strip():
            raise ValueError('공급자의 시총 정의와 사용 조건 근거가 필요합니다.')
        for name in ('source','source_documentation'):
            text = batch[name]
            parsed = urlsplit(text)
            if re.search(r'(?i)(api[_-]?key|secret|token|password|authorization)\s*[:=]',text) or parsed.username or parsed.password:
                raise ValueError('시총 출처 설명에 인증정보를 포함할 수 없습니다.')
        synthetic = batch.get('synthetic', False)
        if type(synthetic) is not bool or (synthetic and not allow_synthetic):
            raise ValueError('합성 시총 자료는 실제 운영 입력으로 사용할 수 없습니다.')
        as_of, received, known = (utc(batch[k]) for k in ('as_of_at', 'received_at', 'known_at'))
        if not as_of <= received <= known <= now or now-as_of > self.max_age:
            raise ValueError('시총 기준·수신·확인 시각이 미래·역순·만료 상태입니다.')
        rows = batch.get('observations')
        if not isinstance(rows, list) or not rows or len(rows) > 1000:
            raise ValueError('시총 원본 배치가 비어 있거나 너무 큽니다.')
        seen, valid, issues = set(), [], []
        for row in rows:
            if not isinstance(row, dict):
                raise ValueError('시총 행은 객체여야 합니다.')
            symbol = row.get('ticker')
            symbol_valid(symbol)
            if symbol in seen:
                raise ValueError('시총 배치에 중복 티커가 있습니다.')
            seen.add(symbol)
            from ..market_reference.contracts import REFERENCE_SYMBOLS
            if symbol in REFERENCE_SYMBOLS:
                issues.append({'ticker':symbol,'code':'reference_etf_not_a_trading_member'})
                continue
            if symbol not in members:
                continue
            try:
                if row.get('data_status') != 'VERIFIED' or row.get('issuer_verified') is not True:
                    raise ValueError('issuer_or_value_unverified')
                issuer = row.get('issuer_id')
                if not isinstance(issuer, str) or not issuer.strip():
                    raise ValueError('issuer_id_missing')
                if any(utc(row.get(k,batch[k])) != expected for k,expected in (('as_of_at',as_of),('received_at',received),('known_at',known))):
                    raise ValueError('mixed_times')
                if row.get('currency', 'USD') != 'USD' or row.get('market_cap_basis', 'issuer_total') != 'issuer_total':
                    raise ValueError('mixed_definition')
                if row.get('market_cap_usd') is None or row.get('market_cap_usd') == '':
                    issues.append({'ticker':symbol,'code':'market_cap_value_missing'})
                    continue
                amount = decimal(row.get('market_cap_usd'), positive=True)
                valid.append({**row, 'market_cap_usd': str(amount), 'issuer_id': issuer})
            except (ValueError, TypeError):
                issues.append({'ticker': symbol, 'code': 'market_cap_or_issuer_unknown'})
        missing = sorted(set(members)-seen)
        issues.extend({'ticker': s, 'code': 'market_cap_missing'} for s in missing)
        grouped = {}
        for row in valid:
            grouped.setdefault(row['issuer_id'], []).append(row)
        selected = []
        for group in grouped.values():
            if len({Decimal(row['market_cap_usd']) for row in group}) != 1:
                issues.extend({'ticker': row['ticker'], 'code': 'issuer_cap_definition_conflict'} for row in group)
                continue
            if len(group) == 1:
                chosen, reason = group[0], '검증된 단일 주식 클래스'
            else:
                candidates = []
                for row in group:
                    try:
                        if row.get('data_supported') is not True or not row.get('representative_evidence_source'):
                            continue
                        value = decimal(row.get('representative_dollar_volume_usd'), positive=True)
                        stamp = utc(row['representative_as_of_at'])
                        if not stamp <= as_of or as_of-stamp > timedelta(days=3):
                            continue
                        candidates.append((value, row))
                    except (ValueError, TypeError, KeyError):
                        continue
                if not candidates:
                    issues.extend({'ticker': row['ticker'], 'code': 'representative_evidence_unknown'} for row in group)
                    continue
                candidates.sort(key=lambda item: (-item[0], item[1]['ticker']))
                chosen, reason = candidates[0][1], '동일 발행사·동일 시총: 지원·유동성 근거로 대표 티커 선정'
            selected.append({**chosen, 'selection_reason': reason})
        selected.sort(key=lambda row: (-Decimal(row['market_cap_usd']), row['ticker']))
        items = []
        for rank, row in enumerate(selected[:self.size], 1):
            items.append({**row, 'rank': rank, 'source': batch['source'], 'currency': 'USD',
                'as_of_at': as_of.isoformat(), 'received_at': received.isoformat(), 'known_at': max(known, now).isoformat(),
                'data_status': 'VERIFIED' if not issues else 'PARTIAL',
                'selection_reason': '시가총액 내림차순·동률 티커순; '+row['selection_reason']})
        payload = {'schema_version': 1, 'created_at': now.isoformat(), 'known_at': now.isoformat(),
            'as_of_at': as_of.isoformat(), 'received_at': received.isoformat(), 'source': batch['source'],
            'source_documentation': batch['source_documentation'], 'market_cap_basis': 'issuer_total',
            'currency': 'USD', 'synthetic': synthetic, 'data_status': 'VALID' if not issues and items else 'PARTIAL',
            'membership_hash': symbols_digest(members), 'membership_count': len(members),
            'membership_known_at': universe.snapshot(now)['known_at'], 'items': items, 'issues': issues,
            'valid_observations': len(valid), 'size_limit': self.size}
        payload.update(cap_selection_metadata(issues,items,self.missing_cap_policy))
        for row in items:
            row['data_status'] = 'VERIFIED' if payload['data_status']=='VALID' else 'PARTIAL'
            row['rank_scope'] = payload['ranking_scope']
        payload = serialize(payload)
        payload['id'] = sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        return WatchlistSnapshot(payload)


class WatchlistStore:
    """기존 장부를 건드리지 않고 PAPER 네임스페이스의 제어 원장에 이력을 추가한다."""
    def __init__(self, repo, universe, *, max_age_days=7, missing_cap_policy=None):
        self.repo, self.universe, self.max_age = repo, universe, timedelta(days=max_age_days)
        self.missing_cap_policy = missing_cap_policy

    def add(self, snapshot):
        data = snapshot.payload if isinstance(snapshot, WatchlistSnapshot) else snapshot
        with self.repo.transaction():
            history = self.repo.get_control(HISTORY, [])
            if not any(row['id'] == data['id'] for row in history):
                self.repo.set_control(HISTORY, history+[data])

    def at(self, now):
        now = utc(now)
        rows = [row for row in self.repo.get_control(HISTORY, [])
                if utc(row['known_at']) <= now and utc(row['as_of_at']) <= now]
        return max(rows, key=lambda row: (row['known_at'], row['id'])) if rows else None

    def status(self, now, *, allow_synthetic=False):
        now = utc(now)
        row = self.at(now)
        members = self.universe.members(now)
        result = {'status': 'UNKNOWN', 'items': [], 'universe_count': len(members), 'membership_status': 'VALID' if members else 'UNKNOWN',
            'snapshot': row, 'refresh': self.repo.get_control(ATTEMPT, {}), 'live_orders_enabled': False}
        if row:
            result['items'] = row['items']
            result['status'] = row['data_status']
            if self.missing_cap_policy=='block' and row.get('excluded_market_cap_symbols'):
                result['status'] = 'PARTIAL'
            if now-utc(row['as_of_at']) > self.max_age or (row.get('valid_until') and now>=utc(row['valid_until'])):
                result['status'] = 'EXPIRED'
            if not members:
                result['status'] = 'MEMBERSHIP_UNKNOWN'
            elif symbols_digest(members) != row['membership_hash']:
                result['status'] = 'MEMBERSHIP_CHANGED'
            if row['synthetic'] and not allow_synthetic:
                result['status'] = 'SYNTHETIC_BLOCKED'
            result.update(ranking_scope=row.get('ranking_scope','full_universe'),
                          ranking_verified=result['status']=='VALID' and row.get('ranking_verified',True),
                          market_cap_coverage=row.get('coverage_status','COMPLETE' if row['data_status']=='VALID' else 'PARTIAL'),
                          excluded_market_cap_symbols=row.get('excluded_market_cap_symbols',[]))
        return result
