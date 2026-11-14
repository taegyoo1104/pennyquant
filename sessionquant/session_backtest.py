"""동일 입력·수수료로 전략과 추가매수·뉴스 위험 제한의 효과를 비교한다."""
from __future__ import annotations

from dataclasses import replace
from collections import Counter
from decimal import Decimal
import json
from pathlib import Path
from statistics import mean
from tempfile import TemporaryDirectory

from .models import utc
from .portfolio.models import PortfolioSettings
from .session_runtime import replay_session


def summarize(result: dict, settings: PortfolioSettings) -> dict:
    fills, state = result["fills"], result["state"]
    open_trades, trades = {}, []
    for fill in fills:
        qty, pnl = Decimal(fill["qty"]), Decimal(fill["realized_pnl"])
        symbol = fill["symbol"]
        if fill["side"] == "buy":
            trade = open_trades.setdefault(symbol, {"qty": Decimal(0), "pnl": Decimal(0), "opened_at": utc(fill["timestamp"])})
            trade["qty"] += qty
        elif symbol in open_trades:
            trade = open_trades[symbol]
            trade["qty"] -= qty
            trade["pnl"] += pnl
            if trade["qty"] == 0:
                trades.append({"symbol": symbol, "pnl": float(trade["pnl"]), "opened_at": trade["opened_at"],
                               "minutes": (utc(fill["timestamp"]) - trade["opened_at"]).total_seconds() / 60})
                del open_trades[symbol]
    values = [x["pnl"] for x in trades]
    gains, losses = sum(max(x, 0) for x in values), -sum(min(x, 0) for x in values)
    peak, mdd = float(settings.initial_cash), 0.
    for row in result["curve"]:
        equity = float(row["equity"])
        peak = max(peak, equity)
        mdd = max(mdd, (peak-equity)/peak if peak else 0)
    by_regime = {}
    for trade in trades:
        known = [x for x in result["regimes"] if utc(x["timestamp"]) <= trade["opened_at"]]
        regime = known[-1]["state"] if known else "UNKNOWN"
        bucket = by_regime.setdefault(regime, {"trades": 0, "net_pnl": 0.})
        bucket["trades"] += 1
        bucket["net_pnl"] += trade["pnl"]
    equity = Decimal(state["equity"])
    exit_orders = [o for o in result["orders"] if o["intent"]["side"] == "sell"]
    from .portfolio.broker import OPEN_STATUSES
    slippages = [Decimal(f['slippage_bps']) for f in fills if f.get('slippage_bps') is not None]
    # 주문 제출 수가 아니라 포지션 수량 0으로 끝난 거래만 완료로 센다.
    return {"fill_model_version":settings.paper_fill_model,
            'risk_policy_version':2,
            'exit_reason_counts':dict(Counter(o['intent']['reason'] for o in exit_orders if Decimal(o['filled_qty']) > 0)),
            'risk_decision_counts':dict(Counter(r['reason'] for r in result.get('risk_decisions',[]))),
            'risk_decision_count_basis':'unique_signals_last_decision',
            'risk_alert_count':len(state.get('alerts',[])),
            "assumed_latency_seconds":str(settings.paper_latency_seconds) if settings.paper_fill_model == "market_v2" else "0",
            "depth_participation_assumption":str(settings.paper_depth_participation) if settings.paper_fill_model == "market_v2" else "1",
            "net_pnl_basis":"realized_and_bid_marked_after_fill_fees",
            "realized_net_pnl":state["realized_pnl"],
            "average_adverse_price_difference_bps":str(sum(slippages)/len(slippages)) if slippages else None,
            "unfilled_reason_counts":dict(Counter(o.get("unfilled_reason", "UNKNOWN") for o in result["orders"] if o["status"] in OPEN_STATUSES)),
            "cost_scope":{"spread_slippage_and_fill_fees_included":True,"llm_cost_included":False,"infrastructure_cost_included":False,"future_exit_cost_included":False},
            "trades": len(trades), "net_pnl": str(equity-settings.initial_cash),
            "return_pct": float((equity/settings.initial_cash-1)*100), "final_equity": str(equity),
            "max_drawdown_pct": mdd*100, "win_rate": sum(x>0 for x in values)/len(values) if values else None,
            "average_win": mean([x for x in values if x>0]) if gains else None,
            "average_loss": mean([x for x in values if x<0]) if losses else None,
            "expectancy": mean(values) if values else None, "profit_factor": gains/losses if losses else None,
            "average_holding_minutes": mean([x["minutes"] for x in trades]) if trades else None,
            "fees_usd": str(sum((Decimal(x["fee"]) for x in fills), Decimal(0))),
            "slippage_rate_assumption": str(settings.slippage_rate), "market_regimes": by_regime,
            "add_buy_orders": sum(o["intent"]["is_add"] for o in result["orders"]),
            "open_positions": len(state["positions"]), "open_orders": sum(o["status"] in OPEN_STATUSES for o in result["orders"]),
            "completed_position_exits": len(trades), "exit_order_attempts": len(exit_orders),
            "session_exit_success_rate": _session_exit_rate(state),
            "llm_api_calls_in_replay": 0,
            "operating_cost_note": "재생은 LLM을 호출하지 않습니다. 실제 LLM·서버 비용은 별도이며 이 수익에서 차감되지 않았습니다."}


def _session_exit_rate(state):
    from zoneinfo import ZoneInfo
    ny = ZoneInfo("America/New_York")
    bought, sold = set(), set()
    quantities = {}
    for fill in state["fills"]:
        day = utc(fill["timestamp"]).astimezone(ny).date().isoformat()
        key = (fill["symbol"], day)
        if fill["side"] == "buy":
            bought.add(key)
            quantities[key] = quantities.get(key, Decimal(0)) + Decimal(fill["qty"])
        elif key in quantities:
            quantities[key] -= Decimal(fill["qty"])
            if quantities[key] == 0:
                sold.add(key)
    # 같은 날 재진입하여 미청산된 경우는 성공으로 남기지 않는다.
    sold = {key for key in sold if quantities.get(key) == 0}
    return len(sold)/len(bought) if bought else None


def compare_session(input_path: Path, universe_path: Path, report: Path, *, settings=None, synthetic=False):
    settings = settings or PortfolioSettings()
    variants = [(name, name, settings, True, True) for name in ("momentum", "pullback", "gap_breakout", "vwap")]
    variants += [("pullback_no_add", "pullback", replace(settings, allow_adds=False), True, True),
                 ("pullback_without_news_risk", "pullback", settings, False, True),
                 ("technical_only", "pullback", settings, False, False)]
    alternate = "legacy_limit_v1" if settings.paper_fill_model == "market_v2" else "market_v2"
    variants += [("pullback_"+alternate,"pullback",replace(settings,paper_fill_model=alternate),True,True)]
    results = {}
    with TemporaryDirectory(prefix="sessionquant-backtest-") as directory:
        for label, strategy, config, use_news, market_filter in variants:
            run = replay_session(input_path, Path(directory)/f"{label}.db", universe_path, strategy=strategy,
                                 settings=config, synthetic=synthetic, backend="sqlite", use_news=use_news, use_market_filter=market_filter)
            results[label] = summarize(run, config)
    from .portfolio.fill_comparison import synthetic_gap_comparison
    payload = {"report_version":2,"synthetic_gap_execution_check":synthetic_gap_comparison(),"project": "SessionQuant", "synthetic": synthetic,
               "source": str(input_path), "universe": str(universe_path), "results": results,
               "limitations": ["수신 이후 확정된 봉과 당시 확인 가능한 구성 이력만 사용합니다.",
                               "기술적 지표 단독 실험과 시장 필터·추가매수·뉴스 제한 효과를 비교합니다.",
                               "재생 자료에 뉴스 위험 이벤트가 없으면 뉴스 포함·제외 결과가 같을 수 있습니다.",
                               "API 권한·실제 체결·실제 슬리피지·서버 비용은 이 시험으로 검증되지 않습니다.",
                               "미청산 포지션은 매수호가로 평가합니다. 미래 청산 비용·LLM·인프라 비용은 차감되지 않았습니다.",
                               "legacy_limit_v1은 이전 가격제한 연구 모델이며 시장가 성과와 구분합니다."]}
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    lines = ["# SessionQuant 전략 비교", "", "합성 검증 자료이며 실제 수익률이 아닙니다." if synthetic else "사용자가 제공한 기록을 재생한 결과입니다.", "",
             "| 실험 | 체결 모델 | 완료 거래 | 비용 차감 수익률 | 최대 낙폭 | 수수료 | 미청산 |", "|---|---|---:|---:|---:|---:|---:|"]
    for label, item in results.items():
        lines.append(f"| {label} | {item['fill_model_version']} | {item['trades']} | {item['return_pct']:.3f}% | {item['max_drawdown_pct']:.3f}% | ${item['fees_usd']} | {item['open_positions']} |")
    lines += ["", *payload["limitations"], "", "LLM·서버 비용은 제외되어 있습니다. 운영 DB의 llm_usage와 실제 인프라 청구서를 별도로 확인하세요."]
    report.with_suffix(".md").write_text("\n\n".join(lines), encoding="utf-8")
    return payload
