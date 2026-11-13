from __future__ import annotations

import argparse
from dataclasses import asdict
import importlib.metadata
import json
from pathlib import Path
import sys

from .config.settings import load_settings


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description="PennyQuant research and paper trading (live orders disabled)")
    root.add_argument("--config", type=Path, help="YAML settings")
    commands = root.add_subparsers(dest="command", required=True)
    commands.add_parser("doctor", help="Offline dependency/configuration check; no credentials printed")
    demo = commands.add_parser("demo", help="Generate synthetic tape, replay and compare four strategies")
    demo.add_argument("--out", type=Path, default=Path("data"))
    demo.add_argument("--strategy", choices=["momentum", "pullback", "gap_breakout", "vwap"], default="pullback")
    backtest = commands.add_parser("backtest", help="Receipt-ordered event-driven comparison")
    backtest.add_argument("--input", type=Path, required=True)
    backtest.add_argument("--report", type=Path, default=Path("data/report.json"))
    backtest.add_argument("--label", default="user supplied; provenance unverified")
    replay = commands.add_parser("replay", help="Paper execution against a normalized recorded stream")
    replay.add_argument("--input", type=Path, required=True)
    replay.add_argument("--db", type=Path, default=Path("data/pennyquant.db"))
    replay.add_argument("--strategy", choices=["momentum", "pullback", "gap_breakout", "vwap"], default="pullback")
    replay.add_argument("--resume", action="store_true")
    replay.add_argument("--speed", type=float, default=0, help="0=unpaced, >0 receipt-time replay speed (idle gaps capped at 5s)")
    follow = commands.add_parser("paper", help="Continuously consume a normalized live JSONL producer file")
    follow.add_argument("--input", type=Path, required=True)
    follow.add_argument("--db", type=Path, default=Path("data/pennyquant.db"))
    follow.add_argument("--strategy", choices=["momentum", "pullback", "gap_breakout", "vwap"], default="pullback")
    collect = commands.add_parser("collect", help="Read-only provider probes/normalized data capture")
    collect.add_argument("provider", choices=["toss", "alpaca", "sec", "rss"])
    collect.add_argument("--symbols", default="AAPL", help="Comma-separated symbols")
    collect.add_argument("--output", type=Path, required=True)
    collect.add_argument("--cik", help="SEC CIK")
    collect.add_argument("--url", help="RSS HTTPS URL")
    collect.add_argument("--poll", type=float, default=0, help="Toss quote sampling seconds, 0=one snapshot")
    collect.add_argument("--stream", action="store_true", help="Use documented Toss/Alpaca WebSocket instead of REST")
    collect.add_argument("--realtime-verified", action="store_true", help="Alpaca only: operator has verified account real-time news entitlement")
    return root


def metadata(path: Path) -> dict:
    manifest = path.with_suffix(".meta.json")
    return json.loads(manifest.read_text(encoding="utf-8")) if manifest.exists() else {}


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        settings = load_settings(args.config)
        if args.command == "doctor":
            packages = ("httpx", "websockets", "PyYAML", "pandas", "numpy", "streamlit", "exchange-calendars")
            print(json.dumps({"python": sys.version.split()[0], "mode": settings.mode,
                "live_orders_enabled": False, "packages": {p: importlib.metadata.version(p) for p in packages}}, indent=2))
        elif args.command == "demo":
            from .demo import write_demo
            from .runtime import replay
            from .backtest.engine import compare, write_report
            db = args.out / "pennyquant.db"
            if db.exists():
                raise ValueError("demo DB exists; choose a new --out directory for a fresh run")
            tape = args.out / "synthetic.jsonl"
            events = write_demo(tape)
            replay(tape, db, settings, args.strategy, synthetic=True, label="SYNTHETIC_FIXTURE")
            report = compare(events, settings, synthetic=True, label="SYNTHETIC_FIXTURE")
            write_report(report, args.out / "report.json")
            print(f"Synthetic demo complete. DB: {db}; report: {args.out / 'report.md'}. Not market performance.")
        elif args.command == "backtest":
            from .events import read_events
            from .backtest.engine import compare, write_report
            meta = metadata(args.input)
            report = compare(read_events(args.input), settings, synthetic=meta.get("synthetic", False),
                             label=meta.get("source_label", args.label))
            write_report(report, args.report)
            print(f"Wrote {args.report} and {args.report.with_suffix('.md')}")
        elif args.command == "replay":
            from .runtime import replay
            meta = metadata(args.input)
            replay(args.input, args.db, settings, args.strategy, resume=args.resume, speed=args.speed,
                   synthetic=meta.get("synthetic", False), label=meta.get("source_label", "recorded user input"))
            print(f"Paper replay complete: {args.db}")
        elif args.command == "paper":
            from .runtime import follow
            follow(args.input, args.db, settings, args.strategy)
        elif args.command == "collect":
            import asyncio
            from .capture import collect
            asyncio.run(collect(args))
        return 0
    except (ValueError, RuntimeError, OSError, ImportError) as exc:
        print(f"PennyQuant: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("Stopped; persisted paper state is retained.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
