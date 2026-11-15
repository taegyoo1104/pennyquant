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
                 macro_refresh_seconds: float = 900, company_refresh_seconds=180, calendar_refresh_seconds=3600,
                 company_max_age_hours=24,macro_max_age_hours=72,calendar_client=None,calendar_enabled=False,
                 enrichment_policies=None,weekend_slowdown=False,enrichment_min_chars=300,enrichment_min_sentences=3,clock=None,
                 audit_settings=None,macro_context_llm=None,environment='PAPER_LOCAL'):
        import math
        if (isinstance(macro_refresh_seconds,bool) or not isinstance(macro_refresh_seconds,(int,float))
                or not math.isfinite(macro_refresh_seconds) or not 300 <= macro_refresh_seconds <= 86400):
            raise ValueError("거시 뉴스 수집 주기는 300~86400초여야 합니다")
        self.repo = repository
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.analyzer = analyzer or BudgetedAnalyzer(repository, settings, clock=self.clock,audit_settings=audit_settings,environment=environment)
        from .context_analysis import MacroContextAnalyzer
        self.context_analyzer=MacroContextAnalyzer(repository,self.analyzer,macro_context_llm,clock=self.clock)
        from ..news.schedule import NewsSchedule
        from ..news.enrichment import ArticleEnricher
        from .calendar import EconomicCalendarClient
        self.schedule=NewsSchedule(repository,clock=self.clock)
        policy=getattr(self.analyzer,'settings',None)
        self.analysis_version=stable_hash([getattr(policy,'prompt_version','unknown'),getattr(policy,'primary_model','unknown'),
            getattr(policy,'escalation_model','unknown'),'stage07-v1'])
        self.enricher=ArticleEnricher(repository,policies=enrichment_policies,clock=self.clock,min_chars=enrichment_min_chars,
            min_sentences=enrichment_min_sentences,analysis_version=self.analysis_version)
        self.calendar=calendar_client or (EconomicCalendarClient(repository,clock=self.clock) if calendar_enabled else None)
        self.company_refresh_seconds=company_refresh_seconds;self.calendar_refresh_seconds=calendar_refresh_seconds
        self.company_max_age_hours=company_max_age_hours;self.macro_max_age_hours=macro_max_age_hours
        self.weekend_slowdown=weekend_slowdown
        self.analyzer.macro_max_age_hours=macro_max_age_hours
        if hasattr(self.analyzer,'max_age_hours'):self.analyzer.max_age_hours=company_max_age_hours
        self.company = company_service or NewsService(companies or {}, analyzer=self.analyzer,
            include_alpaca=include_alpaca, clock=self.clock,repository=repository)
        self.official = official_client or OfficialNewsClient(repository=repository)
        self.gdelt = gdelt_client or GDELTClient(repository=repository)
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
                    importance=row.get("importance", "HIGH"), verified=row.get("verified", False),
                    logical_id=row.get('logical_id',''),revision=row.get('revision',0),status=row.get('status','CONFIRMED'),
                    scheduled_local=row.get('scheduled_local',''))
                self._economic[event.id]=event
            except (KeyError, TypeError, ValueError):
                continue

    def add_economic_event(self, event: EconomicEvent):
        """공식 발표 일정에 확인 시각을 붙인다. 모델이 발표 일정을 생성하지 않는다."""
        if not official_url(event.source_url) or event.verified is not True:
            raise ValueError("경제 일정은 확인한 공식 발표 URL과 verified=true가 필요합니다")
        from dataclasses import asdict
        previous = self.repo.get_record('economic_events', event.id)
        if previous:
            from dataclasses import replace
            event = replace(event, known_at=min(event.known_at, utc(previous['known_at'])))
        self.repo.record("economic_events", asdict(event))
        self._economic[event.id]=event

    def economic_events_at(self, now):
        """각 논리 일정에서 조회 시점까지 확인한 최신 수정만 선택한다."""
        latest = {}
        for event in self._economic.values():
            if utc(event.known_at) > utc(now):
                continue
            key = event.logical_id or event.id
            old = latest.get(key)
            if old is None or (event.revision, event.known_at) > (old.revision, old.known_at):
                latest[key] = event
        return list(latest.values())

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
            raw=article.to_dict()
            if article.discovery_source=='gdelt':raw['body']=''
            self.repo.record("macro_news", {**raw, "event_id": event_id, "category": "macro"})
            self.repo.record("macro_analysis", analysis)
            if not analysis.get('error'):
                self.repo.set_control(self._completed_key(article),True)
        self._analyses[analysis["id"]] = analysis

    def _completed_key(self,article):
        return 'news_macro_analyzed:'+stable_hash([article.id,article.content_hash,self.analysis_version])

    async def refresh(self, symbols, *, held_symbols=(),allowlist=(), now=None) -> dict:
        from ..news.intake import macro_reason,archive,statistics
        from datetime import timedelta
        now = utc(now or self.clock());issues=[]
        company_interval=self.company_refresh_seconds;macro_interval=self.macro_refresh_seconds
        calendar_interval=self.calendar_refresh_seconds
        if self.weekend_slowdown:
            from ..market.bars import session_bounds
            if session_bounds(now) is None:
                company_interval=max(1800,company_interval);macro_interval=max(1800,macro_interval)
                calendar_interval=max(21600,calendar_interval)
        if self.schedule.claim('company_cycle',company_interval,now):
            priority=list(dict.fromkeys([*held_symbols,*symbols]))[:20]
            pool=[s for s in allowlist if s not in priority]
            cursor=self.repo.get_control('news_background_cursor_v1',0)
            background=[pool[(cursor+i)%len(pool)] for i in range(min(2,len(pool)))]
            selected=priority+background
            try:
                results = await self.company.refresh(selected, now)
                for event in self.company.events:self.repo.record('news_events',event)
                for result in results:
                    row=result.to_dict();row.update(id=row['analysis_id'],category='company')
                    self.repo.record('news_analysis',row);self._analyses[row['id']]=row
                issues.extend(self.company.status)
                self.schedule.success('company_cycle',len(results),now)
                self.repo.set_control('news_background_cursor_v1',cursor+len(background))
            except (ValueError, RuntimeError, TypeError, OSError, httpx.HTTPError) as exc:
                self.schedule.failure('company_cycle',exc,now=now)
                issues.append({'provider':'company','code':'company_refresh_failed'})
        articles=[]
        for provider,client in (('official',self.official),('treasury',self.treasury),('gdelt',self.gdelt)):
            key='macro:'+provider
            if not self.schedule.claim(key,macro_interval,now):continue
            try:
                values=await client.fetch(now=now) if provider=='gdelt' else await client.fetch()
                articles.extend(values);issues.extend(getattr(client,'status',[]))
                self.schedule.success(key,len(values),now)
            except (ValueError,RuntimeError,TypeError,OSError,httpx.HTTPError) as exc:
                self.schedule.failure(key,exc,now=now)
                issues.append({'provider':provider,'code':'macro_refresh_failed'})
        # 기업 요청과 독립된 시장 보조 뉴스. 무료 권한의 최근 15분은 요청하지 않는다.
        alpaca=getattr(self.company,'alpaca_client',None)
        if alpaca and getattr(self.company,'include_alpaca',False) and self.schedule.claim('macro:alpaca',macro_interval,now):
            try:
                events=await alpaca.fetch(None,start=now-timedelta(hours=self.macro_max_age_hours),end=now-timedelta(minutes=15),max_pages=1,skip_invalid=True)
                for event in events:
                    articles.append(MacroArticle(event.url,event.title,event.body,event.source,event.published_at,event.received_at,
                        discovery_source='alpaca',source_family=event.source))
                self.schedule.success('macro:alpaca',len(events),now)
            except (ValueError,RuntimeError,TypeError,OSError,httpx.HTTPError) as exc:
                self.schedule.failure('macro:alpaca',exc,now=now)
                issues.append({'provider':'alpaca_macro','code':'auxiliary_news_unavailable'})
        candidates=[]
        for article in articles:
            if article.discovery_source=='gdelt':
                # 발견 자체도 먼저 보존한다. 원문 실패로 발견 기록을 잃지 않는다.
                archive(self.repo,article.to_dict(),provider='gdelt',reason='discovered_only',body_allowed=False)
                self.repo.record('macro_news',{**article.to_dict(),'category':'macro','status':'DISCOVERED_ONLY'})
                result,enriched=await self.enricher.enrich(article)
                if enriched is None:
                    if hasattr(self.analyzer,'record_skip'):self.analyzer.record_skip(article,result['status'])
                    issues.append({'provider':'gdelt_enrichment','code':result['status']});continue
                article=enriched
            reason=macro_reason(article,max(now,utc(self.clock())),self.macro_max_age_hours)
            receipt=archive(self.repo,article.to_dict(),provider=article.discovery_source,reason=reason,body_allowed=article.discovery_source!='gdelt')
            completed=self.repo.get_control(self._completed_key(article),False)
            if reason or (receipt['duplicate'] and completed):
                precheck=self.analyzer.record_skip(article,reason or 'duplicate_article') if hasattr(self.analyzer,'record_skip') else None
                analysis={'id':'macro-analysis:excluded:'+stable_hash([article.id,article.content_hash,reason]),
                    'article_id':article.id,'published_at':article.published_at.isoformat() if article.published_at else None,
                    'received_at':article.received_at.isoformat(),'analyzed_at':utc(self.clock()).isoformat(),
                    'category':'macro','error':reason or 'duplicate_article','status':'UNKNOWN','force_liquidation':False,
                    'precheck_id':precheck,'llm_call_ids':[]}
                self._store_macro(article,analysis);continue
            candidates.append(article)
        # 시간창 검사가 공식성보다 앞선다. 최근 2시간을 우선하며 과거 24~72시간은 배경이다.
        candidates.sort(key=lambda a:((now-a.published_at).total_seconds()<=7200,a.published_at,a.official),reverse=True)
        for article in candidates[:40]:
            analysis=await self.analyzer.analyze_macro(article,now=max(now,utc(self.clock())))
            if now-article.published_at>timedelta(hours=24):analysis={**analysis,'context_only':True}
            self._store_macro(article,analysis)
        if self.calendar and self.schedule.claim('calendar_cycle',calendar_interval,now):
            try:
                values=await self.calendar.fetch()
                for event in values:self.add_economic_event(event)
                issues.extend(self.calendar.status);self.schedule.success('calendar_cycle',len(values),now)
            except (ValueError,RuntimeError,TypeError,OSError,httpx.HTTPError) as exc:
                self.schedule.failure('calendar_cycle',exc,now=now)
                issues.append({'provider':'calendar','code':'calendar_unavailable'})
        if self.schedule.claim('fred_daily',86400,now):
            try:
                values=await self.fred.fetch()
                for row in values:
                    self.repo.record('macro_indicators',row)
                    from ..market_reference.contracts import identity
                    receipt=utc(self.clock()).isoformat()
                    self.repo.record('fred_reference_observations',{**row,'id':'fred-received:'+identity({'row':row,'ingested_at':receipt}),
                        'ingested_at':receipt,'purpose':'OFFICIAL_DAILY_BASELINE','source':'FRED','latency_class':'DAILY',
                        'publication_time_status':'UNKNOWN' if row.get('published_at') is None else 'PROVIDED'})
                self.schedule.success('fred_daily',len(values),now)
            except (ValueError,RuntimeError,TypeError,OSError,httpx.HTTPError) as exc:
                self.schedule.failure('fred_daily',exc,now=now)
                issues.append({'provider':'fred','code':'daily_context_unavailable'})
        self.status={'status':'AVAILABLE' if any(not r.get('error') and self._recent(r,now) for r in self._analyses.values()) else 'UNKNOWN',
            'updated_at':utc(self.clock()).isoformat(),'issues':issues,'budget':self.analyzer.ledger.snapshot(now),
            'force_liquidation':False,'llm_health':self.repo.get_control('llm_provider_health',{}),
            'schedule':self.schedule.state,'statistics':statistics(self.repo.recent('news_ingestions',limit=2000),now),
            'intervals':{'company':company_interval,'macro':macro_interval,'calendar':calendar_interval,'fred':86400}}
        self.repo.set_control('session_news_status',self.status)
        from .context import build_context
        context_at=max(now,utc(self.clock()))
        snapshot=build_context(self.repo,context_at,analyses=self._analyses.values(),events=self.economic_events_at(context_at))
        self.repo.record('macro_context_snapshots',snapshot)
        if self.context_analyzer.settings.enabled:
            try:
                await self.context_analyzer.analyze(snapshot,candidates,self._analyses.values(),
                    market_regime=self.repo.get_control('sessionquant_context',{}).get('market_regime'),now=context_at)
            except (RuntimeError,OSError,ValueError,TypeError):
                self.repo.log('WARNING','거시 맥락 LLM 기록 미확인; 기존 시장·청산 관리는 계속합니다.')
        self._analyses={key:row for key,row in self._analyses.items() if self._recent(row,utc(self.clock()))}
        return self.status

    @staticmethod
    def _recent(row, now):
        try:
            published = row.get("published_at") or row.get("event", {}).get("published_at")
            completed = utc(row["analyzed_at"])
            return (not row.get("context_only") and published is not None and utc(published) <= completed <= now
                    and (now - utc(published)).total_seconds() <= 24 * 3600)
        except (ValueError, TypeError, KeyError, AttributeError):
            return False

    def recorded_evidence(self,now,symbol):
        """당시 위험 조회에 사용 가능한 분석 버전만 기록하며 재분석하지 않는다."""
        from ..portfolio.models import serialize
        rows = [row for row in self._analyses.values() if self._recent(row,now)
                and (row.get('category') != 'company' or row.get('ticker') == symbol)]
        analyses = []
        for row in rows:
            event = row.get('event') or {}
            analyses.append({key:row.get(key) for key in ('id','model','prompt_version','analyzed_at','error')} | {
                'published_at':row.get('published_at') or event.get('published_at'),
                'received_at':row.get('received_at') or event.get('received_at')})
        return serialize({'analyses':analyses,'economic_events':[event for event in self.economic_events_at(now) if event.blackout(now)]})

    def risk_context(self, now: datetime, symbol: str | None = None) -> dict:
        now = utc(now)
        reasons, major_adverse, available = [], False, False
        blackout = [event for event in self.economic_events_at(now) if event.blackout(now)]
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
        from ..news.ranking import company_score
        return {"status": "AVAILABLE" if available or blackout else "UNKNOWN", "block_entries": bool(reasons),
                "major_adverse": major_adverse, "blackout": bool(blackout), "reasons": reasons,
                "force_liquidation": False,'candidate_score_signal':company_score(self._analyses.values(),symbol,now),
                'macro_score_applied':False}

    async def aclose(self):
        for service in (self.company, self.official, self.treasury, self.gdelt, self.fred, self.calendar, self.enricher):
            if service is None:continue
            await service.aclose()
