from __future__ import annotations

import csv
import json
import sqlite3
from collections import Counter
from pathlib import Path
from typing import Any, Mapping, Sequence

import yaml


SCHEMA_VERSION = "web_auto.acmewear_universal_marketing_registry.v1"
DEFAULT_REGISTRY_PATH = Path("config/tasks/acmewear_universal_marketing_registry.yaml")
REQUIRED_ROW_FIELDS = [
    "store",
    "merchant_id",
    "store_code",
    "campaign_id",
    "family",
    "sku_key",
    "tier",
    "inclusion_reason",
    "source_path",
    "source_section",
    "configured_product_sku",
]
TIER_PRIORITY = {"A": 0, "B": 1, "C": 2}
ACTIVE_CAMPAIGN_STATES = {
    "ACTIVE",
    "ENABLED",
    "RUNNING",
    "ON",
    "АКТИВНА",
    "АКТИВНЫЙ",
    "ВКЛЮЧЕН",
    "ВКЛЮЧЕНА",
    "ВКЛЮЧЕНО",
}
SECRET_FIELD_NEEDLES = (
    "token",
    "password",
    "cookie",
    "bearer",
    "authorization",
    "xsrf",
    "api_key",
    "apikey",
    "storage_state",
    "storagestate",
    "sessionid",
)


class UniversalMarketingRegistryError(ValueError):
    pass


def _repo_root(repo_root: Path | None = None) -> Path:
    return (repo_root or Path.cwd()).resolve()


def _repo_path(value: str | Path, repo_root: Path) -> Path:
    path = Path(str(value)).expanduser()
    if path.is_absolute():
        return path
    return repo_root / path


def _read_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise UniversalMarketingRegistryError(f"yaml_not_found:{path}")
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise UniversalMarketingRegistryError(f"yaml_must_be_mapping:{path}")
    return raw


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    return [value]


def _text(value: Any) -> str:
    return str(value or "").strip()


def _upper_text(value: Any) -> str:
    return _text(value).upper()


def _append_unique(items: list[str], value: Any) -> None:
    text = _text(value)
    if text and text not in items:
        items.append(text)


def _join_unique(values: Sequence[Any]) -> str:
    out: list[str] = []
    for value in values:
        if isinstance(value, str) and ";" in value:
            for part in value.split(";"):
                _append_unique(out, part)
        else:
            _append_unique(out, value)
    return ";".join(out)


def _split_campaign_ids(value: Any) -> list[str]:
    out: list[str] = []
    for item in _as_list(value):
        for part in str(item or "").replace(";", ",").split(","):
            _append_unique(out, part)
    return out


def _campaign_ids_from_argv(argv: Any) -> list[str]:
    values = [_text(item) for item in _as_list(argv)]
    out: list[str] = []
    for index, token in enumerate(values):
        if token == "--campaign-id" and index + 1 < len(values):
            for cid in _split_campaign_ids(values[index + 1]):
                _append_unique(out, cid)
        elif token == "--campaign-ids" and index + 1 < len(values):
            for cid in _split_campaign_ids(values[index + 1]):
                _append_unique(out, cid)
        elif token.startswith("--campaign-id="):
            for cid in _split_campaign_ids(token.split("=", 1)[1]):
                _append_unique(out, cid)
        elif token.startswith("--campaign-ids="):
            for cid in _split_campaign_ids(token.split("=", 1)[1]):
                _append_unique(out, cid)
    return out


def _first(mapping: Mapping[str, Any], *keys: str) -> str:
    for key in keys:
        value = mapping.get(key)
        if value not in (None, ""):
            return _text(value)
    return ""


def _is_secret_field(name: Any) -> bool:
    normalized = _text(name).lower().replace("-", "_").replace(" ", "_")
    return any(needle in normalized for needle in SECRET_FIELD_NEEDLES)


def _flatten_text(*values: Any) -> str:
    parts: list[str] = []
    for value in values:
        if isinstance(value, Mapping):
            for nested_value in value.values():
                parts.append(_flatten_text(nested_value))
        elif isinstance(value, list):
            for item in value:
                parts.append(_flatten_text(item))
        else:
            text = _text(value)
            if text:
                parts.append(text)
    return " ".join(parts)


def _family_from_text(value: Any) -> str:
    text = _upper_text(value).replace("_", "-")
    if "LINE31" in text:
        return "LINE31"
    if "LINE51-CHILD" in text:
        return "LINE51_CHILD"
    if "LINE61-CHILD" in text:
        return "LINE61_CHILD"
    if "LINE51" in text and ("LINE-21" in text or "LINE-31" in text):
        return "LINE51_CHILD"
    if "LINE61" in text and ("SUIT-21" in text or "SUIT-31" in text):
        return "LINE61_CHILD"
    if "SUIT31" in text or "SUIT-31" in text or "SUIT21" in text or "SUIT-21" in text:
        return "LINE61_CHILD"
    if "LINE31" in text or "LINE-31" in text or "LINE21" in text or "LINE-21" in text:
        return "LINE51_CHILD"
    if "LINE51" in text:
        return "LINE51"
    if "LINE61" in text:
        return "LINE61"
    return ""


def _family_from_item(item: Mapping[str, Any], fallback: Any = "") -> str:
    configured = _first(item, "family", "product_family")
    if configured:
        inferred_configured = _family_from_text(configured)
        return inferred_configured or configured.upper()
    card_group = _first(item, "card_group", "lane")
    inferred = _family_from_text(card_group)
    if inferred:
        return inferred
    return _family_from_text(fallback)


def _sku_from_item(item: Mapping[str, Any]) -> str:
    return _first(
        item,
        "sku_key",
        "merchant_sku_hint",
        "merchant_sku",
        "merchant_sku_prefix",
        "child_bundle_id",
        "card_group",
        "route_id",
        "lane",
    )


def _configured_product_sku(item: Mapping[str, Any]) -> str:
    return _first(item, "configured_product_sku", "campaign_product_sku", "product_sku")


def _source_tier(source: Mapping[str, Any]) -> str:
    tier = _text(source.get("tier")).upper()
    if tier not in TIER_PRIORITY:
        raise UniversalMarketingRegistryError(f"unsupported_source_tier:{tier or '<missing>'}")
    return tier


def _detect_item_tier(
    *,
    item: Mapping[str, Any],
    source: Mapping[str, Any],
    registry: Mapping[str, Any],
) -> str:
    explicit = _text(item.get("tier")).upper()
    if explicit in TIER_PRIORITY:
        return explicit

    default = _source_tier(source)
    paused_terms = [
        _text(term).lower()
        for term in (registry.get("tier_detection") or {}).get("paused_or_history_terms", [])
        if _text(term)
    ]
    haystack = _flatten_text(item).lower()
    if default != "A" and any(term in haystack for term in paused_terms):
        return "C"
    return default


def _base_row(
    *,
    registry: Mapping[str, Any],
    source: Mapping[str, Any],
    source_section: str,
    item: Mapping[str, Any],
    campaign_id: str,
    inclusion_reason: str,
    fallback_text: Any = "",
) -> dict[str, Any]:
    store = registry["store"]
    return {
        "store": _text(store.get("name")),
        "merchant_id": _text(store.get("merchant_id")),
        "store_code": _text(store.get("store_code")),
        "campaign_id": _text(campaign_id),
        "family": _family_from_item(item, fallback_text),
        "sku_key": _sku_from_item(item),
        "tier": _detect_item_tier(item=item, source=source, registry=registry),
        "inclusion_reason": _join_unique([source.get("inclusion_reason"), inclusion_reason]),
        "source_path": _text(source.get("path")),
        "source_section": source_section,
        "configured_product_sku": _configured_product_sku(item),
    }


def _extract_daily_ops_rows(
    *,
    raw: Mapping[str, Any],
    registry: Mapping[str, Any],
    source: Mapping[str, Any],
) -> list[dict[str, Any]]:
    marketing = (raw.get("sources") or {}).get("marketing") if isinstance(raw.get("sources"), Mapping) else {}
    campaigns = marketing.get("campaigns") if isinstance(marketing, Mapping) else []
    rows: list[dict[str, Any]] = []
    for item in campaigns or []:
        if not isinstance(item, Mapping):
            continue
        campaign_id = _first(item, "campaign_id")
        if not campaign_id:
            continue
        rows.append(
            _base_row(
                registry=registry,
                source=source,
                source_section="sources.marketing.campaigns",
                item=item,
                campaign_id=campaign_id,
                inclusion_reason=f"daily_ops_campaign:{_first(item, 'lane') or campaign_id}",
                fallback_text=_flatten_text(source, item),
            )
        )
    return rows


def _extract_top_level_campaign_rows(
    *,
    raw: Mapping[str, Any],
    registry: Mapping[str, Any],
    source: Mapping[str, Any],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for item in raw.get("campaigns") or []:
        if not isinstance(item, Mapping):
            continue
        campaign_id = _first(item, "campaign_id")
        if not campaign_id:
            continue
        rows.append(
            _base_row(
                registry=registry,
                source=source,
                source_section="campaigns",
                item=item,
                campaign_id=campaign_id,
                inclusion_reason=f"campaign_scope:{_first(item, 'route_id', 'lane') or campaign_id}",
                fallback_text=_flatten_text(raw.get("family"), source, item),
            )
        )
    return rows


def _extract_line31_seed_rows(
    *,
    raw: Mapping[str, Any],
    registry: Mapping[str, Any],
    source: Mapping[str, Any],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for item in raw.get("seed_campaigns") or []:
        if not isinstance(item, Mapping):
            continue
        campaign_id = _first(item, "campaign_id")
        if not campaign_id:
            continue
        rows.append(
            _base_row(
                registry=registry,
                source=source,
                source_section="seed_campaigns",
                item=item,
                campaign_id=campaign_id,
                inclusion_reason=f"line31_seed_campaign:{_first(item, 'color_label') or campaign_id}",
                fallback_text=_flatten_text(raw.get("family"), source, item),
            )
        )
    return rows


def _extract_child_bundle_scope_rows(
    *,
    raw: Mapping[str, Any],
    registry: Mapping[str, Any],
    source: Mapping[str, Any],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for item in raw.get("child_bundle_campaign_scope") or []:
        if not isinstance(item, Mapping):
            continue
        campaign_id = _first(item, "campaign_id")
        if not campaign_id:
            continue
        rows.append(
            _base_row(
                registry=registry,
                source=source,
                source_section="child_bundle_campaign_scope",
                item=item,
                campaign_id=campaign_id,
                inclusion_reason=f"child_bundle_scope:{_first(item, 'child_bundle_id') or campaign_id}",
                fallback_text=_flatten_text(source, item),
            )
        )
    return rows


def _extract_job_rows(
    *,
    raw: Mapping[str, Any],
    registry: Mapping[str, Any],
    source: Mapping[str, Any],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for job in raw.get("jobs") or []:
        if not isinstance(job, Mapping):
            continue
        job_id = _first(job, "job_id") or "unknown_job"
        job_text = _flatten_text(job.get("job_id"), job.get("title"), source.get("source_id"), source.get("path"))
        for product in job.get("products") or []:
            if not isinstance(product, Mapping):
                continue
            for campaign_id in _split_campaign_ids(product.get("campaigns")):
                rows.append(
                    _base_row(
                        registry=registry,
                        source=source,
                        source_section=f"jobs.{job_id}.products",
                        item=product,
                        campaign_id=campaign_id,
                        inclusion_reason=f"scheduled_product:{job_id}:{_first(product, 'sku_key', 'child_bundle_id') or campaign_id}",
                        fallback_text=_flatten_text(job_text, product),
                    )
                )
        for command in job.get("commands") or []:
            if not isinstance(command, Mapping):
                continue
            command_id = _first(command, "id") or "unknown_command"
            for campaign_id in _campaign_ids_from_argv(command.get("argv")):
                command_text = _flatten_text(command.get("id"), command.get("description"))
                rows.append(
                    _base_row(
                        registry=registry,
                        source=source,
                        source_section=f"jobs.{job_id}.commands.{command_id}.argv",
                        item=command,
                        campaign_id=campaign_id,
                        inclusion_reason=f"scheduled_command:{job_id}:{command_id}",
                        fallback_text=command_text,
                    )
                )
    return rows


def load_registry_config(
    path: str | Path = DEFAULT_REGISTRY_PATH,
    *,
    repo_root: Path | None = None,
) -> dict[str, Any]:
    root = _repo_root(repo_root)
    registry_path = _repo_path(path, root)
    registry = _read_yaml(registry_path)
    schema_version = _text(registry.get("schema_version"))
    if schema_version != SCHEMA_VERSION:
        raise UniversalMarketingRegistryError(f"unsupported_schema_version:{schema_version or '<missing>'}")

    store = registry.get("store")
    if not isinstance(store, Mapping):
        raise UniversalMarketingRegistryError("store must be a mapping")
    expected = {"name": "ACMEWEAR", "merchant_id": "759051", "store_code": "30137883", "timezone": "Asia/Almaty"}
    for key, value in expected.items():
        if _text(store.get(key)) != value:
            raise UniversalMarketingRegistryError(f"unexpected_store_{key}:{store.get(key)!r}")

    guardrails = registry.get("guardrails")
    if not isinstance(guardrails, Mapping):
        raise UniversalMarketingRegistryError("guardrails must be a mapping")
    if bool(guardrails.get("live_writes_allowed")):
        raise UniversalMarketingRegistryError("registry must remain read-only")

    tiers = registry.get("tiers")
    if not isinstance(tiers, Mapping) or set(TIER_PRIORITY) - set(tiers):
        raise UniversalMarketingRegistryError("tiers A/B/C must be configured")

    sources = registry.get("seed_sources")
    if not isinstance(sources, list) or not sources:
        raise UniversalMarketingRegistryError("seed_sources must be a non-empty list")
    seen_source_ids: set[str] = set()
    for source in sources:
        if not isinstance(source, Mapping):
            raise UniversalMarketingRegistryError("each seed source must be a mapping")
        source_id = _text(source.get("source_id"))
        if not source_id:
            raise UniversalMarketingRegistryError("seed source missing source_id")
        if source_id in seen_source_ids:
            raise UniversalMarketingRegistryError(f"duplicate_source_id:{source_id}")
        seen_source_ids.add(source_id)
        if not _text(source.get("path")):
            raise UniversalMarketingRegistryError(f"seed source missing path:{source_id}")
        _source_tier(source)
        source_path = _repo_path(source["path"], root)
        if not source_path.exists():
            raise UniversalMarketingRegistryError(f"seed_source_not_found:{source_path}")
    return registry


def scan_registry_sources(
    registry: Mapping[str, Any] | None = None,
    *,
    registry_path: str | Path = DEFAULT_REGISTRY_PATH,
    repo_root: Path | None = None,
) -> list[dict[str, Any]]:
    root = _repo_root(repo_root)
    loaded_registry = registry or load_registry_config(registry_path, repo_root=root)
    rows: list[dict[str, Any]] = []
    for source in loaded_registry.get("seed_sources") or []:
        source_path = _repo_path(source["path"], root)
        raw = _read_yaml(source_path)
        rows.extend(_extract_daily_ops_rows(raw=raw, registry=loaded_registry, source=source))
        rows.extend(_extract_top_level_campaign_rows(raw=raw, registry=loaded_registry, source=source))
        rows.extend(_extract_line31_seed_rows(raw=raw, registry=loaded_registry, source=source))
        rows.extend(_extract_child_bundle_scope_rows(raw=raw, registry=loaded_registry, source=source))
        rows.extend(_extract_job_rows(raw=raw, registry=loaded_registry, source=source))
    return rows


def _merge_tiers(tiers: Sequence[str]) -> str:
    normalized = [tier for tier in (_text(item).upper() for item in tiers) if tier in TIER_PRIORITY]
    if "A" in normalized:
        return "A"
    if "C" in normalized:
        return "C"
    if "B" in normalized:
        return "B"
    return ""


def build_registry_rows(
    registry: Mapping[str, Any] | None = None,
    *,
    registry_path: str | Path = DEFAULT_REGISTRY_PATH,
    repo_root: Path | None = None,
) -> list[dict[str, Any]]:
    candidates = scan_registry_sources(registry=registry, registry_path=registry_path, repo_root=repo_root)
    merged: dict[str, dict[str, Any]] = {}
    multi_fields = ["family", "sku_key", "inclusion_reason", "source_path", "source_section", "configured_product_sku"]
    for candidate in candidates:
        campaign_id = _text(candidate.get("campaign_id"))
        if not campaign_id:
            continue
        row = merged.setdefault(
            campaign_id,
            {
                "store": _text(candidate.get("store")),
                "merchant_id": _text(candidate.get("merchant_id")),
                "store_code": _text(candidate.get("store_code")),
                "campaign_id": campaign_id,
                "family": "",
                "sku_key": "",
                "tier": "",
                "inclusion_reason": "",
                "source_path": "",
                "source_section": "",
                "configured_product_sku": "",
                "_tier_candidates": [],
            },
        )
        row["_tier_candidates"].append(_text(candidate.get("tier")))
        for field in multi_fields:
            row[field] = _join_unique([row.get(field, ""), candidate.get(field, "")])

    rows: list[dict[str, Any]] = []
    for row in merged.values():
        row["tier"] = _merge_tiers(row.pop("_tier_candidates", []))
        rows.append(row)
    rows.sort(key=lambda item: (item.get("family") or "", int(item["campaign_id"]) if item["campaign_id"].isdigit() else item["campaign_id"]))
    return rows


def summarize_registry_rows(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    tier_counts = Counter(_text(row.get("tier")) or "<missing>" for row in rows)
    family_counts = Counter(_text(row.get("family")) or "<missing>" for row in rows)
    return {
        "row_count": len(rows),
        "campaign_count": len({_text(row.get("campaign_id")) for row in rows if _text(row.get("campaign_id"))}),
        "tier_counts": dict(sorted(tier_counts.items())),
        "family_counts": dict(sorted(family_counts.items())),
    }


def validate_registry_rows(rows: Sequence[Mapping[str, Any]]) -> None:
    seen: set[str] = set()
    for index, row in enumerate(rows, start=1):
        missing = [field for field in REQUIRED_ROW_FIELDS if field not in row]
        if missing:
            raise UniversalMarketingRegistryError(f"row_{index}_missing_fields:{','.join(missing)}")
        campaign_id = _text(row.get("campaign_id"))
        if not campaign_id:
            raise UniversalMarketingRegistryError(f"row_{index}_missing_campaign_id")
        if campaign_id in seen:
            raise UniversalMarketingRegistryError(f"duplicate_campaign_id:{campaign_id}")
        seen.add(campaign_id)
        if _text(row.get("tier")) not in TIER_PRIORITY:
            raise UniversalMarketingRegistryError(f"row_{campaign_id}_bad_tier:{row.get('tier')!r}")
        for required_value in ("store", "merchant_id", "store_code", "source_path", "source_section", "inclusion_reason"):
            if not _text(row.get(required_value)):
                raise UniversalMarketingRegistryError(f"row_{campaign_id}_missing_{required_value}")


def split_registry_field_values(value: Any) -> list[str]:
    out: list[str] = []
    for item in _as_list(value):
        for part in str(item or "").replace(",", ";").split(";"):
            _append_unique(out, part)
    return out


def _sqlite_table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type IN ('table','view') AND name = ?",
        (table_name,),
    ).fetchone()
    return row is not None


def _campaign_state_is_active(value: Any) -> bool:
    return _upper_text(value).replace("Ё", "Е") in ACTIVE_CAMPAIGN_STATES


def discover_active_campaign_rows_from_marketing_db(
    db_path: str | Path,
    *,
    merchant_id: str = "759051",
    store_code: str = "30137883",
) -> list[dict[str, Any]]:
    path = Path(db_path).expanduser()
    if not path.exists():
        return []

    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        if not _sqlite_table_exists(conn, "campaign_daily_current"):
            return []
        rows = conn.execute(
            """
            WITH ranked AS (
              SELECT
                campaign_id,
                campaign_name,
                state,
                date,
                merchant_id,
                store_code,
                daily_budget,
                default_bid,
                views,
                clicks,
                carts,
                transactions,
                cost,
                gmv,
                record_timestamp,
                ingested_at,
                ROW_NUMBER() OVER (
                  PARTITION BY campaign_id
                  ORDER BY date DESC, ingested_at DESC, record_timestamp DESC
                ) AS rn
              FROM campaign_daily_current
              WHERE merchant_id = ?
                AND store_code = ?
                AND campaign_id IS NOT NULL
                AND TRIM(campaign_id) <> ''
            )
            SELECT *
            FROM ranked
            WHERE rn = 1
            ORDER BY campaign_id
            """,
            (_text(merchant_id), _text(store_code)),
        ).fetchall()
    finally:
        conn.close()

    out: list[dict[str, Any]] = []
    for row in rows:
        payload = dict(row)
        if not _campaign_state_is_active(payload.get("state")):
            continue
        payload.pop("rn", None)
        payload["campaign_id"] = _text(payload.get("campaign_id"))
        payload["discovery_reason"] = "latest_campaign_daily_current_active"
        out.append(payload)
    return out


def _read_live_inventory_payload(path: Path) -> list[Mapping[str, Any]]:
    if not path.exists():
        return []
    if path.suffix.lower() == ".csv":
        with path.open("r", newline="", encoding="utf-8-sig") as handle:
            return list(csv.DictReader(handle))
    payload = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, Mapping)]
    if isinstance(payload, Mapping):
        for key in ("campaigns", "items", "rows", "data"):
            value = payload.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, Mapping)]
    return []


def discover_active_campaign_rows_from_live_inventory(
    inventory_path: str | Path | None,
    *,
    merchant_id: str = "759051",
    store_code: str = "30137883",
) -> list[dict[str, Any]]:
    if inventory_path is None:
        return []
    path = Path(inventory_path).expanduser()
    if not path.exists():
        return []

    out: list[dict[str, Any]] = []
    for raw in _read_live_inventory_payload(path):
        if not isinstance(raw, Mapping):
            continue
        campaign_id = _first(raw, "campaign_id", "id")
        if not campaign_id:
            continue
        row_merchant_id = _first(raw, "merchant_id", "merchantId")
        row_store_code = _first(raw, "store_code", "storeCode")
        if row_merchant_id and row_merchant_id != _text(merchant_id):
            continue
        if row_store_code and row_store_code != _text(store_code):
            continue
        if not _campaign_state_is_active(_first(raw, "state", "campaign_state", "status")):
            continue
        payload = {
            str(key): value
            for key, value in raw.items()
            if key is not None and not _is_secret_field(key)
        }
        payload["campaign_id"] = campaign_id
        payload["campaign_name"] = _first(raw, "campaign_name", "name") or campaign_id
        payload["merchant_id"] = row_merchant_id or _text(merchant_id)
        payload["store_code"] = row_store_code or _text(store_code)
        payload["state"] = _first(raw, "state", "campaign_state", "status")
        payload["discovery_reason"] = "sanitized_live_campaign_inventory_active"
        payload["source_path"] = str(path)
        out.append(payload)
    out.sort(key=lambda item: int(item["campaign_id"]) if str(item["campaign_id"]).isdigit() else str(item["campaign_id"]))
    return out


def resolve_universal_marketing_scope(
    *,
    registry_path: str | Path = DEFAULT_REGISTRY_PATH,
    repo_root: Path | None = None,
    marketing_db_path: str | Path | None = None,
    include_active_discovery: bool = True,
    live_inventory_path: str | Path | None = None,
) -> dict[str, Any]:
    root = _repo_root(repo_root)
    registry = load_registry_config(registry_path, repo_root=root)
    registry_rows = build_registry_rows(registry=registry, repo_root=root)
    validate_registry_rows(registry_rows)

    registry_campaign_ids: list[str] = []
    sku_keys: list[str] = []
    configured_product_skus: list[str] = []
    for row in registry_rows:
        _append_unique(registry_campaign_ids, row.get("campaign_id"))
        for value in split_registry_field_values(row.get("sku_key")):
            _append_unique(sku_keys, value)
        for value in split_registry_field_values(row.get("configured_product_sku")):
            _append_unique(configured_product_skus, value)

    discovered_rows: list[dict[str, Any]] = []
    if include_active_discovery and marketing_db_path is not None:
        store = registry["store"]
        discovered_rows = discover_active_campaign_rows_from_marketing_db(
            marketing_db_path,
            merchant_id=_text(store.get("merchant_id")),
            store_code=_text(store.get("store_code")),
        )

    live_inventory_rows: list[dict[str, Any]] = []
    if include_active_discovery and live_inventory_path is not None:
        store = registry["store"]
        live_inventory_rows = discover_active_campaign_rows_from_live_inventory(
            live_inventory_path,
            merchant_id=_text(store.get("merchant_id")),
            store_code=_text(store.get("store_code")),
        )

    campaign_ids = list(registry_campaign_ids)
    for row in discovered_rows:
        _append_unique(campaign_ids, row.get("campaign_id"))
    for row in live_inventory_rows:
        _append_unique(campaign_ids, row.get("campaign_id"))

    discovered_campaign_ids = [_text(row.get("campaign_id")) for row in discovered_rows if _text(row.get("campaign_id"))]
    live_inventory_campaign_ids = [
        _text(row.get("campaign_id")) for row in live_inventory_rows if _text(row.get("campaign_id"))
    ]
    return {
        "store": dict(registry["store"]),
        "registry_rows": registry_rows,
        "registry_summary": summarize_registry_rows(registry_rows),
        "registry_campaign_ids": registry_campaign_ids,
        "discovered_active_campaign_rows": discovered_rows,
        "discovered_active_campaign_ids": discovered_campaign_ids,
        "discovered_active_new_campaign_ids": [
            campaign_id for campaign_id in discovered_campaign_ids if campaign_id not in registry_campaign_ids
        ],
        "live_inventory_campaign_rows": live_inventory_rows,
        "live_inventory_campaign_ids": live_inventory_campaign_ids,
        "live_inventory_new_campaign_ids": [
            campaign_id
            for campaign_id in live_inventory_campaign_ids
            if campaign_id not in registry_campaign_ids and campaign_id not in discovered_campaign_ids
        ],
        "campaign_ids": campaign_ids,
        "sku_keys": sku_keys,
        "configured_product_skus": configured_product_skus,
    }
