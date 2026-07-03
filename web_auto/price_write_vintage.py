from __future__ import annotations

import csv
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

PRICE_WRITE_VINTAGE_VERSION = "PRICE_WRITE_VINTAGE_V1"
DEFAULT_MAX_ALLOWED_VINTAGE_DAYS = 7
SOURCE_NAMES = ("cogs", "fx", "floor", "competitor")

PRICE_WRITE_VINTAGE_COLUMNS = [
    "price_write_vintage_version",
    "vintage_logged_at",
    "writer_id",
    "run_id",
    "store_id",
    "store_name",
    "row_id",
    "merchant_sku",
    "operation",
    "target_field",
    "target_value",
    "source_basis",
    "max_allowed_vintage_days",
    "cogs_source_at",
    "fx_source_at",
    "floor_source_at",
    "competitor_source_at",
    "cogs_vintage_days",
    "fx_vintage_days",
    "floor_vintage_days",
    "competitor_vintage_days",
    "max_source_vintage_days",
    "vintage_ok",
    "vintage_fail_reason",
]


class PriceWriteVintageError(ValueError):
    pass


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def parse_source_time(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return _as_utc(parsed)


def source_time_from_path(path: str | Path) -> datetime | None:
    candidate = Path(path)
    if not candidate.exists():
        return None
    return datetime.fromtimestamp(candidate.stat().st_mtime, tz=timezone.utc)


def source_times_from_path(path: str | Path) -> dict[str, datetime | None]:
    observed_at = source_time_from_path(path)
    return {name: observed_at for name in SOURCE_NAMES}


def live_source_times(as_of: datetime | None = None) -> dict[str, datetime]:
    now = _as_utc(as_of or datetime.now(timezone.utc))
    return {name: now for name in SOURCE_NAMES}


def _iso(value: datetime | None) -> str:
    return "" if value is None else _as_utc(value).isoformat()


def _age_days(source_at: datetime | None, as_of: datetime) -> float | None:
    if source_at is None:
        return None
    return round((_as_utc(as_of) - _as_utc(source_at)).total_seconds() / 86400.0, 4)


def build_price_write_vintage_record(
    *,
    writer_id: str,
    run_id: str,
    store_id: int | str,
    store_name: str,
    row_id: int | str,
    merchant_sku: str,
    operation: str,
    target_field: str,
    target_value: int | str,
    source_times: Mapping[str, datetime | str | None],
    source_basis: str,
    as_of: datetime | None = None,
    max_allowed_vintage_days: int = DEFAULT_MAX_ALLOWED_VINTAGE_DAYS,
) -> dict[str, Any]:
    checked_at = _as_utc(as_of or datetime.now(timezone.utc))
    parsed_sources = {name: parse_source_time(source_times.get(name)) for name in SOURCE_NAMES}
    ages = {name: _age_days(parsed_sources[name], checked_at) for name in SOURCE_NAMES}
    missing = [name for name in SOURCE_NAMES if parsed_sources[name] is None]
    max_age = max((age for age in ages.values() if age is not None), default=None)
    stale = max_age is not None and max_age > int(max_allowed_vintage_days)
    fail_reasons: list[str] = []
    if missing:
        fail_reasons.append("missing_source_at:" + ";".join(missing))
    if stale:
        fail_reasons.append(f"max_source_vintage_days>{int(max_allowed_vintage_days)}")
    record = {
        "price_write_vintage_version": PRICE_WRITE_VINTAGE_VERSION,
        "vintage_logged_at": checked_at.isoformat(),
        "writer_id": str(writer_id),
        "run_id": str(run_id),
        "store_id": str(store_id),
        "store_name": str(store_name),
        "row_id": str(row_id),
        "merchant_sku": str(merchant_sku or ""),
        "operation": str(operation),
        "target_field": str(target_field),
        "target_value": str(target_value),
        "source_basis": str(source_basis),
        "max_allowed_vintage_days": int(max_allowed_vintage_days),
        "cogs_source_at": _iso(parsed_sources["cogs"]),
        "fx_source_at": _iso(parsed_sources["fx"]),
        "floor_source_at": _iso(parsed_sources["floor"]),
        "competitor_source_at": _iso(parsed_sources["competitor"]),
        "cogs_vintage_days": "" if ages["cogs"] is None else ages["cogs"],
        "fx_vintage_days": "" if ages["fx"] is None else ages["fx"],
        "floor_vintage_days": "" if ages["floor"] is None else ages["floor"],
        "competitor_vintage_days": "" if ages["competitor"] is None else ages["competitor"],
        "max_source_vintage_days": "" if max_age is None else max_age,
        "vintage_ok": not fail_reasons,
        "vintage_fail_reason": ";".join(fail_reasons),
    }
    return record


def ensure_price_write_vintage_ok(record: Mapping[str, Any]) -> None:
    if not bool(record.get("vintage_ok")):
        raise PriceWriteVintageError(str(record.get("vintage_fail_reason") or "price_write_vintage_not_ok"))


def attach_vintage_fields(row: Mapping[str, Any], record: Mapping[str, Any]) -> dict[str, Any]:
    out = dict(row)
    for column in PRICE_WRITE_VINTAGE_COLUMNS:
        out[column] = record.get(column, "")
    return out


def write_price_write_vintage_log(path: str | Path, rows: list[Mapping[str, Any]]) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=PRICE_WRITE_VINTAGE_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
