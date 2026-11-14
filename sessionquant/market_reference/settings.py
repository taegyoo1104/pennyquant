"""관측 설정은 거래 필터·실시간 권한 활성화와 별개다."""

def reference_settings(value):
    defaults={'enabled':True,'price_seconds':30,'candle_seconds':180,'calls_per_cycle':2}
    if not isinstance(value,dict) or set(value)-set(defaults):raise ValueError('알 수 없는 시장 참조 관측 설정입니다.')
    result=defaults|value
    if type(result['enabled']) is not bool:raise ValueError('시장 참조 관측 활성화는 불리언이어야 합니다.')
    for name,low,high in [('price_seconds',30,60),('candle_seconds',60,300),('calls_per_cycle',0,3)]:
        if type(result[name]) is not int or not low<=result[name]<=high:raise ValueError(f'{name}: {low}~{high} 정수가 필요합니다.')
    return result
