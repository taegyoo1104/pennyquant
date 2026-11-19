"""Daily and lifetime Paper returns use different, persistent financial bases."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from sessionquant.dashboard.home_returns import account_returns
from sessionquant.portfolio.broker import PaperBroker
from sessionquant.portfolio.models import PortfolioSettings

NOW = datetime(2026,11,19,16,tzinfo=timezone.utc)  # Already Nov 20 in Korea.


def calculate(equity='98', daily='95', initial='100', session='2026-11-19', now=NOW):
    state={'broker':{'session_date':session,'settings':{'initial_cash':initial}}}
    account={'equity':equity,'day_start_equity':daily}
    original=deepcopy((state,account))
    result=account_returns(state,account,now)
    assert (state,account)==original
    return result


def test_daily_can_be_profitable_while_total_remains_a_loss():
    daily,total=calculate()
    assert (daily['value'],daily['tone'])==('+3.15%','gain')
    assert (total['value'],total['tone'])==('-2.00%','loss')


@pytest.mark.parametrize('equity,expected,tone',[
    ('102.999','+2.99%','gain'),('97.001','-2.99%','loss'),
    ('100','0.00%','flat'),('99.999','0.00%','flat'),('0','-100.00%','loss'),
])
def test_signed_two_decimal_display_has_neutral_zero(equity,expected,tone):
    rows=calculate(equity=equity,daily='100')
    assert all((r['value'],r['tone'])==(expected,tone) for r in rows)


@pytest.mark.parametrize('bad',[None,True,'NaN','Infinity','unknown'])
def test_unknown_equity_does_not_invent_zero_return(bad):
    assert all(r['value']=='—' for r in calculate(equity=bad))


@pytest.mark.parametrize('bad',[None,True,'0','-1','NaN'])
def test_invalid_bases_are_independent(bad):
    daily,total=calculate(daily=bad)
    assert daily['value']=='—' and total['value']=='-2.00%'
    daily,total=calculate(initial=bad)
    assert daily['value']=='+3.15%' and total['value']=='—'


def test_new_york_day_and_missing_day_do_not_label_yesterday_as_today():
    assert calculate()[0]['value']=='+3.15%'  # Korean midnight does not reset the US day.
    for session in (None,'invalid','2026-11-18','2026-11-20'):
        assert calculate(session=session)[0]['value']=='—'
    daily,total=calculate(now=NOW+timedelta(days=1))
    assert daily['value']=='—' and total['value']=='-2.00%'


def test_actual_ledger_restoration_preserves_bases_and_non_default_initial_cash():
    broker=PaperBroker(PortfolioSettings(initial_cash=Decimal('250')))
    broker.roll_session(NOW)
    broker.cash=Decimal('255')
    before=broker.to_state()
    broker=PaperBroker.from_state(before)
    broker.roll_session(NOW+timedelta(hours=1))
    saved=broker.to_state()
    assert saved['day_start_equity']=='250'
    rows=account_returns({'broker':saved},saved,NOW+timedelta(hours=1))
    assert [r['value'] for r in rows]==['+2.00%','+2.00%']
    broker.roll_session(NOW+timedelta(days=1))
    broker.cash=Decimal('252.45')
    saved=broker.to_state()
    rows=account_returns({'broker':saved},saved,NOW+timedelta(days=1))
    assert [r['value'] for r in rows]==['-1.00%','+0.98%']
