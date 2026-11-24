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
    universe_source: str = 'sp500'
    strategy: str = "pullback"
    market_filter_enabled: bool = True
    source: str = "toss"
    quote_transport: str = "rest"
    paper_buy_fill_timing: str | None = None
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
    watchlist_size: int = 50
    focus_size: int = 20
    focus_selection: str = 'activity'
    focus_rebalance_minutes: int = 0
    selection_method: str = 'market_cap_top'
    market_cap_input: Path = Path('data/market-cap-input.json')
    market_cap_provider: str = 'auto'
    watchlist_missing_cap_policy: str = 'exclude'
    paper_data_policy: str = 'strict'
    actual_account_monitor_enabled: bool = True
    watchlist_refresh_days: int = 7
    watchlist_data_max_age_seconds: int = 120
    focus_min_hold_seconds: int = 300
    focus_refresh_grace_seconds: int = 120
    max_api_calls_per_cycle: int = 24
    light_refresh_seconds: int = 30
    ranking_refresh_seconds: int = 60
    max_cycle_seconds: int = 15
    history_pages_per_cycle: int = 1
    watchlist_candles_per_cycle: int = 4
    market_reference: dict = field(default_factory=dict)
    macro_reference_filter: dict = field(default_factory=dict)
    recording_enabled: bool = True
    retention: dict = field(default_factory=dict)
    news_enabled: bool = True
    include_alpaca: bool = True
    company_rss_catalog: bool = True
    companies: dict = field(default_factory=dict)
    portfolio: dict = field(default_factory=dict)
    llm: dict = field(default_factory=dict)
    llm_debug_audit: dict = field(default_factory=dict)
    macro_context_llm: dict = field(default_factory=dict)
    strategy_explanation: dict = field(default_factory=dict)
    candidate_selection: dict = field(default_factory=dict)

    @property
    def submission_quote_buys(self):
        return (self.paper_buy_fill_timing=='submission_quote' or
                (self.paper_buy_fill_timing is None and self.quote_transport=='websocket'
                 and self.portfolio.get('paper_fill_model','market_v2')=='market_v2'))

    def __post_init__(self):
        if self.universe_source not in {'sp500', 'toss_us'}:
            raise ValueError('후보 모집단은 sp500 또는 toss_us입니다.')
        if self.universe_source == 'toss_us' and (self.source != 'toss' or not self.watchlist_enabled
                or self.market_cap_provider != 'toss' or self.watchlist_refresh_days != 1):
            raise ValueError('미국 전체 후보는 토스 시총 공급·감시목록·일별 갱신 설정이 필요합니다.')
        if self.quote_transport not in {"rest", "websocket"}:
            raise ValueError("quote_transport는 rest 또는 websocket입니다.")
        if self.paper_buy_fill_timing not in {None,'submission_quote','next_quote'}:
            raise ValueError('모의 매수 체결 시점은 submission_quote 또는 next_quote입니다.')
        if self.quote_transport == "websocket" and (self.source != "toss" or not self.watchlist_enabled
                or self.watchlist_size > 50 or self.strategy != "pullback_simple"
                or self.paper_data_policy != "observed_paper_v1"):
            raise ValueError("웹소켓 시험 운영은 토스·감시 50개 이하·단순 눌림목·observed Paper 프로필에서 사용합니다.")
        from .strategy_explanation import ExplanationSettings
        if not isinstance(self.strategy_explanation,dict):
            raise ValueError('AI 설명 설정은 객체여야 합니다.')
        ExplanationSettings(**self.strategy_explanation)
        if self.focus_selection not in {'activity','turnover'}:
            raise ValueError('집중 선정 방식은 activity 또는 turnover입니다.')
        if type(self.focus_rebalance_minutes) is not int or self.focus_rebalance_minutes not in {0,30}:
            raise ValueError('집중 재선정 주기는 0(기존 방식) 또는 30분입니다.')
        if self.focus_rebalance_minutes and (self.strategy!='pullback_simple' or self.focus_selection!='turnover'):
            raise ValueError('30분 집중 재선정은 단순 눌림목·거래대금 선정 프로필에서 사용합니다.')
        from .database.retention import RetentionPolicy
        if not isinstance(self.retention, dict):
            raise ValueError('자료 보관 설정은 객체여야 합니다.')
        RetentionPolicy(**self.retention)
        from .market.paper_policy import validate_policy
        validate_policy(self.paper_data_policy)
        if type(self.actual_account_monitor_enabled) is not bool:
            raise ValueError('실계좌 조회 설정은 불리언이어야 합니다.')
        if self.environment != "PAPER_LOCAL":
            raise ValueError("Phase 0~4 실행 환경은 PAPER_LOCAL만 허용합니다. 실제 주문은 비활성화되어 있습니다.")
        if self.database_backend not in {"mysql", "sqlite"}:
            raise ValueError("database_backend는 mysql 또는 sqlite여야 합니다.")
        if self.strategy not in {"momentum", "pullback", "pullback_simple", "gap_breakout", "vwap"}:
            raise ValueError("지원하지 않는 전략입니다.")
        if type(self.market_filter_enabled) is not bool:
            raise ValueError('시장 방향 필터는 불리언이어야 합니다.')
        if self.source not in {"toss", "file"} or (self.source == "file" and self.input_path is None):
            raise ValueError("source는 toss 또는 file이며, file은 input_path가 필요합니다.")
        if self.market_cap_provider not in {'auto','file','toss'}:
            raise ValueError('market_cap_provider는 auto, file, toss여야 합니다.')
        if self.watchlist_missing_cap_policy not in {'exclude','block'}:
            raise ValueError('watchlist_missing_cap_policy는 exclude 또는 block이어야 합니다.')
        if type(self.watchlist_enabled) is not bool or self.selection_method != 'market_cap_top':
            raise ValueError('감시목록은 불리언·market_cap_top 정책이어야 합니다.')
        for name, low, high in [('watchlist_size',1,100),('focus_size',1,20),('watchlist_refresh_days',1,7),
                               ('watchlist_data_max_age_seconds',10,300),('focus_min_hold_seconds',0,3600),
                               ('focus_refresh_grace_seconds',0,300),
                               ('max_api_calls_per_cycle',8,100),('light_refresh_seconds',10,300),('ranking_refresh_seconds',30,600),
                               ('max_cycle_seconds',5,30),('history_pages_per_cycle',0,3),('watchlist_candles_per_cycle',0,10)]:
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
        if self.submission_quote_buys and self.portfolio.get('paper_fill_model','market_v2')!='market_v2':
            raise ValueError('주문 시점 호가 체결은 market_v2 모의 장부에서만 사용할 수 있습니다.')
        if self.llm.get('briefing_separate_schedule') is True:
            explanation = ExplanationSettings(**self.strategy_explanation)
            if (explanation.min_interval_seconds < 3600 or not explanation.regular_session_only
                    or explanation.max_attempts_per_day is not None):
                raise ValueError('별도 브리핑은 정규장 한 시간 간격·일일 횟수 제한 없음으로 설정하세요.')
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
        from .llm.audit import AuditSettings
        from .macro.context_analysis import ContextSettings
        if not isinstance(self.llm_debug_audit,dict) or not isinstance(self.macro_context_llm,dict):
            raise ValueError('LLM 감사·거시 맥락 설정은 객체여야 합니다.')
        AuditSettings(**self.llm_debug_audit)
        ContextSettings(**self.macro_context_llm)
        from .portfolio.selection import CandidateSettings
        if not isinstance(self.candidate_selection,dict):raise ValueError('후보 선정 설정은 객체여야 합니다.')
        CandidateSettings(**self.candidate_selection)
        refs=reference_settings(self.market_reference)
        if refs['websocket_chart'] and (self.source!='toss' or self.quote_transport!='websocket'):
            raise ValueError('실시간 참고 차트는 토스 웹소켓 프로필에서 사용하세요.')
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
    if payload.get('llm_debug_audit',{}).get('enabled'):
        audit=payload['llm_debug_audit']
        key_file=Path(audit.get('key_file','../data/llm-audit.key'))
        audit['key_file']=str(key_file if key_file.is_absolute() else (path.parent/key_file).resolve())
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
