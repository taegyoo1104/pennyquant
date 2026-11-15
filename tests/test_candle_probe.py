from datetime import datetime, timedelta, timezone

from sessionquant.candle_probe import completed_sessions, probe_session, run
from sessionquant.models import Bar


def test_calendar_weekend_and_early_close():
    rows = completed_sessions(datetime(2026, 11, 15, tzinfo=timezone.utc), 20)
    assert len(rows) == 20 and rows[-1][0] == '2026-11-13'
    rows = completed_sessions(datetime(2026, 11, 28, tzinfo=timezone.utc), 1)
    assert (rows[0][2] - rows[0][1]).total_seconds() == 210 * 60


class FakeClient:
    def __init__(self, pages):
        self.pages = iter(pages)

    async def candles(self, symbol, *, count, before):
        assert symbol == 'AAPL' and count == 200
        return next(self.pages)


def sample():
    opened = datetime(2026, 11, 13, 14, 30, tzinfo=timezone.utc)
    bars = [Bar('AAPL', opened + timedelta(minutes=i), opened + timedelta(hours=8),
                100, 100, 100, 100, i) for i in range(1, 391)]
    return opened, bars


async def test_page_boundary_duplicate_does_not_inflate_coverage():
    opened, bars = sample()
    client = FakeClient([(bars[190:], bars[190].timestamp.isoformat()), (bars[:191], None)])
    row = await probe_session(client, 'AAPL', '2026-11-13', opened, bars[-1].timestamp)
    assert row['complete'] and row['received_minutes'] == 390
    assert row['duplicate_rows'] == 1 and row['conflicting_rows'] == 0
    assert row['volume_sum'] == sum(range(1, 391))


async def test_missing_and_conflicting_bars_fail_coverage():
    opened, bars = sample()
    different = Bar('AAPL', bars[190].timestamp, bars[190].received_at, 100, 100, 100, 100, 999)
    client = FakeClient([(bars[190:], bars[190].timestamp.isoformat()),
                         ([*bars[:50], *bars[51:190], different], None)])
    row = await probe_session(client, 'AAPL', '2026-11-13', opened, bars[-1].timestamp)
    assert not row['complete'] and row['missing_minutes'] == 1 and row['conflicting_rows'] == 1


async def test_stuck_cursor_is_bounded():
    opened, bars = sample()
    client = FakeClient([(bars[-10:], bars[-1].timestamp.isoformat())])
    row = await probe_session(client, 'AAPL', '2026-11-13', opened, bars[-1].timestamp)
    assert not row['complete'] and row['stop_reason'] == 'NON_PROGRESSING_CURSOR'


async def test_missing_credentials_stops_before_network(monkeypatch, tmp_path):
    from argparse import Namespace
    monkeypatch.setenv('SQ_ENV_FILE', str(tmp_path / 'absent'))
    monkeypatch.delenv('TOSS_CLIENT_ID', raising=False)
    monkeypatch.delenv('TOSS_CLIENT_SECRET', raising=False)
    assert await run(Namespace(symbol='AAPL', sessions=20)) == 2


async def test_api_error_saves_sanitized_partial_report(monkeypatch, tmp_path, capsys):
    import json
    from argparse import Namespace
    import httpx
    from sessionquant import candle_probe

    class FailingClient:
        closed = False

        def __init__(self, **kwargs):
            pass

        async def candles(self, *args, **kwargs):
            request = httpx.Request('GET', 'https://example.invalid', headers={'Authorization': 'secret-marker'})
            raise httpx.HTTPStatusError('secret-marker', request=request, response=httpx.Response(403, request=request))

        async def aclose(self):
            FailingClient.closed = True

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv('SQ_ENV_FILE', str(tmp_path / 'absent'))
    monkeypatch.setenv('TOSS_CLIENT_ID', 'fake')
    monkeypatch.setenv('TOSS_CLIENT_SECRET', 'secret-marker')
    monkeypatch.setattr(candle_probe, 'TossMarketClient', FailingClient)
    assert await run(Namespace(symbol='AAPL', sessions=20)) == 1
    text = next((tmp_path / 'data').glob('*.json')).read_text(encoding='utf-8')
    report = json.loads(text)
    assert report['error'] == {'type': 'HTTPStatusError', 'http_status': 403}
    assert not report['history_complete'] and not report['rvol_comparability_verified']
    assert FailingClient.closed and 'secret-marker' not in text + capsys.readouterr().out
