"""Readable, escaped paper execution cards for the home dashboard."""
from html import escape

from .instrument_names import korean_name
from .session_app import decimal_value, timestamp, usd
from .trade_reasons import execution_reasons

STYLE = '''<style>
.sq-trades{display:grid;gap:10px;margin:12px 0 24px}
.sq-trade{border:1px solid #29394e;border-radius:16px;background:#122033;padding:16px 18px;color:#edf3fb}
.sq-trade-top{display:flex;align-items:center;gap:12px;flex-wrap:wrap}
.sq-trade-badge{border-radius:9px;padding:6px 10px;font-weight:700;font-size:.85rem;background:#26384d}
.sq-trade-buy{color:#ff9298;background:#462b37}.sq-trade-sell{color:#8bbcff;background:#203b57}
.sq-trade-name{flex:1;min-width:100px}.sq-trade-name small{display:block;color:#a6b7cb;margin-top:3px}
.sq-trade-amount{text-align:right;font-size:1.12rem;font-weight:700}
.sq-trade-amount small{display:block;font-size:.75rem;font-weight:400;color:#a6b7cb}
.sq-trade-meta{display:flex;gap:8px 20px;flex-wrap:wrap;color:#a6b7cb;font-size:.85rem;margin-top:12px}
.sq-trade-reason{border-top:1px solid #29394e;margin-top:12px;padding-top:10px;font-size:.88rem;overflow-wrap:anywhere}
@media(max-width:600px){.sq-trade{padding:14px}.sq-trade-meta{gap:6px}.sq-trade-meta time{flex-basis:100%}}
</style>'''


def trade_cards_html(rows):
    cards = []
    for row in rows:
        side = str(row.get('side', '')).lower()
        badge = {'buy': '매수', 'sell': '매도'}.get(side, '방향 미확인')
        css = 'sq-trade-' + side if side in {'buy', 'sell'} else ''
        symbol = str(row.get('symbol') or '종목 미확인')
        qty, price = decimal_value(row.get('qty')), decimal_value(row.get('price'))
        quantity = format(qty, 'f').rstrip('0').rstrip('.') if qty is not None and '.' in format(qty, 'f') else str(qty) if qty is not None else '—'
        amount = usd(qty * price) if qty is not None and price is not None else '—'
        reason = ('<div class="sq-trade-reason">매도 이유 · ' + escape(row.get('매도 이유', '기록 없음')) + '</div>') if side == 'sell' else ''
        cards.append('<article class="sq-trade"><div class="sq-trade-top">'
                     f'<span class="sq-trade-badge {css}">{badge}</span>'
                     f'<div class="sq-trade-name"><strong>{escape(symbol)}</strong><small>{escape(korean_name(symbol, row))}</small></div>'
                     f'<div class="sq-trade-amount">{amount}<small>체결 금액</small></div></div>'
                     f'<div class="sq-trade-meta"><span>{quantity}주 · 체결가 {usd(price)}</span>'
                     f'<time>{escape(timestamp(row.get("timestamp") or row.get("filled_at")))}</time></div>{reason}</article>')
    return STYLE + '<div class="sq-trades">' + ''.join(cards) + '</div>'


def render_trade_history(data):
    import streamlit as st
    from .trade_pages import MAX_PAGES, PAGE_SIZE, trade_page
    rows = execution_reasons(data.tables.get('executions', []), orders=data.tables.get('orders', []), state=data.state)
    st.markdown(f'<div class="sq-section"><h2>매매 기록</h2><p>모의 체결 · 조회된 {len(rows):,}건 · 체결 금액은 수수료 제외</p></div>', unsafe_allow_html=True)
    st.caption(f'최신순 · 페이지당 {PAGE_SIZE}건 · 최대 {MAX_PAGES}페이지({PAGE_SIZE * MAX_PAGES}건) 표시')
    page = trade_page(rows, kind='executions', source_count=len(rows), scope='home')
    if page:
        st.markdown(trade_cards_html(page), unsafe_allow_html=True)
