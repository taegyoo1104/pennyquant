"""Off-hours presentation of saved records, separate from live trading inputs."""
from ..market.close_display import completed_session, day_key, NY
from ..models import utc
from .home_cards import _positive, _observed_price
from .instrument_names import korean_name


def closing_record(snapshot, symbol, now):
    bounds = completed_session(now)
    if not bounds:
        return None
    item = ((snapshot or {}).get('items') or {}).get(symbol) or {}
    try:
        previous = completed_session(bounds[0])
        if (item.get('session_date') != day_key(bounds) or item.get('source') != 'toss:daily:unadjusted'
                or utc(item['closed_at']) != bounds[1] or not bounds[1] <= utc(item['received_at']) <= now):
            return None
        price = _positive(item.get('price'))
        reference = _positive(item.get('previous_close')) if previous and item.get('previous_session_date') == day_key(previous) else None
        return {'price':price, 'reference':reference, 'at':bounds[1], 'day':day_key(bounds)} if price else None
    except (KeyError, ValueError, TypeError, AttributeError):
        return None


def closed_focus_cards(snapshot, state, quotes, now):
    bounds = completed_session(now)
    if not bounds:
        return []
    try:
        matching = (snapshot or {}).get('session_date') == day_key(bounds) and bounds[0] <= utc(snapshot['captured_at']) <= bounds[1] <= now
    except (KeyError, ValueError, TypeError, AttributeError):
        matching = False
    try:
        saved_at = utc((state or {}).get('updated_at'))
        saved_matches = saved_at <= now and saved_at.astimezone(NY).date().isoformat() == day_key(bounds)
    except (ValueError, TypeError, AttributeError):
        saved_matches = False
    rows = snapshot.get('focus', []) if matching else [r for r in (state or {}).get('items', []) if r.get('focused')] if saved_matches else []
    cards = []
    seen = set()
    for row in rows[:10]:
        symbol = row.get('ticker')
        if not symbol or symbol in seen:
            continue
        seen.add(symbol)
        # Freeze valuation at the regular-session close. Never use after-hours quotes.
        old = dict(row)
        try:
            at = utc(old.get('last_price_at'))
            if not bounds[0] <= at <= bounds[1]:
                old.pop('last_price', None)
        except (ValueError, TypeError, AttributeError):
            old.pop('last_price', None)
        book = (quotes or {}).get(symbol) or {}
        try:
            at, received = utc(book.get('timestamp')), utc(book.get('received_at'))
            if not bounds[0] <= at <= received <= bounds[1]:
                book = {}
        except (ValueError, TypeError, AttributeError):
            book = {}
        price = _observed_price(old, book, bounds[1], bounds[1], 120)
        price.update(price_fresh=False, price_status='장 마감',
                     price_kind='마지막 장중 가격 · 종가 미확인' if price['price'] != '—' else '종가 미확인')
        final = closing_record(snapshot, symbol, now)
        if final:
            change = (final['price']/final['reference']-1)*100 if final['reference'] else None
            price.update(price=f"${final['price']:,.2f}", price_kind='정규장 종가',
                change=f'{change:+.2f}%' if change is not None else '—',
                change_tone='gain' if change is not None and change>0 else 'loss' if change is not None and change<0 else 'flat',
                change_help='정규장 종가의 전 거래일 종가 대비')
        price['time_label'] = day_key(bounds)+' 미국 거래일 · 마감 기록'
        cards.append({'symbol':symbol, 'name':korean_name(symbol,row), **price,
            'monitored':False, 'stage_fresh':False, 'stage':None, 'steps':[],
            'title':'마지막 집중 종목', 'explanation':'장 마감 기록입니다. 다음 거래일에 다시 선정해요.'})
    return cards
