from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


@dataclass
class KaspiSnapshotConfig:
    task_id: str
    repricer_config_path: str
    refresh_repricer: bool
    source_items_sqlite: str
    snapshot_sqlite: str
    parquet_root: str
    artifacts_root: str
    stores: list[int]
    city_id: str
    limit: int
    max_pages: int
    timeout_seconds: int
    hot_days: int
    cold_days: int


class SnapshotConfigError(ValueError):
    pass


def _as_mapping(value: Any, name: str) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise SnapshotConfigError(f"{name} must be a mapping")
    return value


def _as_list_int(value: Any, name: str) -> list[int]:
    if not isinstance(value, list):
        raise SnapshotConfigError(f"{name} must be a list")
    out: list[int] = []
    for item in value:
        out.append(int(item))
    return out


def load_kaspi_snapshot_config(path: str | Path) -> KaspiSnapshotConfig:
    path = Path(path)
    if not path.exists():
        raise SnapshotConfigError(f"Config file not found: {path}")

    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise SnapshotConfigError("Config must be a YAML mapping")

    storage = _as_mapping(raw.get("storage"), "storage")
    scrape = _as_mapping(raw.get("scrape"), "scrape")
    retention = _as_mapping(raw.get("retention"), "retention")

    stores = raw.get("stores", [30000001, 30000002])
    if not isinstance(stores, list):
        raise SnapshotConfigError("stores must be a list")

    return KaspiSnapshotConfig(
        task_id=str(raw.get("task_id", "kaspi_hourly_snapshot")),
        repricer_config_path=str(raw.get("repricer_config_path", "config/tasks/repricer_competitors.yaml")),
        refresh_repricer=bool(raw.get("refresh_repricer", True)),
        source_items_sqlite=str(storage.get("source_items_sqlite", "data/repricer_items.sqlite")),
        snapshot_sqlite=str(storage.get("snapshot_sqlite", "data/kaspi_snapshots/snapshot.sqlite")),
        parquet_root=str(storage.get("parquet_root", "exports/kaspi_snapshots/parquet")),
        artifacts_root=str(storage.get("artifacts_root", "runs/kaspi_hourly_snapshots")),
        stores=_as_list_int(stores, "stores"),
        city_id=str(raw.get("city_id", "710000000")),
        limit=int(scrape.get("limit", 50)),
        max_pages=int(scrape.get("max_pages", 20)),
        timeout_seconds=int(scrape.get("timeout_seconds", 30)),
        hot_days=int(retention.get("hot_days", 90)),
        cold_days=int(retention.get("cold_days", 365)),
    )
