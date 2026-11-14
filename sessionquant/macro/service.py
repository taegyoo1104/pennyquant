"""기업·거시 뉴스를 분리 수집하며 동기 위험 조회를 제공한다.

refresh는 메인 엔진이 별도 asyncio 작업으로 실행한다. 가격 수신·손절·세션 청산은
이 작업 완료를 기다리지 않으며 risk_context는 네트워크를 호출하지 않는다.
"""
from __future__ import annotations

from datetime import datetime, timezone
import re
import httpx

from ..models import utc
from ..news.service import NewsService
from .analysis import BudgetedAnalyzer
from .models import EconomicEvent, LLMSettings, MacroArticle, stable_hash
from .sources import FREDClient, GDELTClient, OfficialNewsClient, TreasuryClient, official_url


class NewsPipeline:
    def __init__(self, repository, *, companies=None, settings: LLMSettings | None = None,
                 include_alpaca: bool = True, analyzer=None, company_service=None,
                 official_client=None, gdelt_client=None, fred_client=None, treasury_client=None,
                 macro_refresh_seconds: int = 600, clock=None):
        if type(macro_refresh_seconds) is not int or not 300 <= macro_refresh_seconds <= 86400:
            raise ValueError("거시 뉴스 수집 주기는 300~86400초여야 합니다")
        self.repo = repository
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.analyzer = analyzer or BudgetedAnalyzer(repository, settings, clock=self.clock)
        self.company = company_service or NewsService(companies or {}, analyzer=self.analyzer,
            include_alpaca=include_alpaca, clock=self.clock)
        self.official = official_client or OfficialNewsClient()
        self.gdelt = gdelt_client or GDELTClient()
        self.fred = fred_client or FREDClient()
        self.treasury = treasury_client or TreasuryClient()
        self.macro_refresh_seconds = macro_refresh_seconds
        self._last_macro = None
        self._last_fred_day = ""
        self._analyses = {}
        self._events = {}
        self._economic = {}
        self.status = self.repo.get_control("session_news_status", {"status": "UNKNOWN", "issues": []})
        self._restore()

    def _restore(self):
        for row in self.repo.recent("news_analysis", limit=10000):
            self._analyses[row.get("id") or row.get("analysis_id", "")] = row
        for row in self.repo.recent("macro_analysis", limit=2000):
            self._analyses[row["id"]] = row
        for row in self.repo.recent("economic_events", limit=2000):
            try:
                event = EconomicEvent(id=row["id"], name=row["name"], scheduled_at=utc(row["scheduled_at"]),
                    known_at=utc(row["known_at"]), source_url=row["source_url"],
                    importance=row.get("importance", "HIGH"), verified=row.get("verified", False))
                self._economic[event.id] = event
            except (KeyError, TypeError, ValueError):
                continue

    def add_economic_event(self, event: EconomicEvent):
        """공식 발표 일정에 확인 시각을 붙인다. 모델이 발표 일정을 생성하지 않는다."""
        if not official_url(event.source_url) or event.verified is not True:
            raise ValueError("경제 일정은 확인한 공식 발표 URL과 verified=true가 필요합니다")
        from dataclasses import asdict
        self.repo.record("economic_events", asdict(event))
        self._economic[event.id] = event

    def _store_macro(self, article: MacroArticle, analysis: dict):
        # 같은 제목·공개일·사건 유형만 보수적으로 묶는다. 기사 수는 확인 횟수가 아니다.
        title = re.sub(r"\W+", " ", article.title.lower()).strip()
        event_key = stable_hash([title, article.topic, analysis.get("event_type", "UNKNOWN"),
                               article.published_at.date().isoformat() if article.published_at else "unknown"])
        index_name = "session_macro_event:" + event_key
        with self.repo.transaction():
            event_id = self.repo.get_control(index_name) or "event:" + event_key[:32]
            self.repo.set_control(index_name, event_id)
            analysis = {**analysis, "event_id": event_id}
            self.repo.record("macro_news", {**article.to_dict(), "event_id": event_id, "category": "macro"})
            self.repo.record("macro_analysis", analysis)
        self._analyses[analysis["id"]] = analysis

    async def refresh(self, symbols, *, held_symbols=(), now=None) -> dict:
        now = utc(now or self.clock())
        issues = []
        selected = list(dict.fromkeys([*held_symbols, *symbols]))[:20]
        try:
            results = await self.company.refresh(selected, now)
            for event in self.company.events:
                self.repo.record("news_events", event)
            for result in results:
                row = result.to_dict()
                row.update(id=row["analysis_id"], category="company")
                self.repo.record("news_analysis", row)
                self._analyses[row["id"]] = row
            issues.extend(self.company.status)
        except (ValueError, RuntimeError, TypeError, OSError, httpx.HTTPError):
            issues.append({"provider": "company", "code": "company_refresh_failed"})
        if self._last_macro is None or (now - self._last_macro).total_seconds() >= self.macro_refresh_seconds:
            self._last_macro = now
            articles = []
            for provider, client in (("official", self.official), ("treasury", self.treasury), ("gdelt", self.gdelt)):
                try:
                    values = await client.fetch(now=now) if provider == "gdelt" else await client.fetch()
                    articles.extend(values)
                    issues.extend(getattr(client, "status", []))
                except (ValueError, RuntimeError, TypeError, OSError, httpx.HTTPError):
                    issues.append({"provider": provider, "code": "macro_refresh_failed"})
            # 공식 자료 우선. 수집량 증가가 분석 호출량 폭증으로 이어지지 않도록 한도를 둔다.
            articles.sort(key=lambda article: (article.official, article.published_at or article.received_at), reverse=True)
            for article in articles[:40]:
                analysis = await self.analyzer.analyze_macro(article, now=max(now, utc(self.clock())))
                self._store_macro(article, analysis)
        if self._last_fred_day != now.date().isoformat():
            self._last_fred_day = now.date().isoformat()
            try:
                for row in await self.fred.fetch(now=now):
                    self.repo.record("macro_indicators", row)
            except (ValueError, RuntimeError, TypeError, OSError, httpx.HTTPError):
                issues.append({"provider": "fred", "code": "daily_context_unavailable"})
        self.status = {"status": "AVAILABLE" if self._analyses else "UNKNOWN",
            "updated_at": utc(self.clock()).isoformat(), "issues": issues,
            "budget": self.analyzer.ledger.snapshot(now), "force_liquidation": False}
        self.repo.set_control("session_news_status", self.status)
        # 원본·분석 DB 기록을 지우지 않고 현재 위험 조회용 메모리만 제한한다.
        self._analyses = {key: row for key, row in self._analyses.items()
                          if self._recent(row, utc(self.clock()))}
        return self.status

    @staticmethod
    def _recent(row, now):
        try:
            published = row.get("published_at") or row.get("event", {}).get("published_at")
            completed = utc(row["analyzed_at"])
            return (published is not None and utc(published) <= completed <= now
                    and (now - utc(published)).total_seconds() <= 24 * 3600)
        except (ValueError, TypeError, KeyError, AttributeError):
            return False

    def risk_context(self, now: datetime, symbol: str | None = None) -> dict:
        now = utc(now)
        reasons, major_adverse, available = [], False, False
        blackout = [event for event in self._economic.values() if event.blackout(now)]
        for event in blackout:
            reasons.append("경제 발표 전후 신규 진입 제한: " + event.name)
        for row in self._analyses.values():
            if not self._recent(row, now):
                continue
            if row.get("category") == "company":
                if symbol is None or row.get("ticker") != symbol:
                    continue
                available = available or not row.get("error")
                strict = (row.get("primary_verified") is True and row.get("requires_verification") is False
                          and not row.get("missing_information") and not row.get("error")
                          and isinstance(row.get("confidence"), (int, float)) and row["confidence"] >= .75)
                confirmed = strict and any(item.get("status") == "CONFIRMED" and item.get("importance") == "HIGH"
                    and item.get("shareholder_impact") == "NEGATIVE" for item in row.get("events", []))
                if confirmed:
                    major_adverse = True
                    reasons.append("확인된 중요 기업 악재: " + row.get("reason", ""))
                elif row.get("error") == "rules_semantic_conflict":
                    reasons.append("기업 뉴스 해석 충돌: 추가 검토 필요")
            else:
                available = available or not row.get("error")
                if (row.get("status") == "CONFIRMED" and row.get("source_verified") is True
                        and row.get("direction") == "NEGATIVE" and row.get("importance") == "HIGH"):
                    reasons.append("공식 거시 위험 발표: " + row.get("reason", ""))
        return {"status": "AVAILABLE" if available or blackout else "UNKNOWN", "block_entries": bool(reasons),
                "major_adverse": major_adverse, "blackout": bool(blackout), "reasons": reasons,
                "force_liquidation": False}

    async def aclose(self):
        for service in (self.company, self.official, self.treasury, self.gdelt, self.fred):
            await service.aclose()
