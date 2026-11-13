"""Korean paper-trading monitor. Run from the project root with Streamlit."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import html
import json
import math
import os
from pathlib import Path
import sys
from typing import Any
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
    names = ("stocks", "market_bars", "quote_snapshots", "news_events", "signals", "orders", "executions", "positions", "daily_performance", "system_logs")
    tables: dict[str, list[dict]] = {name: [] for name in names}
    if not path.exists():
        return None, {}, tables, None
    repo = None
    try:
        from pennyquant.database.repository import Repository
        repo = Repository(path)
        state = repo.load_state() or {}
        for name in names:
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


def trading_tab(account: dict, state: dict, tables: dict[str, list[dict]]) -> None:
    panel_title("POSITIONS", "보유 포지션", "신규 진입 중지 중에도 보유 수량에 대한 손절·익절 관리는 계속됩니다.")
    positions = account.get("positions") or {}
    if isinstance(positions, list):
        positions = {p.get("symbol", str(i)): p for i, p in enumerate(positions)}
    quotes = latest_by_symbol(tables["quote_snapshots"])
    rows = []
    for symbol, p in positions.items():
        bid = number(quotes.get(symbol, {}).get("bid"), number(p.get("avg_price")))
        qty = int(number(p.get("qty")))
        cost = number(p.get("avg_price"))
        rows.append({"종목": symbol, "수량": qty, "평균 매수가 ($)": cost,
                     "매도 가능가 ($)": bid, "손절가 ($)": p.get("stop_price"), "익절가 ($)": p.get("target_price"),
                     "평가손익 ($)": round((bid - cost) * qty - number(p.get("entry_fee")), 4),
                     "전략": STRATEGY_NAMES.get(p.get("strategy"), p.get("strategy", "—"))})
    if rows:
        dataframe(rows)
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


def render_dashboard(db_path: Path, report_path: Path) -> None:
    repo, state, tables, db_error = load_database(db_path)
    try:
        report, report_error = read_report(report_path)
        account = state.get("account") or {}
        manual_halt = bool(repo.get_control("manual_halt", False)) if repo else False
        source = repo.get_control("source", {}) if repo else {}
        runtime = repo.get_control("runtime", {}) if repo else {}
        synthetic = source.get("synthetic", report.get("synthetic")) is True
        health = event_health(runtime)

        st.markdown('<div class="hero"><div><div class="eyebrow">PENNYQUANT / OPERATIONS</div><h1>미국 저가주 모니터링</h1><p>뉴스 · 시장 · 전략 · 위험 관리</p></div><span class="pill">모의매매 전용</span></div>', unsafe_allow_html=True)
        if db_error:
            st.error(db_error)
        if report_error:
            st.error(report_error)
        if synthetic:
            st.info("모니터링 데이터는 합성 시나리오 데모입니다. 표시된 손익은 구현 검증용이며 실제 시장 성과가 아닙니다.", icon="🧪")
        elif report:
            st.caption("저장된 백테스트 보고서를 표시합니다. 이 화면만으로 실시간 연결 상태나 실거래 가능 여부를 판단하지 마세요.")
        else:
            st.info("데이터를 기다리고 있습니다. DB와 보고서가 생성되면 새로고침해 주세요.")
        event_text = f"마지막 이벤트 {health['last_event']} · 경과 {health['lag_label']} · {health['status']}"
        if synthetic:
            st.caption(f"{event_text} · 합성·재생 기록의 시각이며 실시간 연결 상태가 아닙니다.")
        elif health["stale"]:
            st.warning(event_text)
        else:
            st.caption(event_text)

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
            market_tab(tables, synthetic)
        with trading:
            trading_tab(account, state, tables)
        with analysis:
            if report.get("synthetic"):
                st.caption("이 성과 보고서는 합성 데이터로 생성했습니다. 실제 수익성 검증 결과가 아닙니다.")
            analysis_tab(report)
        with system:
            system_tab(state, tables, report, db_path, report_path, manual_halt, synthetic, health)
        st.markdown(f'<div class="footer">PennyQuant v0.1.0 MVP · 화면 갱신 {datetime.now(timezone.utc).astimezone(SEOUL).strftime("%Y.%m.%d %H:%M:%S KST")} · 수익성을 검증하는 연구·모의매매 환경</div>', unsafe_allow_html=True)
    finally:
        if repo:
            repo.close()


def main() -> None:
    args = arguments()
    db_path, report_path = Path(args.db), Path(args.report)
    st.set_page_config(page_title="PennyQuant · 모의매매 모니터", page_icon="◈", layout="wide", initial_sidebar_state="expanded")
    style()
    repo, _, _, _ = load_database(db_path)
    try:
        manual_halt = bool(repo.get_control("manual_halt", False)) if repo else False
        with st.sidebar:
            st.markdown('<div class="brand">Penny<b>Quant</b></div><div class="brand-sub">NEWS-DRIVEN TRADING LAB</div>', unsafe_allow_html=True)
            st.markdown('<span class="pill">PAPER ONLY</span>', unsafe_allow_html=True)
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
        render_dashboard(db_path, report_path)

    monitor()


if __name__ == "__main__":
    main()
