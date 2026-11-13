"""Korean paper-trading monitor. Run from the project root with Streamlit."""
from __future__ import annotations

import argparse
from array import array
from datetime import datetime, timezone
import html
from io import BytesIO
import json
import math
import os
from pathlib import Path
import sys
from typing import Any
import wave
from zoneinfo import ZoneInfo

import pandas as pd
import streamlit as st

# `streamlit run pennyquant/dashboard/app.py` also works without an editable install.
PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

SEOUL = ZoneInfo("Asia/Seoul")
STRATEGY_NAMES = {
    "momentum": "뉴스 모멘텀",
    "pullback": "급등 후 눌림목",
    "gap_breakout": "갭 돌파",
    "vwap": "VWAP 회복",
}
STATUS_NAMES = {
    "pending": "접수 대기", "open": "미체결", "partial": "부분 체결",
    "filled": "체결 완료", "cancelled": "취소", "rejected": "거절", "unknown": "확인 필요",
}


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--db", default=os.getenv("PQ_DB", "data/pennyquant.db"))
    parser.add_argument("--report", default=os.getenv("PQ_REPORT", "data/report.json"))
    parser.add_argument("--mode", choices=("auto", "paper", "monitor", "system"), default="auto")
    return parser.parse_known_args()[0]


def number(value: Any, default: float = 0.0) -> float:
    try:
        result = float(value)
        return result if math.isfinite(result) else default
    except (TypeError, ValueError):
        return default


def usd(value: Any) -> str:
    return f"${number(value):,.2f}"


def timestamp(value: Any, *, short: bool = False) -> str:
    if not value:
        return "—"
    try:
        date = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if date.tzinfo is None:
            return "시각 확인 필요"
        return date.astimezone(SEOUL).strftime("%m.%d %H:%M:%S" if short else "%Y.%m.%d %H:%M:%S KST")
    except (TypeError, ValueError):
        return str(value)


def event_health(runtime: dict, now: datetime | None = None, *, max_age: float = 5.0) -> dict:
    """Report wall-clock event lag without inferring an API connection."""
    value = runtime.get("last_event")
    result = {"last_event": timestamp(value), "lag_seconds": None, "lag_label": "미확인", "stale": True, "status": "이벤트 수신 이력 없음"}
    if not value:
        return result
    try:
        date = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if date.tzinfo is None:
            raise ValueError("timezone required")
        lag = ((now or datetime.now(timezone.utc)) - date).total_seconds()
    except (TypeError, ValueError):
        result["status"] = "이벤트 시각 확인 필요"
        return result
    if lag < -1:
        result["status"] = "이벤트 시각이 현재보다 미래임"
        return result
    lag = max(0.0, lag)
    if lag < 60:
        label = f"{lag:.1f}초"
    elif lag < 3600:
        label = f"{int(lag // 60)}분 {int(lag % 60)}초"
    elif lag < 86400:
        label = f"{int(lag // 3600)}시간 {int(lag % 3600 // 60)}분"
    else:
        label = f"{int(lag // 86400)}일 {int(lag % 86400 // 3600)}시간"
    result.update(lag_seconds=lag, lag_label=label, stale=lag > max_age,
                  status=f"최신 이벤트 없음 · {max_age:g}초 초과" if lag > max_age else "최근 이벤트 있음 · 연결 여부 별도 확인")
    return result


def monitor_mode(runtime: dict, monitor: dict, requested_mode: str = "auto") -> bool:
    """A stored monitoring session must never be displayed as a paper account."""
    return requested_mode == "monitor" or runtime.get("mode") == "monitor" or monitor.get("mode") == "monitor"


def pipeline_mode(runtime: dict, pipeline: dict, requested_mode: str = "auto", runtime_kind: Any = None) -> bool:
    return (requested_mode == "system" or runtime.get("mode") == "system"
            or runtime_kind in {"market_pipeline", "pipeline", "system"} or bool(pipeline))


def price_value(value: Any) -> float | None:
    try:
        result = float(value)
        return result if math.isfinite(result) and result > 0 else None
    except (TypeError, ValueError):
        return None


def quote_health(value: Any, now: datetime | None = None, *, max_age: float = 5.0) -> dict:
    """Age the provider's price time separately from this program's receipt."""
    result = event_health({"last_event": value}, now, max_age=max_age)
    if value is None or value == "":
        result["status"] = "제공 시각 미확인"
    elif result["lag_seconds"] is None:
        result["status"] = "제공 시각 확인 필요"
    elif result["stale"]:
        result["status"] = "제공 시각이 오래됨"
    else:
        result["status"] = "최근 제공 시각 · 실시간 권한 별도 확인"
    return result


def monitor_status(monitor: dict, health: dict) -> str:
    status = monitor.get("status")
    if status == "connected":
        return "수신 지연" if health["stale"] else "최근 조회 성공"
    if status == "stopped" and monitor.get("error"):
        return "수집 중지 · 오류"
    return {"starting": "연결 확인 중", "error": "조회 오류", "stopped": "수집 중지"}.get(status, "수집 상태 미확인")


def latest_by_symbol(rows: list[dict]) -> dict[str, dict]:
    result = {}
    for row in rows:
        quote = row.get("quote", {})
        symbol = row.get("symbol") or quote.get("symbol")
        if symbol:
            result[str(symbol)] = row
    return result


def read_report(path: Path) -> tuple[dict, str | None]:
    if not path.exists():
        return {}, None
    try:
        report = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(report, dict):
            raise ValueError("최상위 JSON은 객체여야 합니다.")
        return report, None
    except (OSError, ValueError) as exc:
        return {}, f"성과 보고서를 읽을 수 없습니다: {exc}"


def load_database(path: Path) -> tuple[Any, dict, dict[str, list[dict]], str | None]:
    names = ("stocks", "market_bars", "quote_snapshots", "news_events", "signals", "orders", "executions", "positions", "daily_performance", "system_logs", "price_samples", "candidates", "news_analysis", "alerts", "pipeline_status", "order_intents", "broker_state")
    tables: dict[str, list[dict]] = {name: [] for name in names}
    if not path.exists():
        return None, {}, tables, None
    repo = None
    try:
        from pennyquant.database.repository import KINDS, Repository
        repo = Repository(path)
        state = repo.load_state() or {}
        for name in names:
            if name not in KINDS:
                continue
            if name in {"price_samples", "news_analysis", "alerts", "order_intents", "broker_state"}:
                tables[name] = repo.recent(name, limit=2000) if hasattr(repo, "recent") else repo.all(name)[-2000:]
            else:
                tables[name] = repo.all(name)
        return repo, state, tables, None
    except Exception as exc:
        if repo:
            repo.close()
        return None, {}, tables, f"데이터베이스를 읽을 수 없습니다: {exc}"


def panel_title(eyebrow: str, title: str, description: str = "") -> None:
    st.markdown(
        f'<div class="panel-heading"><span>{html.escape(eyebrow)}</span>'
        f'<h3>{html.escape(title)}</h3><p>{html.escape(description)}</p></div>',
        unsafe_allow_html=True,
    )


def empty(message: str) -> None:
    st.info(message, icon="ℹ️")


def dataframe(rows: list[dict] | pd.DataFrame, **kwargs: Any) -> None:
    frame = rows if isinstance(rows, pd.DataFrame) else pd.DataFrame(rows)
    st.dataframe(frame, hide_index=True, width="stretch", **kwargs)


def style() -> None:
    st.markdown("""
<style>
:root { --mint: #55dec5; --muted: #91a7bb; --surface: #132438; }
html,body,[class*="css"],.stApp {font-family: 'Noto Sans KR', -apple-system, BlinkMacSystemFont, sans-serif;}
.stApp { background: #091523; color: #e9f1f7; }
[data-testid="stSidebar"] { background: #0d1c2c; border-right: 1px solid #25384a; }
[data-testid="stHeader"] { background: transparent; }
.block-container {max-width: 1500px; padding-top: 2.2rem; padding-bottom: 3rem;}
.brand {font-size: 1.52rem; font-weight: 700; letter-spacing: -.06rem; color: #f5fbff; margin-bottom: .1rem;}
.brand b {color: var(--mint);}
.brand-sub {color: #88a1b6; font-size: .75rem; letter-spacing: .08rem; margin-bottom: 2rem;}
.hero {display: flex; align-items: flex-start; justify-content: space-between; gap: 1rem; margin: .2rem 0 1.6rem;}
.eyebrow {color: var(--mint); font-size: .73rem; letter-spacing: .15rem; font-weight: 700; margin-bottom: .6rem;}
.hero h1 {font-size: 2rem; font-weight: 700; letter-spacing: -.07rem; margin: 0 0 .45rem; padding: 0; color: #f5fbff;}
.hero p {color: var(--muted); margin: 0; font-size: .91rem;}
.pill {border: 1px solid #2d695e; color: var(--mint); background: #123a36; padding: .42rem .8rem; border-radius: 999px; font-size: .72rem; font-weight: 700; letter-spacing: .07rem; white-space: nowrap;}
.small-text {color: var(--muted); font-size: .78rem; line-height: 1.7;}
[data-testid="stMetric"] {background: var(--surface); border: 1px solid #25394c; padding: 1rem 1.1rem; border-radius: 12px; min-height: 115px;}
[data-testid="stMetricLabel"] {color: #9db2c4; font-size: .81rem;}
[data-testid="stMetricValue"] {font-size: 1.75rem; font-weight: 600; color: #f4f9fc;}
.panel-heading {margin: 1.2rem 0 .9rem;}
.panel-heading span {font-size: .65rem; font-weight: 700; color: var(--mint); letter-spacing: .11rem;}
.panel-heading h3 {font-size: 1.16rem; font-weight: 600; color: #f1f6fa; margin: .25rem 0 .3rem; padding: 0;}
.panel-heading p {font-size: .8rem; color: #91a7bb; margin: 0;}
[data-baseweb="tab-list"] {gap: 1.6rem; border-bottom: 1px solid #26394b; margin-top: 1.25rem;}
button[data-baseweb="tab"] {font-size: .92rem; color: #9bb0c2; padding-bottom: .8rem;}
button[data-baseweb="tab"][aria-selected="true"] {color: var(--mint);}
[data-testid="stExpander"] {border: 1px solid #26394b; background: #101f30;}
.news-card {background: #112235; border: 1px solid #25384a; border-left: 3px solid #55dec5; border-radius: 9px; padding: .9rem 1rem; margin-bottom: .7rem;}
.news-card h4 {font-size: .89rem; color: #eaf3f8; line-height: 1.6; padding: 0; margin: .25rem 0 .4rem;}
.news-card small {color: #8fa7ba; font-size: .71rem;}
.news-tag {color: var(--mint); font-size: .72rem; font-weight: 600;}
.footer {border-top: 1px solid #25384a; color: #71899f; font-size: .73rem; margin-top: 2rem; padding-top: 1rem;}
</style>""", unsafe_allow_html=True)


def market_tab(tables: dict[str, list[dict]], synthetic: bool) -> None:
    panel_title("MARKET WATCH", "관심 종목", "가격·유동성과 뉴스 수신을 함께 확인합니다. 상대 거래량이 없으면 미확인으로 표시합니다.")
    quotes = latest_by_symbol(tables["quote_snapshots"])
    stocks = latest_by_symbol(tables["stocks"])
    bars = latest_by_symbol(tables["market_bars"])
    rows = []
    for symbol in sorted(set(quotes) | set(stocks) | set(bars)):
        stock = stocks.get(symbol, {})
        quote = quotes.get(symbol, stock.get("quote", {}))
        price = number(quote.get("ask"), number(bars.get(symbol, {}).get("close")))
        previous = number(stock.get("prev_close"))
        bid, ask = number(quote.get("bid")), number(quote.get("ask"))
        spread = ((ask - bid) / ((ask + bid) / 2)) if ask + bid > 0 else None
        relative_volume = stock.get("relative_volume")
        rows.append({
            "종목": symbol, "매수 가능가 ($)": round(price, 4),
            "등락률 (%)": round((price / previous - 1) * 100, 2) if previous else None,
            "상대 거래량": round(number(relative_volume), 2) if relative_volume is not None else None,
            "거래대금 ($)": round(number(stock.get("dollar_volume")), 0) if stock.get("dollar_volume") is not None else None,
            "스프레드 (%)": round(spread * 100, 3) if spread is not None else None,
            "거래 상태": "거래정지" if quote.get("halted") else ("거래 가능" if quote.get("tradable") else "미확인"),
            "수신 (KST)": timestamp(quote.get("received_at"), short=True),
        })
    if rows:
        dataframe(rows, column_config={
            "매수 가능가 ($)": st.column_config.NumberColumn(format="$%.4f"),
            "등락률 (%)": st.column_config.NumberColumn(format="%.2f%%"),
            "스프레드 (%)": st.column_config.NumberColumn(format="%.3f%%"),
            "거래대금 ($)": st.column_config.NumberColumn(format="$%.0f"),
        })
    else:
        empty("아직 저장된 시장 데이터가 없습니다. 데모 실행 또는 검증된 데이터 어댑터의 수집이 필요합니다.")

    chart_col, news_col = st.columns([1.3, 1], gap="large")
    with chart_col:
        panel_title("PRICE ACTION", "종목별 가격 흐름", "1분봉의 종가 시각을 기준으로 표시합니다.")
        symbols = sorted({str(row.get("symbol")) for row in tables["market_bars"] if row.get("symbol")})
        if symbols:
            symbol = st.selectbox("차트를 볼 종목", symbols, label_visibility="collapsed")
            points = [{"timestamp": row.get("timestamp"), "종가 ($)": row.get("close")} for row in tables["market_bars"] if row.get("symbol") == symbol]
            frame = pd.DataFrame(points)
            frame["시각 (KST)"] = pd.to_datetime(frame["timestamp"], utc=True, errors="coerce").dt.tz_convert(SEOUL)
            frame = frame.dropna(subset=["시각 (KST)"]).sort_values("시각 (KST)")
            st.line_chart(frame, x="시각 (KST)", y="종가 ($)", color="#55dec5", height=310)
            if synthetic:
                st.caption("합성 시나리오 가격입니다. 실제 종목의 시세가 아닙니다.")
        else:
            empty("분봉 데이터가 수집되면 차트가 표시됩니다.")
    with news_col:
        panel_title("NEWS & EVENTS", "최근 뉴스", "발표 시각과 시스템 수신 시각을 구분합니다.")
        news = tables["news_events"][-6:][::-1]
        if not news:
            empty("수신한 뉴스가 없습니다.")
        for event in news:
            symbols = event.get("symbols", [])
            classification = event.get("classification") or {}
            if isinstance(classification, str):
                classification = {"sentiment": classification}
            sentiment = {"positive": "긍정 후보", "negative": "부정 후보", "neutral": "중립", "uncertain": "불확실"}.get(classification.get("sentiment"), "미분류")
            source = html.escape(str(event.get("source", "출처 미확인")))
            tags = html.escape(" · ".join(map(str, symbols)))
            title = html.escape(str(event.get("title", "제목 없음")))
            st.markdown(
                f'<div class="news-card"><span class="news-tag">{tags or "티커 미확인"} · {html.escape(sentiment)}</span>'
                f'<h4>{title}</h4><small>{source}<br>발표 {html.escape(timestamp(event.get("published_at"), short=True))}'
                f' · 수신 {html.escape(timestamp(event.get("received_at"), short=True))}</small></div>',
                unsafe_allow_html=True,
            )
        st.caption("뉴스 분류는 규칙에 따른 후보 판정입니다. 긍정 분류만으로 주문하지 않습니다.")


def monitor_market_tab(tables: dict[str, list[dict]], monitor: dict) -> None:
    panel_title("TOSS / PRICE WATCH", "토스 현재가", "제공자가 보낸 마지막 가격과 프로그램이 조회한 시각을 구분합니다.")
    stocks = latest_by_symbol([row for row in tables.get("stocks", []) if row.get("source") == "toss"])
    symbols = sorted(set(map(str, monitor.get("symbols") or stocks.keys())))
    rows = []
    for symbol in symbols:
        item = stocks.get(symbol, {})
        provider = quote_health(item.get("timestamp"))
        receipt = event_health({"last_event": item.get("received_at")})
        rows.append({
            "종목": symbol, "현재가 ($)": price_value(item.get("last_price")),
            "제공 시각 (KST)": timestamp(item.get("timestamp")),
            "제공 시각 경과": provider["lag_label"], "제공 시각 상태": provider["status"],
            "수신 시각 (KST)": timestamp(item.get("received_at")), "수신 경과": receipt["lag_label"],
            "거래 가능 여부": "미확인",
        })
    if rows:
        dataframe(rows, column_config={"현재가 ($)": st.column_config.NumberColumn(format="$%.4f")})
    else:
        empty("토스 현재가 조회를 기다리고 있습니다.")
    st.caption("수신 시각은 조회 응답을 받은 시각입니다. 제공 시각이 오래되거나 미확인이면 신선한 시세라고 판단하지 않습니다. 장 종료·최근 거래 부재에도 과거 가격이 반환될 수 있습니다.")
    st.caption("미국 시세는 일부 거래소 기반이며 통합 최우선 호가(NBBO)가 아닙니다. 이 현재가 조회에는 매수·매도 호가, 거래량, 상대 거래량, 거래정지 여부가 포함되지 않습니다.")

    chart_col, context_col = st.columns([1.3, 1], gap="large")
    with chart_col:
        panel_title("OBSERVED PRICES", "조회 시점별 가격", "프로그램이 관측한 현재가 기록입니다. 체결 틱이나 1분봉 차트가 아닙니다.")
        samples = tables.get("price_samples", [])
        chart_symbols = sorted({str(row.get("symbol")) for row in samples if row.get("symbol") in symbols})
        if chart_symbols:
            selected = st.selectbox("가격 기록을 볼 종목", chart_symbols, key="monitor_chart_symbol", label_visibility="collapsed")
            points = [{"received_at": row.get("received_at"), "현재가 ($)": price_value(row.get("last_price"))}
                      for row in samples if row.get("symbol") == selected]
            frame = pd.DataFrame(points)
            frame["조회 시각 (KST)"] = pd.to_datetime(frame["received_at"], utc=True, errors="coerce").dt.tz_convert(SEOUL)
            frame = frame.dropna(subset=["조회 시각 (KST)", "현재가 ($)"]).sort_values("조회 시각 (KST)")
            if not frame.empty:
                st.line_chart(frame, x="조회 시각 (KST)", y="현재가 ($)", color="#55dec5", height=310)
                st.caption("최근 조회 기록 최대 2,000개를 사용합니다. 같은 마지막 가격이 여러 번 관측될 수 있습니다.")
            else:
                empty("표시할 수 있는 가격·수신 시각 기록이 없습니다.")
        else:
            empty("가격 조회 기록이 쌓이면 차트가 표시됩니다.")
    with context_col:
        panel_title("MONITOR MODE", "현재 실행 범위")
        st.info("토스 현재가를 읽고 기록하는 시세 감시 모드입니다.")
        dataframe([
            {"항목": "현재가 수집", "상태": "토스 REST 주기 조회"},
            {"항목": "뉴스 수집", "상태": "미사용"},
            {"항목": "모의매매 엔진", "상태": "비활성"},
            {"항목": "실주문", "상태": "비활성"},
            {"항목": "계좌 잔고", "상태": "조회하지 않음"},
        ])
        st.caption("자동 진입 판단에는 검증된 시장 입력과 뉴스가 추가로 필요합니다.")


def monitor_system_tab(monitor: dict, tables: dict[str, list[dict]], db_path: Path, health: dict) -> None:
    panel_title("MONITOR HEALTH", "토스 시세 수집 상태", "저장된 조회 결과와 수신 경과를 표시합니다. 최근 조회 성공만으로 실시간 권한을 확인할 수 없습니다.")
    left, right = st.columns(2, gap="large")
    with left:
        dataframe([
            {"항목": "실행 모드", "상태": "시세 감시 전용"},
            {"항목": "토스 수집 상태", "상태": monitor_status(monitor, health)},
            {"항목": "마지막 조회 성공 (KST)", "상태": timestamp(monitor.get("last_success"))},
            {"항목": "응답 수신 경과", "상태": health["lag_label"]},
            {"항목": "조회 간격", "상태": f"{number(monitor.get('interval_seconds')):g}초" if monitor.get("interval_seconds") is not None else "미확인"},
            {"항목": "미국 시세 범위", "상태": "일부 거래소 · NBBO 아님"},
            {"항목": "실시간 데이터 권한 검증", "상태": "미확인"},
            {"항목": "자동매매", "상태": "비활성"},
        ])
    with right:
        error = monitor.get("error") or {}
        if isinstance(error, dict) and error:
            status = f" · HTTP {error['http_status']}" if error.get("http_status") is not None else ""
            st.error(f"수집기 오류: {error.get('code', 'unknown')}{status}")
            if error.get("message"):
                st.write(str(error["message"]))
        elif monitor.get("status") == "stopped":
            st.info("시세 수집이 중지되었습니다. 저장된 마지막 가격을 표시하고 있습니다.")
        else:
            st.caption("터미널에서 실행한 run-toss 명령이 수집 상태를 갱신합니다.")
        missing = monitor.get("missing_symbols") or []
        if missing:
            st.warning("응답에서 현재가를 확인하지 못한 종목: " + ", ".join(map(str, missing)))
        st.caption("조회 시각과 시세 제공 시각을 함께 확인하세요. 제공 시각이 없는 가격에 임의의 시각을 부여하지 않습니다.")
    panel_title("EVENT LOG", "시스템 로그")
    logs = tables.get("system_logs", [])[-100:][::-1]
    if logs:
        dataframe(logs)
    else:
        empty("저장된 시스템 로그가 없습니다.")
    with st.expander("데이터 경로", expanded=False):
        st.write("DB", str(db_path.resolve()))


def trading_tab(account: dict, state: dict, tables: dict[str, list[dict]]) -> None:
    panel_title("POSITIONS", "보유 포지션", "신규 진입 중지 중에도 보유 수량에 대한 손절·익절 관리는 계속됩니다.")
    positions = account.get("positions") or {}
    if isinstance(positions, list):
        positions = {p.get("symbol", str(i)): p for i, p in enumerate(positions)}
    quotes = latest_by_symbol(tables["quote_snapshots"])
    costs = None
    if state.get("settings"):
        try:
            from pennyquant.config.settings import Settings
            from pennyquant.trading.costs import FeeSchedule
            settings = Settings(**state["settings"])
            if settings.fee_model != "legacy" or settings.exit_price_policy == "net":
                costs = FeeSchedule(settings)
        except (TypeError, ValueError):
            st.warning("저장된 비용 설정을 확인하지 못해 포지션의 예상 청산 비용은 표시하지 않습니다.")
    rows = []
    for symbol, p in positions.items():
        bid = price_value(quotes.get(symbol, {}).get("bid"))
        qty = int(number(p.get("qty")))
        cost = number(p.get("avg_price"))
        pnl = None if bid is None else ((costs.proceeds(bid, qty) if costs else bid * qty) - cost * qty - number(p.get("entry_fee")))
        row = {"종목": symbol, "수량": qty, "평균 매수가 ($)": cost,
                     "매도 가능가 ($)": bid, "손절가 ($)": p.get("stop_price"), "익절가 ($)": p.get("target_price"),
                     "평가손익 ($)": round(pnl, 4) if pnl is not None else None,
                     "전략": STRATEGY_NAMES.get(p.get("strategy"), p.get("strategy", "—"))}
        if costs and qty > 0 and cost > 0:
            row["예상 손익분기가 ($)"] = costs.breakeven_bid(cost, qty, number(p.get("entry_fee")))
        rows.append(row)
    if rows:
        dataframe(rows)
        if costs:
            st.caption("평가손익과 손익분기가는 매수 비용·예상 매도 수수료·가격 불리함을 반영한 추정치입니다. 실제 체결·청구 금액은 달라질 수 있습니다.")
        else:
            st.caption("평가손익은 최근 매도 호가 기준이며 향후 매도 비용은 별도입니다.")
    else:
        empty("현재 보유한 포지션이 없습니다.")

    panel_title("ORDER BOOK", "주문 진행 상태", "고유 주문 ID로 미체결·부분 체결·취소 이력을 추적합니다.")
    orders = state.get("orders") or tables["orders"]
    if isinstance(orders, dict):
        orders = list(orders.values())
    if orders:
        dataframe([{
            "시각 (KST)": timestamp(row.get("created_at"), short=True), "종목": row.get("symbol"),
            "구분": {"buy": "매수", "sell": "매도"}.get(str(row.get("side")).lower(), row.get("side")),
            "주문 수량": row.get("qty"), "체결 수량": row.get("filled_qty", 0),
            "지정가 ($)": row.get("limit_price"), "평균 체결가 ($)": row.get("avg_fill_price"),
            "상태": STATUS_NAMES.get(str(row.get("status")), str(row.get("status"))),
            "주문 ID": row.get("client_id"),
        } for row in list(orders)[-100:][::-1]])
    else:
        empty("주문 기록이 없습니다.")

    with st.expander("체결 내역 확인", expanded=False):
        fills = state.get("fills") or tables["executions"]
        if fills:
            dataframe([{"시각 (KST)": timestamp(row.get("timestamp"), short=True), "종목": row.get("symbol"),
                        "구분": row.get("side"), "수량": row.get("qty"), "가격 ($)": row.get("price"),
                        "수수료 ($)": row.get("fee"), "주문 ID": row.get("client_id")} for row in fills[-100:][::-1]])
        else:
            st.caption("체결 기록이 없습니다.")

    panel_title("SIGNAL JOURNAL", "신호와 진입 근거")
    if tables["signals"]:
        dataframe([{"시각 (KST)": timestamp(row.get("timestamp"), short=True), "종목": row.get("symbol"),
                    "전략": STRATEGY_NAMES.get(row.get("strategy"), row.get("strategy")),
                    "기준가 ($)": row.get("reference_price"), "판단 근거": row.get("reason"),
                    "연결 뉴스": row.get("news_id")} for row in tables["signals"][-50:][::-1]])
    else:
        empty("생성된 매매 신호가 없습니다.")


def analysis_tab(report: dict) -> None:
    panel_title("STRATEGY LAB", "전략별 성과 비교", "같은 기간과 비용 가정에서 산출한 결과입니다. 표본 수와 데이터 출처를 함께 확인하세요.")
    strategies = report.get("strategies") or {}
    if not strategies:
        empty("성과 보고서가 없습니다. 데모 또는 백테스트를 실행하면 결과가 표시됩니다.")
        return
    rows = []
    for key, metric in strategies.items():
        profit_factor = metric.get("profit_factor")
        validation = metric.get("validation") or {}
        rows.append({
            "전략": STRATEGY_NAMES.get(key, key), "완료 거래": metric.get("trades", 0),
            "순손익 ($)": round(number(metric.get("net_pnl")), 4),
            "수익률 (%)": metric.get("return_pct"), "승률 (%)": number(metric.get("win_rate")) * 100,
            "Profit Factor": profit_factor, "최대 낙폭 (%)": metric.get("max_drawdown_pct"),
            "거래당 순손익 ($)": metric.get("avg_trade_pnl"),
            "최대 연속 손실": metric.get("max_consecutive_losses"),
            "미체결 비율 (%)": number(metric.get("unfilled_order_rate")) * 100,
            "검증 구간 거래": validation.get("trades"),
            "검증 구간 순손익 ($)": validation.get("net_pnl"),
        })
    dataframe(rows, column_config={
        "순손익 ($)": st.column_config.NumberColumn(format="$%.4f"),
        "수익률 (%)": st.column_config.NumberColumn(format="%.2f%%"),
        "승률 (%)": st.column_config.NumberColumn(format="%.2f%%"),
        "Profit Factor": st.column_config.NumberColumn(format="%.2f", help="총이익 ÷ 총손실. 손실 거래가 없으면 값이 정의되지 않을 수 있습니다."),
        "최대 낙폭 (%)": st.column_config.NumberColumn(format="%.2f%%"),
        "미체결 비율 (%)": st.column_config.NumberColumn(format="%.2f%%"),
        "검증 구간 순손익 ($)": st.column_config.NumberColumn(format="$%.4f"),
    })
    st.caption("순손익과 수익률은 미청산 포지션의 평가액을 포함합니다. 승률·PF·거래당 순손익은 완료 거래 기준입니다. 손실 거래가 없으면 PF는 계산 불가로 표시합니다.")
    split = report.get("split") or {}
    if split:
        st.caption(f"시간순 분리: 앞 구간 {split.get('train_sessions', '—')}개 세션 / 검증 구간 {split.get('validation_sessions', '—')}개 세션. 각 구간은 같은 초기자본으로 별도 실행하며 파라미터 최적화는 수행하지 않습니다.")
    st.caption("검증 구간 손익이 양수여도 거래 표본이 충분하다는 의미는 아닙니다. 실거래 승인은 별도 검증과 수동 승인이 필요합니다.")
    panel_title("EQUITY CURVE", "전략별 자산 추이")
    curve = report.get("equity_curve") or []
    if curve:
        frame = pd.DataFrame(curve)
        if {"timestamp", "strategy", "equity"} <= set(frame.columns):
            frame["시각 (KST)"] = pd.to_datetime(frame["timestamp"], utc=True, errors="coerce").dt.tz_convert(SEOUL)
            frame["전략"] = frame["strategy"].map(lambda x: STRATEGY_NAMES.get(x, x))
            wide = frame.dropna(subset=["시각 (KST)"]).pivot_table(index="시각 (KST)", columns="전략", values="equity", aggfunc="last").sort_index()
            st.line_chart(wide, height=360, color=["#55dec5", "#80a9ff", "#f5c578", "#b5a0f4"][:len(wide.columns)])
            st.caption("축의 시각은 한국 표준시(KST), 자산 단위는 USD입니다.")
        else:
            empty("자산 곡선의 형식을 확인해 주세요.")
    else:
        empty("자산 곡선 데이터가 없습니다.")
    trades = report.get("trades") or []
    if trades:
        with st.expander("거래별 성과 원본", expanded=False):
            dataframe(trades)
    st.download_button("성과 보고서 JSON 다운로드", json.dumps(report, ensure_ascii=False, indent=2), file_name="pennyquant-report.json", mime="application/json")


def system_tab(state: dict, tables: dict[str, list[dict]], report: dict, db_path: Path, report_path: Path, manual_halt: bool, synthetic: bool, health: dict) -> None:
    panel_title("SYSTEM HEALTH", "운영 상태", "확인된 상태만 표시합니다. 실거래 주문은 이 MVP에서 활성화할 수 없습니다.")
    left, right = st.columns(2, gap="large")
    with left:
        dataframe([
            {"항목": "주문 모드", "상태": "모의매매 전용"},
            {"항목": "신규 진입", "상태": "사용자 중지" if manual_halt else "사용자 중지 해제 · 위험 조건 적용"},
            {"항목": "토스증권 실거래", "상태": "비활성 · 공식 연동 검증 및 별도 승인 필요"},
            {"항목": "뉴스 연결", "상태": "연결 여부 미확인 · 저장된 이력만 표시"},
            {"항목": "시장 데이터", "상태": "합성 시나리오" if synthetic else "저장 데이터 · 실시간 연결 여부 미확인"},
            {"항목": "마지막 이벤트 (KST)", "상태": health["last_event"]},
            {"항목": "이벤트 경과", "상태": health["lag_label"]},
            {"항목": "데이터 신선도", "상태": health["status"]},
        ])
    with right:
        reasons = state.get("halt_reasons") or []
        if reasons:
            st.warning("엔진이 신규 진입을 제한하고 있습니다.")
            for reason in sorted(reasons):
                st.code(str(reason), language=None)
        else:
            st.info("저장된 엔진 상태에 중단 사유가 없습니다. 실제 엔진 실행 여부는 별도로 확인하세요.")
        st.caption("‘신규 진입 재개’는 사용자 중지만 해제합니다. 일일 손실, 미확인 주문, 잔고 불일치 등의 제한은 계속 적용됩니다.")
    panel_title("EVENT LOG", "시스템 로그")
    logs = tables["system_logs"][-100:][::-1]
    if logs:
        dataframe(logs)
    else:
        empty("저장된 시스템 로그가 없습니다.")
    with st.expander("데이터 경로와 실행 안내", expanded=False):
        st.write("DB", str(db_path.resolve()))
        st.write("보고서", str(report_path.resolve()))
        st.caption("대시보드는 저장된 상태를 읽습니다. 수집·전략·체결 처리는 CLI 엔진에서 실행해야 합니다. 3초 자동 갱신 또는 새로고침으로 최신 기록을 불러옵니다.")
        st.code("python -m pennyquant --help\nstreamlit run pennyquant/dashboard/app.py -- --db data/pennyquant.db --report data/report.json", language="bash")


TRIGGER_NAMES = {"rise": "급등", "surge": "급등", "gain": "상승률", "price_surge": "급등",
                 "fall": "급락", "drop": "급락", "decline": "급락", "price_drop": "급락",
                 "volume": "거래량 증가", "volume_surge": "거래량 급증", "relative_volume": "상대 거래량 증가",
                 "turnover": "거래대금 증가", "dollar_volume": "거래대금 증가", "active": "거래 활발",
                 "price_rise": "급등", "price_fall_watch": "급락 관찰", "high_market_volume": "거래량 상위",
                 "high_market_turnover": "거래대금 상위", "short_window_volume_spike": "단기 거래량 급증"}
READINESS_NAMES = {"READY": "조건 확인 완료", "WATCH": "관찰", "BLOCKED": "신규 매수 차단"}
SENTIMENT_NAMES = {"POSITIVE": "호재 후보", "NEGATIVE": "악재", "NEUTRAL": "중립",
                   "MIXED": "혼합", "UNKNOWN": "판단 보류", "UNCERTAIN": "판단 보류"}
BLOCKER_NAMES = {
    "daily_loss": "일일 손실 한도 도달", "daily_loss_limit": "일일 손실 한도 도달",
    "manual_halt": "사용자가 신규 진입 중지", "unknown_relative_volume": "상대 거래량 이력 미확인",
    "market_inputs_unverified": "매매용 시장 데이터 검증 대기", "missing_verified_snapshot": "종목·시장 조건 검증 대기",
    "unverified_market_data": "매매용 시장 데이터 검증 대기", "unknown_previous_close": "전일 종가 미확인",
    "not_tradable": "현재 거래 가능 상태 미확인", "unapproved_exchange": "거래소 확인 필요",
    "stale_quote": "최신 호가 확인 필요", "wide_spread": "호가 차이가 큼",
    "missing_primary_evidence": "SEC·공식 보도자료 근거 대기", "no_primary_evidence": "SEC·공식 보도자료 근거 대기",
    "llm_unavailable": "AI 분석 연결 필요", "llm_error": "AI 분석 실패 · 판단 보류",
    "negative_news": "악재 확인", "dilution_risk": "주식 희석 위험", "max_positions": "동시 보유 한도 도달",
    "max_positions_reached": "동시 보유 한도 도달", "falling_from_high": "고점 대비 하락폭 초과",
    "verified_market_snapshot_required": "매매용 시장 데이터 검증 대기",
    "primary_news_analysis_required": "SEC·공식 보도자료 분석 대기",
    "ranking_time_unknown": "순위 제공 시각 미확인", "ranking_stale_or_future": "순위 시각 확인 필요",
}


def text_items(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value] if value else []
    if isinstance(value, (list, tuple, set)):
        return [str(item) for item in value if item is not None]
    return []


def pipeline_candidate_rows(candidates: list[dict]) -> list[dict]:
    rows = []
    for item in latest_by_symbol(candidates).values():
        blockers = text_items(item.get("blockers") or item.get("readiness_blockers"))
        gain = item.get("gain_pct")
        state = str(item.get("readiness", "WATCH")).upper()
        rows.append({
            "종목": str(item.get("symbol", "—")),
            "선정 이유": " · ".join(TRIGGER_NAMES.get(v, v) for v in text_items(item.get("triggers"))) or "확인 중",
            "현재가 ($)": price_value(item.get("last_price")),
            "등락률 (%)": number(gain) * 100 if gain is not None else None,
            "거래량": item.get("trading_volume"), "거래대금 ($)": item.get("trading_amount"),
            "상대 거래량": item.get("relative_volume"),
            "판단 상태": READINESS_NAMES.get(state, "확인 중"),
            "관찰·차단 사유": " · ".join(BLOCKER_NAMES.get(v, v) for v in blockers) or "—",
            "제공 시각 상태": quote_health(item.get("timestamp") or item.get("ranked_at"))["status"],
            "수신 시각 (KST)": timestamp(item.get("received_at"), short=True),
        })
    return sorted(rows, key=lambda row: row["종목"])


def analysis_source(item: dict) -> str:
    kind = str(item.get("source_kind") or item.get("source") or "").lower()
    if "alpaca" in kind:
        return "Alpaca · 지연 보조 자료"
    if item.get("primary_verified") is True:
        return {"sec": "SEC · 원문 확인", "rss": "공식 RSS · 출처 확인"}.get(kind, "확인된 원문")
    return {"sec": "SEC · 원문 검증 대기", "rss": "RSS · 출처 검증 대기"}.get(kind, "출처 확인 필요")


def pipeline_alerts(state: dict, status: dict, tables: dict[str, list[dict]]) -> list[dict]:
    alerts = list(tables.get("alerts", []))
    for item in state.get("alerts") or []:
        if isinstance(item, dict) and item not in alerts:
            alerts.append(item)
    reasons = text_items(state.get("halt_reasons")) + text_items(status.get("halt_reasons"))
    if any(reason in {"daily_loss", "daily_loss_limit"} for reason in reasons):
        if not any("daily_loss" in str(item.get("code") or item.get("kind") or item.get("reason")) for item in alerts):
            alerts.append({"id": "daily-loss-latched", "level": "critical", "code": "daily_loss",
                           "message": "일일 손실 한도에 도달했습니다. 오늘 신규 매수는 중단하고 보유 포지션의 위험 관리는 계속합니다."})
    return alerts[-100:]


def alert_sound() -> bytes:
    samples = array("h", (int(6000 * math.sin(2 * math.pi * 660 * i / 16000)) for i in range(3200)))
    if sys.byteorder != "little":
        samples.byteswap()
    buffer = BytesIO()
    with wave.open(buffer, "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(16000)
        output.writeframes(samples.tobytes())
    return buffer.getvalue()


def render_pipeline_dashboard(repo, state: dict, tables: dict[str, list[dict]], status: dict,
                              db_path: Path, manual_halt: bool) -> None:
    mode = "live" if status.get("mode") == "live" else "paper"
    live_enabled = mode == "live" and status.get("live_orders_enabled") is True
    badge = "실주문 연결" if live_enabled else ("실주문 차단" if mode == "live" else "모의매매")
    st.markdown('<div class="hero"><div><div class="eyebrow">PENNYQUANT / MARKET & NEWS</div>'
                '<h1>시세·뉴스 통합 매매</h1><p>급증 종목 발견 → 공시·보도자료 확인 → AI 분석 → 전략 판단</p>'
                f'</div><span class="pill">{badge}</span></div>', unsafe_allow_html=True)
    if mode == "live":
        if live_enabled:
            st.warning("실주문 전송이 활성화된 실행입니다. 주문·체결 상태와 신규 진입 제한을 확인하세요.")
        else:
            st.info("실거래 연결 상태를 확인하고 있습니다. 현재 실주문 전송은 차단되어 있습니다.")
    else:
        st.info("모의매매 실행입니다. 조건을 모두 확인한 종목만 진입하며, 거래가 0건일 수 있습니다.")

    interval = number(status.get("interval_seconds"), 5.0)
    health = event_health({"last_event": status.get("last_cycle_at") or status.get("last_event")},
                          max_age=max(10.0, interval * 2 + 2))
    run_label = {"starting": "시작 중", "running": "감시 중", "stopped": "중지됨", "error": "오류"}.get(status.get("status"), "실행 상태 미확인")
    st.caption(f"실행 상태: {run_label} · 마지막 점검 {health['last_event']} · 경과 {health['lag_label']}")
    if health["stale"]:
        st.warning("최근 수집·분석 갱신을 확인하지 못했습니다. 저장된 결과를 표시하고 있습니다.")

    account = (state.get("account") or {}) if mode == "paper" else {}
    broker = repo.get_control("broker_state", {}) if repo else {}
    if mode == "live":
        broker = broker or (tables.get("broker_state") or [{}])[-1]
        if broker.get("reconciled") is True or broker.get("verified") is True:
            account = broker.get("account") or {}
    positions = account.get("positions") or {}
    positions_verified = bool(account)
    if mode == "live" and broker.get("reconciled") is True:
        positions = broker.get("positions") or positions
        positions_verified = True
    max_positions = int(number(status.get("max_positions"), 5))
    candidates = tables.get("candidates", [])
    if isinstance(status.get("active_symbols"), list):
        active = set(status["active_symbols"])
        candidates = [row for row in candidates if row.get("symbol") in active]
    rows = pipeline_candidate_rows(candidates)
    alerts = pipeline_alerts(state, status, tables)
    halt_reasons = list(dict.fromkeys(text_items(state.get("halt_reasons")) + text_items(status.get("halt_reasons"))))
    daily_latched = any(reason in {"daily_loss", "daily_loss_limit"} for reason in halt_reasons)
    metrics = st.columns(5)
    metrics[0].metric("후보 종목", f"{len(rows)}개")
    metrics[1].metric("보유 / 최대 종목", f"{len(positions)} / {max_positions}" if positions_verified else f"— / {max_positions}")
    equity = account.get("equity")
    start = account.get("day_start_equity")
    metrics[2].metric("총자산", usd(equity) if equity is not None else "—")
    pnl = number(equity) - number(start) if equity is not None and start is not None else None
    metrics[3].metric("당일 순손익", usd(pnl) if pnl is not None else "—",
                      help="수수료를 반영한 실현·평가 손익 기준입니다. 실제 청구 금액은 증권사 체결 내역으로 확인합니다.")
    metrics[4].metric("신규 진입", "당일 손실로 중지" if daily_latched else ("사용자 중지" if manual_halt else "조건 충족 시 검토"))

    for alert in alerts[-5:][::-1]:
        message = str(alert.get("message") or BLOCKER_NAMES.get(str(alert.get("code")), "위험 관리 알림을 확인하세요."))
        severity = str(alert.get("level") or alert.get("severity") or "warning").lower()
        (st.error if severity in {"critical", "error", "fatal"} else st.warning)(message)
    identifiers = {str(alert.get("id") or alert.get("code") or alert.get("timestamp") or index)
                   for index, alert in enumerate(alerts)}
    previous = st.session_state.get("system_alert_ids")
    if previous is not None and identifiers - set(previous) and st.session_state.get("risk_audio_enabled", False):
        st.audio(alert_sound(), format="audio/wav", autoplay=True)
    st.session_state["system_alert_ids"] = sorted(identifiers)

    market, news, trading, operations = st.tabs(["후보 종목", "뉴스 · AI 분석", "주문 · 포지션", "운영 · 알림"])
    with market:
        panel_title("MARKET SIGNALS", "감시 후보", "급락도 관찰 대상으로 표시합니다. 후보 선정은 매수나 공매도 지시가 아닙니다.")
        if rows:
            dataframe(rows, column_config={"현재가 ($)": st.column_config.NumberColumn(format="$%.4f"),
                                          "등락률 (%)": st.column_config.NumberColumn(format="%.2f%%"),
                                          "거래대금 ($)": st.column_config.NumberColumn(format="$%.0f")})
        else:
            empty("아직 후보가 없습니다. 시세 감지가 시작되거나 조건에 맞는 종목이 나올 때까지 기다립니다.")
        st.caption("순위 데이터의 기준가격은 공식 전일 종가와 다를 수 있습니다. 상대 거래량이나 거래 가능 상태가 미확인이면 진입을 보류합니다.")
    with news:
        panel_title("EVIDENCE & AI", "사건과 판단 근거", "SEC·공식 보도자료를 확인하고, Alpaca 지연 뉴스는 보조로 사용합니다.")
        analyses = tables.get("news_analysis", [])[-50:][::-1]
        if not analyses:
            empty("아직 분석 결과가 없습니다. 원문 확인이나 AI 연결에 실패하면 판단을 보류합니다.")
        for item in analyses:
            event = item.get("event") if isinstance(item.get("event"), dict) else {}
            sentiment = str(item.get("sentiment") or item.get("overall_sentiment") or "UNKNOWN").upper()
            symbol = str(item.get("ticker") or item.get("symbol") or "종목 미확인")
            title = str(item.get("title") or item.get("headline") or event.get("title") or "뉴스 분석 결과")
            label = SENTIMENT_NAMES.get(sentiment, "판단 보류")
            st.markdown(f'<div class="news-card"><span class="news-tag">{html.escape(symbol)} · {html.escape(label)}</span>'
                        f'<h4>{html.escape(title)}</h4><small>{html.escape(analysis_source(item))} · '
                        f'{html.escape(timestamp(item.get("analyzed_at"), short=True))}</small></div>', unsafe_allow_html=True)
            st.write(str(item.get("reason") or "판단 근거 확인 필요"))
            st.caption(f"발표 {timestamp(item.get('published_at') or event.get('published_at'), short=True)} · "
                       f"수신 {timestamp(item.get('received_at') or event.get('received_at'), short=True)}")
            if item.get("error"):
                st.warning("분석 처리 상태: " + str(item["error"]))
            missing = text_items(item.get("missing_information"))
            if missing:
                st.caption("추가 확인: " + " · ".join(missing))
            evidence = text_items(item.get("evidence"))
            for event in item.get("events") or []:
                if isinstance(event, dict):
                    evidence.extend(text_items(event.get("evidence")))
            if evidence:
                with st.expander(f"{symbol} 원문 근거", expanded=False):
                    for quote in list(dict.fromkeys(evidence)):
                        st.text(quote)
            st.caption("분석 당시 근거 적합 · 현재 진입은 시세·위험 조건 재확인" if item.get("entry_eligible") is True
                       else "관찰·보조 자료 · 이 결과만으로 진입하지 않음")
        st.caption("호재 분류와 모델의 신뢰도 값은 수익률이나 상승 확률을 뜻하지 않습니다.")
    with trading:
        if account:
            trading_tab(account, state if mode == "paper" else {}, tables)
        elif positions_verified:
            panel_title("CONFIRMED HOLDINGS", "확인된 보유 수량")
            if positions:
                dataframe([{"종목": symbol, "보유 수량": quantity.get("qty") if isinstance(quantity, dict) else quantity}
                           for symbol, quantity in positions.items()])
            else:
                empty("조회한 계좌에 보유 종목이 없습니다.")
            st.caption("확인된 보유 수량만 표시합니다. 매수 가능 금액을 현금 잔고나 총자산으로 대신 표시하지 않습니다.")
        else:
            empty("확인된 계좌·포지션 상태가 없습니다. 임의의 잔고나 손익을 표시하지 않습니다.")
        intents = tables.get("order_intents", [])[-50:][::-1]
        if intents:
            panel_title("ORDER REVIEW", "주문 검토 이력")
            dataframe([{"시각 (KST)": timestamp(v.get("created_at") or v.get("timestamp"), short=True),
                        "종목": v.get("symbol"), "구분": v.get("side"), "수량": v.get("qty"),
                        "상태": STATUS_NAMES.get(str(v.get("status")), v.get("status")),
                        "사유": v.get("reason") or v.get("blockers")} for v in intents])
    with operations:
        panel_title("RISK & OPERATIONS", "운영 상태", "일일 손실 중지는 사용자 중지 해제로 풀리지 않습니다. 보유 수량의 위험 관리는 계속합니다.")
        dataframe([
            {"항목": "실행 모드", "상태": "실거래" if mode == "live" else "모의매매"},
            {"항목": "실주문 전송", "상태": "활성" if live_enabled else "차단"},
            {"항목": "동시 보유 한도", "상태": f"{max_positions}종목"},
            {"항목": "당일 손실 한도", "상태": f"{number(status.get('daily_loss_pct')) * 100:g}%" if status.get("daily_loss_pct") is not None else "설정 확인 필요"},
            {"항목": "당일 손실 중지", "상태": "적용 중" if daily_latched else "한도 도달 이력 없음"},
            {"항목": "사용자 신규 진입 중지", "상태": "적용 중" if manual_halt else "해제"},
            {"항목": "Alpaca 뉴스", "상태": "약 15분 지연 · 보조 자료"},
            {"항목": "AI 분석", "상태": str(status.get("llm_model") or "분석 결과에서 확인")},
            {"항목": "수수료·청산 기준", "상태": str(status.get("fee_summary") or "저장된 비용·익절·손절 설정 적용")},
        ])
        if halt_reasons:
            st.warning("신규 진입 제한: " + " · ".join(BLOCKER_NAMES.get(reason, reason) for reason in halt_reasons))
        if status.get("error"):
            error = status["error"]
            st.error("최근 처리 오류: " + str(error.get("message") or error.get("code")) if isinstance(error, dict)
                     else "최근 처리 오류: " + str(error))
        if alerts:
            panel_title("ALERT HISTORY", "위험 알림 이력")
            dataframe([{"시각 (KST)": timestamp(item.get("timestamp") or item.get("created_at"), short=True),
                        "수준": item.get("level") or item.get("severity"), "종목": item.get("symbol"),
                        "내용": item.get("message")} for item in alerts[::-1]])
        logs = tables.get("system_logs", [])[-100:][::-1]
        if logs:
            with st.expander("운영 로그", expanded=False):
                dataframe(logs)
        with st.expander("저장 경로", expanded=False):
            st.write(str(db_path.resolve()))
    st.markdown(f'<div class="footer">PennyQuant · 화면 갱신 {datetime.now(timezone.utc).astimezone(SEOUL).strftime("%Y.%m.%d %H:%M:%S KST")} · 조건을 충족하지 않으면 거래하지 않습니다.</div>', unsafe_allow_html=True)


def render_dashboard(db_path: Path, report_path: Path, requested_mode: str = "auto") -> None:
    repo, state, tables, db_error = load_database(db_path)
    try:
        account = state.get("account") or {}
        manual_halt = bool(repo.get_control("manual_halt", False)) if repo else False
        source = repo.get_control("source", {}) if repo else {}
        runtime = repo.get_control("runtime", {}) if repo else {}
        monitor = repo.get_control("monitor", {}) if repo else {}
        pipeline = repo.get_control("pipeline_status", {}) if repo else {}
        runtime_kind = repo.get_control("runtime_kind", None) if repo else None
        if pipeline_mode(runtime, pipeline, requested_mode, runtime_kind):
            if db_error:
                st.error(db_error)
            if source.get("synthetic") is True or pipeline.get("synthetic") is True:
                st.info("합성 데이터 데모입니다. AI 분류는 고정된 검증용 결과이며 실제 뉴스·LLM 호출·실거래가 아닙니다.")
            render_pipeline_dashboard(repo, state, tables, pipeline, db_path, manual_halt)
            return
        observing = monitor_mode(runtime, monitor, requested_mode)
        report, report_error = ({}, None) if observing else read_report(report_path)
        synthetic = source.get("synthetic", report.get("synthetic")) is True
        if observing:
            max_age = max(5.0, number(monitor.get("interval_seconds"), 3.0) * 2 + 2)
            health = event_health({"last_event": runtime.get("last_event") or monitor.get("last_success")}, max_age=max_age)
        else:
            health = event_health(runtime)

        if observing:
            st.markdown('<div class="hero"><div><div class="eyebrow">PENNYQUANT / TOSS WATCH</div><h1>토스 시세 모니터링</h1><p>현재가 조회 · 제공 시각 · 수신 시각</p></div><span class="pill">시세 감시 전용</span></div>', unsafe_allow_html=True)
        else:
            st.markdown('<div class="hero"><div><div class="eyebrow">PENNYQUANT / OPERATIONS</div><h1>미국 저가주 모니터링</h1><p>뉴스 · 시장 · 전략 · 위험 관리</p></div><span class="pill">모의매매 전용</span></div>', unsafe_allow_html=True)
        if db_error:
            st.error(db_error)
        if report_error:
            st.error(report_error)
        if observing:
            st.info("토스 현재가를 주기적으로 조회하는 읽기 전용 화면입니다. 자동매매와 계좌 조회는 비활성 상태입니다.")
            status = monitor_status(monitor, health)
            if monitor.get("status") == "error" or monitor.get("error"):
                st.error(f"토스 수집 상태: {status}. 시스템 관리 탭에서 오류 내용을 확인하세요.")
            elif monitor.get("status") == "stopped":
                st.warning("시세 수집이 중지되었습니다. 저장된 마지막 조회 결과를 표시합니다.")
            else:
                st.caption(f"토스 수집 상태: {status}")
            event_text = f"마지막 조회 응답 {health['last_event']} · 응답 수신 후 {health['lag_label']} 경과"
            if health["stale"]:
                st.warning(event_text)
            else:
                st.caption(event_text)
        elif synthetic:
            st.info("모니터링 데이터는 합성 시나리오 데모입니다. 표시된 손익은 구현 검증용이며 실제 시장 성과가 아닙니다.", icon="🧪")
        elif report:
            st.caption("저장된 백테스트 보고서를 표시합니다. 이 화면만으로 실시간 연결 상태나 실거래 가능 여부를 판단하지 마세요.")
        else:
            st.info("데이터를 기다리고 있습니다. DB와 보고서가 생성되면 새로고침해 주세요.")
        if not observing:
            event_text = f"마지막 이벤트 {health['last_event']} · 경과 {health['lag_label']} · {health['status']}"
            if synthetic:
                st.caption(f"{event_text} · 합성·재생 기록의 시각이며 실시간 연결 상태가 아닙니다.")
            elif health["stale"]:
                st.warning(event_text)
            else:
                st.caption(event_text)

        if observing:
            watched = set(monitor.get("symbols") or [])
            available = [row for row in tables.get("stocks", []) if row.get("source") == "toss"
                         and (not watched or row.get("symbol") in watched)
                         and price_value(row.get("last_price")) is not None]
            metrics = st.columns(4)
            metrics[0].metric("수집 상태", monitor_status(monitor, health))
            metrics[1].metric("관심 종목", f"{len(monitor.get('symbols') or available)}개")
            metrics[2].metric("현재가 수신 종목", f"{len(available)}개")
            metrics[3].metric("자동매매", "비활성")
        else:
            equity = number(account.get("equity"))
            start = number(account.get("day_start_equity"), equity)
            daily_pnl = equity - start
            positions = account.get("positions") or {}
            metrics = st.columns(5)
            metrics[0].metric("총자산", usd(equity) if account else "—")
            metrics[1].metric("최근 세션 손익", usd(daily_pnl) if account else "—", delta=f"{daily_pnl / start * 100:+.2f}%" if start else None, help="최근 거래 세션의 실현·평가손익을 합산합니다.")
            if account:
                metrics[2].metric("가용 현금", usd(number(account.get("cash")) - number(account.get("reserved_cash"))))
            else:
                metrics[2].metric("가용 현금", "—")
            metrics[3].metric("보유 종목", f"{len(positions)}개" if account else "—")
            metrics[4].metric("최근 세션 신규 진입", f"{int(number(account.get('entries_today')))}회" if account else "—")

        market, trading, analysis, system = st.tabs(["시장 모니터링", "주문 · 포지션", "전략 분석", "시스템 관리"])
        with market:
            if observing:
                monitor_market_tab(tables, monitor)
            else:
                market_tab(tables, synthetic)
        with trading:
            if observing:
                empty("현재는 시세 감시 모드입니다. 모의주문·체결·포지션 관리는 실행하지 않습니다.")
            else:
                trading_tab(account, state, tables)
        with analysis:
            if observing:
                empty("시세 감시 모드에서는 전략 성과를 계산하지 않습니다. 모의매매 또는 백테스트를 실행한 뒤 성과를 확인할 수 있습니다.")
            else:
                if report.get("synthetic"):
                    st.caption("이 성과 보고서는 합성 데이터로 생성했습니다. 실제 수익성 검증 결과가 아닙니다.")
                analysis_tab(report)
        with system:
            if observing:
                monitor_system_tab(monitor, tables, db_path, health)
            else:
                system_tab(state, tables, report, db_path, report_path, manual_halt, synthetic, health)
        purpose = "토스 현재가를 관측하는 읽기 전용 환경" if observing else "수익성을 검증하는 연구·모의매매 환경"
        st.markdown(f'<div class="footer">PennyQuant v0.1.0 MVP · 화면 갱신 {datetime.now(timezone.utc).astimezone(SEOUL).strftime("%Y.%m.%d %H:%M:%S KST")} · {purpose}</div>', unsafe_allow_html=True)
    finally:
        if repo:
            repo.close()


def main() -> None:
    args = arguments()
    db_path, report_path = Path(args.db), Path(args.report)
    st.set_page_config(page_title="PennyQuant · 시장 모니터", page_icon="◈", layout="wide", initial_sidebar_state="expanded")
    style()
    repo, _, _, _ = load_database(db_path)
    try:
        manual_halt = bool(repo.get_control("manual_halt", False)) if repo else False
        runtime = repo.get_control("runtime", {}) if repo else {}
        monitor_state = repo.get_control("monitor", {}) if repo else {}
        pipeline = repo.get_control("pipeline_status", {}) if repo else {}
        runtime_kind = repo.get_control("runtime_kind", None) if repo else None
        integrated = pipeline_mode(runtime, pipeline, args.mode, runtime_kind)
        observing = monitor_mode(runtime, monitor_state, args.mode)
        with st.sidebar:
            st.markdown('<div class="brand">Penny<b>Quant</b></div><div class="brand-sub">NEWS-DRIVEN TRADING LAB</div>', unsafe_allow_html=True)
            if observing and not integrated:
                st.markdown('<span class="pill">MONITOR ONLY</span>', unsafe_allow_html=True)
                st.markdown("### 시세 감시 전용")
                st.info("자동매매 비활성")
                st.caption("토스 현재가를 주기적으로 조회합니다. 뉴스·매매 엔진·계좌 잔고 조회는 사용하지 않습니다.")
                st.caption("종료하려면 run-toss를 실행한 터미널에서 Ctrl+C를 누르세요.")
            else:
                label = "SYSTEM / LIVE" if integrated and pipeline.get("mode") == "live" else ("SYSTEM / PAPER" if integrated else "PAPER ONLY")
                st.markdown(f'<span class="pill">{label}</span>', unsafe_allow_html=True)
                st.markdown("### 매매 제어")
                st.caption("신규 진입만 제어합니다. 보유 포지션의 위험 관리는 계속됩니다.")
                if manual_halt:
                    st.warning("신규 진입 중지 중", icon="⏸️")
                    if st.button("신규 진입 재개", width="stretch", type="primary", disabled=repo is None):
                        repo.set_control("manual_halt", False)
                        st.rerun()
                else:
                    st.success("사용자 중지 해제", icon="✅")
                    if st.button("신규 진입 중지", width="stretch", disabled=repo is None):
                        repo.set_control("manual_halt", True)
                        st.rerun()
                if integrated:
                    st.caption("사용자 중지 해제 후에도 일일 손실·미확인 주문·데이터 미검증 등의 제한은 유지됩니다.")
                    st.toggle("새 위험 알림 소리", value=False, key="risk_audio_enabled",
                              help="이 화면을 연 상태에서 새 위험 알림이 발생하면 짧은 소리를 재생합니다. 브라우저 설정에 따라 재생이 제한될 수 있습니다.")
                else:
                    st.caption("연속 모의매매 엔진은 대기 중에도 약 0.2초 간격으로 제어값을 확인합니다. 상태 표시는 다음 갱신에 반영됩니다.")
            st.divider()
            auto_refresh = st.toggle("자동 갱신 · 3초", value=True, key="auto_refresh")
            if st.button("데이터 새로고침", width="stretch"):
                st.rerun()
            st.markdown('<div class="small-text">표시 시각: 한국 표준시 (KST)<br>거래 세션 판정: 미국 동부시간<br>통화: 미국 달러 (USD)</div>', unsafe_allow_html=True)
            st.divider()
            st.caption("합성 데이터와 실제 수집 데이터를 구분하고, 검증된 전략만 다음 단계로 진행하세요.")
    finally:
        if repo:
            repo.close()

    @st.fragment(run_every="3s" if auto_refresh else None)
    def monitor() -> None:
        render_dashboard(db_path, report_path, args.mode)

    monitor()


if __name__ == "__main__":
    main()
