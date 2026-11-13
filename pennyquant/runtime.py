"""Receipt-ordered file replay/tailing with one engine writer per SQLite file."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
import hashlib
import json
import time

from .config.settings import Settings
from .database.repository import Repository
from .engine import EventEngine
from .events import parse_event, read_events
from .locking import exclusive_lock


@contextmanager
def engine_lease(db_path: str | Path):
    path = Path(str(db_path) + ".engine.lock")
    with exclusive_lock(path, message="another engine is already using this database"):
        yield


def replay(input_path: Path, db_path: Path, settings: Settings, strategy: str, *,
           resume: bool = False, speed: float = 0, synthetic: bool = False, label: str = "user input") -> EventEngine:
    events = read_events(input_path)
    tape_hash = hashlib.sha256(input_path.read_bytes()).hexdigest()
    if not events:
        raise ValueError("input has no events")
    with engine_lease(db_path):
        repo = Repository(db_path)
        try:
            existing = repo.load_state()
            if existing and not resume:
                raise ValueError("database already contains engine state; use --resume or a new --db path")
            cursor = repo.get_control("replay_cursor", 0) if resume else 0
            identity = str(input_path.resolve())
            if existing and repo.get_control("runtime_kind") != "replay":
                raise ValueError("database runtime mode differs; use a new DB for replay")
            if resume and repo.get_control("input_path") != identity:
                raise ValueError("resume input path differs")
            if resume and repo.get_control("input_sha256") != tape_hash:
                raise ValueError("recorded input changed; resume requires identical file SHA256")
            engine = EventEngine(settings, strategy, repo, restore=resume)
            repo.set_control("runtime_kind", "replay")
            repo.set_control("input_sha256", tape_hash)
            repo.set_control("input_path", identity)
            repo.set_control("source", {"synthetic": synthetic, "label": label})
            previous = engine.last_time
            for index, event in enumerate(events[cursor:], cursor):
                if speed > 0 and previous:
                    # Interruptible short sleeps retain Ctrl+C responsiveness.
                    deadline = time.monotonic() + min((event.available_at - previous).total_seconds() / speed, 5)
                    while time.monotonic() < deadline:
                        time.sleep(min(0.2, max(0, deadline - time.monotonic())))
                with repo.transaction():
                    engine.process(event)
                    repo.set_control("replay_cursor", index + 1)
                previous = event.available_at
            repo.set_control("engine_status", "replay complete")
            return engine
        except BaseException as exc:
            repo.log("ERROR", type(exc).__name__, detail=str(exc))
            raise
        finally:
            repo.close()


def follow(input_path: Path, db_path: Path, settings: Settings, strategy: str) -> None:
    """Tail a producer-written normalized JSONL file. Offset saved with each state.

    Receipt timestamps are retained. A producer must supply complete snapshots,
    reliable event/receipt times, and independent relative volume/eligibility.
    """
    with engine_lease(db_path):
        repo = Repository(db_path)
        try:
            existing = repo.load_state()
            if existing and repo.get_control("runtime_kind") != "follow":
                raise ValueError("database runtime mode differs; use a new DB for live paper")
            engine = EventEngine(settings, strategy, repo, restore=bool(existing), live=True)
            identity = str(input_path.resolve())
            old_path = repo.get_control("follow_path")
            if old_path and old_path != identity:
                raise ValueError("tail source differs from persisted source")
            offset = repo.get_control("follow_offset", 0)
            repo.set_control("runtime_kind", "follow")
            repo.set_control("follow_path", identity)
            repo.set_control("source", {"synthetic": False, "label": "external normalized event stream; provenance not independently verified"})
            repo.set_control("engine_status", "following file")
            input_path.parent.mkdir(parents=True, exist_ok=True)
            input_path.touch(exist_ok=True)
            with input_path.open("rb") as source:
                file_identity = (input_path.stat().st_dev, input_path.stat().st_ino)
                if input_path.stat().st_size < offset:
                    raise ValueError("event source was truncated; use a new database")
                prefix = hashlib.sha256()
                remaining = offset
                while remaining:
                    chunk = source.read(min(65536, remaining))
                    if not chunk:
                        raise ValueError("event source truncated while validating prefix")
                    prefix.update(chunk)
                    remaining -= len(chunk)
                if offset and prefix.hexdigest() != repo.get_control("follow_prefix_sha256"):
                    raise ValueError("consumed event source prefix changed; use a new database")
                source.seek(offset)
                while True:
                    stat = input_path.stat()
                    if (stat.st_dev, stat.st_ino) != file_identity or stat.st_size < source.tell():
                        raise ValueError("event source changed or was truncated; use a new DB")
                    start = source.tell()
                    line = source.readline()
                    if not line.endswith(b"\n"):
                        source.seek(start)
                        time.sleep(0.2)
                        # Manual pause takes effect even between market events.
                        paused = repo.get_control("manual_halt", False)
                        if paused != ("manual" in engine.manager.halt_reasons):
                            if paused: engine.manager.halt("manual")
                            else: engine.manager.resume("manual")
                            engine.persist()
                        wall_now = datetime.now(timezone.utc)
                        if engine.quotes and all((wall_now - quote.timestamp).total_seconds() > settings.max_quote_age_seconds for quote in engine.quotes.values()):
                            if "stale_market_data" not in engine.manager.halt_reasons:
                                engine.manager.halt("stale_market_data")
                                engine.persist()
                        continue
                    event = parse_event(json.loads(line))
                    prefix.update(line)
                    with repo.transaction():
                        engine.process(event)
                        repo.set_control("follow_offset", source.tell())
                        repo.set_control("follow_prefix_sha256", prefix.hexdigest())
        except KeyboardInterrupt:
            repo.set_control("engine_status", "stopped by user; positions remain in saved paper state")
        except BaseException as exc:
            repo.log("ERROR", "event stream stopped", detail=str(exc))
            raise
        finally:
            repo.close()
