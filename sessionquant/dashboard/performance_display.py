"""Display-only precision for saved performance; never rewrite financial records."""
from decimal import Decimal, InvalidOperation, ROUND_DOWN, localcontext
import re

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


def asset_curve_points(rows, now):
    from ..models import utc
    from .session_app import decimal_value
    values = []
    for row in rows:
        try:
            at = utc(row.get('timestamp') or row.get('observed_at'))
            value = decimal_value((row.get('account') or row).get('equity'))
            if at <= now and value is not None:
                values.append((at, value))
        except (ValueError,TypeError,AttributeError):
            continue
    peak = Decimal(0)
    points = []
    for at, value in sorted(values, key=lambda row:row[0]):
        peak = max(peak, value)
        decline = (value/peak-1)*100 if peak > 0 else None
        points.append({'시각':at, '장부 평가자산':float(value),
            '평가자산 표시':two_decimals(value)+' USD',
            '장부 낙폭':float(decline) if decline is not None else None,
            '낙폭 표시':two_decimals(decline)+'%' if decline is not None else '—'})
    return points


def asset_chart_spec(field):
    money = field == '장부 평가자산'
    return {'mark':{'type':'line','color':'#7ec8ff'},
        'encoding':{
            'x':{'field':'시각','type':'temporal','title':'시각'},
            'y':{'field':field,'type':'quantitative','title':field+(' (USD)' if money else ' (%)'),
                 'axis':{'labelExpr':"format((datum.value < 0 ? ceil(datum.value * 100) : floor(datum.value * 100)) / 100, '.2f')"}},
            'tooltip':[{'field':'시각','type':'temporal','format':'%m.%d %H:%M'},
                       {'field':'평가자산 표시' if money else '낙폭 표시','type':'nominal','title':field}]}}
