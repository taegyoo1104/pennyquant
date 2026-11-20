"""Shared instrument checks preserve strict and observed Paper requirements."""
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from sessionquant.market.bars import Indicators
from sessionquant.market.eligibility import toss_eligibility
from sessionquant.market.scanner import InstrumentEligibility, eligibility_blockers, scan
from sessionquant.market.universe import UniverseStore


NOW = datetime(2026, 11, 13, 16, tzinfo=timezone.utc)
VALUES = {'tradable':True, 'fractional':True, 'halted':False, 'corporate_action':False}


def synthetic():
    return InstrumentEligibility('AAPL', NOW, 'SYNTHETIC_FIXTURE', exchange='NASDAQ', **VALUES)


def verified():
    expiry = NOW+timedelta(seconds=120)
    evidence = {name:{'status':'VERIFIED', 'value':value, 'source':'SYNTHETIC_ACCOUNT_FIXTURE',
        'account_scope':'FIXTURE_A', 'as_of_at':NOW.isoformat(), 'received_at':NOW.isoformat(),
        'valid_until':expiry.isoformat()} for name,value in VALUES.items()}
    return replace(synthetic(), account_scope='FIXTURE_A', valid_until=expiry, field_evidence=evidence)


def observed(stamp=NOW):
    return toss_eligibility('AAPL', {'symbol':'AAPL', 'status':'ACTIVE', 'market':'NASDAQ', 'currency':'USD'}, stamp)


def test_synthetic_strict_and_verified_strict_keep_distinct_evidence_requirements():
    assert eligibility_blockers('AAPL', synthetic(), NOW) == []
    assert eligibility_blockers('AAPL', synthetic(), NOW, require_evidence=True) == ['eligibility_unverified']
    assert eligibility_blockers('AAPL', verified(), NOW, require_evidence=True) == []


@pytest.mark.parametrize('name,value', [('tradable',False), ('fractional',False), ('halted',True),
    ('corporate_action',True), ('tradable',None), ('fractional',None), ('halted',None), ('corporate_action',None)])
def test_strict_unknown_or_restricted_fields_block_without_account_evidence(name, value):
    item = replace(synthetic(), **{name:value})
    assert eligibility_blockers('AAPL', item, NOW) == [f'instrument_{name}_unverified_or_restricted']


@pytest.mark.parametrize('fault', ['missing_field', 'wrong_account', 'unknown_source', 'expired_field', 'future_field'])
def test_verified_strict_checks_each_field_evidence_at_evaluation_time(fault):
    item = verified()
    fields = deepcopy(item.field_evidence)
    if fault == 'missing_field':fields.pop('fractional')
    if fault == 'wrong_account':fields['fractional']['account_scope'] = 'OTHER_ACCOUNT'
    if fault == 'unknown_source':fields['fractional']['source'] = 'UNKNOWN'
    if fault == 'expired_field':fields['fractional']['valid_until'] = (NOW-timedelta(seconds=1)).isoformat()
    if fault == 'future_field':fields['fractional']['received_at'] = (NOW+timedelta(seconds=1)).isoformat()
    assert eligibility_blockers('AAPL', replace(item, field_evidence=fields), NOW,
                                require_evidence=True) == ['eligibility_unverified']


def test_strict_age_boundary_and_custom_age_limit_are_unchanged():
    item = verified()
    assert eligibility_blockers('AAPL', item, NOW+timedelta(seconds=60), require_evidence=True) == []
    assert eligibility_blockers('AAPL', item, NOW+timedelta(seconds=61), require_evidence=True) == [
        'instrument_eligibility_stale_or_future']
    assert eligibility_blockers('AAPL', item, NOW+timedelta(seconds=61), require_evidence=True,
                                max_eligibility_age_seconds=90) == []


def test_strict_listing_expiry_future_check_and_missing_identity_block():
    item = replace(synthetic(), valid_until=NOW+timedelta(seconds=5))
    assert eligibility_blockers('AAPL', item, NOW+timedelta(seconds=5)) == []
    assert eligibility_blockers('AAPL', item, NOW+timedelta(seconds=6)) == ['instrument_eligibility_expired']
    assert eligibility_blockers('AAPL', replace(synthetic(), checked_at=NOW+timedelta(seconds=1)), NOW) == [
        'instrument_eligibility_stale_or_future']
    assert eligibility_blockers('MSFT', synthetic(), NOW) == ['instrument_symbol_mismatch']
    assert eligibility_blockers('AAPL', None, NOW) == ['instrument_eligibility_unknown']
    assert eligibility_blockers('AAPL', replace(synthetic(), exchange='UNKNOWN'), NOW) == ['exchange_unverified']


def test_observed_listing_allows_unknown_account_fields_and_uses_listing_age():
    item = observed(NOW-timedelta(minutes=75))
    assert all(getattr(item, name) is None for name in VALUES)
    assert eligibility_blockers('AAPL', item, NOW, require_evidence=True, paper_assumptions=True) == []
    assert eligibility_blockers('MSFT', item, NOW, paper_assumptions=True) == ['instrument_symbol_mismatch']


@pytest.mark.parametrize('name,value', [('tradable',False), ('fractional',False), ('halted',True), ('corporate_action',True)])
def test_observed_policy_keeps_explicit_restrictions(name, value):
    item = replace(observed(), **{name:value})
    assert eligibility_blockers('AAPL', item, NOW, paper_assumptions=True) == ['paper_known_restriction_'+name]


@pytest.mark.parametrize('fault', ['missing', 'stale', 'future', 'expired_listing', 'delisted'])
def test_observed_missing_or_invalid_listing_blocks(fault):
    item = observed()
    if fault == 'missing':item = None
    if fault == 'stale':item = observed(NOW-timedelta(days=2))
    if fault == 'future':item = observed(NOW+timedelta(seconds=1))
    if fault in {'expired_listing', 'delisted'}:
        fields = deepcopy(item.field_evidence)
        if fault == 'expired_listing':fields['listing_status']['valid_until'] = (NOW-timedelta(seconds=1)).isoformat()
        else:fields['listing_status']['value'] = 'DELISTED'
        item = replace(item, field_evidence=fields)
    blockers = eligibility_blockers('AAPL', item, NOW, paper_assumptions=True)
    expected = ('paper_listing_evidence_missing' if fault == 'missing' else
                'paper_listing_not_active' if fault == 'delisted' else 'paper_listing_evidence_stale_or_missing')
    assert expected in blockers


@pytest.mark.parametrize('item,eligibility_reason', [
    (None, 'instrument_eligibility_unknown'),
    (replace(verified(), fractional=False), 'instrument_fractional_unverified_or_restricted'),
])
def test_scan_preserves_blocker_order_and_final_evidence_aggregation(item, eligibility_reason):
    empty = Indicators((), (), None, None, None, None, 0, 0)
    assert scan(UniverseStore(), 'AAPL', None, item, empty, NOW, require_evidence=True) == [
        'universe_unknown_stale_or_nonmember', eligibility_reason, 'quote_missing',
        'closed_bars_missing_or_stale', 'technical_history_incomplete', 'insufficient_liquidity',
        'relative_volume_unknown_or_low', 'eligibility_unverified']
    # The helper deliberately has no dependency on universe, quotes or indicators.
    assert eligibility_blockers('AAPL', verified(), NOW, require_evidence=True) == []
