"""Informational ETF cards; no strategy, order, or network calls."""
from datetime import datetime, timedelta, timezone
from html import escape
from decimal import Decimal
from zoneinfo import ZoneInfo

from ..market.bars import session_bounds
from ..models import utc
from .home_cards import CARD_STYLE, _positive
from .reference_panel import observed_price, stream_price_observation

BENCHMARKS = {'SPY':'S&P 500', 'QQQ':'나스닥 100', 'SOXX':'미국 반도체 ETF'}
COLORS = {'up':'#f87171', 'down':'#60a5fa', 'flat':'#94a3b8'}
NY, KST = ZoneInfo('America/New_York'), ZoneInfo('Asia/Seoul')


def _previous_day(now):
    for days in range(1, 11):
        bounds = session_bounds(now-timedelta(days=days))
        if bounds:
            return bounds[0].astimezone(NY).date()
    return None


def benchmark_cards(view, now=None, *, symbols=None, stream=None, closing=None):
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
        from ..market.close_display import closed_market
        if closed_market(now):
            cards.append(_closed_card(item, symbol, name, stream, closing, now))
            continue
        latest = observed_price(item, symbol, now)
        price = _positive(latest['price']) if latest else None
        fresh = bool(price and 0 <= (now-latest['event_at']).total_seconds() <= latest['max_age'])
        change, reference = None, None
        reference_label = '전일 종가 확인 중'
        try:
            previous = _positive(item.get('previous_close'))
            previous_at = utc(item['previous_close_at'])
            if previous and previous_at <= now and previous_at.astimezone(NY).date() == previous_day:
                reference = previous
                reference_label = previous_day.strftime('%m.%d')+f' 종가 ${previous:,.2f} 기준'
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
            'price_fresh':fresh, 'tone':tone, 'reference':reference, 'reference_label':reference_label,
            'change':f'{change:+.2f}%' if change is not None else '전일 대비 확인 중',
            'time_label':latest['event_at'].astimezone(KST).strftime('%m.%d %H:%M:%S KST') if price else '가격 수신을 기다려요.',
            'points':points, 'trend_status':'최근 1시간 · 1분봉 종가' if trend_fresh else '이전 분봉 기록' if points else '분봉 수집 중'})
        if stream and stream.get('enabled'):
            _stream_card(cards[-1],stream,now)
        if bounds and now < bounds[0]:
            opened = bounds[0].astimezone(KST).strftime('%m.%d %H:%M KST')
            card = cards[-1]
            card['empty_message'] = '개장 대기\n'+opened+'부터 차트를 준비해요.'
            card['trend_status'] = '장전 · 차트는 정규장 개장 후 표시해요.'
            if card.get('chart_kind') == 'trade' and card['price_status'] == '새 체결 대기':
                card['price_status'] = '장전 시세 대기' if card['price'] == '—' else '가격 갱신 대기'
    return cards


def _closed_card(item, symbol, name, stream, closing, now):
    from ..market.close_display import completed_session, day_key
    from ..market_reference.trade_feed import session_samples, timestamp
    from .closed_market import closing_record
    bounds = completed_session(now)
    points, segments = [], []
    try:
        known = timestamp(stream['updated_at'])
        if known <= now:
            raw = (stream.get('session_points') or stream.get('points') or {}).get(symbol, [])
            for millis, value, segment in session_samples(raw, known):
                at = datetime.fromtimestamp(millis/1000, timezone.utc)
                if bounds and bounds[0] <= at <= bounds[1]:
                    points.append((at, Decimal(value)))
                    segments.append(segment)
    except (KeyError, ValueError, TypeError, AttributeError):
        pass
    final = closing_record(closing, symbol, now)
    price = final['price'] if final else points[-1][1] if points else None
    at = final['at'] if final else points[-1][0] if points else None
    if price is None:
        old = observed_price(item, symbol, now)
        if old and bounds and bounds[0] <= old['event_at'] <= bounds[1]:
            price, at = _positive(old['price']), old['event_at']
    reference = final['reference'] if final else None
    if reference is None and bounds:
        try:
            previous = completed_session(bounds[0])
            if previous and utc(item['previous_close_at']).astimezone(NY).date().isoformat() == day_key(previous):
                reference = _positive(item['previous_close'])
        except (KeyError, ValueError, TypeError, AttributeError):
            pass
    change = (price/reference-1)*100 if price and reference else None
    day = day_key(bounds) if bounds else '거래일 미확인'
    return {'symbol':symbol, 'name':name, 'price':f'${price:,.2f}' if price else '—',
        'price_kind':'정규장 종가' if final else '마지막 장중 가격 · 종가 미확인',
        'price_status':'장 마감', 'price_fresh':False,
        'change':f'{change:+.2f}%' if change is not None else '전일 대비 미확인',
        'tone':'up' if change is not None and change>0 else 'down' if change is not None and change<0 else 'flat',
        'reference':reference, 'reference_label':'전 거래일 종가 대비' if final else '마지막 장중 가격 기준 · 참고',
        'time_label':day+' 미국 거래일 · 마감 기록',
        'points':points, 'segments':segments, 'chart_kind':'session_trade',
        'trend_status':'마지막 거래일 · 수신한 구간의 1분 간격 체결가' if points else '저장된 당일 차트 없음',
        'empty_message':'해당 거래일에 저장한 차트가 없습니다.'}


def _stream_card(card,stream,now):
    symbol=card['symbol']
    observation=stream_price_observation(stream,symbol,now)
    points,price,event=observation['points'],observation['price'],observation['event_at']
    fresh,status=observation['fresh'],observation['status']
    change=(price/card['reference']-1)*100 if price and card['reference'] else None
    bounds=session_bounds(now)
    kind='장전 체결가' if bounds and now<bounds[0] else '체결가'
    card.update(price=f'${price:,.2f}' if price else '—',price_kind=kind,price_status=status,price_fresh=fresh,
        change=f'{change:+.2f}%' if change is not None else '전일 대비 확인 중',
        tone='up' if change is not None and change>0 else 'down' if change is not None and change<0 else 'flat',
        time_label=event.astimezone(KST).strftime('%m.%d %H:%M:%S KST') if event else '체결가 수신을 기다려요.',
        points=[(datetime.fromtimestamp(r[0]/1000,timezone.utc),Decimal(r[1])) for r in points],
        segments=[r[2] for r in points],chart_kind='trade',sample_seconds=60,
        trend_status='정규장 개장부터 · 1분 간격 체결가' if points else '실시간 체결가 수신 대기')


def _chart(points, reference, symbol, *, segments=None, trade=False, empty_message=None, session_chart=False, minute_samples=False):
    from ..market_reference.trade_feed import GAP_SECONDS
    if not points:
        if empty_message:
            return '<div class="sq-benchmark-empty">'+escape(empty_message).replace('\n','<br>')+'</div>'
        return '<div class="sq-benchmark-empty">'+('실시간 체결가를 기다려요.' if trade else '분봉을 받으면 흐름이 표시돼요.')+'</div>'
    price_only = reference is None and session_chart
    if reference is None and not price_only:
        return '<div class="sq-benchmark-empty">전일 종가 확인 후<br>등락 차트를 표시해요.</div>'
    if price_only:
        reference = points[0][1]
    first, last = points[0][0], points[-1][0]
    # Show movement within the received window, even after a large opening gap.
    # Yesterday's close determines color, but must not flatten the price range.
    low, high = min(v for _,v in points), max(v for _,v in points)
    middle = (low+high)/2
    span = max(high-low, middle*Decimal('0.0001'))
    lower, upper = middle-span*Decimal('0.62'), middle+span*Decimal('0.62')
    def ordinate(value):
        return 112-96*float((value-lower)/(upper-lower))
    baseline_y = ordinate(reference)
    reference_note = ('전일 종가 ↑ · 표시 구간 위' if reference>upper else
                      '전일 종가 ↓ · 표시 구간 아래' if reference<lower else '점선 · 전일 종가')
    baseline = (f'<path class="sq-benchmark-reference-line" d="M8 {baseline_y:.2f}H292" '
                'stroke="#8192a7" stroke-width="1" stroke-dasharray="4 4"/>') if lower<=reference<=upper else ''
    if price_only:
        baseline, reference_note = '', '전일 종가 미확인 · 저장된 가격 흐름'
    seconds = max(60, (last-first).total_seconds())
    parts, part, previous = [], [], None
    for index,(at, value) in enumerate(points):
        x = 8+284*(at-first).total_seconds()/seconds
        delta = (value/reference-1)*100 if not price_only else Decimal(0)
        y = ordinate(value)
        gap = False
        if previous is not None:
            elapsed = (at-previous).total_seconds()
            gap = (((int(at.timestamp())//60 - int(previous.timestamp())//60 > 1) if session_chart or minute_samples else elapsed > GAP_SECONDS)
                   or segments[index] != segments[index-1]) if trade else elapsed != 60
        if gap:
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
                cross=(x1+(x2-x1)*float(-d1/(d2-d1)),baseline_y)
                add('up' if d1>0 else 'down',(x1,y1),cross)
                add('up' if d2>0 else 'down',cross,(x2,y2))
            else:
                tone='up' if d1>0 or d2>0 else 'down' if d1<0 or d2<0 else 'flat'
                add(tone,(x1,y1),(x2,y2))
    strokes=''.join('<polyline points="'+' '.join(f'{x:.2f},{y:.2f}' for x,y in part)+
        '" fill="none" stroke="'+COLORS[tone]+'" stroke-width="2.5" stroke-linecap="round" '
        'stroke-linejoin="round" vector-effect="non-scaling-stroke"/>' for tone,part in paths)
    if len(parts[-1])>1:
        x,y,delta=parts[-1][-1]
        color=COLORS['up' if delta>0 else 'down' if delta<0 else 'flat']
        dots.append(f'<circle cx="{x:.2f}" cy="{y:.2f}" r="6" fill="{color}" opacity=".15"/>'
                    f'<circle cx="{x:.2f}" cy="{y:.2f}" r="2.5" fill="{color}"/>')
    return ('<div class="sq-benchmark-reference">'+reference_note+'</div><div class="sq-benchmark-plot">'+
        '<svg class="sq-benchmark-chart" viewBox="0 0 300 128" preserveAspectRatio="none" role="img" aria-label="'+escape(symbol)+
        ' 수신 가격 흐름 · 표시 구간에 맞춘 세로축">'+baseline+strokes+''.join(dots)+'</svg></div>'+
        '<div class="sq-benchmark-axis"><span>'+first.astimezone(KST).strftime('%H:%M')+'</span><span>'+
        last.astimezone(KST).strftime('%H:%M')+' KST</span></div>'+
        ('<div class="sq-benchmark-trend">'+('연결 끊김·수신 공백은 빈칸으로 표시해요.' if trade else '분봉 일부 없음 · 빈 구간은 잇지 않아요.')+'</div>' if len(parts)>1 else ''))


def benchmark_cards_html(cards):
    style = '''<style>
.sq-benchmark-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:14px;margin:14px 0 20px}
.sq-benchmark-name{color:var(--sq-muted,#a7b8ca);font-size:.78rem;margin-top:3px}
.sq-benchmark-change{font-size:.95rem;font-weight:650;margin-top:10px;font-variant-numeric:tabular-nums}
.sq-benchmark-change small{font-size:.7rem;font-weight:400;color:var(--sq-muted,#a7b8ca);margin-left:6px}
.sq-benchmark-reference{font-size:.68rem;color:var(--sq-muted,#a7b8ca);margin-top:12px}
.sq-benchmark-plot{margin-top:2px}
.sq-benchmark-chart{display:block;width:100%;height:104px}
.sq-benchmark-axis{display:flex;justify-content:space-between;font-size:.68rem;color:var(--sq-muted,#a7b8ca)}
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
            '<div class="sq-benchmark-change" style="color:'+color+'">'+e('change')+'<small>'+e('reference_label')+'</small></div>'+
            _chart(card['points'],card.get('reference'),card['symbol'],segments=card.get('segments'),trade=card.get('chart_kind') in {'trade','session_trade'},empty_message=card.get('empty_message'),session_chart=card.get('chart_kind')=='session_trade',minute_samples=card.get('sample_seconds')==60)+'<div class="sq-benchmark-trend">'+e('trend_status')+'</div>'+
            '<div class="sq-focus-time">'+e('time_label')+'</div></article>')
    # Separate raw HTML blocks so Markdown does not absorb the cards into CSS.
    return CARD_STYLE+'\n\n'+style+'\n\n<div class="sq-benchmark-grid">'+''.join(elements)+'</div>'


def render_benchmarks(view, context, *, now=None, stream=None, closing=None):
    import streamlit as st
    symbols = context.get('market_reference_symbols')
    if symbols is not None and not set(BENCHMARKS).intersection(symbols):
        return
    st.markdown('<div class="sq-section"><h2>시장 흐름</h2><p>참고용 · 매수 조건과 별개</p></div>',unsafe_allow_html=True)
    st.markdown(benchmark_cards_html(benchmark_cards(view,now,symbols=symbols,stream=stream,closing=closing)),unsafe_allow_html=True)
    from ..market.close_display import closed_market
    if closed_market(now or datetime.now(timezone.utc)):
        st.caption('폐장·휴장에는 마지막 거래일 기록을 표시합니다. 종가는 장 마감 후 확인한 일봉 기준이며, 미확인 시 마지막 장중 가격으로 구분합니다. 차트는 실제 수신한 구간만 남기며 빈 구간을 채우지 않습니다.')
    elif stream and stream.get('enabled'):
        st.caption('장전에도 수신한 체결가를 표시합니다. 현재가는 최신 수신 시세로 갱신하고, 차트는 정규장 개장부터 매분 마지막으로 받은 체결가를 남깁니다. 진행 중인 분의 점은 갱신됩니다. 재접속·수신 기록이 없는 분은 선으로 잇지 않습니다.')
    st.caption('현재 연결한 토스 API에서 해당 미국 지수를 직접 제공하지 않아 ETF로 시장 흐름을 보여드려요. '
        'SPY는 S&P 500, QQQ는 나스닥 100, SOXX는 미국 반도체 업종을 살펴보는 용도입니다. '
        '지수 자체의 수치는 아니며, SOXX는 필라델피아 반도체 지수와 구성 종목·비중이 달라요.')


def render_live_benchmarks(path, *, refresh=True):
    """Independent, small DB read; a chart tick does not rerun controls/news."""
    import streamlit as st
    from .session_app import load_database
    @st.fragment(run_every=2 if refresh else None)
    def chart():
        data=load_database(path,page='대시보드',panel='benchmarks')
        if data.error:
            st.caption('시장 흐름 연결을 확인하고 있어요. 다음 갱신 때 다시 확인합니다.')
        render_benchmarks(data.market_reference,data.context,stream=data.benchmark_stream,closing=data.close_display)
    chart()
