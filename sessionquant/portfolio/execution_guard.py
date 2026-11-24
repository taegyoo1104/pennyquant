"""Runtime execution checks with recorded outcomes; strategy clocks stay unchanged."""
from ..models import utc
from .session import SessionExitManager

MAX_CHECKS = 10_000
REASONS = {'runtime_clock_rollback', 'runtime_clock_unavailable', 'runtime_quote_stale_or_invalid',
           'runtime_entry_window_closed', 'runtime_execution_check_missing', 'runtime_execution_check_mismatch',
           'runtime_execution_check_invalid', 'runtime_execution_check_limit',
           'runtime_instrument_eligibility_restricted', 'runtime_order_no_longer_active',
           'runtime_exit_window_closed', 'runtime_sell_quantity_unavailable'}


class BuyExecutionGuard:
    def __init__(self, broker, clock=None, *, last_checked_at=None, eligibility_check=None):
        self.broker, self.clock = broker, clock
        self.eligibility_check = eligibility_check
        self.last_checked_at = utc(last_checked_at) if last_checked_at else None
        self.recorded = None
        self.checks = []
        self.overflow_count = 0

    @property
    def enabled(self):
        return self.clock is not None or self.recorded is not None

    def begin_batch(self):
        self.checks = []
        self.overflow_count = 0

    def _reason(self, order, quote, checked_at, decision_at):
        if checked_at < decision_at or (self.last_checked_at and checked_at < self.last_checked_at):
            return 'runtime_clock_rollback'
        if not self.broker.quote_is_fresh(quote, checked_at):
            return 'runtime_quote_stale_or_invalid'
        if not SessionExitManager(self.broker).permits_entry(checked_at):
            return 'runtime_entry_window_closed'
        intent = getattr(order, 'intent', order)
        submitted = utc(getattr(order, 'submitted_at', None) or intent.created_at)
        cancelled_at = getattr(order, 'cancel_effective_at', None)
        if ((checked_at-submitted).total_seconds() >= self.broker.settings.order_ttl_seconds
                or (cancelled_at is not None and checked_at >= utc(cancelled_at))):
            return 'runtime_order_no_longer_active'
        if self.eligibility_check is not None and self.eligibility_check(quote, checked_at) is not True:
            return 'runtime_instrument_eligibility_restricted'
        return ''

    def check(self, phase, order, quote, decision_at):
        if not self.enabled:
            return None
        decision_at = utc(decision_at)
        key = {'phase': phase, 'order_id': order.client_id, 'symbol': quote.symbol,
               'quote_at': utc(quote.timestamp).isoformat(),
               'quote_received_at': utc(quote.received_at).isoformat()}
        result = {**key, 'checked_at': None, 'allowed': False,
                  'reason': 'runtime_execution_check_missing'}
        if len(self.checks) >= MAX_CHECKS:
            self.overflow_count += 1
            return {**result, 'reason': 'runtime_execution_check_limit'}
        if self.recorded is not None:
            # An altered strategy may reach a different order or sequence. Its
            # execution must not borrow another order's recorded permission.
            index = len(self.checks)
            row = self.recorded[index] if isinstance(self.recorded, list) and index < len(self.recorded) else None
            if not isinstance(self.recorded, list) or len(self.recorded) > MAX_CHECKS:
                result['reason'] = 'runtime_execution_check_invalid'
                row = None
            elif row is not None and (not isinstance(row, dict)
                    or type(row.get('allowed')) is not bool
                    or not isinstance(row.get('reason'), str)
                    or (row['allowed'] and row.get('reason') != '')
                    or (not row['allowed'] and row.get('reason') not in REASONS)):
                result['reason'] = 'runtime_execution_check_invalid'
                row = None
            elif isinstance(row, dict) and any(row.get(name) != value for name, value in key.items()):
                result['reason'] = 'runtime_execution_check_mismatch'
                row = None
            if row is not None:
                try:
                    checked = utc(row['checked_at']) if row.get('checked_at') else None
                    if row['allowed'] and (checked is None or self._reason(order, quote, checked, decision_at)):
                        raise ValueError('Invalid recorded permission')
                    result = {**key, 'checked_at': checked.isoformat() if checked else None,
                              'allowed': row['allowed'], 'reason': row['reason']}
                except (ValueError, TypeError, AttributeError):
                    result['reason'] = 'runtime_execution_check_invalid'
        else:
            try:
                checked = utc(self.clock())
                reason = self._reason(order, quote, checked, decision_at)
                result.update(checked_at=checked.isoformat(), allowed=not reason, reason=reason)
            except Exception:
                result['reason'] = 'runtime_clock_unavailable'
        if result['checked_at']:
            checked = utc(result['checked_at'])
            self.last_checked_at = max(checked, self.last_checked_at or checked)
        self.checks.append(result)
        return result

    def fill_blocker(self, order, quote, decision_at):
        result = self.check('fill', order, quote, decision_at)
        return (result['reason'] or 'runtime_execution_check_invalid') if result is not None and not result['allowed'] else None


class SellExecutionGuard(BuyExecutionGuard):
    """Exit checks do not inherit entry windows, buy halts or entry eligibility."""
    def _reason(self, order, quote, checked_at, decision_at):
        if checked_at < decision_at or (self.last_checked_at and checked_at < self.last_checked_at):
            return 'runtime_clock_rollback'
        if not self.broker.quote_is_fresh(quote,checked_at):
            return 'runtime_quote_stale_or_invalid'
        schedule=SessionExitManager(self.broker).schedule(checked_at)
        if schedule is None or not schedule.opens_at<=checked_at<schedule.fractional_deadline:
            return 'runtime_exit_window_closed'
        submitted=utc(order.submitted_at or order.intent.created_at)
        if (order.status not in {'accepted','partial','cancel_pending'}
                or (checked_at-submitted).total_seconds()>=self.broker.settings.order_ttl_seconds
                or (order.cancel_effective_at is not None and checked_at>=utc(order.cancel_effective_at))):
            return 'runtime_order_no_longer_active'
        position=self.broker.positions.get(order.intent.symbol)
        if order.intent.side!='sell' or position is None or order.remaining_qty>position.qty:
            return 'runtime_sell_quantity_unavailable'
        return ''
