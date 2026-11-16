"""08A의 간결한 메인과 읽기 전용 상세 화면. 브로커를 호출하지 않는다."""
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

PAGES=('대시보드','시장·종목','전략·백테스트','매매 내역','뉴스·LLM','시스템·설정')


def style():
    import streamlit as st
    st.markdown('''<style>
    .stApp {background:#0f172a;color:#e2e8f0}
    [data-testid="stMetricValue"] {color:#e2e8f0}
    @media(max-width:900px){[data-testid="stHorizontalBlock"]{flex-wrap:wrap}
    [data-testid="stColumn"]{min-width:45%!important}}
    @media(max-width:540px){[data-testid="stColumn"]{min-width:100%!important}}
    </style>''',unsafe_allow_html=True)


def go(page):
    import streamlit as st
    st.session_state['dashboard_page']=page


def link(label,page):
    import streamlit as st
    st.button(label,on_click=go,args=(page,),key='go-'+label)


def main_news(data,view,now):
    from .news_panel import current_company_news
    # Only the active ledger supplies holdings. Never merge actual and Paper positions.
    actual=view['environment']=='LIVE_LOCAL'
    scoped=SimpleNamespace(actual_account=data.actual_account if actual else None,watchlist_state=data.watchlist_state)
    scoped_view={**view,'positions':[] if actual else view['positions']}
    originals={r.get('id'):r for r in data.tables['news_events'] if r.get('id')}
    rows=[]
    for row in data.tables['news_analysis']:
        event=row.get('event') or originals.get(row.get('article_id') or row.get('event_id'))
        if event:rows.append({**row,'event':event})
    if not rows:rows=list(originals.values())
    selected=current_company_news(rows,scoped,scoped_view,now)
    seen=set();result=[]
    held={r['종목'] for r in scoped_view['positions']}
    if actual:
        from .session_app import decimal_value
        held={r['symbol'] for r in (data.actual_account or {}).get('items',[]) if decimal_value(r.get('quantity'),Decimal(0))>0}
    for row in selected:
        event=row.get('event',row);key=event.get('id')
        if key in seen:continue
        seen.add(key)
        tickers=set(event.get('symbols',[]))
        from .watchlist_panel import current_status
        status,active=current_status(data.watchlist_state,now)
        from ..models import utc
        focus=set()
        if active and status=='VALID':
            for item in data.watchlist_state.get('items',[]):
                try:
                    if item.get('focused') and utc(item['focus_valid_until'])>=now:focus.add(item['ticker'])
                except (KeyError,TypeError,ValueError,AttributeError):pass
        labels=[('보유 ' if t in held else '집중 ')+t for t in sorted(tickers & (held|focus))]
        result.append({**row,'labels':', '.join(labels)})
    return result[:5]


def recent_trades(rows,now):
    from .session_app import timestamp,usd
    from ..models import utc
    result=[]
    for row in rows:
        try:
            if utc(row['timestamp'])>now:continue
        except (KeyError,ValueError,TypeError):continue
        result.append(row)
    result.sort(key=lambda r:utc(r['timestamp']),reverse=True)
    return [{'시각':timestamp(r['timestamp']),'종목':r.get('symbol'),'매수/매도':'매수' if str(r.get('side')).lower()=='buy' else '매도',
        '수수료':usd(r.get('fee')),'매도 순실현손익':usd(r.get('realized_pnl')) if str(r.get('side')).lower()=='sell' else '—'} for r in result[:5]]


def render_overview(data,view,path,usage_path=None,usage_environment=None):
    if view['environment']=='PAPER_LOCAL':
        from .operations_overview import render_operations
        return render_operations(data,view,path)
    return render_account_overview(data,view,path,usage_path,usage_environment)


def render_account_overview(data,view,path,usage_path=None,usage_environment=None):
    import streamlit as st
    from .session_app import usd,timestamp,_table,age_seconds
    from ..database.repository import Repository
    from ..trading_controls import render_controls
    now=datetime.now(timezone.utc)
    repo=None
    if data.connected:
        try:
            repo=Repository(path)
            broker=data.state.get('broker',{})
            positions=[{'symbol':symbol,**r} for symbol,r in broker.get('positions',{}).items()] if isinstance(broker.get('positions'),dict) else []
            render_controls(repo,positions=positions,environment=view['environment'],readonly=data.context.get('mode')=='account_readonly',compact=True)
        except Exception:st.warning('거래 제어 연결 미확인 · 시스템·설정에서 확인하세요.')
        finally:
            if repo:repo.close()
    if not data.connected:
        st.button('매매 시작',disabled=True,key='offline-start')
        st.button('일괄 매매 종료·청산',disabled=True,key='offline-stop')
    from .labels import market_hours_label
    st.caption(market_hours_label(now))
    warmup=data.warmup
    if warmup:
        if warmup.get('status') in {'READY','PARTIAL'}:
            st.caption(f"자료 검사 완료 · 과거 분봉 준비 {warmup.get('history_ready_count',0)}종목 · 제외 {warmup.get('history_excluded_count',0)}종목"+
                       (' · 일부 누락은 상세 확인' if warmup.get('status')=='PARTIAL' else ''))
        else:
            stage='당일 분봉' if warmup.get('phase')=='CURRENT_SESSION' else '과거 분봉'
            st.info(f"자료 준비 중 · {stage} 확인 · {warmup.get('completed_tasks',0)}/{warmup.get('required_tasks',0)}개 구간 확인 · 신규 매수 대기")
        if warmup.get('reused_history_tasks'):
            st.caption(f"완료된 과거 검사 재사용 {warmup['reused_history_tasks']}개 구간")
        if warmup.get('deferred_history_tasks'):
            st.caption(f"최근 조회한 누락 구간 {warmup['deferred_history_tasks']}개 재조회 생략 · 해당 종목은 계속 제외 · 마지막 검사 후 6시간 대기")
        with st.expander('자료 준비 자세히'):
            st.caption(warmup.get('message','')+' · 마지막 확인 '+timestamp(warmup.get('updated_at')))
            st.caption('검사 기준 '+timestamp(warmup.get('cutoff_at'))+' · '+str(warmup.get('current_symbol',''))+' '+str(warmup.get('current_session','')))
            _table(warmup.get('issues',[]),'보충 후에도 남은 누락 기록이 없습니다.')
    if view['lag'] is None or view['lag']>60:st.caption('최신 시세 대기 중 · 마지막 저장값을 표시합니다.')
    cols=st.columns(4)
    actual=view['environment']=='LIVE_LOCAL'
    snapshot=data.actual_account or {}
    account=view['account']
    if actual:
        market=((snapshot.get('summary') or {}).get('marketValue') or {}).get('amount') or {}
        cols[0].metric('미국 주식 평가금액 · 실제 계좌',usd(market.get('usd')))
        cols[1].metric('USD 매수 가능 금액 · 실제 계좌',usd((snapshot.get('buying_power') or {}).get('USD')))
        cols[2].metric('오늘 순실현손익 · 실제 계좌','—')
        if age_seconds(snapshot.get('received_at'),now) is None or age_seconds(snapshot.get('received_at'),now)>60 or data.account_status.get('status')=='error':st.caption('⚠ 실제 계좌 조회 미확인/과거 값 · '+timestamp(snapshot.get('received_at')))
    else:
        cols[0].metric('모의 총자산',usd(account.get('equity')))
        from .session_app import decimal_value
        cash=decimal_value(account.get('cash'));reserved=decimal_value(account.get('reserved_cash'))
        cols[1].metric('모의 주문 가능 금액',usd(cash-reserved) if cash is not None and reserved is not None else '—')
        # A bounded recent-execution query cannot establish the entire day's P&L.
        cols[2].metric('오늘 매매 손익','—',help='하루 전체 체결을 확인할 수 없어 비워 둡니다. 누적 손익은 매매 내역에서 확인하세요.')
    from .summary_panels import open_cost_repository
    cost_repo=None
    try:
        cost_repo=open_cost_repository(usage_path or path,environment=usage_environment)
        summary=compact_cost(cost_repo,now) if cost_repo else {}
    except Exception:summary={}
    finally:
        if cost_repo:cost_repo.close()
    cols[3].metric('이번 달 AI 비용 / 월 한도액',usd(summary.get('month'))+' / '+usd(summary.get('monthly_hard_budget_usd')),
        help='이번 달 추정 사용액과 프로그램의 월 사용 한도입니다. OpenAI 실제 잔액이 아닙니다.')
    cols[3].caption('총 충전액(수동 입력): '+usd(summary.get('deposited')))
    st.subheader('보유 종목')
    if actual:st.info('실계좌 관리 포지션 연결은 미확인입니다. 전체 계좌 보유와 자동 합산하지 않습니다.')
    else:_table([{k:r[k] for k in ('종목','보유 수량','평균 매수가','평가액','평가 손익')} for r in view['positions'][:5]],'보유 종목이 없습니다.')
    link('보유 전체 보기','매매 내역')
    st.subheader('최근 거래')
    _table(recent_trades(data.tables['executions'],now))
    link('매매 전체 보기','매매 내역')
    st.subheader('뉴스와 AI 분석')
    flow=data.news_flow
    if flow:
        counts=flow['counts']
        st.write(f"기사 수신 **{counts['news_receipts']}건** · AI 요청 **{counts['llm_request_attempts']}건** · 분석 완료 **{counts['validated_responses']}건**")
        st.caption('최근 24시간 기록 중 최대 300건 기준 · 마지막 수신 '+timestamp(flow['last_news_received_at']))
        if counts['failed_or_rejected']:st.caption(f"AI 요청·검증 실패 {counts['failed_or_rejected']}건 · 뉴스 전체 보기에서 확인하세요.")
        if flow['llm_health'].get('code') in {'NO_KEY','NO_CREDIT','AUTH_ERROR'}:
            from .labels import label
            st.caption('AI 상태: '+label(flow['llm_health']['code']))
    news=main_news(data,view,now)
    for row in news:
        event=row.get('event',row)
        from .labels import label
        st.write(f"[{row['labels']}] [{label(row.get('sentiment','UNKNOWN'))}] {event.get('title','')}")
        st.caption(f"{event.get('source','미확인')} · {timestamp(event.get('published_at'))}")
    if not news:st.info('보유·집중 종목의 최근 뉴스가 없습니다.')
    link('뉴스 전체 보기','뉴스·LLM')


def render_strategies(data,view):
    import streamlit as st
    from .session_app import _table
    st.subheader('전략·백테스트 · 저장된 결과만 표시')
    render_research_report()
    st.caption('Momentum / Pullback / Gap Breakout / VWAP · 합성 데모와 실제 외표본 검증을 구분합니다.')
    render_asset_curve(data)
    _table(data.tables['daily_performance'],'비교 가능한 전략 성과 기록 미확인 · 수익률/MDD/PF/기대수익/MFE·MAE/외표본은 N/A')
    risk=data.context.get('macro_reference_risk') or {}
    st.write('시장 참고 위험 필터: '+str(risk.get('mode','미확인')))
    st.caption('필터 ON/OFF 성과 비교·표본 수·외표본 기간은 연구 보고서가 없으면 N/A입니다.')


def render_system(data,view):
    import streamlit as st
    from .session_app import _table,timestamp
    st.subheader('시스템·설정 · 읽기 전용')
    from .operations_overview import render_warmup_details
    render_warmup_details(data.warmup)
    st.caption('엔진 마지막 수신: '+view['last_event']+' · API 연결 정상 여부는 저장 기록만으로 확정하지 않습니다.')
    st.subheader('알림')
    _table(data.tables['alerts'])
    from .system_panel import pipeline_rows
    st.subheader('처리 기록')
    st.caption('종목 선정과 매수 검사 당시의 기록입니다. 시각은 한국시간이며 현재 연결 상태를 뜻하지 않습니다.')
    _table(pipeline_rows(data.tables['pipeline_status']))
    if data.tables['pipeline_status']:
        with st.expander('처리 기록 원본 보기'):
            st.json(data.tables['pipeline_status'])
    from .reference_panel import reference_rows
    st.subheader('시장 참고 지표')
    _table(reference_rows(data.market_reference))
    from .news_panel import provider_rows
    st.subheader('뉴스 수집 상태')
    _table(provider_rows(data.news_status))
    st.caption('reference/event-LLM 설정은 실행 설정에서 확인합니다. 이 화면에서 위험 한도나 실주문 권한을 변경하지 않습니다.')
    st.caption('뉴스 마지막 작업: '+timestamp(data.news_status.get('updated_at')))


def compact_cost(repo,now):
    """5초 메인 갱신은 전체 호출 원장을 훑지 않고 월별 제어 집계만 읽는다."""
    from ..portfolio.models import decimal
    month=(repo.get_control('session_llm_budget_v1',{}) or {}).get(now.strftime('%Y-%m'))
    limits=repo.get_control('session_llm_limits_v1',{}) or {}
    spent=decimal(month['spent']) if month and month.get('spent') is not None else None
    hard=decimal(limits['monthly_hard_budget_usd']) if limits.get('monthly_hard_budget_usd') is not None else None
    funding=repo.get_control('openai_funding_note',{}) or {}
    deposited=decimal(funding['total_usd'],nonnegative=True) if funding.get('total_usd') is not None else None
    return {'month':spent,'monthly_hard_budget_usd':hard,'deposited':deposited}


def filter_trades(data):
    import streamlit as st
    import pandas as pd
    from ..models import utc
    symbol=st.text_input('종목 필터',key='trade_symbol').strip().upper()
    strategy=st.text_input('전략 필터',key='trade_strategy').strip()
    days=st.selectbox('기간',[1,7,30,365,0],format_func=lambda d:f'최근 {d}일' if d else '조회 범위 전체',key='trade_days')
    now=datetime.now(timezone.utc)
    def matches(r):
        if symbol and r.get('symbol')!=symbol:return False
        if strategy and strategy!=r.get('strategy'):return False
        if days:
            try:
                if not 0<=(now-utc(r.get('timestamp') or r.get('created_at'))).total_seconds()<=days*86400:return False
            except (ValueError,TypeError,AttributeError):return False
        return True
    for kind in ('orders','executions'):
        data.tables[kind]=[r for r in data.tables[kind] if matches(r)]
    st.download_button('체결 CSV 내보내기',pd.DataFrame(data.tables['executions']).to_csv(index=False).encode('utf-8-sig'),file_name='sessionquant-executions.csv',mime='text/csv')
    st.caption('상세도 최근 최대 2,000건 조회 범위입니다. 장기간 전체 기록은 record-export로 내보내세요.')


def render_research_report():
    import streamlit as st
    import json
    st.caption('업로드한 05 평가 보고서를 읽기만 합니다. 계산·설정 변경·LLM 호출은 발생하지 않습니다.')
    uploaded=st.file_uploader('evaluate-recording 보고서 JSON',type=['json'],key='research_report')
    if uploaded is None:return
    try:
        if uploaded.size>5_000_000:raise ValueError()
        report=json.loads(uploaded.getvalue())
        if report.get('report_version')!=1 or not isinstance(report.get('results'),dict):raise ValueError()
        period=st.selectbox('평가 구간',list(report['results']),key='strategy_period')
        rows=report['results'][period]
        if not isinstance(rows,dict):raise ValueError()
        from .session_app import _table
        _table([{'전략/정책':name,'검증':r.get('evaluation_status','UNKNOWN'),**(r.get('diagnostic_simulation') or {}),
            '최초 매수 보류 비교':r.get('entry_block_comparison'),'차단/미검증 사유':r.get('blocking_reasons')} for name,r in rows.items()])
        st.caption('진단 시뮬레이션입니다. 실제 운영 성과로 확정하지 않습니다. 구간: '+period)
        st.caption('실제 외표본 검증: '+str(report.get('actual_out_of_sample_validated',False)))
        with st.expander('표본·자료 품질·분할 계획'):
            st.json({'quality':report.get('quality'),'split_plan':report.get('split_plan')})
    except (ValueError,TypeError,KeyError,AttributeError):st.warning('지원하는 평가 보고서가 아닙니다. 값을 생성하지 않습니다.')


def render_asset_curve(data):
    import streamlit as st
    from .session_app import decimal_value
    from ..models import utc
    now=datetime.now(timezone.utc);points=[]
    for row in data.tables['account_snapshots']:
        try:
            stamp=utc(row.get('timestamp') or row.get('observed_at'))
            value=decimal_value((row.get('account') or row).get('equity'))
            if stamp<=now and value is not None:points.append({'시각':stamp,'장부 평가자산':float(value)})
        except (ValueError,TypeError,AttributeError):continue
    if points:
        import pandas as pd
        frame=pd.DataFrame(points).sort_values('시각').set_index('시각')
        st.line_chart(frame)
        st.line_chart((frame/frame.cummax()-1).rename(columns={'장부 평가자산':'장부 낙폭'}))
    else:st.caption('자산곡선·낙폭: 시각이 검증된 자산 스냅샷 없음 · N/A')
