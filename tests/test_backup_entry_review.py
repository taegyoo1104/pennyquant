import copy
import json

from scripts.review_paper_backup import Review, decode_row, research_projection, stamp


def reviewer():
    return Review(stamp('2026-11-18T13:00Z'), stamp('2026-11-18T21:20Z'),
                  ['pq_research_events','pq_signals'], stamp('2026-11-18T15:45Z'), stamp('2026-11-18T18:30Z'))


def batch(at='2026-11-18T16:00Z'):
    return {'namespace':'paper_test', 'kind':'research_events', 'data':{'kind':'engine_batch',
        'available_at':at, 'data':{'control_state':{'running':True}, 'manual_halt':False, 'decision_count':1,
        'decisions':[{'symbol':'AAPL', 'eligible':True, 'signal':{'id':'SECRET'}, 'blockers':[],
            'regime':'BULLISH', 'indicators':{'strategy_diagnostics':[], 'article_text':'SECRET'}}],
        'quality_rows':[{'symbol':'AAPL', 'strategy_rvol':1.3, 'rvol':{'value':None,'calculated_value':1.3,
            'observed_at':at,'cutoff_at':'2026-11-18T16:00Z', 'current_missing_minutes':0,
            'complete_sessions':20,'required_sessions':20,'reasons':['split_volume_comparability_unknown']}}],
        'portfolio_selection':{'account_seq':'SECRET', 'rows':[{'symbol':'AAPL',
            'blockers':['candidate_quote_time_mismatch','candidate_quote_time_mismatch'],
            'quote_at':'2026-11-18T15:59:55Z','quote_received_at':'2026-11-18T16:00Z'}]}}}}


def test_signal_before_selection_is_not_confused_with_saved_signals_or_orders():
    review=reviewer();review.consume(batch())
    result=review.finish()['namespaces']['paper_test']
    group=result['entry_review']['selected_entry_window']
    assert group['signal_present']==1 and result['counts_in_window']['signals']==0
    assert group['decision_blockers']=={}
    assert group['selection_blockers']=={'candidate_quote_time_mismatch':1}
    assert group['strategy_rvol_present']==group['calculated_rvol_present']==1
    sample=group['by_symbol']['AAPL']['samples'][0]
    assert sample['strategy_rvol']==1.3
    assert sample['volume_reasons']==['split_volume_comparability_unknown']
    assert sample['quote_at_kst']=='2026-11-19T00:59:55+09:00'
    assert 'SECRET' not in json.dumps(result)


def test_entry_window_is_half_open_and_keeps_preopen_out_of_entry_counts():
    review=reviewer()
    for at in ('2026-11-18T14:31Z','2026-11-18T15:45Z','2026-11-18T18:29:59Z','2026-11-18T18:30Z'):
        review.consume(batch(at))
    result=review.finish()['namespaces']['paper_test']['entry_review']
    assert result['all_in_window']['evaluations']==4
    assert result['selected_entry_window']['evaluations']==2


def test_allocation_rejection_is_separate_from_strategy_and_selection():
    review=reviewer();row=batch()
    selected=row['data']['data']['portfolio_selection']['rows'][0]
    selected.update(blockers=[],allocation_reason='risk_budget_insufficient')
    review.consume(row)
    group=review.finish()['namespaces']['paper_test']['entry_review']['selected_entry_window']
    assert group['signal_present']==1 and group['selection_blockers']=={}
    assert group['allocation_reasons']=={'risk_budget_insufficient':1}


def test_unknown_rvol_and_low_numeric_rvol_remain_distinguishable():
    review=reviewer()
    row=batch();body=row['data']['data']
    body['quality_rows'][0]['strategy_rvol']=None
    body['quality_rows'][0]['rvol'].update(calculated_value=None,current_missing_minutes=2,
        current_missing_interior_minutes=1,current_missing_tail_minutes=1,
        reasons=['current_session_coverage_incomplete','split_volume_comparability_unknown'])
    body['decisions'][0].update(signal=None,eligible=False,blockers=['relative_volume_unknown_or_low'])
    review.consume(row)
    second=copy.deepcopy(row)
    second['data']['data']['quality_rows'][0]['strategy_rvol']=0.4
    review.consume(second)
    group=review.finish()['namespaces']['paper_test']['entry_review']['selected_entry_window']
    assert group['strategy_rvol_present']==1 and group['evaluations']==2
    samples=group['by_symbol']['AAPL']['samples']
    assert samples[0]['strategy_rvol'] is None and samples[1]['strategy_rvol']==0.4
    assert samples[0]['current_missing_interior_minutes']==1


def test_legacy_missing_detail_is_reported_and_does_not_invent_empty_decisions():
    review=reviewer();row=batch();body=row['data']['data']
    body.pop('decisions');body.pop('quality_rows')
    review.consume(row)
    result=review.finish()['namespaces']['paper_test']
    assert result['research']['decision_count']==1
    assert result['entry_review']['batches_missing_decision_details']==1
    assert result['entry_review']['all_in_window']['evaluations']==0


def test_warning_source_and_strategy_wait_are_sanitized_and_samples_bounded():
    review=reviewer();row=batch();body=row['data']['data']
    body['decisions'][0]['indicators']['strategy_diagnostics']=['strategy_rebound_volume_missing','SECRET',{}]
    for _ in range(20):review.consume(row)
    for source in ('toss_candles','https://SECRET'):
        review.consume({'namespace':'paper_test','kind':'system_logs','data':{
            'timestamp':'2026-11-18T16:00Z','level':'WARNING','source':source,'symbol':'AAPL',
            'error_type':'TimeoutError','message':'SECRET'}})
    result=review.finish()['namespaces']['paper_test']
    assert result['warnings_by_source']=={'toss_candles:TimeoutError':1,'UNKNOWN:TimeoutError':1}
    group=result['entry_review']['selected_entry_window']
    assert group['strategy_wait']=={'OTHER':20,'strategy_rebound_volume_missing':20}
    assert len(group['by_symbol']['AAPL']['samples'])==6
    assert 'SECRET' not in json.dumps(result)


def test_unicode_transport_and_projection_keep_required_entry_fields():
    row={'message':'한글과 café 🚀'}
    assert decode_row(json.dumps(row,ensure_ascii=False).encode().hex().encode())==row
    sql=research_projection()
    assert "payload->'$.data.decisions'" in sql and "payload->'$.data.quality_rows'" in sql
    assert "payload->'$.data.portfolio_selection'" in sql
    assert "payload->'$.data.events'" not in sql and "payload->'$.data.news_contexts'" not in sql
