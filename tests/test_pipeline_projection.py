import json
import pytest
from sessionquant.database.repository import Repository
from sessionquant.dashboard.system_panel import pipeline_rows


def fixture_records():
    return [
        {'id':'a','kind':'watchlist_focus','known_at':'2026-11-25T06:00:00+00:00',
         'data_status':'VALID','items':[{'blob':'x'*10000} for _ in range(50)]},
        {'id':'b','kind':'portfolio_selection','evaluated_count':50,'final_candidate_count':3,
         'approved_count':0,'rows':[{'blob':'x'*10000} for _ in range(50)]},
        {'id':'c','kind':'risk_decision','approved':False,'reason':'daily_loss_limit'},
        {'id':'d','kind':'macro_reference_risk','effective_enabled':False,'block_entries':True},
        {'id':'e','kind':'legacy','status':{'nested':'x'*10000},'reason':'한'*10000},
    ]


def assert_projection(repo):
    originals=fixture_records()
    for row in originals:repo.record('pipeline_status',row)
    rows=repo.recent_pipeline_summary()
    assert [r['id'] for r in rows]==['a','b','c','d','e']
    assert rows[0]['items_count']==50 and 'items' not in rows[0]
    assert rows[1]['approved_count']==0 and 'rows' not in rows[1]
    assert rows[2]['approved'] is False
    assert rows[3]['effective_enabled'] is False and rows[3]['block_entries'] is True
    assert 'status' not in rows[4] and len(rows[4]['reason'])==512
    assert len(json.dumps(rows).encode())<5000
    assert pipeline_rows(rows[:4])==pipeline_rows(originals[:4])
    assert repo.all('pipeline_status')==originals
    assert [r['id'] for r in repo.recent_pipeline_summary(limit=2)]==['d','e']
    assert not repo.all('orders') and not repo.all('executions')


def test_sqlite_pipeline_projection_is_bounded_and_read_only(tmp_path):
    with_repo=Repository(tmp_path/'projection.db',backend='sqlite')
    try:assert_projection(with_repo)
    finally:with_repo.close()


@pytest.mark.parametrize('limit',[0,101,True,1.5,'10'])
def test_invalid_summary_limits(tmp_path,limit):
    repo=Repository(tmp_path/'limit.db',backend='sqlite')
    try:
        with pytest.raises(ValueError):repo.recent_pipeline_summary(limit)
    finally:repo.close()
