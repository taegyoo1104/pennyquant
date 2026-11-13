from datetime import datetime, timezone
from io import StringIO
import json
from types import SimpleNamespace

import httpx
import pytest

from pennyquant.capture import EventWriter, capture_file, collect
from pennyquant.collectors.common import DataQualityError
from pennyquant.events import read_events
from pennyquant.models import NewsEvent, Quote

NOW = datetime(2026, 11, 13, 15, 0, tzinfo=timezone.utc)


def args(tmp_path, **kwargs):
    return SimpleNamespace(**{"output": tmp_path / "capture.jsonl", "symbols": "TEST", "provider": "toss",
                             "cik": None, "url": None, "poll": 0, "stream": False,
                             "realtime_verified": False, **kwargs})


def quote(received_at=NOW):
    return Quote("TEST", received_at, received_at, 2.0, 2.005, 100, 100, tradable=False, exchange="UNKNOWN")


def test_capture_does_not_merge_recordings_and_locks_writer(tmp_path):
    path = tmp_path / "capture.jsonl"
    path.touch()  # a following consumer may have created an empty file
    with capture_file(path) as stream:
        with pytest.raises(ValueError, match="already writing"):
            with capture_file(path):
                pass
        EventWriter(stream).write("quote", quote())
    with pytest.raises(ValueError, match="already contains data"):
        with capture_file(path):
            pass
    assert len(read_events(path)) == 1


def test_capture_receipt_clock_regression_stops_before_bad_line():
    stream = StringIO()
    writer = EventWriter(stream)
    writer.write("quote", quote())
    with pytest.raises(DataQualityError, match="backwards"):
        writer.write("quote", quote(NOW.replace(minute=59, hour=14)))
    assert len(stream.getvalue().splitlines()) == 1


async def test_toss_capture_writes_monitor_only_normalized_quote(tmp_path, monkeypatch):
    class FakeToss:
        async def get_quote(self, symbol):
            assert symbol == "TEST"
            return quote()
        async def aclose(self):
            pass
    monkeypatch.setattr("pennyquant.capture.TossMarketClient", FakeToss)
    options = args(tmp_path)
    await collect(options)
    captured = read_events(options.output)
    assert len(captured) == 1 and captured[0].kind == "quote"
    assert not captured[0].data.tradable and captured[0].data.exchange == "UNKNOWN"
    meta = json.loads(options.output.with_suffix(".meta.json").read_text())
    assert meta["synthetic"] is False and meta["complete_trading_input"] is False


async def test_capture_http_error_does_not_expose_provider_url_or_auth(tmp_path, monkeypatch):
    class FakeToss:
        async def get_quote(self, symbol):
            request = httpx.Request("GET", "https://feed.test/?token=SECRET", headers={"Authorization": "SECRET"})
            response = httpx.Response(403, request=request)
            raise httpx.HTTPStatusError("SECRET", request=request, response=response)
        async def aclose(self):
            pass
    monkeypatch.setattr("pennyquant.capture.TossMarketClient", FakeToss)
    with pytest.raises(RuntimeError) as exc:
        await collect(args(tmp_path))
    assert "403" in str(exc.value) and "SECRET" not in str(exc.value) and "feed.test" not in str(exc.value)


async def test_rss_capture_does_not_attach_cli_symbol_to_broad_feed(tmp_path, monkeypatch):
    class FakeRSS:
        async def fetch(self, url, **kwargs):
            assert kwargs["symbols"] == () and kwargs["verified"] is False
            return [NewsEvent("rss:one", (), "A company story", NOW, NOW, "RSS", real_time=False, verified=False)]
        async def aclose(self):
            pass
    monkeypatch.setattr("pennyquant.capture.RSSCollector", FakeRSS)
    options = args(tmp_path, provider="rss", url="https://wire.test/feed")
    await collect(options)
    assert read_events(options.output)[0].data.symbols == ()


async def test_poll_stream_and_entitlement_validation(tmp_path):
    with pytest.raises(ValueError, match="at least 1"):
        await collect(args(tmp_path, poll=0.1))
    with pytest.raises(ValueError, match="entitlement"):
        await collect(args(tmp_path, provider="alpaca", stream=True))
    with pytest.raises(ValueError, match="only for toss or alpaca"):
        await collect(args(tmp_path, provider="rss", stream=True))
