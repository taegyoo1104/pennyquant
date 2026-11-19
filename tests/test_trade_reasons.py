"""Reasons follow actual order identity through partial fills, restarts and display."""
from copy import deepcopy
from datetime import timedelta
from decimal import Decimal

from sessionquant.dashboard.trade_reasons import execution_reasons
from sessionquant.portfolio import PaperBroker, PortfolioManager
from sessionquant.portfolio.models import serialize
from test_paper_market_fills import NOW, bought, quote


def test_stop_reason_survives_partial_fill_restart_and_missing_old_fill_metadata():
    broker=bought()
    # This is a real stop evaluation, not a display-invented cause based on P&L.
    orders=PortfolioManager(broker).check_exits(quote(2,'97','97.1'),NOW+timedelta(seconds=2))
    assert len(orders)==1 and orders[0].intent.reason=='stop_loss'
    first=broker.process_quote(quote(3,'97','97.1'),max_fill_qty=Decimal('.04'))
    assert len(first)==1 and first[0].reason=='stop_loss' and broker.positions
    restored=PaperBroker.from_state(broker.to_state())
    last=restored.process_quote(quote(4,'97','97.1'))
    assert last and all(f.reason=='stop_loss' for f in last) and not restored.positions
    fills=[serialize(f) for f in [*first,*last]]
    assert all(r['매도 이유']=='손절 기준 가격 도달' for r in execution_reasons(fills))
    old=deepcopy(fills)
    for r in old:r.pop('reason')
    assert all(r['매도 이유']=='손절 기준 가격 도달'
               for r in execution_reasons(old,state={'broker':restored.to_state()}))
    assert all('reason' not in r and '매도 이유' not in r for r in old)
    legacy=restored.to_state()
    for r in legacy['fills']:r.pop('reason')
    assert PaperBroker.from_state(legacy).reconcile()['ok']


def test_a_loss_or_same_symbol_does_not_imply_a_stop_and_conflicting_links_are_unknown():
    fill={'id':'f', 'client_id':'order-1', 'symbol':'AAPL', 'side':'sell', 'realized_pnl':'-1'}
    order={'client_id':'order-1', 'symbol':'AAPL', 'side':'sell', 'reason':'manual_market_sell'}
    assert execution_reasons([fill])[0]['매도 이유']=='기록 없음'
    for invalid in ({**order,'client_id':'other'}, {**order,'symbol':'AMD'}, {**order,'side':'buy'}):
        assert execution_reasons([fill],orders=[invalid])[0]['매도 이유']=='기록 없음'
    assert execution_reasons([fill],orders=[order])[0]['매도 이유']=='직접 전량 매도'
    conflict={**order,'reason':'stop_loss'}
    assert execution_reasons([fill],orders=[order,conflict])[0]['매도 이유']=='기록 없음'
    assert execution_reasons([{**fill,'reason':'operator_stop_all'}],orders=[conflict])[0]['매도 이유']=='매매 종료 · 전체 매도'
    assert execution_reasons([{**fill,'side':'buy','reason':'stop_loss'}])[0]['매도 이유']=='—'


def test_trade_filter_exports_reason_from_order_outside_the_selected_date_range(monkeypatch):
    from datetime import datetime, timezone
    import pandas as pd
    from io import BytesIO
    import streamlit as st
    from sessionquant.dashboard.overview import filter_trades
    from sessionquant.dashboard.session_app import DashboardData
    now=datetime.now(timezone.utc)
    data=DashboardData()
    data.tables['orders']=[{'client_id':'o', 'symbol':'AAPL', 'side':'sell',
        'created_at':(now-timedelta(days=2)).isoformat(), 'reason':'session_exit'}]
    data.tables['executions']=[{'id':'f', 'client_id':'o', 'symbol':'AAPL', 'side':'sell',
        'timestamp':now.isoformat(), 'qty':'0.057185'}]
    captured={}
    monkeypatch.setattr(st,'text_input',lambda *args,**kwargs:'')
    monkeypatch.setattr(st,'selectbox',lambda *args,**kwargs:1)
    monkeypatch.setattr(st,'caption',lambda *args,**kwargs:None)
    monkeypatch.setattr(st,'download_button',lambda label,data,**kwargs:captured.update(data=data))
    filter_trades(data)
    exported=pd.read_csv(BytesIO(captured['data']),dtype=str)
    assert not data.tables['orders']
    assert exported.iloc[0]['매도 이유']=='장 마감 전 정리'
    assert exported.iloc[0]['qty']=='0.057185'
