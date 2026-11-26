"""Small operational news snapshots, never a substitute for full replay evidence."""

REFERENCE_LIMIT = 16


def _scalar(value):
    # Never turn a shortened identifier into an apparently valid reference.
    if value is None or type(value) in (bool, int, float):
        return value
    if isinstance(value, str) and len(value) <= 256:
        return value
    return None


def _references(values):
    values = values if isinstance(values, list) else []
    sample = [value for value in values[:REFERENCE_LIMIT]
              if isinstance(value, str) and len(value) <= 256]
    return {'sample': sample, 'count': len(values), 'omitted': len(values) - len(sample)}


def summarize(context, evidence):
    """Copy bounded scalar decisions and version references, not article/reason trees.

    Original context/evidence and order-linked selection archives are untouched.
    Counts and omitted flags explicitly distinguish this sample from full replay.
    """
    context = context if isinstance(context, dict) else {}
    evidence = evidence if isinstance(evidence, dict) else {}
    decision = {key: _scalar(context.get(key)) for key in (
        'status', 'block_entries', 'major_adverse', 'blackout', 'force_liquidation',
        'llm_role', 'macro_score_applied') if key in context}
    reasons = context.get('reasons') or []
    reasons = reasons if isinstance(reasons, list) else []
    decision['reason_count'] = len(reasons)
    # Long free text is deliberately omitted, not mislabeled as complete.
    decision['reason_sample'] = [r[:240] for r in reasons[:3] if isinstance(r, str)]
    score = context.get('candidate_score_signal') or {}
    if isinstance(score, dict):
        decision['candidate_score_signal'] = {
            key: _scalar(score.get(key)) for key in (
                'status', 'value', 'symbol', 'observed_at', 'valid_until',
                'reason', 'policy_version', 'macro_score_applied') if key in score}
        for key in ('analysis_ids', 'llm_call_ids'):
            decision['candidate_score_signal'][key] = _references(score.get(key))
    result = {'format': 'news_diagnostic_v1', 'full_evidence': False, 'context': decision}
    for group, fields in (
        ('analyses', ('id', 'analysis_id', 'news_id', 'model', 'prompt_version',
                      'analyzed_at', 'published_at', 'received_at')),
        ('economic_events', ('id', 'known_at', 'scheduled_at')),
    ):
        rows = evidence.get(group) or []
        rows = rows if isinstance(rows, list) else []
        sample = [{key: _scalar(row.get(key)) for key in fields if key in row}
                  for row in rows[:REFERENCE_LIMIT] if isinstance(row, dict)]
        count = evidence.get(group + '_count', len(rows))
        count = count if type(count) is int and count >= len(rows) else len(rows)
        result[group] = {'sample': sample, 'count': count, 'omitted': count - len(sample)}
    if evidence.get('error_type'):
        result['error_type'] = _scalar(evidence['error_type'])
    return result
