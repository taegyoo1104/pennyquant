import copy
import json
import io

import pytest

from scripts.review_paper_backup import Review, RESEARCH_FIELDS, consume_stream, decode_columns, read_query, stamp


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


def encode_cells(row):
    head=[b'42',row['namespace'].encode().hex().encode(),b'NULL']
    if row['kind']=='research_events':
        values=[]
        for key in RESEARCH_FIELDS:
            value=row['data']
            for part in key.split('.'):value=value.get(part) if isinstance(value,dict) else None
            values.append(value)
    else:values=[row['data']]
    return b'\t'.join(head+[json.dumps(v,ensure_ascii=False).encode().hex().encode() for v in values])+b'\n'


def test_unicode_transport_and_projection_keep_required_entry_fields():
    row=batch()
    row['data']['data']['decisions'][0]['indicators']['message']='한글과 café 🚀\n\t\\"' * 100000
    decoded=decode_columns(encode_cells(row),'research_events')
    assert decoded['data']['kind']==row['data']['kind']
    assert decoded['data']['data']['decisions']==row['data']['data']['decisions']
    assert decoded['data']['data']['decision_count']==1
    sql=read_query('research_events')
    assert "payload->'$.data.decisions'" in sql and "payload->'$.data.quality_rows'" in sql
    assert "payload->'$.data.portfolio_selection'" in sql
    assert "payload->'$.data.events'" not in sql and "payload->'$.data.news_contexts'" not in sql
    assert 'JSON_OBJECT' not in sql


def test_failed_json_row_is_counted_with_safe_location_and_next_row_is_read():
    review=reviewer();transport={'rows_failed':0,'samples':[],'tables':{}}
    bad=encode_cells(batch()).split(b'\t')
    bad[8]=b'{"SECRET":'.hex().encode()  # decisions column, deliberately incomplete
    stream=io.BytesIO(b'\t'.join(bad)+encode_cells(batch()))
    consume_stream(stream,'research_events',review,transport)
    assert transport['rows_failed']==1
    assert transport['tables']['research_events']=={'rows_seen':2,'rows_decoded':1,'rows_failed':1}
    assert transport['samples'][0]['field']=='data.decisions'
    assert transport['samples'][0]['seq']==42
    assert transport['samples'][0]['error_type']=='JSONDecodeError'
    assert review.finish()['namespaces']['paper_test']['entry_review']['all_in_window']['evaluations']==1
    assert 'SECRET' not in json.dumps(transport)


def test_bad_column_count_and_invalid_hex_are_bounded_and_never_exported():
    transport={'rows_failed':0,'samples':[],'tables':{}}
    line=b'1\tSECRET\tNULL\t7B7D\n'
    consume_stream(io.BytesIO(line*25+b'SECRET\n'),'system_logs',reviewer(),transport)
    assert transport['rows_failed']==26 and len(transport['samples'])==20
    assert 'SECRET' not in json.dumps(transport)
    with pytest.raises(ValueError):read_query('SECRET; DROP TABLE any')


def test_partial_report_is_saved_but_cli_exits_nonzero(tmp_path,monkeypatch,capsys):
    from scripts import review_paper_backup as module
    backup=tmp_path/'backup.tar.gz';backup.write_bytes(b'fixture')
    output=tmp_path/'result.json'
    result={'report_status':'PARTIAL','transport':{'rows_failed':1},'namespaces':{}}
    monkeypatch.setattr(module,'review_backup',lambda *args:result)
    monkeypatch.setattr('sys.argv',['review','--backup',str(backup),'--output',str(output),
        '--since','2026-11-18T13:00Z','--until','2026-11-18T21:20Z'])
    with pytest.raises(SystemExit) as exc:module.main()
    assert exc.value.code==2 and json.loads(output.read_text())==result
    assert '일부 기록 읽기 실패' in capsys.readouterr().out
