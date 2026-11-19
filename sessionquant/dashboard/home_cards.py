"""Display-only focus cards: current observations and closed-bar strategy stages."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from html import escape
from zoneinfo import ZoneInfo

from ..models import utc
from .watchlist_panel import current_status, focus_is_monitored
from .instrument_names import korean_name


STAGES = {
    'DATA_WAIT': ('자료 준비', '판단할 분봉을 모으고 있어요.', ('wait', 'pending', 'pending')),
    'TREND_MISSING': ('상승 대기', '상승 흐름이 확인되면 조정을 살펴봐요.', ('wait', 'pending', 'pending')),
    'TREND': ('조정 대기', '상승 흐름 확인. 조정을 기다려요.', ('done', 'wait', 'pending')),
    'CORRECTION': ('반등 대기', '조정 중이에요. 반등을 기다려요.', ('done', 'done', 'wait')),
    'REBOUND': ('반등 확인', '반등을 확인했어요. 주문 조건을 따로 확인해요.', ('done', 'done', 'done')),
    'TOO_LONG': ('새 조정 대기', '조정이 길어져 다음 기회를 기다려요.', ('done', 'warn', 'pending')),
    'TOO_DEEP': ('새 조정 대기', '하락 폭이 커서 다음 기회를 기다려요.', ('done', 'warn', 'pending')),
}


def _positive(value):
    try:
        if isinstance(value, bool):return None
        number=Decimal(str(value))
        return number if number.is_finite() and 0<number<Decimal('1e10') else None
    except (InvalidOperation, TypeError, ValueError):return None


def _observed_price(row, quote, published_at, now, max_age):
    choices=[]
    try:
        price=_positive(row.get('last_price')); at=utc(row.get('last_price_at'))
        if price is not None and at<=utc(published_at)<=now:
            choices.append((at,price,'관측가',max_age))
    except (ValueError,TypeError,AttributeError):pass
    try:
        bid,ask=_positive(quote.get('bid')),_positive(quote.get('ask'))
        at,received=utc(quote.get('timestamp')),utc(quote.get('received_at'))
        if quote.get('symbol')==row['ticker'] and bid is not None and ask is not None and bid<=ask and at<=received<=now:
            choices.append((at,(bid+ask)/2,'호가 중간값',30))
    except (ValueError,TypeError,KeyError,AttributeError):pass
    if not choices:return {'price':'—','price_kind':'관측가','price_status':'수신 대기','price_fresh':False,'time_label':'시세 미확인'}
    at,price,kind,limit=max(choices,key=lambda value:value[0])
    age=(now-at).total_seconds()
    fresh=0<=age<=limit
    stamp=at.astimezone(ZoneInfo('Asia/Seoul')).strftime('%m.%d %H:%M:%S')
    return {'price':f'${price:,.2f}','price_kind':kind,'price_status':'최근 수신' if fresh else '이전 가격',
            'price_fresh':fresh,'time_label':stamp+' KST'}


def focus_cards(state, assessments=None, *, quotes=None, now=None):
    """Read saved snapshots only. Stage progress never means order permission."""
    now=utc(now or datetime.now(timezone.utc));state=state or {}
    status,current=current_status(state,now)
    limit=state.get('data_max_age_seconds',120)
    if isinstance(limit,bool) or not isinstance(limit,(int,float)) or not 0<limit<=120:limit=120
    try:current=bool(current and 0<=(now-utc(state['updated_at'])).total_seconds()<=limit)
    except (ValueError,TypeError,KeyError,AttributeError):current=False
    records={r.get('symbol'):r for r in (assessments or {}).get('rows',[]) if isinstance(r,dict)}
    result=[];seen=set();preopen=None
    for row in state.get('items',[]):
        if not isinstance(row,dict) or row.get('focused') is not True:continue
        symbol=row.get('ticker')
        if not isinstance(symbol,str) or not symbol or symbol in seen:continue
        seen.add(symbol)
        monitored=bool(current and status=='VALID' and focus_is_monitored(row,now))
        saved=records.get(symbol,{})
        progress=saved.get('progress') or {}
        if not isinstance(progress,dict):progress={}
        fresh=False
        try:
            evaluated=utc(saved['evaluated_at'])
            five,fifteen=utc(progress['five_at']),utc(progress['fifteen_at'])
            boundary=now.replace(second=0,microsecond=0)
            fresh=bool(monitored and not row.get('focus_refresh_pending')
                and fifteen<=five<=evaluated<=now and (now-evaluated).total_seconds()<=limit
                and five==boundary-timedelta(minutes=now.minute%5)
                and fifteen==boundary-timedelta(minutes=now.minute%15))
        except (KeyError,TypeError,ValueError,AttributeError):pass
        stage=progress.get('stage') if fresh and isinstance(progress.get('stage'),str) else None
        title,explanation,steps=STAGES.get(stage,('판단 대기','새 분석 자료를 기다려요.',('pending',)*3))
        if not monitored:
            title,explanation='목록 갱신 대기','마지막 집중 기록이에요. 현재 목록을 확인 중이에요.'
        elif row.get('focus_refresh_pending'):
            title,explanation='자료 갱신 대기','집중 감시는 유지하며 새 자료를 기다려요.'
        elif not fresh:
            if preopen is None:
                from ..session_window import schedule_times
                try:
                    schedule=schedule_times(now)
                    preopen=bool(schedule and now<utc(schedule['opens_at']))
                except (KeyError,ValueError,TypeError,AttributeError):preopen=False
            if preopen:
                title,explanation='개장 대기','정규장 분봉이 쌓이면 전략을 확인해요.'
        elif fresh and progress.get('entry_history_ready') is not True:
            explanation='흐름은 관찰 중이에요. 진입에 필요한 분봉은 준비 중이에요.'
        elif fresh and saved.get('order_status') in {'BLOCKED','ALLOCATION_REJECTED'}:
            explanation='주문 조건을 충족하지 않아 매수를 보류했어요.'
            if saved.get('blockers'):
                from .status_labels import blocker_summary
                explanation='매수 보류 · '+blocker_summary(saved['blockers'],limit=1)
        elif fresh and saved.get('order_status')=='APPROVED':
            explanation='모의 주문 승인 · 체결은 별도로 확인해요.'
        price=_observed_price(row,(quotes or {}).get(symbol) or {},state.get('updated_at'),now,limit)
        result.append({'symbol':symbol,'name':korean_name(symbol,row),**price,'monitored':monitored,'stage_fresh':fresh,
            'stage':stage,'title':title,'explanation':explanation,
            'steps':[{'label':label,'state':value} for label,value in zip(('상승','조정','반등'),steps)]})
        if len(result)>=10:break
    return result


CARD_STYLE='''<style>
.sq-focus-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:14px;margin:14px 0 20px}
.sq-focus-card{border:1px solid var(--sq-card-border,#29374a);background:var(--sq-card-bg,#111c2d);border-radius:18px;padding:20px;color:var(--sq-text,#e9f1f7);min-width:0}
.sq-focus-top{display:flex;justify-content:space-between;gap:8px;align-items:center;margin-bottom:15px}
.sq-focus-identity{min-width:0}
.sq-focus-name{font-size:.73rem;line-height:1.4;margin-top:4px;color:var(--sq-muted,#a7b8ca);overflow-wrap:anywhere}
.sq-focus-symbol{font-size:1.08rem;letter-spacing:.035em;font-weight:750}
.sq-focus-badge{font-size:.73rem;border-radius:20px;padding:4px 9px;background:var(--sq-soft,#1b2b3d);color:var(--sq-muted,#a7b8ca);white-space:nowrap}
.sq-focus-badge.fresh{color:var(--sq-accent,#55dec5)}
.sq-focus-price{font-size:1.9rem;font-weight:720;letter-spacing:-.045em;line-height:1.15;font-variant-numeric:tabular-nums}
.sq-focus-kind,.sq-focus-time{font-size:.73rem;color:var(--sq-muted,#a7b8ca);margin-top:5px}
.sq-focus-time{margin-top:2px}
.sq-focus-steps{display:flex;gap:7px;list-style:none;margin:20px 0 13px;padding:0}
.sq-focus-steps li{flex:1;border-top:3px solid var(--sq-card-border,#29374a);padding-top:7px;font-size:.76rem;color:var(--sq-muted,#a7b8ca)}
.sq-focus-steps li.done{border-color:var(--sq-accent,#55dec5);color:var(--sq-accent,#55dec5)}
.sq-focus-steps li.wait{border-color:var(--sq-wait,#edc578);color:var(--sq-wait,#edc578)}
.sq-focus-steps li.warn{border-color:var(--sq-wait,#edc578);color:var(--sq-wait,#edc578)}
.sq-focus-card p.sq-focus-title{font-size:.91rem;font-weight:650;margin:0 0 5px}
.sq-focus-card p.sq-focus-explanation{font-size:.82rem;line-height:1.55;color:var(--sq-muted,#a7b8ca);margin:0;min-height:2.4em;word-break:keep-all;overflow-wrap:anywhere}
.sq-focus-empty{border:1px dashed var(--sq-card-border,#29374a);border-radius:18px;padding:26px;color:var(--sq-muted,#a7b8ca)}
@media(max-width:540px){.sq-focus-grid{grid-template-columns:repeat(2,minmax(0,1fr));gap:10px}.sq-focus-card{padding:14px}.sq-focus-price{font-size:1.5rem}.sq-focus-symbol{font-size:.95rem}.sq-focus-badge{padding:3px 6px;font-size:.65rem}.sq-focus-steps{gap:4px}.sq-focus-time{font-size:.65rem}}
@media(max-width:359px){.sq-focus-grid{grid-template-columns:1fr}}
</style>'''


def focus_cards_html(cards):
    """Escape every supplied string; CSS classes are selected from fixed tokens."""
    if not cards:return CARD_STYLE+'<div class="sq-focus-empty">집중 종목을 선정하고 있어요. 자료가 준비되면 여기에 표시해요.</div>'
    elements=[]
    for card in cards[:10]:
        e=lambda key:escape(str(card.get(key,'')),quote=True)
        badge=' fresh' if card.get('price_fresh') is True else ''
        steps=''.join('<li class="'+(s['state'] if s.get('state') in {'done','wait','pending','warn'} else 'pending')+'">'+escape(str(s.get('label','')),quote=True)+'</li>' for s in card.get('steps',[]))
        name='<div class="sq-focus-name">'+e('name')+'</div>' if card.get('name') else ''
        elements.append('<article class="sq-focus-card"><div class="sq-focus-top"><div class="sq-focus-identity"><div class="sq-focus-symbol">'+e('symbol')+'</div>'+name+'</div><span class="sq-focus-badge'+badge+'">'+e('price_status')+'</span></div><div class="sq-focus-price">'+e('price')+'</div><div class="sq-focus-kind">'+e('price_kind')+' · USD</div><div class="sq-focus-time">'+e('time_label')+'</div><ol class="sq-focus-steps" aria-label="현재 전략 단계">'+steps+'</ol><p class="sq-focus-title">'+e('title')+'</p><p class="sq-focus-explanation">'+e('explanation')+'</p></article>')
    return CARD_STYLE+'<div class="sq-focus-grid">'+''.join(elements)+'</div>'


def render_focus_cards(state, assessments=None, *, quotes=None, now=None):
    import streamlit as st
    st.markdown(focus_cards_html(focus_cards(state,assessments,quotes=quotes,now=now)),unsafe_allow_html=True)
