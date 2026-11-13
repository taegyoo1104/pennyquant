from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo
import json

from ..config.settings import Settings
from ..engine import EventEngine
from ..events import Event
from ..models import jsonable
from ..strategies import STRATEGIES
from .performance import summarize, validate_report

NY = ZoneInfo("America/New_York")


def run_once(events: list[Event], settings: Settings, strategy: str) -> tuple[dict, EventEngine]:
    engine = EventEngine(settings, strategy)
    for event in events:
        engine.process(event)
    metrics = summarize(engine.manager, engine.equity_curve, settings.initial_cash)
    metrics.update(signals=engine.signal_count, rejected_signals=engine.rejected_signals)
    return metrics, engine


def compare(events: list[Event], settings: Settings, *, synthetic: bool = False, label: str = "user supplied") -> dict:
    if not events:
        raise ValueError("no events")
    # Chronological split at NY session boundaries. No parameter optimization is performed.
    dates = sorted({event.available_at.astimezone(NY).date() for event in events})
    split = max(1, int(len(dates) * 0.6))
    train_dates = set(dates[:split])
    validation_dates = set(dates[split:])
    train = [event for event in events if event.available_at.astimezone(NY).date() in train_dates]
    validation = [event for event in events if event.available_at.astimezone(NY).date() in validation_dates]
    report = {
        "schema_version": 1, "created_at": datetime.now(timezone.utc).isoformat(), "synthetic": synthetic,
        "source_label": label, "mode": "paper", "initial_cash": settings.initial_cash,
        "period_start": events[0].available_at.isoformat(), "period_end": events[-1].available_at.isoformat(),
        "sessions": len(dates), "event_count": len(events), "settings": asdict(settings),
        "strategies": {}, "equity_curve": [], "trades": [], "split": {
            "method": "first 60% NY sessions / remaining sessions, independent starting capital; no tuning",
            "train_sessions": len(train_dates), "validation_sessions": len(validation_dates)},
        "limitations": [
            "Synthetic/demo data cannot establish profitability or live readiness." if synthetic else
            "Input data provenance, survival bias and corporate-action adjustments require independent review.",
            "News is available only after both publication and recorded receipt. Historical receipt provenance is not inferred.",
            "Quotes must follow order creation; displayed size is consumed once per quote. Queue position and depth beyond level 1 are unknown.",
            "Stops are local synthetic limit exits. Gaps, halts and failed liquidity can leave positions open.",
            "Final equity includes open positions marked at bid; no fabricated end-of-period liquidation is inserted.",
            "Fees are illustrative symmetric basis-point costs; broker-specific minimums and regulatory/FX fees require configuration work.",
        ],
        "live_readiness": {"approved": False, "reason": "4+ weeks of prospective paper observations, adequate trade sample and separate manual approval required"},
    }
    for name in STRATEGIES:
        metrics, engine = run_once(events, settings, name)
        metrics["training"] = run_once(train, settings, name)[0] if train else None
        metrics["validation"] = run_once(validation, settings, name)[0] if validation else None
        report["strategies"][name] = metrics
        report["equity_curve"].extend(engine.equity_curve)
        report["trades"].extend([{**jsonable(trade), "strategy": name} for trade in engine.manager.closed_trades])
    validate_report(report)
    return report


def write_report(report: dict, path: str | Path) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2, ensure_ascii=False, allow_nan=False), encoding="utf-8")
    title = "합성 데이터 기능 검증" if report["synthetic"] else "사용자 데이터 전략 비교"
    rows = [f"# PennyQuant {title} 보고서", "", f"기간: {report['period_start']} ~ {report['period_end']}",
            f"세션: {report['sessions']} / 이벤트: {report['event_count']}", "",
            "실제 시장 수익성 검증이나 실거래 승인 결과가 아닙니다." if report["synthetic"] else
            "데이터 출처·기업행동·상장폐지 포함 여부를 별도로 검증해야 합니다.", "",
            "| 전략 | 종료 자산 USD | 순손익 USD | 완료 거래 | 승률 | PF | MDD | 검증구간 손익 USD |",
            "|---|---:|---:|---:|---:|---:|---:|---:|"]
    for name, m in report["strategies"].items():
        pf = f"{m['profit_factor']:.2f}" if m["profit_factor"] is not None else "계산 불가"
        vp = f"{m['validation']['net_pnl']:.4f}" if m["validation"] else "없음"
        rows.append(f"| {name} | {m['final_equity']:.4f} | {m['net_pnl']:.4f} | {m['trades']} | {m['win_rate']:.1%} | {pf} | {m['max_drawdown_pct']:.2f}% | {vp} |")
    rows += ["", "각 전략은 같은 이벤트·자본·비용 조건을 사용합니다. 파라미터 최적화는 수행하지 않았습니다.",
             "마지막 자산에는 보유 포지션의 매수호가 평가액이 포함되며, 완료 거래의 손익과 다를 수 있습니다.",
             "거래가 적거나 손실 거래가 없는 경우 PF는 계산 불가로 표시합니다.", "", "## 해석 제한", ""]
    rows += [f"- {text}" for text in report["limitations"]]
    rows += ["", "실거래 승인: 보류. 4주 이상의 실시간 모의매매와 충분한 표본, 계정별 API 복구 검증이 필요합니다.", ""]
    path.with_suffix(".md").write_text("\n".join(rows), encoding="utf-8")
