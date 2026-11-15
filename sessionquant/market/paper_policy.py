"""Explicit experimental Paper assumptions; never rewrite broker eligibility evidence."""
from ..models import utc
from .scanner import EXCHANGES

STRICT = 'strict'
OBSERVED = 'observed_paper_v1'
POLICIES = {STRICT, OBSERVED}


def validate_policy(value):
    if not isinstance(value,str) or value not in POLICIES:
        raise ValueError('paper_data_policy는 strict 또는 observed_paper_v1이어야 합니다.')
    return value


def paper_eligibility(item, now):
    now = utc(now)
    blockers = []
    assumptions = ['paper_fractional_trading_enabled', 'actual_account_permissions_not_verified']
    if item is None:
        blockers.append('paper_listing_evidence_missing')
    else:
        listing = item.field_evidence.get('listing_status', {})
        if not isinstance(listing,dict):listing={}
        try:
            fresh = (item.checked_at <= now and (now-item.checked_at).total_seconds() <= 86400
                     and item.valid_until is not None and now <= item.valid_until
                     and 0 <= (now-utc(listing['received_at'])).total_seconds() <= 86400
                     and now <= utc(listing['valid_until']))
        except (ValueError, TypeError, KeyError):
            fresh = False
        if not fresh:
            blockers.append('paper_listing_evidence_stale_or_missing')
        if (listing.get('status') != 'VERIFIED' or listing.get('value') != 'ACTIVE'
                or not isinstance(listing.get('source'),str) or listing['source'].strip().upper() in {'','UNKNOWN'}):
            blockers.append('paper_listing_not_active')
        if not isinstance(item.exchange,str) or item.exchange.upper() not in EXCHANGES:
            blockers.append('exchange_unverified')
        for name, restricted in [('tradable',False), ('fractional',False), ('halted',True), ('corporate_action',True)]:
            if getattr(item,name) is restricted:
                blockers.append('paper_known_restriction_'+name)
        for name in ('halted','corporate_action'):
            if getattr(item,name) is None:
                assumptions.append(name+'_unknown')
    return {'allowed':not blockers,'blockers':blockers,'assumptions':assumptions,
            'status':'SIMULATION_ASSUMPTIONS' if not blockers else 'BLOCKED',
            'actual_account_verified':False}
