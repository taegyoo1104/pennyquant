"""거시 사건과 LLM 운영 설정. 금액은 Decimal로 보존한다."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from decimal import Decimal
from hashlib import sha256
import json
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from ..models import utc

TOPICS = {
    "us_monetary_policy": '("Federal Reserve" OR FOMC OR "interest rate decision")',
    "inflation_and_employment": '("US inflation" OR "US employment" OR "nonfarm payrolls")',
    "trade_and_tariffs": '(tariff OR "trade restrictions") AND (US OR America)',
    "export_controls": '("export controls" OR "semiconductor restrictions")',
    "geopolitical_conflict": '(war OR conflict OR sanctions) AND (economy OR markets)',
    "oil_supply_disruption": '("oil supply" OR "oil disruption" OR "OPEC production")',
    "financial_system_risk": '("bank failure" OR "financial stability" OR "credit crisis")',
    "technology_regulation": '("technology regulation" OR "antitrust" OR "AI regulation")',
}


def canonical_url(value: str) -> str:
    """추적 인수와 fragment만 제거한다. 다른 기사를 임의로 합치지 않는다."""
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("기사 주소는 인증정보 없는 HTTP 또는 HTTPS URL이어야 합니다")
    query = [(key, item) for key, item in parse_qsl(parsed.query, keep_blank_values=True)
             if not key.lower().startswith("utm_") and key.lower() not in {"fbclid", "gclid"}]
    return urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), parsed.path or "/", urlencode(sorted(query)), ""))


def stable_hash(value) -> str:
    return sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


@dataclass(frozen=True)
class MacroArticle:
    url: str
    title: str
    body: str
    source: str
    published_at: datetime | None
    received_at: datetime
    topic: str = "unknown"
    official: bool = False
    language: str = "English"
    discovery_source: str = "rss"
    source_family: str = ""

    def __post_init__(self):
        canonical_url(self.url)
        utc(self.received_at)
        if self.published_at is not None and utc(self.published_at) > utc(self.received_at):
            raise ValueError("기사 발표 시각이 수집 시각보다 미래입니다")
        if not isinstance(self.title, str) or not self.title.strip() or type(self.official) is not bool:
            raise ValueError("기사 제목 또는 출처 검증 값이 잘못되었습니다")

    @property
    def id(self):
        return "macro:" + stable_hash(canonical_url(self.url))[:32]

    @property
    def content_hash(self):
        return stable_hash({"title": self.title.strip(), "body": self.body.strip(), "topic": self.topic,
                            "published_at": utc(self.published_at).isoformat() if self.published_at else None,
                            "official": self.official, "source_family": self.source_family})

    def to_dict(self):
        value = asdict(self)
        value.update(id=self.id, content_hash=self.content_hash, canonical_url=canonical_url(self.url),
                     published_at=utc(self.published_at).isoformat() if self.published_at else None,
                     received_at=utc(self.received_at).isoformat())
        return value


@dataclass(frozen=True)
class EconomicEvent:
    id: str
    name: str
    scheduled_at: datetime
    known_at: datetime
    source_url: str
    importance: str = "HIGH"
    verified: bool = False

    def blackout(self, now: datetime, minutes: int = 15) -> bool:
        return (self.verified and utc(self.known_at) <= utc(now) and self.importance == "HIGH"
                and abs((utc(now) - utc(self.scheduled_at)).total_seconds()) <= minutes * 60)


@dataclass(frozen=True)
class LLMSettings:
    primary_model: str = "gpt-4.1-mini"
    escalation_model: str = "gpt-6.1-terra"
    daily_primary_call_cap: int = 45
    daily_escalation_call_cap: int = 5
    monthly_soft_budget_usd: Decimal = Decimal("2.0")
    monthly_hard_budget_usd: Decimal = Decimal("3.0")
    max_concurrent_calls: int = 3
    max_input_chars: int = 12000
    max_output_tokens: int = 1200
    enable_event_cache: bool = True
    # 기획서의 비교용 가정이다. 실제 계정의 모델명·가격·JSON 지원 확인 후 변경한다.
    primary_input_per_million: Decimal | None = None
    primary_output_per_million: Decimal | None = None
    escalation_input_per_million: Decimal | None = None
    escalation_output_per_million: Decimal | None = None
    pricing_verified: bool = False
    prompt_version: str = "session-facts-en-v1"
    max_age_hours: float = 24.0

    def __post_init__(self):
        known_rates = {"gpt-4.1-mini": ("0.40", "1.60"), "gpt-6-luna": ("0.10", "0.50"),
                       "gpt-6.1-terra": ("1.00", "5.00")}
        for stage in ("primary", "escalation"):
            model = getattr(self, stage + "_model")
            for index, direction in enumerate(("input", "output")):
                name = stage + "_" + direction + "_per_million"
                if getattr(self, name) is None:
                    if model not in known_rates:
                        raise ValueError("새 모델의 입력·출력 토큰 단가를 명시해야 합니다")
                    object.__setattr__(self, name, Decimal(known_rates[model][index]))
        for name in ("monthly_soft_budget_usd", "monthly_hard_budget_usd", "primary_input_per_million",
                     "primary_output_per_million", "escalation_input_per_million", "escalation_output_per_million"):
            value = Decimal(str(getattr(self, name)))
            if not value.is_finite() or value < 0:
                raise ValueError("LLM 예산·단가는 유한한 0 이상의 값이어야 합니다")
            object.__setattr__(self, name, value)
        for name in ("daily_primary_call_cap", "daily_escalation_call_cap", "max_concurrent_calls", "max_input_chars", "max_output_tokens"):
            if type(getattr(self, name)) is not int or getattr(self, name) < 0:
                raise ValueError("LLM 호출·출력 한도는 0 이상의 정수여야 합니다")
        if (not 1 <= self.max_concurrent_calls <= 3 or not 1000 <= self.max_input_chars <= 50000
                or not 100 <= self.max_output_tokens <= 4096
                or self.monthly_soft_budget_usd > self.monthly_hard_budget_usd):
            raise ValueError("LLM 동시 호출·본문·예산 한도를 확인하십시오")
        if not self.primary_model.strip() or not self.escalation_model.strip():
            raise ValueError("LLM 모델명이 필요합니다")
        if type(self.enable_event_cache) is not bool or type(self.pricing_verified) is not bool:
            raise ValueError("LLM 캐시·요금 확인 설정은 boolean이어야 합니다")
        if not 0 < self.max_age_hours <= 168:
            raise ValueError("뉴스 유효 기간은 0~168시간 이내여야 합니다")

    @classmethod
    def from_dict(cls, data: dict | None = None):
        return cls(**(data or {}))

    def rates(self, stage: str):
        if stage not in {"primary", "escalation"}:
            raise ValueError("알 수 없는 LLM 분석 단계입니다")
        return getattr(self, stage + "_input_per_million"), getattr(self, stage + "_output_per_million")
