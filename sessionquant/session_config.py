"""SessionQuant 모의 운영 설정. 비밀정보는 환경변수에서만 읽는다."""
from __future__ import annotations

from dataclasses import dataclass, field, fields
from pathlib import Path
import math
import yaml


@dataclass(frozen=True)
class SessionConfig:
    environment: str = "PAPER_LOCAL"
    database_backend: str = "mysql"
    db_path: Path = Path("data/sessionquant-paper-local.db")
    universe_path: Path = Path("data/sp500-universe.json")
    strategy: str = "pullback"
    source: str = "toss"
    input_path: Path | None = None
    poll_seconds: float = 10.0
    news_refresh_seconds: float = 600.0
    symbols_per_cycle: int = 2
    news_enabled: bool = True
    include_alpaca: bool = True
    companies: dict = field(default_factory=dict)
    portfolio: dict = field(default_factory=dict)
    llm: dict = field(default_factory=dict)

    def __post_init__(self):
        if self.environment != "PAPER_LOCAL":
            raise ValueError("Phase 0~4 실행 환경은 PAPER_LOCAL만 허용합니다. 실제 주문은 비활성화되어 있습니다.")
        if self.database_backend not in {"mysql", "sqlite"}:
            raise ValueError("database_backend는 mysql 또는 sqlite여야 합니다.")
        if self.strategy not in {"momentum", "pullback", "gap_breakout", "vwap"}:
            raise ValueError("지원하지 않는 전략입니다.")
        if self.source not in {"toss", "file"} or (self.source == "file" and self.input_path is None):
            raise ValueError("source는 toss 또는 file이며, file은 input_path가 필요합니다.")
        for name, minimum in (("poll_seconds", 1), ("news_refresh_seconds", 300)):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value) or value < minimum:
                raise ValueError(f"{name}은 {minimum}초 이상이어야 합니다.")
        if type(self.symbols_per_cycle) is not int or not 1 <= self.symbols_per_cycle <= 10:
            raise ValueError("symbols_per_cycle은 1~10의 정수여야 합니다.")
        if type(self.news_enabled) is not bool or type(self.include_alpaca) is not bool:
            raise ValueError("뉴스 사용 설정은 true 또는 false여야 합니다.")
        for name in ("companies", "portfolio", "llm"):
            if not isinstance(getattr(self, name), dict):
                raise ValueError(f"{name}은 설정 객체여야 합니다.")


def load_session_config(path: str | Path) -> SessionConfig:
    path = Path(path).resolve()
    payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(payload, dict) or set(payload) - {f.name for f in fields(SessionConfig)}:
        raise ValueError("알 수 없는 SessionQuant 설정 항목이 있습니다.")
    for name, default in (("db_path", "../data/sessionquant-paper-local.db"),
                          ("universe_path", "../data/sp500-universe.json"), ("input_path", None)):
        value = payload.get(name, default)
        if value is not None:
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name}은 비어 있지 않은 경로여야 합니다.")
            candidate = Path(value)
            payload[name] = candidate if candidate.is_absolute() else (path.parent / candidate).resolve()
    return SessionConfig(**payload)
