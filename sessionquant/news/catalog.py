"""검증 당시 S&P 500 기업의 공식 RSS 목록을 네트워크 없이 읽는다."""
from importlib.resources import files
import json

from .service import CompanySource


def load_company_catalog():
    """패키지에 포함한 목록만 읽으며 운영 중 외부 주소를 자동 신뢰하지 않는다."""
    path = files('sessionquant').joinpath('config/company_rss.json')
    payload = json.loads(path.read_text(encoding='utf-8'))
    if payload.get('schema_version') != 1 or not isinstance(payload.get('companies'), dict):
        raise ValueError('기업 RSS 목록의 형식을 확인하세요.')
    scope = json.loads(files('sessionquant').joinpath('config/rss_top100.json').read_text(encoding='utf-8'))
    selected = scope.get('companies', [])
    if scope.get('scope') != 'rss_only' or len(selected) != 100 or len({r['cik'] for r in selected}) != 100:
        raise ValueError('기업 RSS 시가총액 상위 100개 범위를 확인하세요.')
    allowed = {symbol: row['cik'] for row in selected for symbol in row['symbols']}
    for symbol, row in payload['companies'].items():
        source = CompanySource(symbol=symbol, **row)
        if allowed.get(symbol) != source.cik:
            raise ValueError('기업 RSS 기본 목록에 상위 100개 범위를 벗어난 종목이 있습니다.')
        if not source.rss_urls or not source.rss_verified or not source.cik:
            raise ValueError('기업 RSS 목록에 확인되지 않은 항목이 있습니다.')
    return payload


def merge_companies(companies, *, enabled=True):
    """사용자 입력 필드를 우선한다. 빈 rss_urls는 해당 기업 RSS의 명시적 해제다."""
    result = {symbol: dict(row) for symbol, row in load_company_catalog()['companies'].items()} if enabled else {}
    for symbol, row in companies.items():
        if not isinstance(row, dict):
            raise ValueError('기업별 뉴스 설정은 객체여야 합니다.')
        result[symbol] = {**result.get(symbol, {}), **row}
    return result
