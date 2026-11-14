"""금융 소수값을 부동소수점 변환 없이 JSON에 저장한다."""
from __future__ import annotations

from decimal import Decimal
import json

from ..models import jsonable


def exact_jsonable(value):
    value = jsonable(value)
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError("금융 값은 유한한 Decimal이어야 합니다")
        return str(value)
    if isinstance(value, dict):
        return {str(k): exact_jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [exact_jsonable(v) for v in value]
    return value


def dumps(value) -> str:
    return json.dumps(exact_jsonable(value), ensure_ascii=False, allow_nan=False)


def record_identity(kind: str, data: dict):
    rid = data.get("id") or data.get("client_id")
    if rid is None and isinstance(data.get("intent"), dict):
        rid = data["intent"].get("client_id")
    if kind == "positions":
        rid = data.get("symbol")
    elif kind == "news_events":
        rid = f"{data.get('id')}:{data.get('received_at')}"
    elif kind in ("market_bars", "quote_snapshots"):
        rid = f"{data.get('symbol')}:{data.get('timestamp')}:{data.get('received_at')}"
    return str(rid) if rid is not None else None
