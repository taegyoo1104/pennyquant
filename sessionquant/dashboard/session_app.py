"""SessionQuant의 실제 계좌와 전략·뉴스·운영 상태 및 거래 제어를 표시한다."""
from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import os
from pathlib import Path
import sys
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
    connected: bool = False
    actual_account: dict | None = None
    account_status: dict = field(default_factory=dict)
    watchlist_state: dict = field(default_factory=dict)
    market_quality: dict = field(default_factory=dict)
    market_reference: dict = field(default_factory=dict)
    fred_references: list = field(default_factory=list)
    news_status: dict = field(default_factory=dict)
    portfolio_selection: dict = field(default_factory=dict)


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


def load_database(path: str | Path) -> DashboardData:
    """최근 기록만 읽고 연결은 즉시 닫는다. MySQL은 로컬 파일 존재 여부와 무관하다."""
    result = DashboardData()
    repository = None
    try:
        from sessionquant.local_env import load_local_env
        load_local_env(database_only=True)
        from sessionquant.database.config import database_backend
        from sessionquant.database.repository import KINDS, Repository

        result.backend = database_backend()
        if result.backend == "sqlite" and not Path(path).exists():
            return result
        repository = Repository(path)
        result.state = repository.load_state() or {}
        result.watchlist_state=repository.get_control("watchlist_dashboard_v1",{}) or {}
        result.market_quality=repository.get_control('market_data_quality_v1',{}) or {}
        result.portfolio_selection=repository.get_control('portfolio_selection_v1',{}) or {}
        result.market_reference=repository.get_control('market_reference_view_v1',{}) or {}
        result.fred_references=repository.recent('fred_reference_observations',limit=30)
        result.news_status=repository.get_control('session_news_status',{}) or {}
        result.news_status['schedule']=repository.get_control('news_provider_schedule_v1',{}) or {}
        result.actual_account=repository.get_control("actual_account_snapshot")
        result.account_status=repository.get_control("account_monitor_status",{}) or {}
        result.context = repository.get_control("sessionquant_context", {}) or {}
        broker_status = (repository.get_control("broker_state", {}) or {}) if not result.context else {}
        if broker_status.get("mode") == "live" and not result.context:
            result.context={"environment":"LIVE_LOCAL","mode":"live","synthetic":False,
                "updated_at":broker_status.get("observed_at"),"source_label":"실계좌 주문·체결 기록"}
        if result.actual_account and not result.context:
            result.context={"environment":"LIVE_LOCAL","mode":"account_readonly","synthetic":False,
                "updated_at":result.actual_account.get("received_at"),"source_label":"토스 실제 계좌 조회"}
        for name in TABLES:
            if name in KINDS:
                result.tables[name] = repository.recent(name, limit=2000)
        result.connected = True
    except Exception:
        # 드라이버의 원문 오류에는 접속 주소나 인증 정보가 포함될 수 있다.
        result = DashboardData(backend=result.backend, error="DB 기록을 읽을 수 없습니다. 연결 설정과 서버 상태를 확인하세요.")
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
        avg = decimal_value(row.get("avg_price"), Decimal(0))
        quote = quotes.get(symbol, {})
        mark = decimal_value(quote.get("bid"), avg)
        value = qty * mark
        weight = value / equity * 100 if equity is not None and equity > 0 else None
        stop = decimal_value(row.get("stop_price"))
        position_view.append({
            "종목": symbol, "보유 수량": str(qty), "평균 매수가": usd(avg), "평가액": usd(value),
            "비중": f"{weight:.1f}%" if weight is not None else "—",
            "최초 진입 금액": usd(row.get("initial_amount", row.get("initial_notional"))),
            "추가매수 금액": usd(row.get("add_amount", row.get("add_notional"))),
            "추가매수 횟수": row.get("add_count", row.get("adds", row.get("additions", 0))), "손절 기준": usd(stop),
            "평가 손익": usd((mark - avg) * qty - decimal_value(row.get("entry_fee"), Decimal(0))),
            "가격 기준": "최근 매수호가" if quote else "매수가 · 시세 미확인",
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
        st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")
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


def render_dashboard(path: str | Path, *, usage_path=None, usage_environment=None) -> None:
    import streamlit as st

    data = load_database(path)
    view = build_view(data)
    if data.error:
        st.error(data.error)
    if not data.connected and not data.error:
        st.info("저장된 실행 기록이 없습니다. 먼저 합성 데모 또는 모의매매 엔진을 실행하세요.")
    if data.account_status.get("status")=="error":
        st.warning(data.account_status.get("message","실계좌 조회 실패: 이전 기록을 확인하세요."))
    if view["synthetic"]:
        st.warning("합성 데이터 데모입니다. 실제 시장 수익이나 실제 체결을 나타내지 않습니다.")
    st.caption(f"환경: {view['environment']} · 저장소: {data.backend.upper()} · 마지막 전략 수신: {view['last_event']}")
    if data.connected:
        control_repo=None
        try:
            from sessionquant.database.repository import Repository
            from sessionquant.trading_controls import render_controls
            control_repo=Repository(path)
            render_controls(control_repo,positions=data.actual_account['items'] if data.actual_account and data.context.get('environment')=='LIVE_LOCAL' else view['positions'])
        except Exception:
            st.caption('거래 제어 연결을 확인할 수 없습니다.')
        finally:
            if control_repo: control_repo.close()
    metrics = st.columns(5)
    account = view["account"]
    if data.actual_account:
        from sessionquant.dashboard.account_app import money
        actual=data.actual_account
        market_amount=((actual.get('summary') or {}).get('marketValue') or {}).get('amount') or {}
        buying_power=actual.get('buying_power') or {}
        metrics[0].metric('실계좌 미국 주식 평가금액',money(market_amount.get('usd'),'USD'))
        metrics[1].metric('실계좌 달러 매수 가능 금액',money(buying_power.get('USD'),'USD'))
        metrics[2].metric('실계좌 국내 주식 평가금액',money(market_amount.get('krw'),'KRW'))
        metrics[3].metric('실계좌 원화 매수 가능 금액',money(buying_power.get('KRW'),'KRW'))
    else:
        metrics[0].metric('전략 평가금액 · 모의/검증 장부',usd(account.get('equity')))
        metrics[1].metric('전략 현금 · 모의/검증 장부',usd(account.get('cash')))
        metrics[2].metric('예약 현금',usd(account.get('reserved_cash')))
        metrics[3].metric('관리 보유 종목',f"{len(view['positions'])} / 5")
    metrics[4].metric('시장 상태',view['regime_label'])
    from sessionquant.dashboard.summary_panels import render_actual_account,render_cost_panel
    if data.actual_account:
        render_actual_account(data.actual_account,data.account_status,show_metrics=False)
    if data.connected:
        render_cost_panel(path,usage_path=usage_path,usage_environment=usage_environment)
    if view["lag"] is not None and view["lag"] > 60:
        st.info("저장된 마지막 수신 시각이 60초를 초과했습니다. 재생·종료된 실행에서는 정상일 수 있습니다. 최근 기록만으로 현재 API 연결을 판단하지 않습니다.")
    market, candidates, portfolio, news, operations = st.tabs(["시장 현황", "종목 현황", "포트폴리오", "뉴스 분석", "주문 및 운영"])
    with market:
        st.subheader("시장 방향과 데이터 상태")
        st.write(f"**{view['regime_label']} · {view['regime']}**")
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
        from sessionquant.dashboard.reference_panel import render_references
        render_references(data.market_reference, data.context.get('macro_reference_risk'), data.fred_references)
        from sessionquant.dashboard.news_panel import calendar_rows
        schedule = calendar_rows(data.tables['economic_events'])
        if schedule:
            _table(schedule)
        else:
            st.caption("중요 경제 일정: 미확인. 일정이 없다는 뜻은 아닙니다.")
    with candidates:
        if view['environment'] == 'PAPER_LOCAL':
            from sessionquant.dashboard.market_quality_panel import render_quality
            render_quality(data.market_quality)
        from sessionquant.dashboard.watchlist_panel import render_watchlist
        if view['environment'] == 'PAPER_LOCAL':
            render_watchlist(data.watchlist_state, data.tables['candidates'])
            from sessionquant.dashboard.selection_panel import render_selection
            render_selection(data.portfolio_selection)
        else:
            st.caption('시총 감시 100·집중 20 정책은 PAPER_LOCAL에 적용합니다. 현재 실계좌 조회·제한적 검증 경로는 별도입니다.')
        st.subheader("기술 분석 이력과 진입 근거")
        st.caption('아래 표는 과거 분석 기록을 포함합니다. 현재 감시·집중 여부는 위 목록에서 확인하세요.')
        rows = []
        for row in latest_by_symbol(data.tables["candidates"]).values():
            rows.append({"종목": row.get("symbol"), "점수": row.get("score"), "상태": "진입 신호" if row.get("eligible") and row.get("signal") else "관찰",
                         "전략": row.get("strategy", ""), "진입 근거": row.get("reason", row.get("signal", {}).get("reason", "") if isinstance(row.get("signal"), dict) else ""),
                         "제한 사유": ", ".join(row.get("blockers", [])), "시장": row.get("regime", "UNKNOWN")})
        _table(rows)
        st.caption("뉴스가 없어도 기술적 조건으로 후보가 될 수 있습니다. 거래 가능 여부·소수점 지원·거래정지·기업행동을 확인하지 못하면 신규 주문을 허용하지 않습니다.")
    with portfolio:
        st.subheader("전략 관리 포지션과 위험 예산")
        st.caption("상단의 토스 실제 계좌와 별도로 이 실행의 전략 장부를 표시합니다.")
        _table(view["positions"], "현재 전략 관리 포지션이 없습니다. 조건이 맞지 않으면 현금을 유지합니다.")
        cols = st.columns(3)
        cols[0].metric("실현 손익", usd(account.get("realized_pnl")))
        cols[1].metric("일일 기준 자산", usd(account.get("day_start_equity")))
        cols[2].metric("초기 계획", "2종목 중심")
        st.caption("기본값: 최초 진입 20% · 추가매수 최대 1회/20% · 종목당 최대 40% · 전체 최대 80% · 종목 위험 예산 0.75% · 일일 손실 한도 2%. 실제 주문 금액은 현금·손절 거리·비용·브로커 규칙에 따라 더 작아질 수 있습니다.")
        if view['environment'] == 'PAPER_LOCAL':
            st.caption('현재 한 전략만 단독 평가합니다. 10%는 고정 대기 조건이 아닌 설정 목표이며, 이익 보호·악재·손절·시간 청산이 먼저 작동할 수 있습니다. 일일 2%는 신규 매수 중단 기준으로 갭·장애 손실을 보장하지 않습니다.')
            risk = data.context.get('risk_snapshot') or {}
            st.caption('저장 시점 예상 위험: '+str(risk.get('status','UNKNOWN'))+' · 검사 시각: '+timestamp(risk.get('checked_at')))
            _table([{'종목':symbol,'손절 예상 손실':usd(row.get('loss_estimate')),'종목 위험 예산':usd(row.get('risk_budget')),
                     '평가금액':usd(row.get('market_value')),'예상 위험 사유':', '.join(row.get('issues',[]))}
                    for symbol,row in risk.get('positions',{}).items()],'예상 위험 기록이 없습니다. 입력 미확인은 안전 판정이 아닙니다.')
    with news:
        from sessionquant.dashboard.news_panel import render_news_diagnostics
        render_news_diagnostics(data)
        corporate, macro, usage = st.tabs(["기업 뉴스", "경제·정치·국제정세", "LLM 사용량"])
        with corporate:
            from sessionquant.dashboard.news_panel import current_company_news
            rows=data.tables['news_analysis'] or data.tables['news_events']
            _table(_news_rows(current_company_news(rows,data,view)),
                '보유·집중 종목의 검증된 최근 24시간 기업 뉴스가 없습니다.')
            with st.expander('전체 기업 뉴스·과거·오류 상세'):
                _table(_news_rows(rows))
            st.caption("SEC·검증된 기업 IR/RSS를 우선 확인합니다. 무료 Alpaca REST는 보조 자료로 표시합니다.")
        with macro:
            originals={row['id']:row for row in data.tables['macro_news'] if row.get('id')}
            rows=[{**originals.get(row.get('article_id'),{}),**row} for row in data.tables['macro_analysis']]
            _table(_news_rows(rows or data.tables['macro_news']))
            st.caption("GDELT 검색 결과는 사건 확정의 증거가 아닙니다. 출처·공개 시각·독립 검증·실제 시장 반응을 확인하며 미확인 국제 뉴스만으로 강제 청산하지 않습니다.")
        with usage:
            if data.connected:
                from sessionquant.dashboard.llm_panel import render_llm_details
                render_llm_details(path,usage_path=usage_path,usage_environment=usage_environment)
            else:st.info('LLM 호출·사용량 기록이 없습니다. 실제 사용량을 0으로 확정하지 않습니다.')
    with operations:
        st.subheader("주문·체결과 당일 청산")
        session = view["session"] if isinstance(view["session"], dict) else {}
        schedule = session.get("schedule", session)
        session = schedule if isinstance(schedule, dict) else {}
        st.caption(f"청산 시작: {timestamp(session.get('exit_start', session.get('liquidation_start')))} · 미청산 경고: {timestamp(session.get('alert_at', session.get('emergency_at')))}")
        st.caption("주문 전송과 체결 완료를 구분합니다. 부분 체결·잔량·시세 지연·미청산 사유는 아래 기록으로 확인하세요.")
        if view['environment'] == 'PAPER_LOCAL':
            full_session = view['session'] if isinstance(view['session'],dict) else {}
            st.caption('청산 상태는 잔여 수량과 미결 주문을 함께 확인합니다. 매수 취소 확인 대기: '+str(len(full_session.get('pending_buy_cancellations',[])))+'건 · 미확인 주문: '+str(len(full_session.get('unknown_orders',[])))+'건')
        _table([{"주문 ID": row.get("client_id", row.get("id", "")), "종목": row.get("symbol", ""), "방향": row.get("side", ""),
                 "상태": STATUS_NAMES.get(str(row.get("status", "")).lower(), row.get("status", "")), "금액": usd(row.get("amount")),
                 "수량": str(row.get("qty", "")), "체결 수량": str(row.get("filled_qty", "")), "사유": row.get("reason", ""),
                 "생성 시각": timestamp(row.get("created_at"))} for row in data.tables["orders"]])
        if view['environment'] == 'PAPER_LOCAL':
            st.caption('모의 체결 모델: '+str(data.context.get('paper_fill_model','기존 기록·미확인'))+' · 접수·지연·유동성은 가정이며 실제 증권사 체결을 보장하지 않습니다.')
            with st.expander('모의 체결 가정·미체결 근거'):
                _table([{'주문 ID':row.get('client_id'), '체결 모델':row.get('fill_model_version','UNKNOWN'),
                    '예상 가격':usd(row.get('expected_price')),'누적 모의 체결가':usd(row.get('simulated_fill_price')),
                    '가정 지연(초)':row.get('assumed_latency'),'가격 차이(bps)':row.get('slippage_bps'),
                    '수수료 항목':row.get('fee_breakdown'),'미체결 사유':row.get('unfilled_reason'),
                    '접수 시각':timestamp(row.get('accepted_at'))} for row in data.tables['orders']])
        with st.expander("체결 기록"):
            _table([{"종목": row.get("symbol", ""), "방향": row.get("side", ""), "수량": str(row.get("qty", "")),
                     "가격": usd(row.get("price")), "수수료": usd(row.get("fee")), "체결 시각": timestamp(row.get("timestamp")),
                     '체결 모델':row.get('fill_model_version','실제 브로커' if row.get('execution_mode')=='live' else 'UNKNOWN'),
                     '예상 가격':usd(row.get('expected_price')),'가격 차이(bps)':row.get('slippage_bps'),
                     "체결 ID": row.get("id", "")} for row in data.tables["executions"]])
        st.markdown("**운영 경고**")
        _table([{"시각": timestamp(row.get("timestamp", row.get("created_at"))), "수준": row.get("level", row.get("severity", "")),
                 "코드": row.get("code", row.get("type", "")), "설명": row.get("message", row.get("reason", ""))} for row in data.tables["alerts"]])
        st.caption("화면은 엔진에 제어 요청을 전달합니다. 실제 주문·취소·체결 확인은 실행 중인 엔진에서 처리합니다.")


def main() -> None:
    """로컬 전략·실계좌 통합 화면을 실행한다."""
    import streamlit as st

    parser = argparse.ArgumentParser(description="SessionQuant 통합 대시보드")
    parser.add_argument("--usage-db",default=None)
    parser.add_argument("--usage-environment",default=None)
    parser.add_argument("--db", default=os.getenv("SQ_DB", "data/sessionquant-paper-local.db"))
    args, _ = parser.parse_known_args()
    st.set_page_config(page_title="SessionQuant · S&P 500 장중매매", page_icon="◈", layout="wide")
    st.title("SessionQuant")
    st.caption("S&P 500 · 시장·뉴스·전략 · 실제 계좌 연동과 위험 관리")
    with st.sidebar:
        st.subheader("로컬 거래 기록 조회")
        st.info("모의·실계좌 구분 표시 · 주문은 연결된 엔진에서 처리")
        refresh = st.toggle("자동 갱신 · 5초", value=True)
        if st.button("화면 새로고침", width="stretch"):
            st.rerun()
        st.caption("표시 시각: 한국 표준시\n\n거래 시간: 뉴욕 현지시각\n\n통화: 미국 달러")
        st.caption("데이터가 부족하면 관찰 상태를 유지합니다. 저장된 기록은 현재 API 연결을 보증하지 않습니다.")

    @st.fragment(run_every="5s" if refresh else None)
    def monitor() -> None:
        render_dashboard(Path(args.db),usage_path=Path(args.usage_db) if args.usage_db else None,usage_environment=args.usage_environment)

    monitor()


if __name__ == "__main__":
    main()
