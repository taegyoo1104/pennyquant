"""Deterministic acceptance demonstration, using the same Paper engine as live inputs."""
import json
from datetime import timedelta
from pathlib import Path

from .database.repository import Repository
from .session_engine import event_time
from .market.universe import UniverseStore
from .portfolio.selection import VIEW
from .session_demo import synthetic_tape
from .session_engine import SessionEngine


class FixtureNews:
    """An explicit simulated LLM result, not a hidden API call or real news claim."""
    def __init__(self, available):
        self.available=available

    def risk_context(self, now, symbol):
        signal={'status':'UNKNOWN','value':None}
        if now>=self.available:
            signal={'status':'VERIFIED','symbol':symbol,'value':.9,
                'policy_version':'company-rank-v1','macro_score_applied':False,
                'observed_at':self.available.isoformat(),
                'valid_until':(self.available+timedelta(hours=24)).isoformat(),
                'analysis_ids':['scenario-analysis-not-real-llm'],
                'llm_call_ids':['fixture-no-api-call'], 'synthetic':True}
        return {'status':'FIXTURE','block_entries':False,'major_adverse':False,'blackout':False,
                'candidate_score_signal':signal,'reasons':['가상 뉴스 분석 결과 주입 · 실제 LLM 호출 없음']}


class FailedNews:
    def risk_context(self, now, symbol):
        raise TimeoutError('synthetic LLM failure')


def demonstrate(out):
    out=Path(out)
    if out.exists():
        raise ValueError('새 출력 폴더를 지정하세요. 기존 실행 자료는 덮어쓰지 않습니다.')
    out.mkdir(parents=True)
    rows,opened=synthetic_tape()
    # RVOL is not a required input to the simplified strategy.
    rows=[r for r in rows if r['kind']!='rvol']
    entry=opened+timedelta(minutes=75,seconds=1)
    universe=UniverseStore(out/'universe.json')
    universe.refresh(['AAPL'],source='SYNTHETIC_FIXTURE',observed_at=opened-timedelta(days=1))
    report={'synthetic':True,'live_orders_enabled':False,'llm_mode':'FIXTURE',
            'llm_api_calls':0,'strategy':'pullback_simple','scenarios':[]}
    for case in ('entry_exit','restart','stale_quote','no_rebound','llm_unavailable','llm_failure'):
        repo=Repository(out/(case+'.db'),backend='sqlite')
        provider=None if case=='llm_unavailable' else FailedNews() if case=='llm_failure' else FixtureNews(entry)
        def engine(restore=False):
            return SessionEngine(repo,universe,strategy='pullback_simple',synthetic=True,
                use_market_filter=False,risk_provider=provider,restore=restore,
                candidate_settings={'enabled':True,'use_sector_score':False})
        current=engine()
        scenario=json.loads(json.dumps(rows))
        if case=='stale_quote':
            for r in scenario:
                if r['kind']=='quote' and r['data']['symbol']=='AAPL':
                    from .models import utc
                    r['data']['timestamp']=(utc(r['data']['timestamp'])-timedelta(minutes=2)).isoformat()
        if case=='no_rebound':
            for r in scenario:
                if r['kind']=='bar' and r['data']['symbol']=='AAPL' and event_time(r)>opened+timedelta(minutes=70):
                    r['data'].update(open=100.99,high=101.15,low=100.85,close=101.0)
        try:
            restarted=False
            decisions=[]
            last_frame=None
            for stamp in sorted({event_time(r) for r in scenario}):
                batch=[r for r in scenario if event_time(r)==stamp]
                current.apply_batch(batch,stamp)
                frame=repo.get_control(VIEW,{})
                if frame.get('id') and frame['id'] != last_frame:
                    decisions.extend(frame['rows'])
                    last_frame=frame['id']
                if case=='restart' and stamp==entry and not restarted:
                    current=engine(restore=True);restarted=True
            sides=[f.side for f in current.broker.fills]
            expect=[] if case in {'stale_quote','no_rebound'} else ['buy','sell']
            candidates=decisions
            checks={'expected_fills':sides==expect,'flat_at_end':not current.broker.positions,
                    'single_entry':sum(o.intent.side=='buy' for o in current.broker.orders.values())==(1 if expect else 0),
                    'paper_engine':repo.load_state().get('environment')=='PAPER_LOCAL'
                    and repo.get_control('sessionquant_context',{}).get('live_orders_enabled') is False}
            report['scenarios'].append({'case':case,'passed':all(checks.values()),'checks':checks,
                'fills':sides,'orders':repo.all('orders'),
                'signal_count':len(repo.all('signals')),
                'decisions':candidates,'last_selection':repo.get_control(VIEW,{})})
        finally:
            repo.close()
    report['passed']=all(r['passed'] for r in report['scenarios'])
    (out/'acceptance.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    write_html(out/'report.html',report)
    return {'report':str(out/'report.html'),'output':str(out/'acceptance.json'),'passed':report['passed'],
            'scenarios':[{k:r[k] for k in ('case','passed','fills')} for r in report['scenarios']],
            'synthetic':True,'llm_mode':'FIXTURE','llm_api_calls':0,'live_orders_enabled':False}


def write_html(path, report):
    from html import escape
    labels={'entry_exit':'조건 충족 → 매수 → 시간 청산', 'restart':'주문 후 재시작 → 복구 → 청산',
            'stale_quote':'오래된 호가 → 주문 차단', 'no_rebound':'반등 없음 → 신호 없음',
            'llm_unavailable':'LLM 미확인 → 가점 없이 전략 판단',
            'llm_failure':'LLM 오류 → 오류 기록·가격 전략 계속'}
    cards=[]
    for case in report['scenarios']:
        rows=[]
        for d in case['decisions']:
            checks=(d.get('indicators',{}).get('strategy_pattern') or {}).get('checks',[])
            news=d.get('news_score_signal') or {}
            cells=[d.get('evaluated_at',d.get('timestamp','')),d.get('symbol',''),
                ' / '.join(c['label']+(': 통과' if c['passed'] else ': 미충족') for c in checks) or '자료 준비',
                news.get('status','UNKNOWN'),
                str(d.get('indicators',{}).get('score_components',{}).get('news',0)*10),
                d.get('portfolio_status','판단 대기'),
                ', '.join(d.get('blockers',[])),d.get('order_intent_id') or '—']
            rows.append('<tr>'+''.join('<td>'+escape(str(c))+'</td>' for c in cells)+'</tr>')
        cards.append('<section><h2>'+escape(labels[case['case']])+' · '+('통과' if case['passed'] else '실패')+
            '</h2><p>체결: '+escape(' → '.join(case['fills']) or '없음')+
            '</p><details><summary>조건·LLM 가점·주문 근거 보기</summary><div class="scroll"><table><tr>'+
            ''.join('<th>'+h+'</th>' for h in ['시각 UTC','종목','전략 조건','가상 LLM 결과','뉴스 가점','주문 판단','차단 사유','주문 의도'])+
            '</tr>'+''.join(rows)+'</table></div></details></section>')
    path.write_text('<!doctype html><html lang="ko"><meta charset="utf-8"><meta name="viewport" content="width=device-width">'
        '<title>SessionQuant 기획 구현 검증</title><style>body{font:16px sans-serif;max-width:1100px;margin:32px auto;padding:16px;background:#101827;color:#ecf2ff}'
        'section{background:#1b293d;padding:20px;margin:16px 0;border-radius:12px}td,th{padding:10px;border-bottom:1px solid #506070;text-align:left}'
        '.scroll{overflow:auto}summary{cursor:pointer}p{line-height:1.7}</style><h1>기획대로 동작했는가?</h1>'
        '<p>합성 시세 · 가상 LLM 결과 주입 · 실제 LLM 호출 0회 · 실제 주문 없음. 실시간 시장 성과가 아닙니다.</p>'
        '<p>상승 추세 → 1~4개 조정봉 → 확정봉 반등. 뉴스 분석은 우선순위에만 반영합니다.</p>'+
        ''.join(cards)+'</html>',encoding='utf-8')
