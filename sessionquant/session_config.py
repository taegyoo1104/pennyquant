"""SessionQuant 모의 운영 설정. 비밀정보는 환경변수에서만 읽는다."""
from __future__ import annotations

from dataclasses import dataclass, field, fields
from pathlib import Path
import math
import yaml


@dataclass(frozen=True)
class SessionConfig:
    environment: str = "PAPER_LOCAL"
    database_backend: str = "mysql"
    db_path: Path = Path("data/sessionquant-paper-local.db")
    universe_path: Path = Path("data/sp500-universe.json")
    strategy: str = "pullback"
    source: str = "toss"
    input_path: Path | None = None
    poll_seconds: float = 10.0
    news_refresh_seconds: float = 600.0
    company_news_refresh_seconds: int = 180
    macro_news_refresh_seconds: int = 900
    economic_calendar_refresh_seconds: int = 3600
    company_new_analysis_max_age_hours: int = 24
    macro_new_analysis_max_age_hours: int = 72
    news_weekend_slowdown: bool = False
    news_enrichment: dict = field(default_factory=dict)
    symbols_per_cycle: int = 2
    watchlist_enabled: bool = True
    watchlist_size: int = 100
    focus_size: int = 20
    selection_method: str = 'market_cap_top'
    market_cap_input: Path = Path('data/market-cap-input.json')
    market_cap_provider: str = 'auto'
    watchlist_refresh_days: int = 7
    watchlist_data_max_age_seconds: int = 120
    focus_min_hold_seconds: int = 300
    max_api_calls_per_cycle: int = 24
    light_refresh_seconds: int = 30
    ranking_refresh_seconds: int = 60
    max_cycle_seconds: int = 15
    history_pages_per_cycle: int = 1
    market_reference: dict = field(default_factory=dict)
    macro_reference_filter: dict = field(default_factory=dict)
    recording_enabled: bool = True
    news_enabled: bool = True
    include_alpaca: bool = True
    company_rss_catalog: bool = True
    companies: dict = field(default_factory=dict)
    portfolio: dict = field(default_factory=dict)
    llm: dict = field(default_factory=dict)

    def __post_init__(self):
        if self.environment != "PAPER_LOCAL":
            raise ValueError("Phase 0~4 실행 환경은 PAPER_LOCAL만 허용합니다. 실제 주문은 비활성화되어 있습니다.")
        if self.database_backend not in {"mysql", "sqlite"}:
            raise ValueError("database_backend는 mysql 또는 sqlite여야 합니다.")
        if self.strategy not in {"momentum", "pullback", "gap_breakout", "vwap"}:
            raise ValueError("지원하지 않는 전략입니다.")
        if self.source not in {"toss", "file"} or (self.source == "file" and self.input_path is None):
            raise ValueError("source는 toss 또는 file이며, file은 input_path가 필요합니다.")
        if self.market_cap_provider not in {'auto','file','toss'}:
            raise ValueError('market_cap_provider는 auto, file, toss여야 합니다.')
        if type(self.watchlist_enabled) is not bool or self.selection_method != 'market_cap_top':
            raise ValueError('감시목록은 불리언·market_cap_top 정책이어야 합니다.')
        for name, low, high in [('watchlist_size',1,100),('focus_size',1,20),('watchlist_refresh_days',1,7),
                               ('watchlist_data_max_age_seconds',10,300),('focus_min_hold_seconds',0,3600),
                               ('max_api_calls_per_cycle',8,100),('light_refresh_seconds',10,300),('ranking_refresh_seconds',30,600),
                               ('max_cycle_seconds',5,30),('history_pages_per_cycle',0,3)]:
            value=getattr(self,name)
            if type(value) is not int or not low <= value <= high:
                raise ValueError(f'{name} 값은 {low}~{high} 정수여야 합니다.')
        if self.focus_size > self.watchlist_size:
            raise ValueError('집중 분석 수는 감시목록 수보다 클 수 없습니다.')
        for name, minimum in (("poll_seconds", 1), ("news_refresh_seconds", 300)):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value) or value < minimum:
                raise ValueError(f"{name}은 {minimum}초 이상이어야 합니다.")
        if type(self.symbols_per_cycle) is not int or not 1 <= self.symbols_per_cycle <= 10:
            raise ValueError("symbols_per_cycle은 1~10의 정수여야 합니다.")
        if type(self.recording_enabled) is not bool:raise ValueError('연구 기록 설정은 불리언이어야 합니다.')
        if (type(self.news_enabled) is not bool or type(self.include_alpaca) is not bool
                or type(self.company_rss_catalog) is not bool):
            raise ValueError("뉴스 사용 설정은 true 또는 false여야 합니다.")
        for name in ("companies", "portfolio", "llm"):
            if not isinstance(getattr(self, name), dict):
                raise ValueError(f"{name}은 설정 객체여야 합니다.")
        for name,low,high in [('company_news_refresh_seconds',180,86400),('macro_news_refresh_seconds',300,86400),
                ('economic_calendar_refresh_seconds',3600,86400),('company_new_analysis_max_age_hours',1,24),
                ('macro_new_analysis_max_age_hours',1,72)]:
            value=getattr(self,name)
            if (isinstance(value,bool) or not isinstance(value,(int,float)) or not math.isfinite(value)
                    or not low<=value<=high):raise ValueError(f'{name} 범위를 확인하세요.')
        if type(self.news_weekend_slowdown) is not bool:raise ValueError('주말 수집 감속은 불리언이어야 합니다.')
        if not isinstance(self.news_enrichment,dict) or set(self.news_enrichment)-{'publishers','min_chars','min_sentences'}:
            raise ValueError('기사 원문 검증 설정을 확인하세요.')
        policies=self.news_enrichment.get('publishers',{})
        if not isinstance(policies,dict) or len(policies)>50:raise ValueError('발행사 허용 목록을 확인하세요.')
        for host,policy in policies.items():
            from urllib.parse import urlsplit
            from .news.enrichment import safe_url
            safe_url('https://'+host+'/',policies)
            if not isinstance(policy,dict) or set(policy)-{'publisher_name','terms_url','allow_fetch','allow_llm','store_body'}:
                raise ValueError('발행사 이용 권한 설정을 확인하세요.')
            if policy.get('store_body') is True:raise ValueError('현재 원문은 메모리에서만 사용합니다. 장기 본문 저장은 지원하지 않습니다.')
            for key in ('allow_fetch','allow_llm'):
                if type(policy.get(key,False)) is not bool:raise ValueError('발행사 이용 권한은 불리언이어야 합니다.')
            if urlsplit(policy.get('terms_url','')).scheme!='https':raise ValueError('발행사 이용 조건의 HTTPS 근거가 필요합니다.')
        for key,default,minimum,maximum in [('min_chars',300,100,5000),('min_sentences',3,2,20)]:
            value=self.news_enrichment.get(key,default)
            if type(value) is not int or not minimum<=value<=maximum:raise ValueError('본문 검증 임계값을 확인하세요.')
        from .market_reference.settings import reference_settings
        reference_settings(self.market_reference)
        from .market_reference.risk import filter_settings
        filter_settings(self.macro_reference_filter)
        from .news.catalog import merge_companies
        object.__setattr__(self, 'companies', merge_companies(self.companies, enabled=self.company_rss_catalog))


def load_session_config(path: str | Path) -> SessionConfig:
    path = Path(path).resolve()
    payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(payload, dict) or set(payload) - {f.name for f in fields(SessionConfig)}:
        raise ValueError("알 수 없는 SessionQuant 설정 항목이 있습니다.")
    # 구 YAML에서만 명시된 600초 옵션은 기업·거시 수집 주기에 매핑한다.
    if 'news_refresh_seconds' in payload:
        for key in ('company_news_refresh_seconds','macro_news_refresh_seconds'):
            payload.setdefault(key,payload['news_refresh_seconds'])
    for name, default in (("db_path", "../data/sessionquant-paper-local.db"),
                          ("universe_path", "../data/sp500-universe.json"), ("market_cap_input", "../data/market-cap-input.json"), ("input_path", None)):
        value = payload.get(name, default)
        if value is not None:
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name}은 비어 있지 않은 경로여야 합니다.")
            candidate = Path(value)
            payload[name] = candidate if candidate.is_absolute() else (path.parent / candidate).resolve()
    return SessionConfig(**payload)
