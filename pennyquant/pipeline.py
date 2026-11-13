"""Market-first runner: observe candidates, analyze primary evidence, then trade.

The orchestration never upgrades Toss's incomplete market observations into a
verified scanner snapshot. Complete external market events can be supplied by
an independently verified producer through the normalized JSONL interface.
"""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import datetime
from datetime import timedelta
from hashlib import sha256
import json
from pathlib import Path

import httpx

from .collectors.common import CapabilityUnavailable, DataQualityError, now_utc
from .collectors.market_scanner import Candidate, MarketScanner
from .collectors.toss_market import TossMarketClient
from .config.settings import Settings
from .database.repository import Repository
from .events import Event, parse_event
from .models import StockSnapshot, utc
from .monitor import _observations
from .news.event_filter import positive_event
from .pipeline_config import PipelineConfig
from .scanners.stock_scanner import scan


def _safe_error(exc: Exception, stage: str) -> dict:
    result = {"stage": stage, "code": "provider_unavailable", "message": "Provider data could not be validated; entries remain restricted"}
    if isinstance(exc, httpx.HTTPStatusError):
        result.update(code="http_error", http_status=exc.response.status_code)
    elif isinstance(exc, httpx.RequestError):
        result["code"] = "network_error"
    elif isinstance(exc, CapabilityUnavailable):
        result["code"] = "capability_unavailable"
    elif isinstance(exc, (ValueError, TypeError, KeyError, AttributeError)):
        result["code"] = "invalid_data"
    return result


class EventTail:
    """Consume complete lines only, reject replaced/truncated consumed history."""
    def __init__(self, path: Path, state: dict | None = None):
        self.path = Path(path)
        self.offset = int((state or {}).get("offset", 0))
        self.prefix_hash = (state or {}).get("prefix_hash", sha256(b"").hexdigest())
        self.last_time = utc(state["last_time"]) if state and state.get("last_time") else None

    def read(self, limit: int = 1000) -> list[Event]:
        if not self.path.exists():
            if self.offset:
                raise DataQualityError("Normalized producer file disappeared after consumption")
            return []
        events = []
        with self.path.open("rb") as stream:
            prefix = sha256()
            remaining = self.offset
            while remaining:
                chunk = stream.read(min(remaining, 1024 * 1024))
                if not chunk:
                    raise DataQualityError("Normalized producer file was truncated")
                prefix.update(chunk)
                remaining -= len(chunk)
            if prefix.hexdigest() != self.prefix_hash:
                raise DataQualityError("Consumed normalized producer data was changed")
            for _ in range(limit):
                line = stream.readline(1024 * 1024 + 1)
                if not line:
                    break
                if len(line) > 1024 * 1024:
                    raise DataQualityError("Normalized event exceeds the one-megabyte line limit")
                if not line.endswith(b"\n"):
                    break  # The producer has not finished this record yet.
                if line.strip():
                    try:
                        event = parse_event(json.loads(line.decode("utf-8")))
                    except (ValueError, TypeError, KeyError, AttributeError, UnicodeError):
                        raise DataQualityError("Normalized producer contains an invalid event") from None
                    if self.last_time is not None and event.available_at < self.last_time:
                        raise DataQualityError("Normalized events are out of receipt order")
                    self.last_time = event.available_at
                    events.append(event)
                prefix.update(line)
                self.offset += len(line)
        self.prefix_hash = prefix.hexdigest()
        return events

    def state(self) -> dict:
        return {"offset": self.offset, "prefix_hash": self.prefix_hash,
                "last_time": self.last_time.isoformat() if self.last_time else None}


def _analysis_dict(value) -> dict:
    result = value.to_dict() if hasattr(value, "to_dict") else value
    if not isinstance(result, dict):
        raise DataQualityError("News service returned an invalid analysis")
    return result


def _held_symbols(engine) -> list[str]:
    manager = getattr(engine, "manager", None)
    account = getattr(manager, "account", None)
    return list(getattr(account, "positions", {}))


def _halt_reasons(engine) -> list[str]:
    return sorted(getattr(getattr(engine, "manager", None), "halt_reasons", set()))


def _candidate_readiness(candidate: Candidate, engine, analyses: dict, now: datetime, settings: Settings) -> None:
    original = [item for item in candidate.blockers if item in {"ranking_time_unknown", "ranking_stale_or_future"}]
    snapshot = getattr(engine, "snapshots", {}).get(candidate.symbol)
    market_reasons = ["verified_market_snapshot_required"]
    if isinstance(snapshot, StockSnapshot):
        accepted, market_reasons = scan(snapshot, now, settings)
        if accepted:
            market_reasons = []
    primary = analyses.get(candidate.symbol)
    approved = positive_event(list(getattr(engine, "news", {}).values()), candidate.symbol, now, settings,
                              getattr(engine, "analyses", {}))
    news_reasons = [] if approved is not None else ["primary_news_analysis_required"]
    if primary and (primary.get("sentiment") == "NEGATIVE" or primary.get("dilution_risk")):
        news_reasons = ["negative_news_or_dilution_risk"]
    halts = [f"halt:{reason}" for reason in _halt_reasons(engine)]
    candidate.blockers = list(dict.fromkeys(original + market_reasons + news_reasons + halts))
    candidate.readiness = "BLOCKED" if halts or "negative_news_or_dilution_risk" in news_reasons else (
        "WATCH" if candidate.blockers else "READY")


async def run_system(config: PipelineConfig, repository: Repository, *, engine=None, settings: Settings | None = None,
                     market=None, news_service=None, max_cycles: int | None = None,
                     clock: Callable[[], datetime] = now_utc,
                     sleep: Callable[[float], Awaitable] = asyncio.sleep) -> dict:
    """Run with caller-owned DB lock/engine; injected providers remain caller-owned.

    LLM/network news work runs as a separate task so outstanding analysis cannot
    suspend market polling or held-position management. The caller must provide
    a live-enabled engine to request a live run; this function never enables one.
    """
    if max_cycles is not None and (isinstance(max_cycles, bool) or not isinstance(max_cycles, int) or max_cycles < 1):
        raise ValueError("max_cycles must be a positive integer or None")
    if repository.get_control("runtime_kind") not in (None, "market_pipeline"):
        raise ValueError("This database belongs to another runtime")
    if repository.get_control("pipeline_mode") not in (None, config.mode):
        raise ValueError("This database belongs to another pipeline mode")
    if config.mode == "live" and (engine is None or getattr(getattr(engine, "manager", None), "execution_mode", "paper") != "live"):
        raise CapabilityUnavailable("Live pipeline requires an explicitly verified broker engine")
    settings = settings or getattr(engine, "settings", None) or Settings()
    if engine is None:
        from .engine import EventEngine
        engine = EventEngine(settings, config.strategy, repository, restore=repository.load_state() is not None, live=True)
    binding = {"input_event_path": str(config.input_event_path.resolve()) if config.input_event_path else None,
               "discover_with_toss": config.discover_with_toss}
    previous = repository.get_control("pipeline_input_binding")
    if previous is not None and previous != binding:
        raise ValueError("Pipeline market source changed; use a separate database")
    repository.set_control("pipeline_input_binding", binding)
    repository.set_control("runtime_kind", "market_pipeline")
    repository.set_control("pipeline_mode", config.mode)
    repository.set_control("source", {"synthetic": False, "label": "Market-first primary news pipeline; incomplete inputs remain WATCH"})
    owns_market = market is None and config.discover_with_toss
    owns_news = news_service is None
    if market is None and config.discover_with_toss:
        market = TossMarketClient()
    scanner = MarketScanner(market, config, clock=clock)
    if news_service is None:
        from .news.analysis import NewsAnalyzer
        from .news.service import NewsService
        news_service = NewsService(config.companies, include_alpaca=config.include_alpaca,
                                   analyzer=NewsAnalyzer(max_age_hours=settings.primary_news_max_age_hours))
    tail = EventTail(config.input_event_path, repository.get_control("pipeline_feed_cursor")) if config.input_event_path else None
    candidates: dict[str, Candidate] = {}
    analyses: dict[str, dict] = {}
    # Preserve display/entry context on restart; engine restores its own registry.
    for row in repository.all("news_analysis"):
        if row.get("ticker"):
            analyses[row["ticker"]] = row
    status = {"status": "starting", "mode": config.mode, "strategy": config.strategy,
              "last_cycle_at": None, "last_event": None, "max_positions": settings.max_positions,
              "daily_loss_pct": settings.daily_loss_pct, "halt_reasons": [], "live_orders_enabled": False,
              "interval_seconds": config.poll_seconds, "cycles": 0, "analysis_count": 0, "error": None}
    status["llm_model"] = getattr(getattr(news_service, "analyzer", None), "model", "")
    status["fee_summary"] = (f"{settings.fee_model} · 순손익 손절 {settings.stop_pct:.1%} / "
                             f"기본 익절 목표 {settings.take_profit_pct:.1%} · "
                             f"조기 청산 {'사용' if settings.early_exit_enabled else '중지'} · "
                             f"예상 슬리피지 {settings.slippage_bps:g}bps")
    repository.set_control("pipeline_status", status)
    next_rankings = next_news = None
    news_task = None
    analysis_seen = {row.get("id") or row.get("analysis_id") for row in repository.all("news_analysis")}
    latest_bar: dict[str, datetime] = {}
    quote_cursor = 0

    def ingest_analyses(results):
        for analysis in results:
            try:
                row = _analysis_dict(analysis)
                row["id"] = row.get("id") or row.get("analysis_id")
                if not row["id"]:
                    raise DataQualityError("News analysis requires a stable ID")
                if row["id"] in analysis_seen:
                    continue
                repository.record("news_analysis", row)
                if row.get("ticker"):
                    old = analyses.get(row["ticker"])
                    # Supplemental Alpaca context must not replace an eligible
                    # primary result on the candidate readiness screen.
                    is_risk = row.get("sentiment") in {"NEGATIVE", "MIXED"} or row.get("dilution_risk")
                    if not old or is_risk or row.get("entry_eligible") or not old.get("entry_eligible"):
                        analyses[row["ticker"]] = row
                event = getattr(analysis, "event", None)
                associated = row.get("ticker") in getattr(event, "symbols", ())
                if associated and callable(getattr(engine, "register_analysis", None)):
                    engine.register_analysis(analysis)
                analysis_seen.add(row["id"])
                status["analysis_count"] += 1
            except Exception as exc:
                repository.set_control("news_provider_status", [_safe_error(exc, "analysis_registration")])
                manager = getattr(engine, "manager", None)
                if manager is not None:
                    manager.halt("news_analysis_registration_error")

    def drain_news():
        nonlocal news_task
        # The service updates this mapping after each completed article. A
        # negative result must not wait behind the remaining candidates' LLMs.
        incremental = getattr(news_service, "analyses", {})
        if isinstance(incremental, dict):
            ingest_analyses(list(incremental.values()))
        if news_task is None or not news_task.done():
            return
        try:
            ingest_analyses(news_task.result())
            issues = getattr(news_service, "status", [])
            repository.set_control("news_provider_status", issues)
        except Exception as exc:
            repository.set_control("news_provider_status", [_safe_error(exc, "news_analysis")])
        finally:
            news_task = None

    def market_problem(exc: Exception, stage: str):
        problem = _safe_error(exc, stage)
        status["error"] = problem
        repository.set_control("market_provider_status", problem)
        manager = getattr(engine, "manager", None)
        if manager is not None:
            manager.halt(f"{stage}_error")

    def market_recovered(stage: str):
        manager = getattr(engine, "manager", None)
        if manager is not None:
            manager.resume(f"{stage}_error")

    try:
        while max_cycles is None or status["cycles"] < max_cycles:
            now = utc(clock())
            if callable(getattr(engine, "apply_runtime_controls", None)):
                engine.apply_runtime_controls(now)
            drain_news()
            # Verified producers are authoritative; never mix their market feed
            # with incomplete Toss observations for trading/fill decisions.
            if tail is not None:
                with repository.transaction():
                    for event in tail.read():
                        engine.process(event)
                        status["last_event"] = event.available_at.isoformat()
                        if event.kind == "snapshot":
                            snapshot = event.data
                            symbol = snapshot.quote.symbol
                            if symbol not in candidates and len(candidates) < config.max_candidates:
                                candidates[symbol] = Candidate(symbol, snapshot.quote.ask, None,
                                    snapshot.cumulative_volume, snapshot.dollar_volume, snapshot.quote.timestamp,
                                    snapshot.quote.received_at, ["verified_producer_candidate"], first_seen_at=now,
                                    source="normalized_producer")
                    repository.set_control("pipeline_feed_cursor", tail.state())
            if config.discover_with_toss:
                if next_rankings is None or now >= next_rankings:
                    previous_candidates = candidates
                    try:
                        discovered = await scanner.discover(now)
                    except Exception as exc:
                        market_problem(exc, "market_rankings")
                    else:
                        retained = {symbol: previous_candidates[symbol] for symbol in _held_symbols(engine)
                                    if symbol in previous_candidates}
                        retained.update({item.symbol: item for item in discovered})
                        candidates = dict(list(retained.items())[:config.max_candidates])
                        market_recovered("market_rankings")
                    next_rankings = now + timedelta(seconds=config.rankings_refresh_seconds)
                symbols = list(dict.fromkeys(_held_symbols(engine) + list(candidates) + list(config.symbols)))[:20]
                if symbols:
                    try:
                        payload = await market.prices(symbols)
                        received = utc(clock())
                        rows, missing, invalid = _observations(payload, symbols, received.isoformat())
                    except Exception as exc:
                        market_problem(exc, "market_prices")
                        rows, missing, invalid = [], symbols, []
                    for row in rows:
                        repository.record("price_samples", {**row, "id": None})
                        repository.record("stocks", row)
                        if row["symbol"] in candidates:
                            candidates[row["symbol"]].last_price = float(row["last_price"])
                    if missing or invalid:
                        repository.set_control("market_provider_status", {"code": "partial_prices", "missing_symbols": missing, "invalid_symbols": invalid})
                    else:
                        market_recovered("market_prices")
                    try:
                        bars = await scanner.candle_updates(symbols)
                    except Exception as exc:
                        market_problem(exc, "market_candles")
                        bars = []
                    else:
                        market_recovered("market_candles")
                    for bar in bars:
                        if bar.timestamp <= latest_bar.get(bar.symbol, datetime.min.replace(tzinfo=bar.timestamp.tzinfo)):
                            continue
                        latest_bar[bar.symbol] = bar.timestamp
                        if tail is None:
                            engine.process(Event("bar", bar))
                        else:
                            repository.record("market_bars", bar)
                    held = _held_symbols(engine)
                    other = [symbol for symbol in symbols if symbol not in held]
                    requested = held + [other[(quote_cursor + i) % len(other)]
                                        for i in range(min(config.quote_candidates_per_cycle, len(other)))]
                    quote_cursor += config.quote_candidates_per_cycle
                    quote_failed = False
                    for symbol in requested:
                        try:
                            quote = await market.get_quote(symbol)
                        except Exception as exc:
                            market_problem(exc, "market_quotes")
                            quote_failed = True
                            continue
                        if tail is None:
                            engine.process(Event("quote", quote))
                            status["last_event"] = quote.received_at.isoformat()
                        else:
                            repository.record("quote_snapshots", quote)
                    if not quote_failed:
                        market_recovered("market_quotes")
            if next_news is None or now >= next_news:
                if news_task is None:
                    symbols = list(dict.fromkeys(_held_symbols(engine) + list(candidates)))[:20]
                    if symbols:
                        news_task = asyncio.create_task(news_service.refresh(symbols, now=utc(clock())))
                    next_news = now + timedelta(seconds=config.news_refresh_seconds)
            # Give a ready/injected news service an opportunity to finish without
            # waiting for real LLM work or interrupting position management.
            await asyncio.sleep(0)
            drain_news()
            if callable(getattr(engine, "apply_runtime_controls", None)):
                engine.apply_runtime_controls(utc(clock()))
            manager = getattr(engine, "manager", None)
            if callable(getattr(manager, "drain", None)):
                await manager.drain()
            if callable(getattr(engine, "persist", None)):
                engine.persist()
            for candidate in candidates.values():
                _candidate_readiness(candidate, engine, analyses, utc(clock()), settings)
                repository.record("candidates", candidate.to_dict())
            status.update(status="running", cycles=status["cycles"] + 1,
                          last_cycle_at=utc(clock()).isoformat(), halt_reasons=_halt_reasons(engine),
                          live_orders_enabled=bool(getattr(engine, "live_orders_enabled", False)),
                          active_symbols=list(candidates), candidate_count=len(candidates))
            repository.set_control("pipeline_status", status)
            if max_cycles is None or status["cycles"] < max_cycles:
                await sleep(config.poll_seconds)
        status["status"] = "stopped"
        return dict(status)
    except asyncio.CancelledError:
        status["status"] = "stopped"
        raise
    except Exception as exc:
        status.update(status="error", error=_safe_error(exc, "pipeline"), halt_reasons=_halt_reasons(engine))
        manager = getattr(engine, "manager", None)
        if manager is not None:
            manager.halt("pipeline_data_error")
        raise RuntimeError("Market-first pipeline stopped; inspect its sanitized provider status") from None
    finally:
        if news_task is not None:
            news_task.cancel()
            await asyncio.gather(news_task, return_exceptions=True)
        repository.set_control("pipeline_status", status)
        if owns_news:
            await news_service.aclose()
        if owns_market and market is not None:
            await market.aclose()
