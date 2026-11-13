"""Read-only provider capture to the receipt-ordered JSONL interchange."""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
import json
import math
import os
from pathlib import Path
import sys

import httpx

from .collectors.alpaca_news import AlpacaNewsClient
from .collectors.common import CapabilityUnavailable, DataQualityError, now_utc
from .collectors.rss_collector import RSSCollector
from .collectors.sec_edgar import SecEdgarClient
from .collectors.toss_market import TossMarketClient, symbols_parameter
from .collectors.toss_websocket import TossWebSocket, quote_from_frame
from .events import Event
from .models import jsonable
from .locking import exclusive_lock


@contextmanager
def capture_file(path: Path):
    """Allow a follower's empty file, but reject previous data and other writers."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with exclusive_lock(str(path) + ".writer.lock", message="Another collector is already writing this output file",
                        error_type=ValueError):
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as stream:
            if os.fstat(stream.fileno()).st_size:
                raise ValueError("Capture output already contains data; choose a fresh --output")
            yield stream


class EventWriter:
    def __init__(self, stream):
        self.stream = stream
        self.last_time = None
        self.count = 0

    def write(self, kind: str, value) -> None:
        event = Event(kind, value)
        if self.last_time is not None and event.available_at < self.last_time:
            raise DataQualityError("Provider receipt clock moved backwards; capture stopped")
        self.stream.write(json.dumps(event.to_dict(), ensure_ascii=False, allow_nan=False) + "\n")
        self.stream.flush()
        self.last_time = event.available_at
        self.count += 1


def _metadata(path: Path, provider: str, *, streaming: bool):
    path.with_suffix(".meta.json").write_text(json.dumps({
        "synthetic": False, "source_label": f"{provider} read-only {'stream' if streaming else 'poll'} capture",
        "captured_at": now_utc().isoformat(),
        "complete_trading_input": False,
        "limitations": "Provider capture alone lacks verified scanner snapshots/RVOL and is for monitoring or data assembly.",
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


async def collect(args) -> None:
    """Collect one provider per file. No broker write operation is performed."""
    output = Path(args.output)
    symbols = [symbol.strip().upper() for symbol in args.symbols.split(",") if symbol.strip()]
    symbols_parameter(symbols)
    poll = float(args.poll)
    if poll < 0 or (poll > 0 and poll < 1) or not math.isfinite(poll):
        raise ValueError("--poll must be 0 (one read) or at least 1 second")
    streaming = bool(getattr(args, "stream", False))
    if streaming and args.provider not in {"toss", "alpaca"}:
        raise ValueError("--stream is available only for toss or alpaca")
    if args.provider == "sec" and not args.cik:
        raise ValueError("SEC capture requires --cik")
    if args.provider == "rss" and not args.url:
        raise ValueError("RSS capture requires --url")
    if streaming and args.provider == "alpaca" and not getattr(args, "realtime_verified", False):
        raise ValueError("Alpaca streaming requires verified account entitlement and --realtime-verified")
    if args.provider == "toss" and len(symbols) > 20:
        raise ValueError("Toss capture supports at most 20 candidate symbols")
    if args.provider == "sec":
        sidecar = output.with_suffix(".filings.jsonl")
        if sidecar.exists() and sidecar.stat().st_size:
            raise ValueError("SEC metadata sidecar already contains data; choose a fresh --output")

    with capture_file(output) as stream:
        _metadata(output, args.provider, streaming=streaming)
        writer = EventWriter(stream)
        if args.provider == "toss":
            client = TossMarketClient()
        elif args.provider == "alpaca":
            client = AlpacaNewsClient(realtime_verified=bool(getattr(args, "realtime_verified", False)))
        elif args.provider == "sec":
            client = SecEdgarClient()
        elif args.provider == "rss":
            client = RSSCollector()
        else:
            raise ValueError("unknown provider")

        print(f"Read-only {args.provider} capture -> {output}. No live orders.")
        if args.provider == "toss":
            print("Monitor-only quotes: tradable=false and exchange=UNKNOWN; scanner snapshots, RVOL and eligibility are not fabricated.")
        elif args.provider in {"sec", "rss"} or not streaming:
            print("Collected news is auxiliary data (real_time=false).")
        completed = False
        try:
            if streaming:
                if args.provider == "toss":
                    async for frame in TossWebSocket(client).stream(symbols):
                        quote = quote_from_frame(frame, received_at=frame["received_at"])
                        if quote is not None:
                            writer.write("quote", quote)
                else:
                    async for news in client.stream(symbols):
                        writer.write("news", news)
            else:
                seen = set()
                while True:
                    if args.provider == "toss":
                        for symbol in symbols:
                            writer.write("quote", await client.get_quote(symbol))
                    elif args.provider == "alpaca":
                        for news in await client.fetch(symbols):
                            writer.write("news", news)
                    elif args.provider == "sec":
                        filings = await client.submissions(args.cik)
                        new_filings = [item for item in filings if item.accession not in seen]
                        if new_filings:
                            # Preserve missing acceptance timestamps as null, never midnight.
                            sidecar = output.with_suffix(".filings.jsonl")
                            with sidecar.open("a", encoding="utf-8") as metadata_stream:
                                for item in new_filings:
                                    metadata_stream.write(json.dumps(jsonable(item), ensure_ascii=False, allow_nan=False) + "\n")
                                    seen.add(item.accession)
                                    if (news := item.as_news()) is not None:
                                        writer.write("news", news)
                    else:
                        # --symbols is intentionally NOT applied to a broad RSS feed.
                        for news in await client.fetch(args.url, source="RSS", symbols=(), verified=False):
                            if news.id not in seen:
                                seen.add(news.id)
                                writer.write("news", news)
                    if not poll:
                        break
                    await asyncio.sleep(poll)
            completed = True
        except (CapabilityUnavailable, DataQualityError):
            raise
        except httpx.HTTPStatusError as exc:
            # Never print request headers, auth bodies, feed query strings or response bodies.
            raise RuntimeError(f"{args.provider} request failed with HTTP {exc.response.status_code}; verify account access and provider status") from None
        except httpx.RequestError as exc:
            raise RuntimeError(f"{args.provider} connection failed ({type(exc).__name__}); capture stopped") from None
        except (ValueError, KeyError, TypeError) as exc:
            raise RuntimeError(f"{args.provider} response could not be normalized ({type(exc).__name__}); capture stopped") from None
        except Exception as exc:
            raise RuntimeError(f"{args.provider} collector stopped ({type(exc).__name__}); verify stream permissions and connection") from None
        finally:
            await client.aclose()
            if completed:
                print(f"Captured {writer.count} normalized events.", file=sys.stderr)
            else:
                print(f"Capture stopped after saving {writer.count} normalized events; collection did not complete.", file=sys.stderr)
