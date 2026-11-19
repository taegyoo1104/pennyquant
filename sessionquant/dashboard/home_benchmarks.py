"""Informational ETF cards; no strategy, order, or network calls."""
from datetime import datetime, timedelta, timezone
from html import escape
from decimal import Decimal
from zoneinfo import ZoneInfo

from ..market.bars import session_bounds
from ..models import utc
from .home_cards import CARD_STYLE, _positive
from .reference_panel import observed_price

BENCHMARKS = {'SPY':'S&P 500', 'QQQ':'나스닥 100', 'SOXX':'미국 반도체 ETF'}
COLORS = {'up':'#f87171', 'down':'#60a5fa', 'flat':'#94a3b8'}
NY, KST = ZoneInfo('America/New_York'), ZoneInfo('Asia/Seoul')


def _previous_day(now):
    for days in range(1, 11):
        bounds = session_bounds(now-timedelta(days=days))
        if bounds:
            return bounds[0].astimezone(NY).date()
    return None


def benchmark_cards(view, now=None, *, symbols=None):
    now = utc(now or datetime.now(timezone.utc))
    bounds = session_bounds(now)
    previous_day = _previous_day(now)
    cards = []
    selected = symbols if symbols is not None else (view or {}).get('active_symbols')
    if selected is None:
        selected = ('SPY', 'QQQ')  # Older profiles did not collect SOXX.
    for symbol, name in BENCHMARKS.items():
        if symbol not in selected:
            continue
        item = ((view or {}).get('items') or {}).get(symbol) or {}
        latest = observed_price(item, symbol, now)
        price = _positive(latest['price']) if latest else None
        fresh = bool(price and 0 <= (now-latest['event_at']).total_seconds() <= latest['max_age'])
        change, reference = None, None
        try:
            previous = _positive(item.get('previous_close'))
            previous_at = utc(item['previous_close_at'])
            if previous and previous_at <= now and previous_at.astimezone(NY).date() == previous_day:
                reference = previous
            if (reference and price and previous_at <= latest['event_at'] <= now
                    and latest['event_at'].astimezone(NY).date() == now.astimezone(NY).date()):
                change = (price/reference-1)*100
        except (KeyError, ValueError, TypeError, AttributeError):
            pass
        tone = 'up' if change is not None and change > 0 else 'down' if change is not None and change < 0 else 'flat'
        points, conflicts = {}, set()
        for row in item.get('trend_points') or []:
            try:
                at, available = utc(row['at']), utc(row['available_at'])
                value = _positive(row['price'])
                if (not bounds or value is None or not bounds[0] < at <= bounds[1]
                        or not now-timedelta(hours=1) <= at <= available <= now
                        or at.second or at.microsecond):
                    continue
                if at in points and points[at] != value:
                    conflicts.add(at)
                points[at] = value
            except (KeyError, ValueError, TypeError, AttributeError):
                continue
        points = [(at, value) for at, value in sorted(points.items()) if at not in conflicts][-61:]
        trend_fresh = bool(points and (now-points[-1][0]).total_seconds() <= 180)
        cards.append({'symbol':symbol, 'name':name, 'price':f'${price:,.2f}' if price else '—',
            'price_kind':latest['kind'] if price else '관측 가격',
            'price_status':'최근 수신' if fresh else '이전 가격' if price else '수신 대기',
            'price_fresh':fresh, 'tone':tone, 'reference':reference,
            'change':f'{change:+.2f}%' if change is not None else '전일 대비 확인 중',
            'time_label':latest['event_at'].astimezone(KST).strftime('%m.%d %H:%M:%S KST') if price else '가격 수신을 기다려요.',
            'points':points, 'trend_status':'최근 1시간 · 1분봉 종가' if trend_fresh else '이전 분봉 기록' if points else '분봉 수집 중'})
    return cards


def _chart(points, reference, symbol):
    if not points:
        return '<div class="sq-benchmark-empty">분봉을 받으면 흐름이 표시돼요.</div>'
    if reference is None:
        return '<div class="sq-benchmark-empty">전일 종가 확인 후<br>등락 차트를 표시해요.</div>'
    first, last = points[0][0], points[-1][0]
    # Symmetric range keeps yesterday's close at the vertical center, even when
    # every observation is positive or negative. Never invent a zero first point.
    scale = max(Decimal('0.01'), max(abs((v/reference-1)*100) for _,v in points))
    seconds = max(60, (last-first).total_seconds())
    parts, part, previous = [], [], None
    for at, value in points:
        x = 8+284*(at-first).total_seconds()/seconds
        delta = (value/reference-1)*100
        y = 64-48*float(delta/scale)
        if previous is not None and (at-previous).total_seconds() != 60:
            parts.append(part); part = []
        part.append((x,y,delta)); previous = at
    parts.append(part)
    paths, dots = [], []
    def add(tone, start, end):
        if paths and paths[-1][0] == tone and paths[-1][1][-1] == start:
            paths[-1][1].append(end)
        else:
            paths.append((tone,[start,end]))
    for part in parts:
        if len(part)==1:
            x,y,delta=part[0]
            tone='up' if delta>0 else 'down' if delta<0 else 'flat'
            dots.append(f'<circle cx="{x:.2f}" cy="{y:.2f}" r="2.5" fill="{COLORS[tone]}"/>')
        for (x1,y1,d1),(x2,y2,d2) in zip(part,part[1:]):
            if d1*d2<0:
                cross=(x1+(x2-x1)*float(-d1/(d2-d1)),64)
                add('up' if d1>0 else 'down',(x1,y1),cross)
                add('up' if d2>0 else 'down',cross,(x2,y2))
            else:
                tone='up' if d1>0 or d2>0 else 'down' if d1<0 or d2<0 else 'flat'
                add(tone,(x1,y1),(x2,y2))
    strokes=''.join('<polyline points="'+' '.join(f'{x:.2f},{y:.2f}' for x,y in part)+
        '" fill="none" stroke="'+COLORS[tone]+'" stroke-width="2.5" stroke-linecap="round" '
        'stroke-linejoin="round" vector-effect="non-scaling-stroke"/>' for tone,part in paths)
    return ('<div class="sq-benchmark-plot"><div class="sq-benchmark-y"><span>+'+f'{scale:.2f}'+
        '%</span><span>0%</span><span>−'+f'{scale:.2f}'+'%</span></div>'+
        '<svg class="sq-benchmark-chart" viewBox="0 0 300 128" preserveAspectRatio="none" role="img" aria-label="'+escape(symbol)+
        ' 전일 종가 대비 등락 흐름"><path d="M8 16H292 M8 112H292" stroke="#26354a" stroke-width="1"/>'+
        '<path d="M8 64H292" stroke="#8192a7" stroke-width="1" stroke-dasharray="4 4"/>'+strokes+''.join(dots)+'</svg></div>'+
        '<div class="sq-benchmark-axis"><span>'+first.astimezone(KST).strftime('%H:%M')+'</span><span>'+
        last.astimezone(KST).strftime('%H:%M')+' KST</span></div>'+
        ('<div class="sq-benchmark-trend">분봉 일부 없음 · 빈 구간은 잇지 않아요.</div>' if len(parts)>1 else ''))


def benchmark_cards_html(cards):
    style = '''<style>
.sq-benchmark-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:14px;margin:14px 0 20px}
.sq-benchmark-name{color:var(--sq-muted,#a7b8ca);font-size:.78rem;margin-top:3px}
.sq-benchmark-change{font-size:.95rem;font-weight:650;margin-top:10px;font-variant-numeric:tabular-nums}
.sq-benchmark-change small{font-size:.7rem;font-weight:400;color:var(--sq-muted,#a7b8ca);margin-left:6px}
.sq-benchmark-plot{display:grid;grid-template-columns:40px minmax(0,1fr);margin-top:12px}
.sq-benchmark-chart{display:block;width:100%;height:128px}
.sq-benchmark-y{position:relative;font-size:.62rem;color:var(--sq-muted,#a7b8ca)}
.sq-benchmark-y span{position:absolute;right:4px;transform:translateY(-50%)}
.sq-benchmark-y span:nth-child(1){top:16px}.sq-benchmark-y span:nth-child(2){top:64px}.sq-benchmark-y span:nth-child(3){top:112px}
.sq-benchmark-axis{margin-left:40px;display:flex;justify-content:space-between;font-size:.68rem;color:var(--sq-muted,#a7b8ca)}
.sq-benchmark-empty{height:128px;display:flex;align-items:center;justify-content:center;color:var(--sq-muted,#a7b8ca);font-size:.78rem;text-align:center}
.sq-benchmark-trend{font-size:.7rem;color:var(--sq-muted,#a7b8ca);margin-top:10px}
@media(max-width:540px){.sq-benchmark-grid{grid-template-columns:repeat(2,minmax(0,1fr));gap:10px}.sq-benchmark-card:last-child:nth-child(odd){grid-column:1/-1}.sq-benchmark-change small{display:block;margin:3px 0}}
@media(max-width:359px){.sq-benchmark-grid{grid-template-columns:1fr}}
</style>'''
    elements = []
    for card in cards:
        e = lambda key: escape(str(card.get(key,'')),quote=True)
        color = COLORS.get(card.get('tone'), COLORS['flat'])
        badge = ' fresh' if card.get('price_fresh') else ''
        elements.append('<article class="sq-focus-card sq-benchmark-card"><div class="sq-focus-top"><div><div class="sq-focus-symbol">'+e('symbol')+
            '</div><div class="sq-benchmark-name">'+e('name')+'</div></div><span class="sq-focus-badge'+badge+'">'+e('price_status')+
            '</span></div><div class="sq-focus-price">'+e('price')+'</div><div class="sq-focus-kind">'+e('price_kind')+' · USD</div>'+
            '<div class="sq-benchmark-change" style="color:'+color+'">'+e('change')+'<small>전일 종가 대비 · 참고</small></div>'+
            _chart(card['points'],card.get('reference'),card['symbol'])+'<div class="sq-benchmark-trend">'+e('trend_status')+'</div>'+
            '<div class="sq-focus-time">'+e('time_label')+'</div></article>')
    # Separate raw HTML blocks so Markdown does not absorb the cards into CSS.
    return CARD_STYLE+'\n\n'+style+'\n\n<div class="sq-benchmark-grid">'+''.join(elements)+'</div>'


def render_benchmarks(view, context, *, now=None):
    import streamlit as st
    symbols = context.get('market_reference_symbols')
    if symbols is not None and not set(BENCHMARKS).intersection(symbols):
        return
    st.markdown('<div class="sq-section"><h2>시장 흐름</h2><p>참고용 · 매수 조건과 별개</p></div>',unsafe_allow_html=True)
    st.markdown(benchmark_cards_html(benchmark_cards(view,now,symbols=symbols)),unsafe_allow_html=True)
