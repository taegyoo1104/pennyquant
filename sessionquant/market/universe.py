"""관측 시각을 보존하는 S&P 500 구성 종목 목록.

현재 목록을 내려받아 과거에 소급하지 않는다. 과거 구성 이력은 출처와
당시 확인 가능 시각을 포함한 별도 자료를 가져와야 한다.
"""
from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import io
import json
from pathlib import Path

import httpx

from ..models import symbol_valid, utc

PUBLIC_URL = "https://raw.githubusercontent.com/datasets/s-and-p-500-companies/main/data/constituents.csv"


@dataclass(frozen=True)
class Constituent:
    symbol: str
    name: str = ""
    sector: str = ""

    def __post_init__(self):
        symbol_valid(self.symbol)


class UniverseStore:
    """전체 스냅숏과 편입·제외 관측 이력을 원자적으로 저장한다."""

    def __init__(self, path: str | Path | None = None, *, max_age_days: int = 7):
        if type(max_age_days) is not int or max_age_days < 1:
            raise ValueError("유니버스 유효 기간은 1일 이상이어야 합니다")
        self.path = Path(path) if path else None
        self.max_age_days = max_age_days
        self.snapshots: list[dict] = []
        if self.path and self.path.exists():
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            if payload.get("schema_version") != 1:
                raise ValueError("지원하지 않는 유니버스 형식입니다")
            self.import_snapshots(payload.get("snapshots", []), persist=False)

    def import_snapshots(self, snapshots: list[dict], *, persist: bool = True) -> None:
        """검증된 이력 파일도 known_at 이후에만 전략에 노출한다."""
        validated = []
        for item in snapshots:
            effective_at, known_at = utc(item["effective_at"]), utc(item["known_at"])
            if not isinstance(item.get("source"), str) or not item["source"]:
                raise ValueError("유니버스 출처가 필요합니다")
            rows = self._rows(item["members"])
            canonical = {"effective_at": effective_at.isoformat(), "known_at": known_at.isoformat(),
                         "source": item["source"], "members": rows}
            canonical["id"] = sha256(json.dumps(canonical, sort_keys=True).encode()).hexdigest()
            validated.append(canonical)
        self.snapshots = sorted({s["id"]: s for s in self.snapshots + validated}.values(),
                                key=lambda x: (x["effective_at"], x["known_at"]))
        if persist:
            self._save()

    @staticmethod
    def _rows(rows) -> list[dict]:
        if not isinstance(rows, (list, tuple)) or not rows:
            raise ValueError("유니버스에는 한 개 이상의 종목이 필요합니다")
        result = []
        for row in rows:
            if isinstance(row, str):
                item = Constituent(row)
            elif isinstance(row, Constituent):
                item = row
            else:
                item = Constituent(**row)
            result.append({"symbol": item.symbol, "name": item.name, "sector": item.sector})
        if len({row["symbol"] for row in result}) != len(result):
            raise ValueError("유니버스에 중복 티커가 있습니다")
        return sorted(result, key=lambda x: x["symbol"])

    def refresh(self, rows, *, source: str, observed_at: datetime | None = None) -> dict:
        """새 관측은 관측 시점부터 유효하며 이전 구성 내역을 덮어쓰지 않는다."""
        now = utc(observed_at or datetime.now(timezone.utc))
        previous = self.members(now, allow_stale=True)
        self.import_snapshots([{"effective_at": now.isoformat(), "known_at": now.isoformat(),
                                "source": source, "members": self._rows(rows)}])
        members = self.members(now)
        return {"observed_at": now.isoformat(), "source": source, "count": len(members),
                "added": sorted(set(members) - set(previous)), "removed": sorted(set(previous) - set(members))}

    async def refresh_public(self, *, client: httpx.AsyncClient | None = None,
                             observed_at: datetime | None = None) -> dict:
        """공개 CSV의 현재 목록을 수집한다. 지수 제공사의 과거 이력 상품은 아니다."""
        owns_client = client is None
        client = client or httpx.AsyncClient(timeout=20, follow_redirects=False)
        try:
            response = await client.get(PUBLIC_URL)
            response.raise_for_status()
            if len(response.content) > 2_000_000:
                raise ValueError("유니버스 응답 크기가 너무 큽니다")
            rows = [{"symbol": row["Symbol"], "name": row.get("Security", row.get("Name", "")),
                     "sector": row.get("GICS Sector", row.get("Sector", ""))}
                    for row in csv.DictReader(io.StringIO(response.text))]
            if not 450 <= len(rows) <= 550:
                raise ValueError("S&P 500 구성 목록의 종목 수를 확인할 수 없습니다")
            return self.refresh(rows, source=PUBLIC_URL, observed_at=observed_at)
        finally:
            if owns_client:
                await client.aclose()

    def snapshot(self, at: datetime, *, allow_stale: bool = False) -> dict | None:
        moment = utc(at)
        available = [x for x in self.snapshots if utc(x["effective_at"]) <= moment and utc(x["known_at"]) <= moment]
        if not available:
            return None
        selected = max(available, key=lambda x: (utc(x["effective_at"]), utc(x["known_at"])))
        if not allow_stale and moment - utc(selected["effective_at"]) > timedelta(days=self.max_age_days):
            return None
        return selected

    def members(self, at: datetime, *, allow_stale: bool = False) -> dict[str, Constituent]:
        snapshot = self.snapshot(at, allow_stale=allow_stale)
        return {x["symbol"]: Constituent(**x) for x in snapshot["members"]} if snapshot else {}

    def contains(self, symbol: str, at: datetime) -> bool:
        return symbol in self.members(at)

    def _save(self) -> None:
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_name(self.path.name + ".tmp")
        temp.write_text(json.dumps({"schema_version": 1, "snapshots": self.snapshots},
                                   ensure_ascii=False, indent=2), encoding="utf-8")
        temp.replace(self.path)
