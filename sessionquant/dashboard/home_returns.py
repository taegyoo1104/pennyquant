"""Paper account returns from persisted bases; display never resets the ledger."""
from datetime import date
from decimal import Decimal
from zoneinfo import ZoneInfo

from .performance_display import two_decimals
from .session_app import decimal_value

COLORS = {'gain':'#f87171', 'loss':'#60a5fa', 'flat':'#9baec4'}


def account_returns(state, account, now):
    broker = state.get('broker') or {}
    equity = decimal_value(account.get('equity'))
    initial = decimal_value((broker.get('settings') or {}).get('initial_cash'))
    daily = decimal_value(account.get('day_start_equity'))
    try:
        if date.fromisoformat(str(broker.get('session_date'))) != now.astimezone(ZoneInfo('America/New_York')).date():
            daily = None
    except (ValueError, TypeError):
        daily = None
    rows = []
    for key, label, base, caption, help_text in (
        ('daily','일간 수익률',daily,'미국 거래일 기준',
         '오늘의 기준 자산 대비 현재 모의 총자산 변화입니다. 미국 날짜 기준이며 같은 날 재시작해도 기준을 유지합니다.'),
        ('total','총 수익률',initial,'최초 모의 자금 기준',
         '이 모의 장부의 최초 자금 대비 현재 총자산 변화입니다. 날짜 변경·재시작으로 초기화하지 않습니다.'),
    ):
        text, tone = '—', 'flat'
        if equity is not None and base is not None and base > 0:
            try:
                cut = two_decimals((equity-base)/base*100)
                if cut != '—':
                    shown = Decimal(cut)
                    tone = 'gain' if shown > 0 else 'loss' if shown < 0 else 'flat'
                    text = ('+' if shown > 0 else '') + cut + '%'
            except ArithmeticError:
                pass
        rows.append({'key':key,'label':label,'value':text,'tone':tone,'caption':caption,
                     'help':help_text+' 이미 낸 수수료와 보유 종목의 평가손익을 포함한 마지막 저장 장부 기준입니다. 소수점 둘째 자리까지 표시합니다.'})
    return rows


def render_account_returns(columns, state, account, now):
    import streamlit as st
    for column, row in zip(columns, account_returns(state,account,now)):
        key = 'home_return_'+row['key']
        with column.container(key=key):
            st.markdown('<style>.st-key-home_metrics .st-key-'+key+
                        ' [data-testid="stMetricValue"]{color:'+COLORS[row['tone']]+'}</style>',unsafe_allow_html=True)
            st.metric(row['label'],row['value'],help=row['help'])
            st.caption(row['caption'])
