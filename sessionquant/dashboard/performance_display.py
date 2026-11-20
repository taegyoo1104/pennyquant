"""Display-only precision for saved performance; never rewrite financial records."""
from decimal import Decimal, InvalidOperation, ROUND_DOWN, localcontext
from datetime import date, datetime
import re
from zoneinfo import ZoneInfo

_NUMERIC = re.compile(r"[-+]?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?")


def two_decimals(value):
    try:
        number = Decimal(str(value))
        if not number.is_finite() or abs(number.adjusted()) > 300 or len(number.as_tuple().digits) > 400:
            return '—'
        with localcontext() as context:
            context.prec = max(28, len(number.as_tuple().digits) + abs(number.adjusted()) + 4)
            cut = number.quantize(Decimal('0.01'), rounding=ROUND_DOWN)
        return format(abs(cut) if cut == 0 else cut, '.2f')
    except (ValueError, InvalidOperation):
        return '—'


def performance_rows(rows):
    def display(value, key=''):
        if key in {'id', 'symbol', 'ticker', 'date', 'timestamp', 'observed_at'} or key.endswith('_id'):
            return value
        if isinstance(value, dict):
            return {k:display(v,k) for k,v in value.items()}
        if isinstance(value, list):
            return [display(v) for v in value]
        if isinstance(value, (float, Decimal)) or (isinstance(value,str) and _NUMERIC.fullmatch(value)):
            return two_decimals(value)
        return value
    return [display(row) for row in rows]


def _money(value, *, signed=False):
    cut = two_decimals(value)
    if cut == '—':
        return cut
    number = Decimal(cut)
    prefix = '-' if number < 0 else '+' if signed and number > 0 else ''
    return prefix + f'${abs(number):,.2f}'


def daily_performance_points(rows, now):
    """One latest saved point per US ledger date, not a sampled intraday curve."""
    from .session_app import decimal_value
    ny, seoul = ZoneInfo('America/New_York'), ZoneInfo('Asia/Seoul')
    latest = {}
    for row in rows:
        try:
            day = date.fromisoformat(str(row.get('id')))
            stamp = row.get('timestamp')
            at = stamp if isinstance(stamp, datetime) else datetime.fromisoformat(str(stamp).replace('Z', '+00:00'))
            if at.tzinfo is None or at > now or at.astimezone(ny).date() != day:
                continue
            if day not in latest or at > latest[day][0]:
                latest[day] = (at, row)
        except (ValueError,TypeError,AttributeError):
            continue
    points = []
    for day, (at, row) in sorted(latest.items()):
        equity, base = decimal_value(row.get('equity')), decimal_value(row.get('day_start_equity'))
        change = None
        if equity is not None and base is not None and base > 0:
            try:
                change = (equity-base)/base*100
            except ArithmeticError:
                pass
        cut = two_decimals(change)
        shown = Decimal(cut) if cut != '—' else None
        points.append({'거래일':day.isoformat(),
            '총자산':float(equity) if two_decimals(equity) != '—' else None,
            '총자산 표시':_money(equity),
            '일간 수익률':float(change) if shown is not None else None,
            '수익률 표시':('+' if shown > 0 else '')+cut+'%' if shown is not None else '—',
            '하루 시작 자산':_money(base),
            '누적 실현손익':_money(row.get('realized_pnl'), signed=True),
            '신규 매수 중지':'예' if row.get('halted') is True else '아니요' if row.get('halted') is False else '—',
            '마지막 기록':at.astimezone(seoul).strftime('%Y-%m-%d %H:%M:%S')})
    return points


def daily_performance_rows(points):
    return [{'거래일 (미국)':p['거래일'], '총자산 ($)':p['총자산 표시'],
             '일간 수익률 (%)':p['수익률 표시'], '누적 실현손익 ($)':p['누적 실현손익'],
             '하루 시작 자산 ($)':p['하루 시작 자산'], '신규 매수 중지':p['신규 매수 중지'],
             '마지막 기록 (KST)':p['마지막 기록']} for p in reversed(points)]


def daily_chart_spec(field):
    money = field == '총자산'
    digits = "format((datum.value < 0 ? ceil(datum.value * 100) : floor(datum.value * 100)) / 100, '.2f')"
    spec = {'height':220, 'title':'일별 총자산 ($)' if money else '일간 수익률 (%)',
        'mark':{'type':'line','point':True,'color':'#7ec8ff','invalid':'break-paths-show-domains'} if money else {'type':'bar','size':24},
        'encoding':{
            'x':{'field':'거래일','type':'ordinal','sort':'ascending','title':'미국 거래일',
                 'axis':{'labelAngle':-30,'labelOverlap':True}},
            'y':{'field':field,'type':'quantitative','title':field+(' ($)' if money else ' (%)'),
                 'scale':{'zero':not money},
                 'axis':{'labelExpr':"'$' + "+digits if money else digits+" + '%'"}},
            'tooltip':[{'field':'거래일','type':'ordinal','title':'미국 거래일'},
                       {'field':'총자산 표시' if money else '수익률 표시','type':'nominal','title':field},
                       {'field':'마지막 기록','type':'nominal','title':'마지막 기록 (KST)'}]}}
    if not money:
        spec['encoding']['color'] = {'condition':[
            {'test':"datum['일간 수익률'] > 0",'value':'#f87171'},
            {'test':"datum['일간 수익률'] < 0",'value':'#60a5fa'}], 'value':'#94a3b8'}
    return spec
