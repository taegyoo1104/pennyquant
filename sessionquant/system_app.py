"""Local launcher for the market-first system, its broker and dashboard."""
from __future__ import annotations

import asyncio
from contextlib import suppress
from dataclasses import replace
from datetime import datetime
from decimal import Decimal
from pathlib import Path
import subprocess
import sys
from zoneinfo import ZoneInfo

from .collectors.toss_market import TossMarketClient
from .config.settings import Settings
from .database.repository import Repository
from .engine import EventEngine
from .locking import exclusive_lock
from .models import Account
from .notifications import DesktopNotifier, dashboard_environment
from .pipeline import run_system
from .pipeline_config import load_pipeline_config
from .toss_app import _port_available, _stop_dashboard


def system_dashboard_command(db: Path, port: int) -> list[str]:
    app = Path(__file__).resolve().parent / "dashboard" / "app.py"
    return [sys.executable, "-m", "streamlit", "run", str(app),
            "--server.address", "127.0.0.1", "--server.port", str(port),
            "--server.headless", "true", "--browser.gatherUsageStats", "false",
            "--", "--db", str(db), "--mode", "system"]


def bind_system_database(repo: Repository, mode: str) -> None:
    kind = repo.get_control("runtime_kind")
    if kind not in (None, "market_pipeline"):
        raise ValueError("Use a separate database for the market-first system")
    if kind is None and repo.load_state() is not None:
        raise ValueError("Existing replay state cannot be reused as a system database")
    if repo.get_control("source", {}).get("synthetic") is True:
        raise ValueError("Use a fresh database instead of the synthetic demo database")
    previous = repo.get_control("pipeline_mode")
    if previous is not None and previous != mode:
        raise ValueError("Paper and live trading require separate databases")
    repo.set_control("runtime_kind", "market_pipeline")
    repo.set_control("pipeline_mode", mode)


def account_commission_settings(settings: Settings, rates: list[dict], today: str) -> Settings:
    current = [item for item in rates if item.get("marketCountry") == "US"
               and (not item.get("startDate") or item["startDate"] <= today)
               and (not item.get("endDate") or item["endDate"] >= today)]
    if not current:
        raise ValueError("A current US account commission rate could not be verified")
    current.sort(key=lambda item: item.get("startDate") or "", reverse=True)
    rate = Decimal(current[0]["commissionRate"])
    if not rate.is_finite() or not 0 <= rate < 1:
        raise ValueError("The US account commission rate is invalid")
    bps = float(rate * 10000)
    return replace(settings, fee_model="toss_us", exit_price_policy="net",
                   commission_buy_bps=bps, commission_sell_bps=bps)


async def run_system_app(args, settings: Settings) -> int:
    config = load_pipeline_config(args.pipeline_config)
    if args.acknowledge_live_orders and not args.live:
        raise ValueError("--acknowledge-live-orders also requires --live")
    if config.mode == "live" and not args.live:
        raise ValueError("A live configuration requires --live and --acknowledge-live-orders")
    if args.live:
        if not args.acknowledge_live_orders:
            raise ValueError("Actual orders require --live and --acknowledge-live-orders")
        if config.input_event_path is None:
            raise ValueError("Live entries require a verified complete market input_event_path; Toss observations alone are insufficient")
        config = replace(config, mode="live")
    if not 1 <= settings.max_positions <= 5:
        raise ValueError("The market-first system supports at most five concurrent positions")
    settings = replace(settings, news_policy="primary", max_candidates=config.max_candidates,
                       min_price=config.price_min, max_price=config.price_max)
    if not args.no_dashboard:
        _port_available(args.port)
    repo = None
    market = None
    engine = None
    child = None
    runner = None
    notifier = DesktopNotifier(enabled=not args.no_desktop_notifications)
    db = Path(config.db_path).resolve()
    with exclusive_lock(str(db) + ".engine.lock", message="Another system process is already using this DB"):
        try:
            repo = Repository(db)
            bind_system_database(repo, config.mode)
            manager = None
            if config.mode == "live":
                from .trading.live_manager import LiveOrderManager
                from .trading.toss_broker import TossBroker
                market = TossMarketClient()
                broker = TossBroker(market, repo, live_enabled=True, acknowledge_live_orders=True,
                                    max_positions=settings.max_positions,
                                    max_quote_age_seconds=settings.max_quote_age_seconds)
                if not await broker.reconcile():
                    raise ValueError("Broker reconciliation did not pass; check broker_state and the account in Toss")
                today = datetime.now(ZoneInfo("America/New_York")).date().isoformat()
                settings = account_commission_settings(settings, await broker.commissions(), today)
                account = Account(cash=settings.initial_cash, equity=settings.initial_cash,
                                  day_start_equity=settings.initial_cash)
                manager = LiveOrderManager(settings, account, broker, repository=repo)
            engine = EventEngine(settings, config.strategy, repo, restore=repo.load_state() is not None,
                                 live=True, manager=manager)
            engine.manager.on_alert = notifier
            if manager is not None:
                await manager.synchronize()
            if not args.no_dashboard:
                child = subprocess.Popen(system_dashboard_command(db, args.port),
                    cwd=Path(__file__).resolve().parents[1], env=dashboard_environment())
                print(f"PennyQuant 대시보드: http://localhost:{args.port}", flush=True)
            print(f"PennyQuant: {config.mode.upper()} · 동시 보유 최대 {settings.max_positions}종목 · "
                  f"일일 손실 한도 {settings.daily_loss_pct:.1%}. 종료: Ctrl+C", flush=True)
            runner = asyncio.create_task(run_system(config, repo, engine=engine, settings=settings,
                                                   market=market, max_cycles=args.max_cycles))
            while not runner.done():
                if child is not None and child.poll() is not None:
                    raise RuntimeError("Dashboard exited; the system is stopping")
                await asyncio.sleep(0.2)
            result = await runner
            print(f"PennyQuant: 실행 종료 · {result}", flush=True)
            return 0
        finally:
            if runner is not None and not runner.done():
                runner.cancel()
                with suppress(asyncio.CancelledError):
                    await runner
            if engine is not None and config.mode == "live":
                engine.manager.halt("shutdown")
                with suppress(Exception):
                    await engine.manager.drain()
                    engine.persist()
                if engine.manager.account.positions:
                    alert = {"id": "shutdown-with-positions", "type": "shutdown", "message":
                             "프로그램이 종료되어 자동 감시가 중단됩니다. 토스에서 보유 종목과 미체결 주문을 확인하세요."}
                    notifier(alert)
                    if repo is not None:
                        repo.record("alerts", alert)
            await _stop_dashboard(child)
            if market is not None:
                await market.aclose()
            if repo is not None:
                repo.close()
