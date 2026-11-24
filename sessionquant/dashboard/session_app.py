"""SessionQuant의 실제 계좌와 전략·뉴스·운영 상태 및 거래 제어를 표시한다."""
from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import os
import logging
from pathlib import Path
import sys
from time import monotonic
from typing import Any
from zoneinfo import ZoneInfo

# 파일 경로로 Streamlit을 실행할 때도 설치한 패키지와 같은 모듈을 찾는다.
PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

SEOUL = ZoneInfo("Asia/Seoul")
TABLES = (
    "market_regimes", "candidates", "quote_snapshots", "market_bars", "news_events", "news_analysis",
    "macro_news", "macro_analysis", "llm_usage", "orders", "executions", "positions", "alerts",
    "session_state", "account_snapshots", "reconciliations", "daily_performance", "pipeline_status",
    "news_ingestions", "news_enrichments", "economic_events",
)
REGIMES = {"BULLISH": "상승", "NEUTRAL": "중립", "BEARISH": "하락", "UNKNOWN": "확인 필요"}
STATUS_NAMES = {
    'submitted':'제출', 'accepted':'모의 접수', 'cancel_pending':'취소 확인 대기',
    "pending": "접수 대기", "open": "미체결", "partial": "부분 체결", "filled": "체결 완료",
    "cancelled": "취소", "rejected": "거절", "unknown": "확인 필요",
}


@dataclass
class DashboardData:
    """비밀정보를 포함하지 않는 화면용 데이터 묶음이다."""

    state: dict = field(default_factory=dict)
    context: dict = field(default_factory=dict)
    tables: dict[str, list[dict]] = field(default_factory=lambda: {name: [] for name in TABLES})
    backend: str = "sqlite"
    error: str | None = None
    unavailable: set[str] = field(default_factory=set)
    connected: bool = False
    actual_account: dict | None = None
    account_status: dict = field(default_factory=dict)
    watchlist_state: dict = field(default_factory=dict)
    market_quality: dict = field(default_factory=dict)
    market_performance: dict = field(default_factory=dict)
    candle_incidents: dict = field(default_factory=dict)
    watchlist_rvol: dict = field(default_factory=dict)
    history_backfill: dict = field(default_factory=dict)
    market_reference: dict = field(default_factory=dict)
    benchmark_stream: dict = field(default_factory=dict)
    close_display: dict = field(default_factory=dict)
    fred_references: list = field(default_factory=list)
    news_status: dict = field(default_factory=dict)
    strategy_explanation: dict = field(default_factory=dict)
    portfolio_selection: dict = field(default_factory=dict)
    news_flow: dict = field(default_factory=dict)
    warmup: dict = field(default_factory=dict)
    engine_state: dict = field(default_factory=dict)
    retention: dict = field(default_factory=dict)


def decimal_value(value: Any, default: Decimal | None = None) -> Decimal | None:
    """표시용 금액을 Decimal로 읽고 NaN과 무한대는 거부한다."""
    try:
        if isinstance(value, bool) or value is None:
            return default
        result = Decimal(str(value))
        return result if result.is_finite() else default
    except (InvalidOperation, ValueError, TypeError):
        return default


def usd(value: Any) -> str:
    amount = decimal_value(value)
    return "—" if amount is None else f"${amount:,.2f}"


def timestamp(value: Any) -> str:
    """시간대가 명시된 시각만 KST로 변환한다."""
    if not value:
        return "—"
    try:
        parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            return "시간대 확인 필요"
        return parsed.astimezone(SEOUL).strftime("%m.%d %H:%M:%S KST")
    except (TypeError, ValueError, OverflowError):
        return "시각 확인 필요"


def age_seconds(value: Any, now: datetime) -> float | None:
    try:
        parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None or now.tzinfo is None:
            return None
        age = (now - parsed).total_seconds()
        return age if age >= 0 else None
    except (TypeError, ValueError):
        return None


def load_database(path: str | Path, *, page=None, news_section=None, panel=None) -> DashboardData:
    """선택한 화면의 기록만 읽는다. 중간 실패는 이미 읽은 자료를 지우지 않는다."""
    from sessionquant.dashboard.load_plan import CONTROLS, SOURCE_LABELS, read_plan
    controls, tables = read_plan(page, news_section=news_section, panel=panel)
    include_state = panel is None or panel == 'live'
    include_fred = page is None or (page == '시장·종목' and panel in {None, '시장 참고'})
    include_news = page is None or page == '뉴스·LLM' or (page == '대시보드' and panel in {None, 'news'})
    pending = {*controls, *tables}
    if include_state:
        pending.add('state')
    if include_fred:
        pending.add('fred_references')
    if 'news_status' in controls:
        pending.add('news_schedule')
    if include_news:
        pending.add('news_flow')
    result = DashboardData()
    repository = None
    source = 'connection'
    started = monotonic()

    def read(name, operation):
        nonlocal source, started
        source, started = name, monotonic()
        value = operation()
        pending.discard(name)
        return value

    try:
        from sessionquant.local_env import load_local_env
        load_local_env(database_only=True)
        from sessionquant.database.config import database_backend
        from sessionquant.database.repository import KINDS, Repository

        result.backend = database_backend()
        if result.backend == "sqlite" and not Path(path).exists():
            return result
        repository = Repository(path, initialize=False)
        from contextlib import nullcontext
        with repository.transaction() if hasattr(repository,'transaction') else nullcontext():
            if include_state:
                result.state = read('state', repository.load_dashboard_state if page == '대시보드' else repository.load_state) or {}
            for name in controls:
                key, _ = CONTROLS[name]
                value = read(name, lambda: repository.get_control(key))
                setattr(result, name, value if name == 'actual_account' else value or {})
            broker_status = (read('broker_status', lambda: repository.get_control('broker_state', {})) or {}) if not result.context else {}
            if broker_status.get("mode") == "live" and not result.context:
                result.context={"environment":"LIVE_LOCAL","mode":"live","synthetic":False,
                    "updated_at":broker_status.get("observed_at"),"source_label":"실계좌 주문·체결 기록"}
            if result.actual_account and not result.context:
                result.context={"environment":"LIVE_LOCAL","mode":"account_readonly","synthetic":False,
                    "updated_at":result.actual_account.get("received_at"),"source_label":"토스 실제 계좌 조회"}
            if 'news_status' in controls:
                result.news_status['schedule'] = read('news_schedule', lambda: repository.get_control('news_provider_schedule_v1', {})) or {}
            for name, limit in tables.items():
                if name in KINDS:
                    result.tables[name] = read(name, lambda: repository.recent(name, limit=limit))
            if page == '대시보드' and include_state and result.tables['executions']:
                latest = max(result.tables['executions'], key=lambda row: str(row.get('timestamp') or row.get('filled_at') or ''))
                client_id = latest.get('client_id')
                own_reason = latest.get('reason')
                if (str(latest.get('side')).lower() == 'sell' and isinstance(client_id, str) and client_id
                        and not (isinstance(own_reason, str) and own_reason.strip())):
                    def order_reasons():
                        stored = repository.load_dashboard_order(client_id)
                        if stored:
                            result.state.setdefault('broker', {}).setdefault('orders', {})[client_id] = stored
                        order = repository.load_dashboard_order(client_id, recovery_state=False)
                        if order:
                            result.tables['orders'] = [order]
                    read('latest_order', order_reasons)
                older_order_ids = list(dict.fromkeys(
                    row['client_id'] for row in result.tables['executions']
                    if str(row.get('side')).lower() == 'sell'
                    and isinstance(row.get('client_id'), str) and row['client_id']
                    and row['client_id'] != latest.get('client_id')
                    and not row.get('reason')))
                if older_order_ids:
                    orders = read('history_orders', lambda: repository.get_records('orders', older_order_ids))
                    result.tables['orders'].extend(orders.values())
            if include_fred:
                result.fred_references = read('fred_references', lambda: repository.recent('fred_reference_observations', limit=30))
            if include_news:
                from sessionquant.news.diagnostics import summarize_news
                limit=300 if page=='대시보드' else 500
                result.news_flow=read('news_flow', lambda: summarize_news(result.tables['news_ingestions'],repository.recent('llm_calls',limit=limit),
                    network_calls=repository.recent_llm_attempts(limit=limit),
                    schedule=result.news_status.get('schedule',{}),health=repository.get_control('llm_provider_health',{}),
                    engine=result.engine_state,
                    ingestion_limit=tables['news_ingestions'],audit_limit=limit))
            source, started = 'transaction', monotonic()
        result.connected = True
    except Exception as exc:
        # 끊긴 연결로 후속 조회를 반복하지 않는다. 숫자 코드·고정 항목명만 남긴다.
        from sessionquant.database.mysql_repository import MySQLRepositoryError
        code = exc.code if isinstance(exc, MySQLRepositoryError) else None
        result.unavailable = pending | {source}
        suffix = f' (DB 오류 {code})' if code is not None else ''
        result.error = f"{SOURCE_LABELS[source]} 기록을 읽지 못했습니다{suffix}. 조회가 끝난 자료만 표시하며, 나머지는 다음 갱신 때 다시 확인합니다."
        logging.getLogger(__name__).warning(
            'dashboard_read_failed page=%s source=%s elapsed_seconds=%.3f error_type=%s mysql_code=%s',
            page or 'all', source, monotonic()-started, type(exc).__name__, code)
    finally:
        if repository is not None:
            try:
                repository.close()
            except Exception:
                pass
    return result


def latest_by_symbol(rows: list[dict]) -> dict[str, dict]:
    result = {}
    for row in rows:
        symbol = row.get("symbol") or row.get("quote", {}).get("symbol")
        if symbol:
            result[str(symbol)] = row
    return result


def _account(state: dict, rows: list[dict]) -> dict:
    broker = state.get("broker", state.get("portfolio", state))
    account = broker.get("account", broker if "cash" in broker or "equity" in broker else {}) if isinstance(broker, dict) else {}
    if not account and rows:
        account = rows[-1].get("account", rows[-1])
    return account if isinstance(account, dict) else {}


def _position_rows(state: dict, account: dict, rows: list[dict]) -> list[dict]:
    if rows:
        return list(latest_by_symbol(rows).values())
    broker = state.get("broker", state.get("portfolio", state))
    positions = account.get("positions", broker.get("positions", {})) if isinstance(broker, dict) else {}
    if isinstance(positions, dict):
        return [{"symbol": key, **value} for key, value in positions.items() if isinstance(value, dict)]
    return positions if isinstance(positions, list) else []


def build_view(data: DashboardData, now: datetime | None = None) -> dict:
    """저장된 사실과 확인되지 않은 상태를 구분하여 화면 표시값을 만든다."""
    now = now or datetime.now(timezone.utc)
    account = data.context.get("account") or _account(data.state, data.tables.get("account_snapshots", []))
    positions = _position_rows(data.state, account, data.tables.get("positions", []))
    positions = [row for row in positions if decimal_value(row.get("qty"), Decimal(0)) > 0]
    regimes = data.tables.get("market_regimes", [])
    regime = regimes[-1] if regimes else data.context.get("market_regime", data.context.get("regime", {}))
    if not isinstance(regime, dict):
        regime = {"state": str(regime)}
    state_code = str(regime.get("state", regime.get("regime", "UNKNOWN")))
    last_event = data.context.get("last_event", data.context.get("last_time", data.context.get("updated_at", data.state.get("last_time"))))
    lag = age_seconds(last_event, now)
    session_rows = data.tables.get("session_state", [])
    session = session_rows[-1] if session_rows else data.context.get("session", {})
    equity = decimal_value(account.get("equity"))
    position_view = []
    quotes = latest_by_symbol(data.tables.get("quote_snapshots", []))
    for row in positions:
        symbol = row.get("symbol", "")
        qty = decimal_value(row.get("qty"), Decimal(0))
        avg = decimal_value(row.get("avg_price"))
        quote = quotes.get(symbol, {})
        mark = decimal_value(quote.get("bid")) if age_seconds(quote.get("timestamp"), now) is not None and age_seconds(quote.get("timestamp"),now)<=60 else None
        value = qty * mark if mark is not None else None
        weight = value / equity * 100 if value is not None and equity is not None and equity > 0 else None
        stop = decimal_value(row.get("stop_price"))
        entry_fee = decimal_value(row.get("entry_fee", "0"))
        cost = qty * avg + entry_fee if avg is not None and avg > 0 and entry_fee is not None and entry_fee >= 0 else None
        pnl = value - cost if value is not None and mark > 0 and cost is not None else None
        return_pct = pnl / cost * 100 if pnl is not None and cost > 0 else None
        position_view.append({
            "종목": symbol, "보유 수량": str(qty), "평균 매수가": usd(avg), "평가액": usd(value),
            "비중": f"{weight:.1f}%" if weight is not None else "—",
            "최초 진입 금액": usd(row.get("initial_amount", row.get("initial_notional"))),
            "추가매수 금액": usd(row.get("add_amount", row.get("add_notional"))),
            "추가매수 횟수": row.get("add_count", row.get("adds", row.get("additions", 0))), "손절 기준": usd(stop),
            "평가 손익": usd(pnl),
            "평가 수익률": f"{return_pct:+.2f}%" if return_pct is not None else "—",
            "가격 기준": "최근 매수호가" if mark is not None else "시세 미확인/만료",
        })
    usage = data.tables.get("llm_usage", [])
    total_cost = sum((decimal_value(row.get("cost_usd", row.get("estimated_cost_usd")), Decimal(0)) for row in usage), Decimal(0))
    return {
        "account": account, "positions": position_view, "regime": state_code,
        "regime_label": REGIMES.get(state_code, "확인 필요"), "regime_detail": regime,
        "environment": data.context.get("environment", data.state.get("environment", "PAPER_LOCAL")),
        "synthetic": bool(data.context.get("synthetic", data.state.get("synthetic", False))),
        "last_event": timestamp(last_event), "lag": lag, "session": session,
        "usage_cost": total_cost, "usage_count": len(usage), "quotes": quotes,
    }


def _table(rows: list[dict], message: str = "아직 저장된 기록이 없습니다.") -> None:
    import pandas as pd
    import streamlit as st

    if rows:
        from sessionquant.dashboard.labels import display_rows
        visible,unknown=display_rows(rows)
        st.dataframe(pd.DataFrame(visible), hide_index=True, width="stretch")
        if unknown and st.session_state.get('dashboard_raw_data', False):
            with st.popover('원본 기록 보기'):
                st.json(rows)
    else:
        st.info(message)


def _news_rows(rows: list[dict]) -> list[dict]:
    from sessionquant.news.intake import age_bucket
    result = []
    for row in rows:
        event = row.get("event", row)
        result.append({
            "제목": event.get("title", row.get("title", "")), "종목·주제": ", ".join(event.get("symbols", [])) or row.get("topic", ""),
            "출처": event.get("source", row.get("source", "")), "발표 시각": timestamp(event.get("published_at")),
            "수집 시각": timestamp(event.get("received_at")), "방향": row.get("sentiment", row.get("direction", "UNKNOWN")),
            "나이 구간": age_bucket(event.get('published_at'),datetime.now(timezone.utc)),
            "제외·오류": row.get('error',row.get('skip_reason','')),
            "확인 상태": row.get("verification_status", row.get("confirmation_status", row.get("status", "UNKNOWN"))),
            "분석 근거": row.get("reason", row.get("rationale", row.get("summary", ""))),
            "원문": event.get("url", row.get("url", "")),
        })
    return result


def render_dashboard(path: str | Path, *, usage_path=None, usage_environment=None, selected_page=None,
                     panel=None, styled=False) -> None:
    import streamlit as st
    from sessionquant.dashboard.overview import PAGES, render_overview, render_system, render_strategies, style
    if not styled:
        style()
    from sessionquant.dashboard_mode import inspection_mode
    if inspection_mode():
        st.info('휴장·폐장 후 점검 화면입니다. 시세·뉴스·AI는 새로 수집하지 않으며, 표시된 값은 저장된 시점의 기록입니다.')
    page=selected_page or st.radio('화면',PAGES,horizontal=True,key='dashboard_page',label_visibility='collapsed')
    if page == '시스템·설정':
        # Build identity is local to this process/image, available even if DB reads fail.
        from sessionquant.dashboard.version_panel import render_version
        render_version(st)
    news_section = None
    if page == '뉴스·LLM':
        from sessionquant.dashboard.load_plan import NEWS_SECTIONS
        news_section = st.radio('뉴스 상세', tuple(NEWS_SECTIONS), horizontal=True, key='news_detail')
    data=load_database(path,page=page,news_section=news_section,panel=panel)
    view=build_view(data)
    show_benchmarks = data.context.get('benchmark_collection_enabled') is not False
    if page == '대시보드' and panel == 'news' and not data.error and view['environment'] != 'PAPER_LOCAL':
        return
    if page == '대시보드' and panel == 'live' and not data.error and view['environment'] != 'PAPER_LOCAL':
        # 실계좌 조회 홈은 기존 표시 범위를 유지한다.
        data=load_database(path,page=page)
        view=build_view(data)
    account=view['account']
    from sessionquant.dashboard.labels import label
    if page != '대시보드' or view['environment']!='PAPER_LOCAL':
        st.caption(f"{label(view['environment'])} · 마지막 시세 {view['last_event']}")
    if data.error:
        st.error(data.error)
        # 미완료 조회를 빈 장부·보유 없음으로 표시하거나 거래 제어에 쓰지 않는다.
        if page != '시장·종목':
            return
    elif not data.connected:
        st.info('저장된 실행 기록이 없습니다. 계좌·연결 상태는 미확인입니다.')

    def available(*sources):
        from sessionquant.dashboard.load_plan import SOURCE_LABELS
        missing = [SOURCE_LABELS[name] for name in sources if name in data.unavailable]
        if missing:
            st.info(' · '.join(missing)+' · 조회 미완료')
        return not missing
    if view['synthetic']: st.warning('합성 데이터 데모 · 실제 시장 성과가 아닙니다.')
    if page == '대시보드' and view['environment'] != 'PAPER_LOCAL' and panel != 'news' and available('watchlist_state'):
        from sessionquant.dashboard.focus_summary import render_focus_summary
        render_focus_summary(data.watchlist_state,data.context.get('strategy_lab'))
    if data.context.get('strategy')=='pullback_simple' and page != '대시보드' and panel != 'news':
        st.info('전략 실험 · 상승 추세 → 1~4개 봉 조정 → 확정봉 반등. LLM은 판단을 설명하며 뉴스는 보조 가점입니다.')
        st.caption('과거 20일 분봉·RVOL은 진입 필수가 아닙니다. 당일 분봉·최신 호가와 주문 제한은 검사합니다. 매수 판단 표에서 조건별 결과를 확인하세요.')
        if page == '시장·종목' and panel in {None, '매수 판단'}:
            from sessionquant.dashboard.status_labels import blocker_summary, llm_status_label
            summary=data.context.get('strategy_lab') or {}
            with st.expander('전략 → AI → 주문 상세 기록'):
                st.caption('마지막 평가 기록: '+timestamp(summary.get('evaluated_at'))+' · 주문 승인과 체결은 별개입니다.')
                _table([{'종목':r['symbol'],
                    '전략 조건':' / '.join(c['label']+(' ✓' if c['passed'] else ' 대기') for c in r['checks']) or '판단 자료 준비',
                    'AI 분석':llm_status_label(r['llm_status']),'뉴스 가점':r['llm_points'],
                    'LLM 호출 근거':', '.join(r['llm_call_ids']) or '없음',
                    '주문 판단':r['order_status'],'대기 이유':blocker_summary(r['blockers']) or '없음',
                    '주문 의도':r['order_intent_id'] or '없음'} for r in summary.get('rows',[])],
                    '매매 판단 기록을 기다립니다.')


    if (page == '뉴스·LLM' or (page == '대시보드' and view['environment'] != 'PAPER_LOCAL')) and panel != 'live':
        from sessionquant.dashboard.explanation_panel import render_explanation
        render_explanation(data.strategy_explanation)
    if data.context.get('paper_data_policy')=='observed_paper_v1':
        if page != '대시보드':
            st.info('합성 자료 사용 · 거래는 모의로 진행합니다.' if view['synthetic'] else '실제 시세 사용 · 거래는 모의로 진행합니다.')
        if page=='시스템·설정':
            st.caption('실제 계좌 권한은 확인하지 않습니다. 소수점 거래와 미확인 거래정지·기업행동, 거래량 비교 조건은 모의 가정으로 기록합니다.')
    if page=='대시보드':
        if panel is not None and view['environment'] == 'PAPER_LOCAL':
            from sessionquant.dashboard.operations_overview import render_operations
            render_operations(data,view,path,panel=panel)
        else:
            render_overview(data,view,path,usage_path,usage_environment)
    elif page=='전략·백테스트': render_strategies(data,view)
    elif page=='시스템·설정': render_system(data,view)
    def market_section(name):
        return page == '시장·종목' and panel in {None, name}

    if market_section('시장 참고') and show_benchmarks and available('market_regimes', 'quote_snapshots'):
        st.subheader("시장 방향과 데이터 상태")
        st.write(f"**{view['regime_label']}**")
        reason = view["regime_detail"].get("reason", view["regime_detail"].get("reasons", ""))
        if reason:
            st.caption(str(reason))
        market_rows = []
        for symbol in ("SPY", "QQQ", "IWM", "IEF", "TLT", "USO", "XLE"):
            quote = view["quotes"].get(symbol)
            component = view["regime_detail"].get("components", {}).get(symbol, {})
            if quote:
                market_rows.append({"종목": symbol, "매수호가": usd(quote.get("bid")), "매도호가": usd(quote.get("ask")),
                                    "세션 VWAP": usd(component.get("vwap")), "추세": REGIMES.get(component.get("direction", "UNKNOWN"), "확인 필요"),
                                    "확정봉 시각": timestamp(component.get("last_closed_at")),
                                    "제공 시각": timestamp(quote.get("timestamp")), "수집 시각": timestamp(quote.get("received_at"))})
        _table(market_rows, "SPY·QQQ의 최신 확정봉과 호가를 기다리고 있습니다.")
        st.caption("SPY·QQQ가 모두 최신 상태이고 확정봉 추세가 확인되어야 시장 방향을 사용합니다. FRED는 일별 참고 자료이며 TLT는 금리, USO는 원유 현물 가격으로 표시하지 않습니다.")
    if market_section('시장 참고'):
        if available('market_reference'):
            from sessionquant.dashboard.reference_panel import render_references, reference_benchmarks_visible
            render_references(data.market_reference, data.context.get('macro_reference_risk'), data.fred_references,
                quotes=view['quotes'], fred_available='fred_references' not in data.unavailable,
                show_benchmarks=reference_benchmarks_visible(data.context),stream=data.benchmark_stream)
        if available('economic_events'):
            from sessionquant.dashboard.news_panel import calendar_rows
            schedule = calendar_rows(data.tables['economic_events'])
            if schedule:
                _table(schedule)
            else:
                st.caption("중요 경제 일정: 미확인. 일정이 없다는 뜻은 아닙니다.")
    if page == '시장·종목':
        if view['environment'] == 'PAPER_LOCAL':
            from sessionquant.dashboard.market_quality_panel import render_quality
            if market_section('시세·품질') and available('market_quality'):
                render_quality(data.market_quality,performance=data.market_performance,details=panel is None,
                               show_benchmarks=show_benchmarks)
            if panel == '처리 속도' and available('market_quality', 'market_performance'):
                from sessionquant.dashboard.market_quality_panel import render_quality_details
                render_quality_details(data.market_quality, data.market_performance, show_benchmarks=show_benchmarks)
            if panel == '처리 속도' and available('candle_incidents'):
                from sessionquant.dashboard.candle_incidents import render_incidents
                render_incidents(data.candle_incidents)
            from sessionquant.dashboard.market_quality_panel import render_watchlist_rvol
            if market_section('거래량 비교') and available('watchlist_rvol', 'history_backfill'):
                render_watchlist_rvol(data.watchlist_rvol,data.history_backfill,
                                     paper_policy=data.context.get('paper_data_policy'),
                                     strategy=data.context.get('strategy'))
        from sessionquant.dashboard.watchlist_panel import render_watchlist
        if view['environment'] == 'PAPER_LOCAL':
            if market_section('감시·집중 종목') and available('watchlist_state'):
                render_watchlist(data.watchlist_state, None if 'candidates' in data.unavailable else data.tables['candidates'])
            from sessionquant.dashboard.selection_panel import render_selection
            if market_section('매수 판단') and available('portfolio_selection'):
                render_selection(data.portfolio_selection)
        else:
            st.caption('시총 감시·집중 분석 정책은 모의매매에 적용합니다. 현재 실계좌 조회·제한적 검증 경로는 별도입니다.')
    if market_section('분석 이력'):
        st.subheader("기술 분석 이력과 진입 근거")
        if not available('candidates'):
            return
        st.caption('아래 표는 과거 분석 기록을 포함합니다. 현재 감시·집중 여부는 위 목록에서 확인하세요.')
        rows = []
        for row in latest_by_symbol(data.tables["candidates"]).values():
            rows.append({"종목": row.get("symbol"), "점수": row.get("score"), "상태": "진입 신호" if row.get("eligible") and row.get("signal") else "관찰",
                         "전략": row.get("strategy", ""), "진입 근거": row.get("reason", row.get("signal", {}).get("reason", "") if isinstance(row.get("signal"), dict) else ""),
                         "제한 사유": ", ".join(row.get("blockers", [])), "시장": row.get("regime", "UNKNOWN")})
        _table(rows)
        st.caption('뉴스가 없어도 차트 조건으로 후보가 될 수 있습니다. 선택한 거래 조건과 위험 한도를 통과해야 매수합니다.')
    if page == '매매 내역':
        if data.connected and view['environment']=='PAPER_LOCAL':
            from sessionquant.database.repository import Repository
            from sessionquant.trading_controls import render_controls
            from contextlib import closing
            with closing(Repository(path, initialize=False)) as repo:
                positions=(data.state.get('broker') or {}).get('positions') or {}
                render_controls(repo,positions=[{'symbol':s,**r} for s,r in positions.items()],environment=view['environment'])
        from sessionquant.dashboard.summary_panels import render_actual_account
        if view['environment']=='LIVE_LOCAL': render_actual_account(data.actual_account,data.account_status)
        from sessionquant.dashboard.overview import filter_trades
        trade_source_counts = filter_trades(data)
        st.subheader("전략 관리 포지션과 위험 예산")
        st.caption("상단의 토스 실제 계좌와 별도로 이 실행의 전략 장부를 표시합니다.")
        _table(view["positions"], "현재 전략 관리 포지션이 없습니다. 조건이 맞지 않으면 현금을 유지합니다.")
        cols = st.columns(3)
        cols[0].metric("실현 손익", usd(account.get("realized_pnl")))
        cols[1].metric("일일 기준 자산", usd(account.get("day_start_equity")))
        limits = (data.state.get('broker') or {}).get('settings') or {}
        maximum, preferred = limits.get('max_positions'), limits.get('preferred_positions')
        policy = data.state.get('candidate_selection') or {}
        if type(maximum) is int and type(preferred) is int and 1 <= preferred <= maximum <= 5:
            limit = maximum if policy.get('allow_above_preferred') or policy.get('enabled') is False else preferred
            cols[2].metric("동시 보유 한도", f"{limit}종목")
        else:
            cols[2].metric("동시 보유 한도", "확인 대기")
        if data.context.get('strategy', data.state.get('strategy')) == 'pullback_simple':
            st.caption('현재 단순 눌림목 전략: 추가매수 없음. 최초 매수 목표는 자산의 20%이며, 손절 기준 가격에 도달하면 매도합니다. 실제 매수 금액은 현금·손절 거리·비용에 따라 줄어들 수 있습니다.')
        else:
            st.caption("기본값: 최초 진입 20% · 추가매수 최대 1회/20% · 종목당 최대 40% · 전체 최대 80% · 종목 위험 예산 0.75% · 일일 손실 한도 2%. 실제 주문 금액은 현금·손절 거리·비용·브로커 규칙에 따라 더 작아질 수 있습니다.")
        if view['environment'] == 'PAPER_LOCAL':
            st.caption('현재 한 전략만 단독 평가합니다. 10%는 고정 대기 조건이 아닌 설정 목표이며, 이익 보호·악재·손절·시간 청산이 먼저 작동할 수 있습니다. 일일 2%는 신규 매수 중단 기준으로 갭·장애 손실을 보장하지 않습니다.')
            risk = data.context.get('risk_snapshot') or {}
            st.caption('저장 시점 예상 위험: '+str(risk.get('status','UNKNOWN'))+' · 검사 시각: '+timestamp(risk.get('checked_at')))
            _table([{'종목':symbol,'손절 예상 손실':usd(row.get('loss_estimate')),'종목 위험 예산':usd(row.get('risk_budget')),
                     '평가금액':usd(row.get('market_value')),'예상 위험 사유':', '.join(row.get('issues',[]))}
                    for symbol,row in risk.get('positions',{}).items()],'예상 위험 기록이 없습니다. 입력 미확인은 안전 판정이 아닙니다.')
    if page == '뉴스·LLM':
        from sessionquant.dashboard.news_panel import render_news_diagnostics
        render_news_diagnostics(data)
        from sessionquant.dashboard.summary_panels import open_cost_repository
        cost_repo=None
        try:
            cost_repo=open_cost_repository(usage_path or path,environment=usage_environment)
            health=cost_repo.get_control('llm_provider_health',{}) if cost_repo else {}
            if health.get('code') not in {None,'UNKNOWN','AVAILABLE'}:st.warning(health.get('message','LLM 사용 상태 확인 필요'))
        except Exception:st.caption('LLM 상태 미확인')
        finally:
            if cost_repo:cost_repo.close()
        if news_section == '기업 뉴스':
            from sessionquant.dashboard.news_panel import current_company_news
            rows=data.tables['news_analysis'] or data.tables['news_events']
            _table(_news_rows(current_company_news(rows,data,view)),
                '보유·집중 종목의 검증된 최근 24시간 기업 뉴스가 없습니다.')
            with st.expander('전체 기업 뉴스·과거·오류 상세'):
                _table(_news_rows(rows))
            st.caption("SEC·검증된 기업 IR/RSS를 우선 확인합니다. 무료 Alpaca REST는 보조 자료로 표시합니다.")
        elif news_section == '경제·정치·국제정세':
            originals={row['id']:row for row in data.tables['macro_news'] if row.get('id')}
            rows=[{**originals.get(row.get('article_id'),{}),**row} for row in data.tables['macro_analysis']]
            _table(_news_rows(rows or data.tables['macro_news']))
            st.caption("GDELT 검색 결과는 사건 확정의 증거가 아닙니다. 출처·공개 시각·독립 검증·실제 시장 반응을 확인하며 미확인 국제 뉴스만으로 강제 청산하지 않습니다.")
        elif news_section == 'LLM 사용량':
            if data.connected:
                from sessionquant.dashboard.llm_panel import render_llm_details
                render_llm_details(path,usage_path=usage_path,usage_environment=usage_environment)
            else:st.info('LLM 호출·사용량 기록이 없습니다. 실제 사용량을 0으로 확정하지 않습니다.')
    if page == '매매 내역':
        st.subheader("주문·체결과 당일 청산")
        session = view["session"] if isinstance(view["session"], dict) else {}
        schedule = session.get("schedule", session)
        session = schedule if isinstance(schedule, dict) else {}
        st.caption(f"청산 시작: {timestamp(session.get('exit_start', session.get('liquidation_start')))} · 미청산 경고: {timestamp(session.get('alert_at', session.get('emergency_at')))}")
        st.caption("주문 전송과 체결 완료를 구분합니다. 부분 체결·잔량·시세 지연·미청산 사유는 아래 기록으로 확인하세요.")
        if view['environment'] == 'PAPER_LOCAL':
            full_session = view['session'] if isinstance(view['session'],dict) else {}
            st.caption('청산 상태는 잔여 수량과 미결 주문을 함께 확인합니다. 매수 취소 확인 대기: '+str(len(full_session.get('pending_buy_cancellations',[])))+'건 · 미확인 주문: '+str(len(full_session.get('unknown_orders',[])))+'건')
        from sessionquant.dashboard.trade_pages import trade_page
        from sessionquant.dashboard.performance_display import two_decimals
        order_page = trade_page(data.tables['orders'], kind='orders', source_count=trade_source_counts['orders'])
        _table([{"주문 ID": row.get("client_id", row.get("id", "")), "종목": row.get("symbol", ""), "방향": row.get("side", ""),
                 "상태": STATUS_NAMES.get(str(row.get("status", "")).lower(), row.get("status", "")), "금액": usd(row.get("amount")),
                 "수량": str(row.get("qty", "")), "체결 수량": str(row.get("filled_qty", "")), "사유": row.get("reason", ""),
                 "생성 시각": timestamp(row.get("created_at"))} for row in order_page], "선택한 조건에서 표시할 주문이 없습니다.")
        if view['environment'] == 'PAPER_LOCAL':
            from sessionquant.dashboard.trade_reasons import fill_timing_label
            st.caption('모의 체결 모델: '+str(data.context.get('paper_fill_model','기존 기록·미확인'))+' · 접수·지연·유동성은 가정이며 실제 증권사 체결을 보장하지 않습니다.')
            if data.context.get('submission_quote_buys'):
                st.caption('신규 모의매수는 주문 시점의 최신 매도 호가에 비용을 반영해 바로 체결을 시도합니다. 수량 부족분은 새 호가를 기다립니다.')
            if data.context.get('submission_quote_sells'):
                st.caption('신규 모의매도도 주문 시점의 최신 매수 호가에 비용을 반영해 바로 체결을 시도합니다. 매도 이유와 부분체결 잔량은 별도로 기록합니다.')
            with st.expander('모의 체결 가정·미체결 근거'):
                from sessionquant.dashboard.trade_reasons import order_cancellation_details, order_quote_wait_details
                _table([{'주문 ID':row.get('client_id'), '체결 모델':row.get('fill_model_version','UNKNOWN'),
                    '체결 방식':fill_timing_label(row),
                    '예상 가격':usd(row.get('expected_price')),'누적 모의 체결가':usd(row.get('simulated_fill_price')),
                    '가정 지연(초)':row.get('assumed_latency'),'가격 차이(bps)':two_decimals(row.get('slippage_bps')),
                    '수수료 항목':row.get('fee_breakdown'),'미체결 사유':row.get('unfilled_reason'),
                    '접수 시각':timestamp(row.get('accepted_at')), **order_cancellation_details(row),
                    **order_quote_wait_details(row)} for row in order_page], '현재 주문 페이지에 표시할 미체결 근거가 없습니다.')
                st.caption('취소 사유와 마지막 호가 대기 원인을 구분합니다. 대기 시작 당시의 수신·엔진 호가 시각이며, 과거에 남지 않은 원인은 기록 없음으로 표시합니다.')
        with st.expander("체결 기록", expanded=True):
            from sessionquant.dashboard.trade_reasons import fill_timing_label
            execution_page = trade_page(data.tables['executions'], kind='executions', source_count=trade_source_counts['executions'])
            st.caption('매도 이유는 해당 주문에 기록된 사유입니다. 과거 체결은 연결된 주문에서 확인하며, 근거가 없으면 기록 없음으로 표시합니다.')
            st.caption('가격 차이는 예상 가격과 체결 가격의 차이이며, 1bps는 0.01%입니다. 소수점 둘째 자리까지 표시하고 나머지는 버립니다.')
            _table([{"종목": row.get("symbol", ""), "방향": row.get("side", ""), "매도 이유": row.get('매도 이유', '기록 없음') if str(row.get('side')).lower()=='sell' else '—', "수량": str(row.get("qty", "")),
                     "가격": usd(row.get("price")), "수수료": usd(row.get("fee")), "체결 시각": timestamp(row.get("timestamp")),
                     '체결 모델':row.get('fill_model_version','실제 브로커' if row.get('execution_mode')=='live' else 'UNKNOWN'),
                     '체결 방식':fill_timing_label(row),
                     '예상 가격':usd(row.get('expected_price')),'가격 차이(bps)':two_decimals(row.get('slippage_bps')),
                     "체결 ID": row.get("id", "")} for row in execution_page], "선택한 조건에서 표시할 체결이 없습니다.")
        st.markdown("**운영 경고**")
        _table([{"시각": timestamp(row.get("timestamp", row.get("created_at"))), "수준": row.get("level", row.get("severity", "")),
                 "코드": row.get("code", row.get("type", "")), "설명": row.get("message", row.get("reason", ""))} for row in data.tables["alerts"]])
        st.caption("화면은 엔진에 제어 요청을 전달합니다. 실제 주문·취소·체결 확인은 실행 중인 엔진에서 처리합니다.")


def refresh_interval(page, enabled=True, *, panel=None):
    """조회량에 맞춘 화면 갱신 주기. 거래 제어 요청은 주기와 무관하게 처리한다."""
    if not enabled:
        return None
    if page == '대시보드' and panel == 'news':
        return 60
    if page == '시장·종목' and panel in {'처리 속도', '분석 이력'}:
        return None
    return {'대시보드': 5, '시장·종목': 10, '뉴스·LLM': 30}.get(page)


def main() -> None:
    """로컬 전략·실계좌 통합 화면을 실행한다."""
    import streamlit as st

    # 상세 화면이 렌더링되지 않아도 확인/검색 위젯의 세션 값을 보존한다.
    for key in list(st.session_state):
        if key.startswith(('confirm-','trade_','llm-','strategy_','news_', 'market_detail')):
            st.session_state[key]=st.session_state[key]

    parser = argparse.ArgumentParser(description="SessionQuant 통합 대시보드")
    parser.add_argument("--usage-db",default=None)
    parser.add_argument("--usage-environment",default=None)
    parser.add_argument("--db", default=os.getenv("SQ_DB", "data/sessionquant-paper-local.db"))
    args, _ = parser.parse_known_args()
    st.set_page_config(page_title="SessionQuant · S&P 500 장중매매", page_icon="◈", layout="wide")
    from sessionquant.dashboard.navigation import restore_navigation, remember_navigation
    restore_navigation(st.query_params, st.session_state)
    from sessionquant.dashboard.overview import PAGES, style
    style()
    with st.sidebar:
        st.title("SessionQuant")
        page=st.radio('화면',PAGES,key='dashboard_page',label_visibility='collapsed')
        st.divider()
        refresh = st.toggle("자동 갱신", value=True, key='dashboard_auto_refresh',
                            help='시장 흐름 차트 2초 · 시세·보유 5초 · 선택한 시장 표 10초 · 메인 뉴스 집계 60초. 꺼도 수집과 매매는 계속됩니다.')
        if st.button("화면 새로고침", width="stretch"):
            st.rerun()
        st.toggle('원본 기록도 보기', value=False, key='dashboard_raw_data',
                  help='상세 진단용입니다. 켜면 추가 자료를 화면에 표시하므로 느려질 수 있습니다.')
        st.caption("한국시간 · 미국 달러")

    panel = None
    if page == '시장·종목':
        from sessionquant.dashboard.load_plan import MARKET_SECTIONS
        panel = st.radio('확인할 자료', tuple(MARKET_SECTIONS), horizontal=True, key='market_detail')
    remember_navigation(st.query_params, st.session_state, page, panel)
    interval = refresh_interval(page, refresh, panel=panel)
    st.sidebar.caption(f'현재 상태는 {interval}초마다 갱신합니다.' if interval else '현재 표는 수동으로 갱신합니다.')
    if page == '대시보드' and refresh:
        st.sidebar.caption('시장 흐름 차트는 2초, 뉴스·AI 요약은 60초마다 갱신합니다.')

    @st.fragment(run_every=interval)
    def monitor() -> None:
        render_dashboard(Path(args.db),usage_path=Path(args.usage_db) if args.usage_db else None,
                         usage_environment=args.usage_environment,selected_page=page,
                         panel='live' if page == '대시보드' else panel,styled=True)

    monitor()
    if page == '대시보드':
        @st.fragment(run_every=refresh_interval(page, refresh, panel='news'))
        def news_summary() -> None:
            render_dashboard(Path(args.db), selected_page=page, panel='news', styled=True)

        news_summary()


if __name__ == "__main__":
    main()
