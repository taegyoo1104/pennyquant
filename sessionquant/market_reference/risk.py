"""검증된 ETF의 이상 변화를 관찰한다. 금리·현물 추정이나 강제 매도는 하지 않는다."""
from datetime import timedelta
from decimal import Decimal

from ..models import utc
from ..portfolio.models import decimal, serialize
from .contracts import DEFAULT_REFERENCE_SYMBOLS, identity
from .store import VIEW

STATE = 'macro_reference_risk_state_v1'
STATUS = 'macro_reference_risk_view_v1'


def filter_settings(value, *, research_only=False):
    defaults = {'enabled': False, 'paper_shadow_only': True, 'fail_closed': True,
        'cooldown_seconds': 300, 'clear_samples': 3, 'threshold_version': 'hypothesis_v1',
        'ief_15m_pct': '-0.30', 'tlt_15m_pct': '-0.70', 'uso_30m_abs_pct': '2.0'}
    if not isinstance(value, dict) or set(value)-set(defaults):
        raise ValueError('알 수 없는 시장 참고 위험 필터 설정입니다.')
    result = defaults | value
    for name in ('enabled', 'paper_shadow_only', 'fail_closed'):
        if type(result[name]) is not bool: raise ValueError('위험 필터 설정은 불리언이어야 합니다.')
    for name, low, high in [('cooldown_seconds', 60, 3600), ('clear_samples', 2, 20)]:
        if type(result[name]) is not int or not low <= result[name] <= high:
            raise ValueError('위험 필터 회복 대기·표본 설정이 올바르지 않습니다.')
    if not isinstance(result['threshold_version'], str) or not result['threshold_version'].strip():
        raise ValueError('위험 임계값의 가설 버전이 필요합니다.')
    for name in ('ief_15m_pct', 'tlt_15m_pct', 'uso_30m_abs_pct'):
        result[name] = decimal(result[name])
    if not (-100 < result['ief_15m_pct'] < 0 and -100 < result['tlt_15m_pct'] < 0 and 0 < result['uso_30m_abs_pct'] < 100):
        raise ValueError('위험 임계값은 음의 채권 ETF 등락률·양의 원유 ETF 절대 등락률이어야 합니다.')
    if result['enabled'] and not result['paper_shadow_only'] and not research_only:
        raise ValueError('현재 단계는 관찰 전용입니다. 05 외표본·공급자 검증과 사용자 활성화 전 매매 필터를 켤 수 없습니다.')
    return result


class MacroReferenceRiskMonitor:
    """네트워크 없이 참고 원장만 읽는다. ON 정책 검사는 격리 합성 연구에 한정한다."""
    def __init__(self, repo, settings=None, *, research_only=False):
        self.repo = repo
        self.settings = filter_settings(settings or {}, research_only=research_only)
        self.research_only = research_only
        self.state = repo.get_control(STATE, {}) or {}

    @property
    def effective_enabled(self):
        return self.settings['enabled'] and not self.settings['paper_shadow_only']

    def _measure(self, view, now):
        measurements, reasons, ends = {}, [], []
        for symbol in DEFAULT_REFERENCE_SYMBOLS:
            item = (view.get('items') or {}).get(symbol) or {}
            minutes = 30 if symbol == 'USO' else 15
            window = (item.get('windows') or {}).get(str(minutes)) or {}
            measurements[symbol] = {key: item.get(key) for key in ('id', 'event_at', 'available_at', 'observed_at', 'valid_until',
                'latency_class', 'quality_status', 'day_return_pct', 'cross_day_comparability', 'synthetic')}
            measurements[symbol]['windows'] = item.get('windows', {})
            try:
                end = utc(window['event_at']); start = utc(window['start_at'])
                valid = (item.get('symbol') == symbol and window['quality_status'] == 'GOOD'
                    and item.get('latency_class') == 'REALTIME' and utc(item['available_at']) <= now
                    and utc(item['observed_at']) <= now and end <= now <= utc(item['valid_until'])
                    and utc(window['available_at']) <= now and end-start == timedelta(minutes=minutes)
                    and type(window['sample_count']) is int and window['sample_count'] >= minutes+1
                    and window['required_samples'] == minutes+1 and len(set(window['input_ids'])) >= minutes+1)
                decimal(window['pct_change']); decimal(window['volatility_pct'], nonnegative=True)
                if not valid: reasons.append(symbol+':quality_or_time_unverified')
                ends.append(end)
            except (ValueError, TypeError, KeyError, AttributeError):
                reasons.append(symbol+':window_unavailable')
        if len(set(ends)) > 1: reasons.append('mixed_window_end_times')
        return measurements, reasons

    def evaluate(self, now, *, view=None):
        """UNKNOWN은 안전 판정이 아니다. 데이터 오류는 단독 매도 사유가 아니다."""
        now = utc(now)
        if view is None: view = self.repo.get_control(VIEW, {}) or {}
        if not isinstance(view, dict): view = {}
        try:
            measurements, reasons = self._measure(view, now)
        except (ValueError, TypeError, AttributeError):
            measurements, reasons = {}, ['reference_view_invalid']
        settings = self.settings
        frame = identity(measurements)
        triggers, clear_reason = [], None
        direction = 'UNKNOWN'
        if reasons:
            status = 'UNKNOWN'
        else:
            def change(symbol, minutes): return Decimal(measurements[symbol]['windows'][str(minutes)]['pct_change'])
            if change('IEF', 15) <= settings['ief_15m_pct']: triggers.append('IEF_15m_drop')
            if change('TLT', 15) <= settings['tlt_15m_pct']: triggers.append('TLT_15m_drop')
            if abs(change('USO', 30)) >= settings['uso_30m_abs_pct']: triggers.append('USO_30m_move')
            spy, qqq = change('SPY', 15), change('QQQ', 15)
            direction = 'DOWN' if spy < 0 and qqq < 0 else 'UP' if spy > 0 and qqq > 0 else 'MIXED_OR_FLAT'
            status = 'ELEVATED' if triggers and direction == 'DOWN' else 'WATCH' if triggers else 'NORMAL'
        if status in {'WATCH', 'ELEVATED'}:
            self.state.update(latched=True, normal_count=0)
            if self.state.get('last_frame') != frame: self.state['last_risk_at'] = now.isoformat()
        elif status == 'UNKNOWN':
            self.state['normal_count'] = 0
        elif self.state.get('latched'):
            if self.state.get('last_frame') != frame:
                self.state['normal_count'] = self.state.get('normal_count', 0)+1
            past = self.state.get('last_risk_at')
            elapsed = (now-utc(past)).total_seconds() if past else 0
            if elapsed >= settings['cooldown_seconds'] and self.state.get('normal_count', 0) >= settings['clear_samples']:
                self.state['latched'] = False
                clear_reason = 'verified_normal_samples_and_cooldown'
            else:
                status = 'WATCH'
                clear_reason = 'recovery_pending'
        if clear_reason is None and status == 'NORMAL' and self.state.get('last_frame') == frame:
            clear_reason = self.state.get('last_clear_reason')
        self.state['last_frame'] = frame
        self.state['last_clear_reason'] = clear_reason
        blocked = bool(self.effective_enabled and (status in {'WATCH', 'ELEVATED'} or self.state.get('latched')
            or (status == 'UNKNOWN' and settings['fail_closed'])))
        event = serialize({'symbol': 'MARKET_REFERENCE', 'kind': 'macro_reference_risk', 'status': status,
            'observed_at': now, 'available_at': now, 'threshold_version': settings['threshold_version'],
            'thresholds': settings, 'triggering_measurements': measurements, 'triggers': triggers,
            'quality': 'UNKNOWN' if reasons else 'VERIFIED', 'quality_reasons': reasons,
            'market_comovement': direction, 'clear_reason': clear_reason, 'enabled': settings['enabled'],
            'paper_shadow_only': settings['paper_shadow_only'], 'effective_enabled': self.effective_enabled,
            'block_entries': blocked, 'block_adds': blocked, 'force_exit': False,
            'normal_sample_count': self.state.get('normal_count', 0), 'latched': bool(self.state.get('latched'))})
        event['reference_event_id'] = 'reference-risk:'+identity({key: value for key, value in event.items()
            if key not in {'observed_at', 'available_at'}})
        event['id'] = event['reference_event_id']
        previous = self.repo.get_record('pipeline_status', event['id'])
        if previous is None:
            self.repo.record('pipeline_status', event)
            if status in {'WATCH', 'ELEVATED'}:
                self.repo.log('WARNING', '시장 참고 ETF 위험 변화 감지: 관찰 상태와 매매 적용 여부를 확인하세요.',
                    status=status, effective_enabled=self.effective_enabled, reference_event_id=event['id'])
        else:
            event = previous
        self.repo.set_control(STATE, self.state)
        self.repo.set_control(STATUS, event)
        return event
