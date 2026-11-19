"""로컬 모의 운영, 기록 재생, 뉴스 배경 작업 및 대시보드 실행."""
from __future__ import annotations

import asyncio
from contextlib import ExitStack
from dataclasses import asdict
from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import subprocess
import sys
import time

from .database.repository import Repository
from .locking import exclusive_lock
from .market.source import TossSessionSource
from .market.universe import UniverseStore
from .models import jsonable, utc
from .notifications import DesktopNotifier, dashboard_environment
from .portfolio.models import PortfolioSettings
from .session_config import SessionConfig
from .session_engine import SessionEngine, event_time


def bind_runtime(repo, *, source: str, identity: str, synthetic: bool = False):
    expected = {"version": 2, "environment": "PAPER_LOCAL", "source": source,
                "identity": identity, "synthetic": synthetic, "live_orders_enabled": False}
    previous = repo.get_control("sq_runtime")
    if previous is not None and previous != expected:
        raise ValueError("저장된 실행 자료 또는 모드가 다릅니다. 별도 DB를 사용하세요.")
    if repo.load_state() and previous is None:
        raise ValueError("기존 장부를 새 실행으로 덮어쓸 수 없습니다.")
    repo.set_control("sq_runtime", expected)


def _open(repo_path, backend=None):
    """DB 연결보다 먼저 파일 잠금을 잡고 종료 시 역순으로 해제한다."""
    stack = ExitStack()
    try:
        stack.enter_context(exclusive_lock(str(repo_path) + ".engine.lock", message="다른 엔진이 이 실행 공간을 사용 중입니다."))
        repo = Repository(repo_path, backend=backend)
        stack.callback(repo.close)
        stack.enter_context(repo.engine_lease())
        return stack, repo
    except BaseException:
        stack.close()
        raise


def replay_session(input_path: Path, db_path: Path, universe_path: Path, *, strategy="pullback", resume=False,
                   synthetic=False, backend=None, settings=None, use_news=True, use_market_filter=True, watchlist_history=None, exit_policy=None):
    tape = input_path.read_bytes()
    identity = sha256(tape + b"\x00" + universe_path.read_bytes()).hexdigest()
    records = [json.loads(line) for line in tape.splitlines() if line.strip()]
    previous = None
    for raw in records:
        available = event_time(raw)
        if previous and available < previous:
            raise ValueError("재생 자료는 실제 수신 시각 순서로 정렬되어야 합니다.")
        previous = available
    if not records:
        raise ValueError("재생할 이벤트가 없습니다.")
    universe = UniverseStore(universe_path)
    if not universe.members(event_time(records[0])):
        raise ValueError("첫 이벤트 당시 확인 가능한 구성 종목 이력이 없습니다. 현재 목록을 과거에 적용하지 않습니다.")
    stack, repo = _open(db_path, backend)
    with stack:
        if repo.load_state() and not resume:
            raise ValueError("기존 장부를 복구하려면 --resume 또는 새로운 --db를 지정하세요.")
        bind_runtime(repo, source="replay", identity=identity, synthetic=synthetic)
        watchlist_policy = None
        if watchlist_history is not None:
            from .watchlist.selection import HistoricalWatchlistPolicy
            history = json.loads(Path(watchlist_history).read_text(encoding='utf-8-sig'))
            watchlist_policy = HistoricalWatchlistPolicy(history, universe, allow_synthetic=synthetic)
            digest = sha256(Path(watchlist_history).read_bytes()).hexdigest()
            previous_digest = repo.get_control('watchlist_replay_history_digest')
            if previous_digest is not None and previous_digest != digest:
                raise ValueError('감시목록 재생 이력이 변경됐습니다. 별도 실행 공간을 사용하세요.')
            repo.set_control('watchlist_replay_history_digest', digest)
        elif repo.get_control('watchlist_replay_history_digest'):
            raise ValueError('복구할 때 이전 감시목록 이력을 다시 지정하세요.')
        engine = SessionEngine(repo, universe, strategy=strategy, restore=resume, synthetic=synthetic,
                               settings=settings, use_news=use_news, use_market_filter=use_market_filter, watchlist_policy=watchlist_policy,exit_policy=exit_policy)
        index = engine.cursor
        if type(index) is not int or not 0 <= index <= len(records):
            raise ValueError("저장된 재생 커서가 올바르지 않습니다.")
        while index < len(records):
            now = event_time(records[index])
            end = index + 1
            while end < len(records) and event_time(records[end]) == now:
                end += 1
            engine.apply_batch(records[index:end], now, cursor=end)
            index = end
        return {"state": engine.broker.to_state(), "context": repo.get_control("sessionquant_context"),
                "fills": jsonable(engine.broker.fills), "orders": jsonable(list(engine.broker.orders.values())),
                "curve": repo.all("account_snapshots"), "regimes": repo.all("market_regimes"),
                'risk_decisions':[row for row in repo.all('pipeline_status') if row.get('kind') == 'risk_decision']}


def batch_events(batch, *, delivered_at: datetime | None = None,
                 last_time: datetime | None = None, delayed_quotes: bool = False) -> list[dict]:
    """Keep late bars/listing evidence without refreshing their source clocks.

    REST keeps its legacy watermark behavior. Stream mode explicitly admits late
    quotes at delivery time; execution still checks original clocks and book IDs.
    """
    result = [{"kind": "bar", "data": jsonable(x)} for x in batch.bars]
    result += [{"kind":"bar","data":jsonable(x),"restored_at":jsonable(batch.observed_at)}
               for x in getattr(batch,'cached_bars',())]
    result += [{"kind": "quote", "data": jsonable(x)} for x in batch.quotes]
    result += [{"kind": "eligibility", "data": jsonable(x)} for x in batch.eligibility]
    if last_time is not None:
        retained = []
        for raw in result:
            if event_time(raw) < last_time:
                if raw['kind'] not in {'bar', 'eligibility'} and not (delayed_quotes and raw['kind']=='quote'):
                    continue
                if delivered_at is None:
                    raise ValueError('지연 자료에는 실제 전달 시각이 필요합니다.')
                raw = {**raw, 'delivered_at': utc(delivered_at).isoformat()}
            retained.append(raw)
        result = retained
    return sorted(result, key=event_time)


async def run_session(config: SessionConfig, *, no_dashboard=False, port=8501, max_cycles=None,
                      desktop_notifications=True):
    from .market_shutdown import with_graceful_sigterm
    return await with_graceful_sigterm(lambda: _run_session(config, no_dashboard=no_dashboard,
        port=port, max_cycles=max_cycles, desktop_notifications=desktop_notifications))


async def _run_session(config: SessionConfig, *, no_dashboard=False, port=8501, max_cycles=None,
                       desktop_notifications=True):
    if max_cycles is not None and (type(max_cycles) is not int or max_cycles < 1):
        raise ValueError("max_cycles는 1 이상이어야 합니다.")
    if type(port) is not int or not 1024 <= port <= 65535:
        raise ValueError("대시보드 포트는 1024~65535여야 합니다.")
    os.environ["SQ_DB_BACKEND"] = config.database_backend
    os.environ["SQ_ENVIRONMENT"] = config.environment
    universe = UniverseStore(config.universe_path)
    now = datetime.now(timezone.utc)
    if not universe.members(now):
        if config.source == "file":
            raise ValueError("유효한 S&P 500 구성 이력이 없습니다. universe-refresh 또는 출처가 있는 이력을 준비하세요.")
        await universe.refresh_public()
    stack, repo = _open(config.db_path, config.database_backend)
    market = news = market_task = news_task = account_task = dashboard = None
    explainer = explanation_task = explanation_owner = None
    quote_feed = quote_task = quote_delivery = None
    quote_batch_seconds=0.
    notifier = DesktopNotifier(enabled=desktop_notifications)
    engine = None
    watchlist = None
    with stack:
        try:
            identity = str(config.input_path.resolve()) if config.input_path else ("toss-readonly-websocket-v1" if config.quote_transport=="websocket" else "toss-readonly")
            bind_runtime(repo, source=config.source, identity=identity)
            if config.news_enabled:
                from .macro.models import LLMSettings
                from .macro.service import NewsPipeline
                news = NewsPipeline(repo, companies=config.companies, settings=LLMSettings.from_dict(config.llm),
                                    include_alpaca=config.include_alpaca,
                                    macro_refresh_seconds=config.macro_news_refresh_seconds,
                                    company_refresh_seconds=config.company_news_refresh_seconds,
                                    calendar_refresh_seconds=config.economic_calendar_refresh_seconds,
                                    company_max_age_hours=config.company_new_analysis_max_age_hours,
                                    macro_max_age_hours=config.macro_new_analysis_max_age_hours,
                                    calendar_enabled=True,enrichment_policies=config.news_enrichment.get('publishers',{}),
                                    weekend_slowdown=config.news_weekend_slowdown,
                                    enrichment_min_chars=config.news_enrichment.get('min_chars',300),
                                    enrichment_min_sentences=config.news_enrichment.get('min_sentences',3),
                                    audit_settings=config.llm_debug_audit,macro_context_llm=config.macro_context_llm,
                                    environment=config.environment)
            from .trading_controls import begin_session
            control_state=begin_session(repo,mode="paper")
            from .trading_controls import finish_session
            stack.callback(finish_session,repo,control_state)
            if config.watchlist_enabled:
                from .watchlist.selection import WatchlistManager
                from .watchlist.history_selection import DailyHistorySelection
                if config.source == 'toss' and config.strategy != 'pullback_simple':control_state['data_ready'] = False
                watchlist = WatchlistManager(repo, universe, config,
                    history_selection=DailyHistorySelection(repo,startup_pending=True) if config.source == 'toss' and config.strategy != 'pullback_simple' else None)
            from .market_reference.risk import MacroReferenceRiskMonitor
            reference_risk = MacroReferenceRiskMonitor(repo,config.macro_reference_filter)
            engine = SessionEngine(repo, universe, strategy=config.strategy, settings=PortfolioSettings(**config.portfolio),
                                   restore=bool(repo.load_state()), risk_provider=news,control_state=control_state, watchlist_policy=watchlist,
                                   strict_market_data=True, use_market_filter=config.market_filter_enabled, macro_reference_risk=reference_risk, record_inputs=config.recording_enabled,
                                   candidate_settings=config.candidate_selection,paper_data_policy=config.paper_data_policy)
            if config.recording_enabled:
                from .research.journal import start,stop
                start(repo,engine,source=config.source)
                stack.callback(stop,repo)
            for snapshot in universe.snapshots:
                repo.record("universe_history", snapshot)
            if config.source == "toss":
                from .market.history import HistoryStore
                history_store = HistoryStore(repo) if watchlist is not None and config.strategy != "pullback_simple" else None
                warmup = None
                if history_store is not None:
                    from .market.warmup import StartupWarmup
                    warmup = StartupWarmup(repo,history_store,watchlist.history_selection,control_state)
                    engine.market.history_provider = lambda symbol,stamp: history_store.load(symbol,stamp,record=config.recording_enabled)
                def receive_priority(batch):
                    # 위험 자료는 후보 탐색 완료를 기다리지 않고 단일 작성자 엔진에 전달한다.
                    observed_at = datetime.now(timezone.utc)
                    raw = batch_events(batch, delivered_at=observed_at, last_time=engine.last_time,
                        delayed_quotes=config.quote_transport=="websocket")
                    engine.apply_batch(raw,observed_at)
                from .market_reference.collector import MarketReferenceMonitor
                from .market_reference.settings import reference_settings
                reference_config = reference_settings(config.market_reference)
                reference = MarketReferenceMonitor(repo,clock=lambda:datetime.now(timezone.utc),
                    price_seconds=reference_config['price_seconds'],candle_seconds=reference_config['candle_seconds'],
                    calls_per_cycle=reference_config['calls_per_cycle']) if reference_config['enabled'] else None
                market = TossSessionSource(candidates_per_cycle=config.symbols_per_cycle, watchlist=watchlist,
                    max_api_calls=config.max_api_calls_per_cycle, light_refresh_seconds=config.light_refresh_seconds,
                    ranking_refresh_seconds=config.ranking_refresh_seconds,max_cycle_seconds=config.max_cycle_seconds,
                    history_pages_per_cycle=config.history_pages_per_cycle,on_priority_batch=receive_priority,reference_monitor=reference,
                    history_store=history_store,watchlist_candles_per_cycle=config.watchlist_candles_per_cycle,warmup=warmup,
                    include_benchmarks=config.market_filter_enabled or config.focus_selection!='turnover' or reference_config['enabled'],
                    current_session_only=config.strategy=='pullback_simple')
                if config.quote_transport == 'websocket':
                    from .collectors.toss_quote_feed import TossQuoteFeed
                    from .market.live_quotes import LiveQuoteDelivery
                    quote_feed=TossQuoteFeed(market.client,max_age_seconds=min(
                        engine.broker.settings.max_quote_age_seconds,engine.market.max_quote_age_seconds))
                    quote_delivery=LiveQuoteDelivery(quote_feed)
                    market.quote_feed=quote_feed
                    engine.quote_entry_guard=lambda symbol,stamp: quote_feed.latest(symbol,stamp) is not None
                    quote_task=asyncio.create_task(quote_feed.run())
                if hasattr(market, 'restore_seen'):
                    market.restore_seen(engine.market.bars)
            if not no_dashboard:
                app = Path(__file__).parent / "dashboard" / "session_app.py"
                dashboard = subprocess.Popen([sys.executable, "-m", "streamlit", "run", str(app),
                    "--server.address", "127.0.0.1", "--server.port", str(port), "--server.headless", "true",
                    "--", "--db", str(config.db_path)], env=dashboard_environment(database=True))
                print(f"SessionQuant 모의매매 대시보드: http://127.0.0.1:{port}", flush=True)
            if config.strategy_explanation.get('enabled'):
                from .strategy_explanation import StrategyExplainer
                if news is None:
                    from .macro.analysis import BudgetedAnalyzer
                    from .macro.models import LLMSettings
                    explanation_owner=BudgetedAnalyzer(repo,LLMSettings.from_dict(config.llm))
                explainer=StrategyExplainer(repo,news.analyzer if news else explanation_owner,config.strategy_explanation)
            print("PAPER_LOCAL 실행 중입니다. 실제 주문은 비활성화되어 있습니다.", flush=True)
            next_account=0.
            cycles, next_market, next_news, next_tick, next_universe = 0, 0., 0., 0., time.monotonic() + 86400
            next_explanation=0.
            next_quotes=next_quote_status=0.
            while max_cycles is None or cycles < max_cycles:
                current, monotonic = datetime.now(timezone.utc), time.monotonic()
                if quote_delivery and monotonic >= next_quotes:
                    if quote_task.done():
                        quote_task.result()
                        raise RuntimeError('웹소켓 수신 작업이 예기치 않게 종료됐습니다.')
                    selected=quote_delivery.synchronize(watchlist,engine.broker,current)
                    raw=quote_delivery.events(selected,current,known_quotes=engine.market.quotes)
                    if raw:
                        began=time.monotonic()
                        engine.apply_batch(raw,current)
                        quote_batch_seconds=round(time.monotonic()-began,4)
                    if monotonic >= next_quote_status:
                        from .market.live_quotes import VIEW
                        repo.set_control(VIEW,{**quote_feed.snapshot(),'delivery_batches':quote_delivery.batches,
                            'delivered_quotes':quote_delivery.quotes,'engine_batch_seconds':quote_batch_seconds,
                            'selected_symbols':selected,'selected_fresh_count':sum(quote_feed.latest(s) is not None for s in selected),
                            'transport':'websocket','rest_role':'bootstrap_and_risk_fallback'})
                        next_quote_status=time.monotonic()+10
                    next_quotes=time.monotonic()+2
                if market and market_task is None and monotonic >= next_market:
                    priority = list(dict.fromkeys([*engine.broker.positions, *engine.broker.exit_intents,
                        *(o.intent.symbol for o in engine.broker.get_open_orders())]))
                    market_task = asyncio.create_task(market.poll(list(universe.members(current)), priority_symbols=priority))
                if config.actual_account_monitor_enabled and market and account_task is None and monotonic>=next_account:
                    from .account_monitor import fetch_account_snapshot
                    account_task=asyncio.create_task(fetch_account_snapshot(market.client))
                    next_account=monotonic+30
                if account_task and account_task.done():
                    try:
                        actual=account_task.result()
                        repo.set_control('actual_account_snapshot',actual)
                        repo.set_control('account_monitor_status',{'status':'ok','updated_at':actual['received_at']})
                    except Exception as exc:
                        repo.set_control('account_monitor_status',{'status':'error','message':'실계좌 갱신 실패: 이전 조회 기록입니다.','error_type':type(exc).__name__})
                    account_task=None
                if news and news_task is None and monotonic >= next_news:
                    priority = list(dict.fromkeys([*engine.broker.positions, *engine.broker.exit_intents,
                        *(o.intent.symbol for o in engine.broker.get_open_orders())]))
                    if watchlist:
                        contract = watchlist.news_targets(current, priority)
                        repo.set_control('watchlist_news_contract_v1', contract)
                        symbols = contract['analysis_symbols']
                    else:
                        symbols = list(dict.fromkeys([*priority, *engine.market.quotes]))
                    symbols = [s for s in symbols if s not in {"SPY", "QQQ"}][:20]
                    news_task = asyncio.create_task(news.refresh(symbols, held_symbols=tuple(engine.broker.positions),allowlist=contract['company_allowlist'] if watchlist else ()))
                    next_news = monotonic + min(config.company_news_refresh_seconds,config.macro_news_refresh_seconds,config.economic_calendar_refresh_seconds)
                if news_task and news_task.done():
                    try:
                        news_task.result()
                    except Exception as exc:
                        repo.log("WARNING", "뉴스 갱신에 실패했습니다. 시세·청산 관리는 계속합니다.", error_type=type(exc).__name__)
                    news_task = None
                if market_task and market_task.done():
                    batch = market_task.result()
                    observed_at = datetime.now(timezone.utc)
                    raw = batch_events(batch, delivered_at=observed_at, last_time=engine.last_time,
                        delayed_quotes=config.quote_transport=="websocket")
                    engine.apply_batch(raw, observed_at, controls={'sq_market_cycle_at':observed_at.isoformat()})
                    for error in batch.errors:
                        repo.log("WARNING", error["message"], source=error["source"], symbol=error["symbol"],
                                 error_type=error["error_type"])
                    market_task = None
                    cycles += 1
                    next_market = time.monotonic() + config.poll_seconds
                elif config.source == "file" and monotonic >= next_market:
                    input_path = config.input_path
                    if not input_path.exists():
                        raise ValueError("시장 입력 파일이 없습니다.")
                    with input_path.open("rb") as source:
                        consumed = source.read(engine.cursor)
                        expected = repo.get_control("sq_input_prefix")
                        if len(consumed) != engine.cursor or (expected and sha256(consumed).hexdigest() != expected):
                            raise ValueError("이미 처리한 입력 파일이 변경·축소되었습니다.")
                        raw = []
                        position = engine.cursor
                        for _ in range(2000):
                            line = source.readline()
                            if not line.endswith(b"\n"):
                                break
                            position += len(line)
                            consumed += line
                            if line.strip():
                                raw.append(json.loads(line))
                    engine.apply_batch(raw, current, cursor=position,
                                       controls={"sq_input_prefix": sha256(consumed).hexdigest()})
                    cycles += 1
                    next_market = monotonic + config.poll_seconds
                if watchlist and not market:
                    await watchlist.refresh(current)
                    watchlist.publish(current, priority_symbols=list(engine.broker.positions))
                if monotonic >= next_tick:
                    engine.tick(datetime.now(timezone.utc))
                    for alert in engine.broker.alerts:
                        notifier(alert)
                    next_tick = monotonic + 1
                if explanation_task and explanation_task.done():
                    try:explanation_task.result()
                    except Exception as exc:
                        repo.log('WARNING','AI 설명 실패: 규칙 기반 매매는 계속합니다.',error_type=type(exc).__name__)
                    explanation_task=None
                if explainer and explanation_task is None and monotonic>=next_explanation:
                    from .strategy_explanation import snapshot
                    explanation_task=asyncio.create_task(explainer.refresh(snapshot(engine,news,current),current))
                    next_explanation=monotonic+10
                if monotonic >= next_universe:
                    # 갱신은 별도 작업으로 돌려 청산 시계를 막지 않는다.
                    async def refresh_universe():
                        try:
                            await universe.refresh_public()
                            for snapshot in universe.snapshots:
                                repo.record("universe_history", snapshot)
                        except Exception as exc:
                            repo.log("WARNING", "S&P 500 목록 갱신 실패. 만료된 목록으로 신규 진입하지 않습니다.", error_type=type(exc).__name__)
                    task = asyncio.create_task(refresh_universe())
                    stack.callback(task.cancel)
                    next_universe = monotonic + 86400
                await asyncio.sleep(.1)
        finally:
            if quote_feed:
                quote_feed.stop()
            for task in (quote_task, market_task, news_task, account_task, explanation_task):
                if task and not task.done():
                    task.cancel()
                if task:
                    await asyncio.gather(task, return_exceptions=True)
            if market:
                await market.aclose()
            if news:
                await news.aclose()
            if explanation_owner:
                await explanation_owner.aclose()
            if dashboard:
                dashboard.terminate()
                try:
                    dashboard.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    dashboard.kill()
                    dashboard.wait(timeout=5)
            if engine and not engine.failed:
                with repo.transaction():
                    if quote_feed:
                        from .market.live_quotes import VIEW
                        repo.set_control(VIEW,{**repo.get_control(VIEW,{}),**quote_feed.snapshot(),
                            'delivery_batches':quote_delivery.batches,'delivered_quotes':quote_delivery.quotes,
                            'engine_batch_seconds':quote_batch_seconds,'selected_fresh_count':0})
                    for order in engine.broker.get_open_orders():
                        if order.intent.side == "buy":
                            engine.broker.cancel_order(order.client_id)
                    engine.tick(datetime.now(timezone.utc))
                    repo.set_control("engine_status", "정지됨: 미청산 모의 포지션은 다음 실행에서 복구됩니다.")
    return 0
