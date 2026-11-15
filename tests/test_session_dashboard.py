"""SessionQuant 화면의 미확인 상태, 금액 표시와 접속 오류 처리를 검증한다."""
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from streamlit.testing.v1 import AppTest

from sessionquant.dashboard.session_app import DashboardData, build_view, decimal_value, load_database

APP = str(Path(__file__).resolve().parents[1] / "sessionquant/dashboard/session_app.py")


def test_empty_dashboard_does_not_invent_account(tmp_path, monkeypatch):
    monkeypatch.setenv("SQ_DB", str(tmp_path / "absent.db"))
    monkeypatch.setenv("SQ_DB_BACKEND", "sqlite")
    monkeypatch.setenv("PQ_DB_BACKEND", "sqlite")
    app = AppTest.from_file(APP, default_timeout=30).run()
    assert not app.exception
    assert app.title[0].value == "SessionQuant"
    assert app.metric[0].value == "—"
    assert len(app.metric)==4
    assert app.radio(key='dashboard_page').options == ['대시보드','시장·종목','전략·백테스트','매매 내역','뉴스·LLM','시스템·설정']
    assert not any("실거래 활성화" == button.label for button in app.button)
    assert not (tmp_path / "absent.db").exists()


def test_dashboard_decimal_positions_and_unknown_timestamps():
    data = DashboardData(state={"broker": {"account": {"cash": "80.00", "equity": "100.00", "reserved_cash": "0"},
                                                   "positions": {"AAPL": {"qty": "0.1", "avg_price": "200", "entry_fee": "0.01", "stop_price": "196"}}}})
    view = build_view(data, datetime(2026, 11, 13, 17, tzinfo=timezone.utc))
    assert view["positions"][0]["보유 수량"] == "0.1"
    assert view["positions"][0]["비중"] == "—"
    assert view["positions"][0]["평가 손익"] == "—"
    assert view["lag"] is None
    assert view["regime"] == "UNKNOWN"
    assert decimal_value("NaN") is None
    assert decimal_value("Infinity") is None
    assert decimal_value(True) is None
    assert decimal_value("0.000001") == Decimal("0.000001")


def test_mysql_loader_does_not_require_local_file_and_sanitizes_errors(tmp_path, monkeypatch):
    import sessionquant.database.config as config
    import sessionquant.database.repository as module

    called = []
    monkeypatch.setattr(config, "database_backend", lambda: "mysql")

    def broken_repository(path):
        called.append(path)
        raise RuntimeError("password=PRIVATE_TEST_SECRET host=some-private-server")

    monkeypatch.setattr(module, "Repository", broken_repository)
    data = load_database(tmp_path / "not-a-local-db.db")
    assert called
    assert data.error
    assert "PRIVATE_TEST_SECRET" not in data.error
    assert "some-private-server" not in data.error
    assert not data.connected


def test_loader_bounds_every_journal_and_closes(tmp_path, monkeypatch):
    import sessionquant.database.config as config
    import sessionquant.database.repository as module

    calls = []

    class FakeRepository:
        def __init__(self, path):
            pass
        def load_state(self):
            return {"account": {"equity": "100"}}
        def get_control(self, name, default=None):
            return {"sessionquant_context":{"environment":"PAPER_LOCAL"}}.get(name,default)
        def recent(self, name, limit):
            calls.append((name, limit))
            return []
        def close(self):
            calls.append("closed")

    monkeypatch.setattr(config, "database_backend", lambda: "mysql")
    monkeypatch.setattr(module, "Repository", FakeRepository)
    data = load_database(tmp_path / "unused.db")
    assert data.connected
    assert calls[-1] == "closed"
    assert ('fred_reference_observations', 30) in calls
    expected_limits = {'fred_reference_observations': 30, 'llm_calls': 500}
    assert ('llm_calls', 500) in calls
    assert all(limit == expected_limits.get(name, 2000) for name, limit in calls[:-1])


def test_dashboard_reads_actual_engine_flat_state_and_context():
    data = DashboardData(state={"environment": "PAPER_LOCAL", "broker": {"cash": "80", "equity": "100",
        "positions": {"AAPL": {"qty": "0.1", "avg_price": "200", "initial_amount": "20", "adds": 1}}}},
        context={"updated_at": "2026-11-13T17:00:00+00:00", "synthetic": True,
                 "market_regime": {"state": "BULLISH", "components": {"SPY": {"vwap": 500}}}})
    view = build_view(data, datetime(2026, 11, 13, 17, tzinfo=timezone.utc))
    assert view["account"]["equity"] == "100"
    assert view["positions"][0]["최초 진입 금액"] == "$20.00"
    assert view["positions"][0]["추가매수 횟수"] == 1
    assert view["regime"] == "BULLISH"
    assert view["lag"] == 0
    assert view["synthetic"]


def test_existing_main_tabs_show_actual_holdings_and_recorded_openai_cost(tmp_path, monkeypatch):
    from sessionquant.database.repository import Repository
    path = tmp_path/'dashboard.db'
    repo = Repository(path, backend='sqlite')
    repo.save_state({'cash': '70', 'equity': '70'})
    repo.set_control('actual_account_snapshot', {
        'received_at': datetime.now(timezone.utc).isoformat(),
        'items': [{'symbol': 'AAPL', 'name': 'Apple', 'currency': 'USD', 'quantity': '0.123456',
            'averagePurchasePrice': '180', 'lastPrice': '190', 'marketValue': {'amount': '23.45664'},
            'profitLoss': {'amount': '1.23456'}}],
        'summary': {'marketValue': {'amount': {'usd': '1200.50', 'krw': '350000'}}},
        'buying_power': {'USD': '321.45', 'KRW': None}, 'issues': []})
    repo.set_control('session_llm_budget_v1', {datetime.now(timezone.utc).strftime('%Y-%m'): {'spent': '0.20001'}})
    repo.set_control('session_llm_limits_v1', {'monthly_hard_budget_usd': '3'})
    repo.set_control('openai_funding_note', {'total_usd': '10'})
    repo.set_control('llm_provider_health', {'code': 'NO_CREDIT', 'message': '잔액 부족: 차트·청산 관리 계속'})
    repo.close()
    monkeypatch.setenv('SQ_DB_BACKEND', 'sqlite')
    monkeypatch.setenv('SQ_DB', str(path))
    monkeypatch.setattr('sys.argv', ['session_app.py', '--db', str(path)])
    app = AppTest.from_file(APP, default_timeout=30).run()
    assert not app.exception and not app.error
    assert any('실제 계좌' in row.value for row in app.caption)
    assert [metric.value for metric in app.metric][:3] == ['$1,200.50', '$321.45', '—']
    assert app.metric[3].label == '이번 달 AI 비용 / 월 한도액'
    assert app.metric[3].value == '$0.20 / $3.00'
    assert any(row.value == '총 충전액(수동 입력): $10.00' for row in app.caption)
    assert app.radio(key='dashboard_page').options == ['대시보드','시장·종목','전략·백테스트','매매 내역','뉴스·LLM','시스템·설정']
    app.radio(key='dashboard_page').set_value('뉴스·LLM').run()
    assert any(metric.label == '프로그램 누적 비용 · 추정' and metric.value == '$0.20' for metric in app.metric)
    assert any(metric.label == 'OpenAI 충전 누계 · 직접 기록' and metric.value == '$10.00' for metric in app.metric)
    assert any('잔액 부족' in row.value for row in app.warning)
    app.radio(key='dashboard_page').set_value('매매 내역').run()
    assert any('0.123456' in str(frame.value) for frame in app.dataframe)
    app.radio(key='dashboard_page').set_value('뉴스·LLM').run()
    app.text_input(key='openai-deposit').set_value('0')
    app.button(key='save-openai-deposit').click().run()
    assert not app.exception
    app.radio(key='dashboard_page').set_value('대시보드').run()
    assert app.metric[3].value == '$0.20 / $3.00'
    assert any(row.value == '총 충전액(수동 입력): $0.00' for row in app.caption)
    repo = Repository(path, backend='sqlite')
    assert repo.get_control('openai_funding_note')['total_usd'] == '0'
    assert repo.get_control('llm_provider_health')['code'] == 'NO_CREDIT'
    assert repo.all('orders') == []
    repo.close()


def test_main_screen_with_closed_market_null_schedule(tmp_path, monkeypatch):
    """휴장일의 실제 청산 관리자 응답을 읽어도 메인 화면 전체가 중단되지 않는다."""
    from sessionquant.database.repository import Repository
    from sessionquant.portfolio.session import SessionExitManager
    from sessionquant.portfolio.broker import PaperBroker
    # 토요일에는 schedule 키가 존재하되 값이 None이다.
    status = SessionExitManager(PaperBroker()).tick(datetime(2026, 11, 14, 14, 32, tzinfo=timezone.utc), {})
    assert status['schedule'] is None
    path = tmp_path/'closed-market.db'
    repo = Repository(path, backend='sqlite')
    repo.record('session_state', {'id': 'closed-market', **status})
    repo.close()
    monkeypatch.setenv('SQ_DB_BACKEND', 'sqlite')
    monkeypatch.setattr('sys.argv', ['session_app.py', '--db', str(path)])
    app = AppTest.from_file(APP, default_timeout=30).run()
    assert not app.exception
    app.radio(key='dashboard_page').set_value('매매 내역').run()
    assert any('청산 시작: — · 미청산 경고: —' in row.value for row in app.caption)


def test_stage06_main_news_scopes_verified_current_company_and_keeps_details():
    from copy import deepcopy
    from datetime import timedelta
    from sessionquant.dashboard.news_panel import current_company_news, provider_rows, calendar_rows
    from sessionquant.models import jsonable, NewsEvent
    now = datetime(2026, 11, 13, 16, tzinfo=timezone.utc)
    data = DashboardData(state={'broker': {'positions': {'AAPL': {'qty': '1', 'avg_price': '100'}}}},
        watchlist_state={'status': 'VALID', 'updated_at': now.isoformat(), 'items': [
            {'ticker': 'MSFT', 'focused': True, 'focus_valid_until': (now+timedelta(minutes=5)).isoformat()}]})
    def news(identity, ticker='AAPL', **changes):
        event = jsonable(NewsEvent(identity, (ticker,), 'Results', now-timedelta(minutes=1), now,
            'Company IR', 'https://issuer.test/news', 'Full verified company release.', True, False))
        return {'event': event | changes, 'ticker': ticker, 'source_kind': 'rss', 'primary_verified': True,
                'analyzed_at': now.isoformat()}
    rows = [news('rss:held'), news('rss:focus', 'MSFT'), news('rss:unrelated', 'AMZN'),
            news('rss:stale', published_at=(now-timedelta(hours=25)).isoformat()),
            news('rss:unverified', verified=False), news('rss:future', published_at=(now+timedelta(minutes=1)).isoformat()),
            news('rss:empty', body=''), news('rss:error') | {'error': 'llm_budget_limit'}]
    before = deepcopy(rows)
    assert {r['event']['id'] for r in current_company_news(rows, data, build_view(data, now), now)} == {'rss:held', 'rss:focus'}
    assert rows == before
    assert provider_rows({'schedule': {'gdelt': {'status': 'PROVIDER_RATE_LIMITED', 'http_429': 1}}})[0]['429'] == 1
    event = {'id': 'a', 'logical_id': 'jobs', 'name': 'Jobs', 'verified': True, 'known_at': now.isoformat(),
             'scheduled_at': (now+timedelta(hours=1)).isoformat(), 'source_url': 'https://www.bls.gov/', 'revision': 0}
    cancelled = event | {'id': 'b', 'revision': 1, 'status': 'CANCELLED', 'known_at': (now+timedelta(minutes=1)).isoformat()}
    assert calendar_rows([event, cancelled], now)[0]['상태'] == 'CONFIRMED'
    assert calendar_rows([event, cancelled], now+timedelta(minutes=1))[0]['상태'] == 'CANCELLED'


def test_stage06_dashboard_renders_populated_provider_and_enrichment_details(tmp_path, monkeypatch):
    from datetime import timedelta
    from sessionquant.database.repository import Repository
    now = datetime.now(timezone.utc)
    path = tmp_path/'news-dashboard.db'
    repo = Repository(path, backend='sqlite')
    repo.set_control('session_news_status', {'updated_at': now.isoformat(),
        'issues': [{'provider': 'bls', 'code': 'http_403'}]})
    repo.set_control('news_provider_schedule_v1', {'gdelt': {'status': 'PROVIDER_RATE_LIMITED', 'calls': 1,
        'http_429': 1, 'cooldown_until': (now+timedelta(hours=1)).isoformat()}})
    repo.record('news_ingestions', {'id': 'old', 'provider': 'alpaca', 'published_at': '2015-01-01T00:00:00Z',
        'skip_reason': 'stale_article', 'eligible_for_llm': False})
    repo.record('news_enrichments', {'id': 'one', 'publisher_domain': 'publisher.test', 'status': 'RIGHTS_UNKNOWN',
        'body': None, 'body_char_count': 0, 'eligible_for_llm': False})
    repo.close()
    monkeypatch.setenv('SQ_DB_BACKEND', 'sqlite')
    monkeypatch.setenv('SQ_DB', str(path))
    monkeypatch.setattr('sys.argv', ['session_app.py', '--db', str(path)])
    app = AppTest.from_file(APP, default_timeout=30).run()
    assert not app.exception and not app.error
    assert len(app.radio(key='dashboard_page').options)==6
    app.radio(key='dashboard_page').set_value('뉴스·LLM').run()
    assert any('뉴스 공급자' in item.label for item in app.expander)
    values = ' '.join(frame.value.to_string() for frame in app.dataframe)
    assert '공급자 요청 제한' in values and '이용 권한 미확인' in values and '오래된 기사' in values
    assert 'PROVIDER_RATE_LIMITED' not in values and 'stale_article' not in values
