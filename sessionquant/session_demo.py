"""수익성이 아닌 연결·주문·당일 청산을 확인하는 명시적 합성 예제."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path

from .market.universe import UniverseStore
from .session_runtime import replay_session


def synthetic_tape() -> tuple[list[dict], datetime]:
    opened = datetime(2026, 11, 13, 14, 30, tzinfo=timezone.utc)
    records = []
    closes = [100 + i*.1 for i in range(12)] + [101.05, 101.0, 101.35]
    for minute in range(1, 76):
        stamp = opened + timedelta(minutes=minute)
        group = (minute-1)//5
        for symbol, base in (("AAPL", 100), ("SPY", 500), ("QQQ", 600)):
            close = closes[group] if symbol == "AAPL" else base + group*.25
            volume = (150000 if group == 14 else 80000 if group in (12, 13) else 100000)
            records.append({"kind": "bar", "data": {"symbol": symbol, "timestamp": stamp.isoformat(),
                "received_at": stamp.isoformat(), "open": close-.01, "high": close+.15,
                "low": close-.15, "close": close, "volume": volume}})
    entry = opened + timedelta(minutes=75, seconds=1)
    records.append({"kind": "eligibility", "data": {"symbol": "AAPL", "checked_at": entry.isoformat(),
        "source": "SYNTHETIC_FIXTURE", "tradable": True, "fractional": True, "halted": False,
        "corporate_action": False, "exchange": "NASDAQ"}})
    records.append({"kind": "rvol", "data": {"symbol": "AAPL", "value": 1.8,
        "observed_at": entry.isoformat(), "verified": True}})

    def quote(symbol, bid, ask, stamp):
        return {"kind": "quote", "data": {"symbol": symbol, "timestamp": stamp.isoformat(),
            "received_at": stamp.isoformat(), "bid": bid, "ask": ask, "bid_size": 1000, "ask_size": 1000,
            "tradable": True, "halted": False, "exchange": "NASDAQ" if symbol == "AAPL" else "ARCA"}}

    records.extend([quote("SPY", 503.51, 503.53, entry), quote("QQQ", 603.51, 603.53, entry),
                    quote("AAPL", 101.35, 101.37, entry)])
    records.append(quote("AAPL", 101.35, 101.37, entry+timedelta(seconds=1)))
    exit_time = opened + timedelta(hours=5, minutes=10)
    records.append(quote("AAPL", 102.0, 102.02, exit_time))
    records.append(quote("AAPL", 102.0, 102.02, exit_time+timedelta(seconds=1)))
    return records, opened


def write_session_demo(out: Path, *, strategy="pullback", backend="sqlite", fill_model="market_v2"):
    out = Path(out)
    if out.exists() and any(out.iterdir()):
        raise ValueError("출력 폴더에 기존 자료가 있습니다. 새로운 --out 경로를 지정하세요.")
    out.mkdir(parents=True, exist_ok=True)
    records, opened = synthetic_tape()
    universe_path, tape = out/"universe.json", out/"synthetic.jsonl"
    universe = UniverseStore(universe_path)
    universe.refresh([{"symbol": "AAPL", "name": "합성 검증용 AAPL", "sector": "Information Technology"}],
                     source="SYNTHETIC_FIXTURE", observed_at=opened-timedelta(days=1))
    tape.write_text("".join(json.dumps(x, ensure_ascii=False, allow_nan=False)+"\n" for x in records), encoding="utf-8")
    tape.with_suffix(".meta.json").write_text(json.dumps({"synthetic": True, "source_label": "SYNTHETIC_FIXTURE"}), encoding="utf-8")
    # MySQL 이름은 폴더별로 달라져 다른 합성 실행 공간을 덮어쓰지 않는다.
    db = out/("sessionquant-demo-"+out.name+".db")
    from .portfolio.models import PortfolioSettings
    settings = PortfolioSettings(paper_fill_model=fill_model)
    result = replay_session(tape, db, universe_path, strategy=strategy, synthetic=True, backend=backend,settings=settings)
    from .session_backtest import compare_session
    report = out/"report.json"
    compare_session(tape, universe_path, report, synthetic=True,settings=settings)
    return {"database": str(db), "backend": backend, "input": str(tape), "universe": str(universe_path),
            "report": str(report), "strategy": strategy,"fill_model_version":fill_model, "fills": len(result["fills"]),
            "open_positions": len(result["state"]["positions"]), "synthetic": True, "live_orders_enabled": False}
