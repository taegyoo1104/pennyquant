"""기존 시장 탭에 거래 후보와 구분된 참고 ETF를 표시한다."""
from ..market_reference.contracts import DEFAULT_REFERENCE_SYMBOLS, REFERENCES


def stream_price_observation(stream, symbol, now):
    """Shared display snapshot for the home cards and reference table."""
    from datetime import datetime, timezone
    from ..market_reference.trade_feed import samples, price_sample, timestamp, MAX_AGE
    from .session_app import decimal_value
    points, current, latest, connected = [], None, None, False
    try:
        known = timestamp(stream['updated_at'])
        if known <= now:
            points = samples((stream.get('session_points') or stream.get('points') or {}).get(symbol,[]),now)
            current = price_sample((stream.get('price_samples') or {}).get(symbol),now)
            latest = (stream.get('latest') or {}).get(symbol)
            connected = (stream.get('state') == 'CONNECTED' and stream.get('acknowledged') is True
                         and (now-known).total_seconds() <= 10)
    except (ValueError,TypeError,KeyError,AttributeError):
        pass
    current = current or (points[-1] if points else None)
    price = decimal_value(current[1]) if current else None
    event = datetime.fromtimestamp(current[0]/1000,timezone.utc) if current else None
    received, fresh = None, False
    if latest and current:
        try:
            at = timestamp(latest['received_at'])
            if (latest['at'] == current[0] and latest['segment'] == current[2]
                    and decimal_value(latest['price']) == price and event <= at <= now):
                received = at
                fresh = connected and (now-event).total_seconds() <= MAX_AGE
        except (ValueError,TypeError,KeyError,AttributeError):
            pass
    status = ('최근 수신' if fresh else '새 체결 대기' if connected else
              '재접속 중' if stream.get('state') == 'BACKOFF' else '연결 대기')
    if stream.get('state') in {'STOPPED','STOPPING'}:
        status = '수신 중지'
    return {'price':price, 'event_at':event, 'received_at':received, 'fresh':fresh,
            'status':status, 'points':points}


def reference_benchmarks_visible(context):
    return (context.get('benchmark_collection_enabled') is not False
            or bool({'SPY','QQQ','SOXX'} & set(context.get('market_reference_symbols') or [])))


def observed_price(item, symbol, now, quotes=None):
    """Display received prices without treating them as verified trading inputs."""
    from ..models import utc
    from .session_app import decimal_value
    choices = []
    for stream in ('price','candle'):
        row = (item.get('latest_observations') or {}).get(stream) or {}
        try:
            price = decimal_value(row.get('price'))
            event, received, available = (utc(row[k]) for k in ('event_at','received_at','available_at'))
            if (row.get('symbol') != symbol or row.get('currency') != 'USD' or price is None or price <= 0
                    or row.get('quality_status') in {'INVALID','UNAVAILABLE','REVISION_PENDING'}
                    or not event <= received <= available <= now):continue
            choices.append({'price':price,'event_at':event,'received_at':received,
                'max_age':row.get('max_age_seconds',60),'kind':'현재가' if stream=='price' else '확정 분봉 종가',
                'source':row.get('source','미확인')})
        except (ValueError,TypeError,KeyError,AttributeError):continue
    quote = (quotes or {}).get(symbol) or {}
    try:
        bid,ask = decimal_value(quote.get('bid')),decimal_value(quote.get('ask'))
        event,received = utc(quote['timestamp']),utc(quote['received_at'])
        if (quote.get('symbol')==symbol and bid is not None and ask is not None and 0 < bid <= ask
                and event <= received <= now):
            choices.append({'price':(bid+ask)/2,'event_at':event,'received_at':received,
                'max_age':30,'kind':'호가 중간값','source':'엔진 수신 호가'})
    except (ValueError,TypeError,KeyError,AttributeError):pass
    if not choices:return None
    return max(choices,key=lambda r:(r['event_at'],r['received_at']))


def reference_rows(view, now=None, *, quotes=None, show_benchmarks=True, stream=None):
    from datetime import datetime, timezone
    from ..models import utc
    from .session_app import timestamp, usd
    now = now or datetime.now(timezone.utc)
    rows = []
    for symbol, (_, label) in REFERENCES.items():
        if symbol not in view.get('active_symbols', DEFAULT_REFERENCE_SYMBOLS):
            continue
        if not show_benchmarks and symbol in {'SPY', 'QQQ', 'SOXX'}:
            continue
        item = (view.get('items') or {}).get(symbol) or {}
        windows = item.get('windows') or {}
        quality = item.get('quality_status', 'UNKNOWN')
        try:
            if quality == 'GOOD' and now > utc(item['valid_until']): quality = 'STALE'
        except (ValueError, KeyError, TypeError, AttributeError): quality = 'UNKNOWN'
        valid=quality=='GOOD'
        latest=observed_price(item,symbol,now,quotes)
        try:fresh=bool(latest and 0 <= (now-latest['event_at']).total_seconds() <= float(latest['max_age']))
        except (ValueError,TypeError):fresh=False
        streamed = bool(stream and stream.get('enabled') and symbol in {'SPY','QQQ','SOXX'})
        stream_status = None
        if streamed:
            trade = stream_price_observation(stream,symbol,now)
            fresh, stream_status = trade['fresh'], trade['status']
            latest = ({**trade,'kind':'체결가','source':'토스 웹소켓 체결'}
                      if trade['price'] is not None else None)
        window=windows.get('30') or {}
        reasons=[]
        if not latest:reasons.append('가격 수신 대기')
        elif not fresh:reasons.append('가격 갱신 지연')
        if item.get('latency_class') not in {'REALTIME','DELAYED'}:reasons.append('실시간 권한 미확인')
        if window.get('sample_count',0)<window.get('required_samples',31):reasons.append('비교 분봉 부족')
        elif not valid:reasons.append('분봉 신선도·가격 비교 근거 확인 필요')
        rows.append({'종목': symbol, '의미': label, '용도': '시장 참고 전용',
            '관측 가격':usd(latest['price']) if latest else '—',
            '가격 종류':latest['kind'] if latest else '미수신',
            '가격 수신':stream_status if streamed else '최근 수신' if fresh else '과거 기록' if latest else '미수신',
            '분석 상태':'검증 완료' if valid else '확인 필요', '확인할 자료':' · '.join(reasons) if not valid else '—',
            '지연 구분': 'STALE' if quality=='STALE' else item.get('latency_class','UNKNOWN_LATENCY'),
            '공급원':latest['source'] if latest else '미확인','수집 시각':timestamp(latest['received_at']) if latest else '—',
            '전일 대비(%)':item.get('day_return_pct') if valid and item.get('cross_day_comparability')=='VERIFIED' else None,
            '5분 ETF 등락률(%)':(windows.get('5') or {}).get('pct_change') if valid and (windows.get('5') or {}).get('quality_status')=='GOOD' else None,
            '품질': quality, '15분 ETF 등락률(%)': (windows.get('15') or {}).get('pct_change') if valid and (windows.get('15') or {}).get('quality_status')=='GOOD' else None,
            '30분 ETF 등락률(%)': (windows.get('30') or {}).get('pct_change') if valid and (windows.get('30') or {}).get('quality_status')=='GOOD' else None,
            '원본 시각': timestamp(latest['event_at']) if latest else '—', '확인 가능 시각': timestamp(item.get('available_at')),
            '유효 시각': timestamp(item.get('valid_until')), '근거': str(item.get('quality_reasons') or [])})
    return rows


def render_references(view, risk=None, fred=None, *, quotes=None, fred_available=True, show_benchmarks=True, stream=None):
    import streamlit as st
    from .session_app import _table
    st.subheader('시장 참고 ETF · 거래 후보 제외')
    st.caption('가격 수신과 분석 검증은 별개입니다. 과거 가격은 시각과 함께 표시하고, 호가 중간값은 체결 가격과 구분합니다.')
    _table(reference_rows(view,quotes=quotes,show_benchmarks=show_benchmarks,stream=stream))
    if stream and stream.get('enabled'):
        st.caption('SPY·QQQ·SOXX 가격은 메인과 같은 웹소켓 체결 기록입니다. 분석 상태는 별도의 분봉 비교 검증 결과입니다.')
    symbols = '·'.join(s for s in view.get('active_symbols',REFERENCES) if show_benchmarks or s not in {'SPY','QQQ'})
    st.caption(symbols+'는 감시·집중 종목 정원에서 제외됩니다. 권한·가격 비교 가능성이 미확인되면 UNKNOWN으로 표시합니다.')
    if risk:
        from .session_app import timestamp
        st.caption(f"참고 위험 관찰: {risk.get('status','UNKNOWN')} · 평가 시각: {timestamp(risk.get('observed_at'))} · 가설: {risk.get('threshold_version','UNKNOWN')}")
        st.caption('저장된 과거 위험 평가입니다. 현재 단계는 관찰 전용이며 매매에 적용하지 않습니다. UNKNOWN은 안전하다는 뜻이 아닙니다.')
    st.caption('FRED 공식 일별 기준 · 장중 ETF와 수집 주기·단위가 다릅니다.')
    if not fred_available:
        st.info('일별 참고 지표 · 조회 미완료')
    elif fred:
        from .session_app import timestamp
        latest = {}
        for row in fred: latest[row.get('series')] = row
        _table([{'지표': r.get('series'), '값': r.get('value'), '단위': r.get('units'),
            '관측일': r.get('observation_date'), '실제 발표 시각': timestamp(r.get('published_at')),
            '수신 시각': timestamp(r.get('received_at')), '조회 기준일': r.get('requested_asof'),
            '자료 구분': '공식 일별 · 실제 발표/수정 시각 미확인'} for r in latest.values()])
    else:
        st.caption('저장된 FRED 일별 참고 자료가 없습니다. 값을 추정하지 않습니다.')
