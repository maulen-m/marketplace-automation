from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

DEFAULT_DELIVERY_DAYS_CACHE_PATH = Path(__file__).resolve().parents[1] / "data/kaspi_delivery_days_cache.json"
_UTC = timezone.utc


def _canonical_offer_link(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    text = text.split("?", 1)[0].split("#", 1)[0]
    return text.rstrip("/").lower()


def _parse_dt(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_UTC)
    return dt.astimezone(_UTC)


def _normalize_days_by_mid(days_by_mid: dict[Any, Any] | None) -> dict[str, int]:
    out: dict[str, int] = {}
    if not isinstance(days_by_mid, dict):
        return out
    for mid, raw_days in days_by_mid.items():
        mid_key = str(mid or "").strip()
        if not mid_key:
            continue
        try:
            days = int(float(raw_days))
        except Exception:
            continue
        if days < 0:
            continue
        out[mid_key] = days
    return out


def load_delivery_days_cache(path: Path | str = DEFAULT_DELIVERY_DAYS_CACHE_PATH) -> dict[str, Any]:
    p = Path(path)
    if not p.exists():
        return {"version": 1, "entries": {}}
    try:
        payload = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {"version": 1, "entries": {}}
    if not isinstance(payload, dict):
        return {"version": 1, "entries": {}}
    entries = payload.get("entries")
    if not isinstance(entries, dict):
        entries = {}
    return {
        "version": 1,
        "entries": entries,
    }


def save_delivery_days_cache(path: Path | str, payload: dict[str, Any]) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(p)


def get_cached_delivery_days(
    payload: dict[str, Any],
    offer_link: str,
    *,
    now: datetime | None = None,
    ttl_hours: int = 24,
) -> dict[str, int] | None:
    link_key = _canonical_offer_link(offer_link)
    if not link_key:
        return None
    entries = payload.get("entries") if isinstance(payload, dict) else None
    if not isinstance(entries, dict):
        return None
    entry = entries.get(link_key)
    if not isinstance(entry, dict):
        return None
    fetched_at = _parse_dt(entry.get("fetched_at"))
    if fetched_at is None:
        return None
    now_dt = (now or datetime.now(_UTC)).astimezone(_UTC)
    if now_dt - fetched_at > timedelta(hours=max(0, int(ttl_hours))):
        return None
    days_by_mid = _normalize_days_by_mid(entry.get("days_by_mid"))
    if "days_by_mid" not in entry:
        return None
    return days_by_mid


def put_cached_delivery_days(
    payload: dict[str, Any],
    offer_link: str,
    days_by_mid: dict[Any, Any],
    *,
    fetched_at: datetime | None = None,
) -> None:
    link_key = _canonical_offer_link(offer_link)
    if not link_key:
        return
    normalized = _normalize_days_by_mid(days_by_mid)
    entries = payload.setdefault("entries", {})
    if not isinstance(entries, dict):
        payload["entries"] = {}
        entries = payload["entries"]
    when = (fetched_at or datetime.now(_UTC)).astimezone(_UTC).isoformat()
    entries[link_key] = {
        "fetched_at": when,
        "days_by_mid": normalized,
    }
