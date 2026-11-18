"""국내·미국 실제 보유와 매수 가능 금액을 표시하는 주문 없는 화면."""
from __future__ import annotations
import argparse
from datetime import datetime,timezone
from decimal import Decimal,InvalidOperation
from pathlib import Path
import sys
from zoneinfo import ZoneInfo

ROOT=Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path: sys.path.insert(0,str(ROOT))


def load_account(path):
    from sessionquant.local_env import load_local_env
    from sessionquant.database.repository import Repository
    repo=None
    try:
        load_local_env(database_only=True)
        repo=Repository(path, initialize=False)
        return repo.get_control('actual_account_snapshot'),repo.get_control('account_monitor_status',{}),None
    except Exception:
        return None,{},'계좌 기록을 읽지 못했습니다. MySQL 연결과 조회 프로그램을 확인하세요.'
    finally:
        if repo: repo.close()


def money(value,currency):
    if value is None: return '—'
    try:
        number=Decimal(str(value))
        if not number.is_finite(): return '—'
        return f'${number:,.2f}' if currency=='USD' else f'₩{number:,.0f}'
    except (InvalidOperation,ValueError,TypeError): return '—'


def render(path):
    import streamlit as st
    snapshot,status,error=load_account(path)
    if error: st.error(error)
    if not snapshot:
        st.info('실제 계좌 조회를 기다리고 있습니다. run-account 터미널 상태를 확인하세요.')
        return
    received=datetime.fromisoformat(snapshot['received_at'])
    age=(datetime.now(timezone.utc)-received).total_seconds()
    st.caption('마지막 조회: '+received.astimezone(ZoneInfo('Asia/Seoul')).strftime('%Y-%m-%d %H:%M:%S KST'))
    if status.get('status')=='error': st.error(status.get('message','계좌 갱신 실패'))
    if age>60: st.warning('60초 이상 지난 기록입니다. 조회 프로그램이 실행 중인지 확인하세요.')
    for issue in snapshot.get('issues',[]): st.warning(issue)
    summary=snapshot.get('summary') or {}
    market=(summary.get('marketValue') or {}).get('amount') or {}
    cash=snapshot.get('buying_power') or {}
    columns=st.columns(4)
    for col,label,value,currency in [(columns[0],'미국 주식 평가금액',market.get('usd'),'USD'),
        (columns[1],'달러 매수 가능 금액',cash.get('USD'),'USD'),
        (columns[2],'국내 주식 평가금액',market.get('krw'),'KRW'),
        (columns[3],'원화 매수 가능 금액',cash.get('KRW'),'KRW')]:
        col.metric(label,money(value,currency))
    st.caption('매수 가능 금액은 예수금·총자산과 다를 수 있습니다. 원화와 달러를 합산하거나 미제공 값을 0으로 만들지 않습니다.')
    control_repo=None
    try:
        from sessionquant.database.repository import Repository
        from sessionquant.trading_controls import render_controls
        control_repo=Repository(path, initialize=False)
        render_controls(control_repo,positions=[],environment='LIVE_LOCAL',readonly=True)
    except Exception:
        st.caption('거래 제어 상태를 확인할 수 없습니다.')
    finally:
        if control_repo: control_repo.close()
    st.subheader('실제 보유 종목')
    rows=[]
    for row in snapshot['items']:
        currency=row['currency']; valuation=row.get('marketValue') or {}; pnl=row.get('profitLoss') or {}
        rows.append({'종목':row['symbol'],'이름':row.get('name'),'통화':currency,'보유 수량':row['quantity'],
            '평균 매수가':money(row.get('averagePurchasePrice'),currency),'현재가':money(row.get('lastPrice'),currency),
            '평가금액':money(valuation.get('amount'),currency),'평가손익':money(pnl.get('amount'),currency)})
    if rows: st.dataframe(rows,hide_index=True,width='stretch')
    else: st.info('조회된 계좌에 보유 주식이 없습니다.')
    st.subheader('미체결 주문')
    orders=snapshot.get('open_orders')
    if orders is None: st.info('미체결 목록은 조회하지 못했습니다.')
    elif not orders: st.info('API에서 조회 가능한 미체결 주문이 없습니다.')
    else:
        st.dataframe([{'종목':o['symbol'],'방향':o['side'],'상태':o['status'],'통화':o['currency'],
                       '주문 수량':o.get('quantity'),'금액 주문':o.get('orderAmount')} for o in orders],hide_index=True,width='stretch')
    st.caption('토스 API가 지원하는 국내·미국 주식과 주문 범위입니다. 옵션·채권 등 전체 자산이나 모든 주문 유형을 포함하지 않을 수 있습니다.')


def main():
    import streamlit as st
    parser=argparse.ArgumentParser(); parser.add_argument('--db',default='data/sessionquant-account.db')
    args,_=parser.parse_known_args()
    st.set_page_config(page_title='SessionQuant 실제 계좌',layout='wide')
    st.title('내 실제 토스증권 계좌')
    st.info('실제 계좌 조회 화면입니다. 활성화된 거래 엔진이 연결되면 매매 제어 요청 버튼을 표시합니다.')
    @st.fragment(run_every='5s')
    def monitor(): render(Path(args.db))
    monitor()

if __name__=='__main__': main()
