from __future__ import annotations

import csv
import fnmatch
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping

import yaml

from .kaspi_merchant_common import normalize_store_name


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_FLOOR_CSV = REPO_ROOT / "exports/pricelist_snapshots/min_price_floor_5pct_by_sku_v8.csv"
DEFAULT_ARTICLE_MAP = REPO_ROOT / "config/price_floor_article_map.yaml"
PRICE_FLOOR_GUARD_ID = "kaspi_price_floor_v8_5pct_guard_2026_07_06"

ARTICLE_FIELDS = (
    "SKU",
    "sku",
    "article",
    "seller_article",
    "merchant_article",
    "merchant_sku",
    "merchant_sku_article",
    "seller_sku",
    "shop_sku",
)
SKU_KEY_FIELDS = (
    "sku_key",
    "resolved_sku_key",
    "effective_sku_key",
    "stock_lookup_sku_key",
)


@dataclass(frozen=True)
class PriceFloorMatch:
    sku_key: str
    floor: int
    article: str
    match_type: str
    source: str

    @property
    def reason(self) -> str:
        return f"{PRICE_FLOOR_GUARD_ID}: {self.match_type} {self.article} -> {self.sku_key} floor {self.floor}"


def _clean_text(value: Any) -> str:
    return " ".join(str(value or "").strip().split())


def _norm(value: Any) -> str:
    return _clean_text(value).upper()


def _parse_int(value: Any) -> int:
    text = str(value or "").strip().replace(" ", "").replace(",", ".")
    if not text:
        return 0
    try:
        return int(float(text))
    except Exception:
        return 0


def _repo_path(value: str | Path | None) -> Path:
    if not value:
        return Path()
    path = Path(value)
    if path.is_absolute():
        return path
    return REPO_ROOT / path


def load_floor_csv(path: Path = DEFAULT_FLOOR_CSV) -> dict[str, int]:
    floors: dict[str, int] = {}
    with path.open("r", encoding="utf-8-sig", newline="") as fh:
        for row in csv.DictReader(fh):
            sku_key = _norm(row.get("SKU_key"))
            floor = _parse_int(row.get("floor_5pct") or row.get("Min_price_35pct"))
            if sku_key and floor > 0:
                floors[sku_key] = max(floors.get(sku_key, 0), floor)
    return floors


def load_article_map(path: Path = DEFAULT_ARTICLE_MAP) -> dict[str, Any]:
    if not path.exists():
        return {}
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ValueError(f"price floor article map must be a YAML mapping: {path}")
    return raw


class PriceFloorGuard:
    def __init__(
        self,
        *,
        floor_csv_path: Path | str | None = None,
        article_map_path: Path = DEFAULT_ARTICLE_MAP,
    ) -> None:
        self.article_map_path = article_map_path
        self.article_map = load_article_map(article_map_path)
        configured_floor_csv = self.article_map.get("floor_csv_path")
        if floor_csv_path is None and configured_floor_csv:
            self.floor_csv_path = _repo_path(configured_floor_csv)
        else:
            self.floor_csv_path = _repo_path(floor_csv_path) or DEFAULT_FLOOR_CSV
        self.floors = load_floor_csv(self.floor_csv_path)
        self.article_to_sku_key = self._load_article_to_sku_key()
        self.alias_to_sku_key = self._load_alias_to_sku_key()
        self.ambiguous_patterns = self._load_ambiguous_patterns()
        self.carve_out_stores = {
            normalize_store_name(store)
            for store in self.article_map.get("carve_outs", {}).get("stores", [])
            if _clean_text(store)
        }
        self.carve_out_sku_keys = {
            _norm(sku_key)
            for sku_key in self.article_map.get("carve_outs", {}).get("sku_keys", [])
            if _clean_text(sku_key)
        }
        self.carve_out_tokens = [
            _norm(token)
            for token in self.article_map.get("carve_outs", {}).get("article_tokens", [])
            if _clean_text(token)
        ]
        self.exception_rows = self._load_exception_rows()

    def _load_article_to_sku_key(self) -> dict[str, tuple[str, str]]:
        out: dict[str, tuple[str, str]] = {}
        for row in self.article_map.get("article_mappings", []) or []:
            if not isinstance(row, dict):
                continue
            article = _norm(row.get("article"))
            sku_key = _norm(row.get("sku_key"))
            if article and sku_key:
                out[article] = (sku_key, _clean_text(row.get("source")) or "article_map")
        return out

    def _load_alias_to_sku_key(self) -> dict[str, tuple[str, str]]:
        out: dict[str, tuple[str, str]] = {}
        for row in self.article_map.get("sku_key_aliases", []) or []:
            if not isinstance(row, dict):
                continue
            if row.get("price_write_authority") is not True:
                continue
            alias = _norm(row.get("alias"))
            sku_key = _norm(row.get("sku_key"))
            if alias and sku_key:
                out[alias] = (sku_key, _clean_text(row.get("source")) or "alias_map")
        return out

    def _load_ambiguous_patterns(self) -> list[str]:
        patterns: list[str] = []
        for row in self.article_map.get("ambiguous_article_candidates", []) or []:
            if not isinstance(row, dict):
                continue
            pattern = _norm(row.get("article_pattern"))
            if pattern:
                patterns.append(pattern)
        return patterns

    def _load_exception_rows(self) -> list[dict[str, str]]:
        rows: list[dict[str, str]] = []
        for row in self.article_map.get("scoring_exception_authority", {}).get("entries", []) or []:
            if not isinstance(row, dict):
                continue
            store = normalize_store_name(row.get("store_code", ""))
            action = _norm(row.get("action"))
            for sku_key in row.get("sku_keys", []) or []:
                rows.append(
                    {
                        "id": _clean_text(row.get("id")),
                        "store": store,
                        "action": action,
                        "sku_key": _norm(sku_key),
                    }
                )
        return rows

    def _is_carved_out(self, store_name: str, article: str, sku_key: str = "") -> bool:
        store = normalize_store_name(store_name)
        article_norm = _norm(article)
        sku_norm = _norm(sku_key)
        if store and store in self.carve_out_stores:
            return True
        if sku_norm and sku_norm in self.carve_out_sku_keys:
            return True
        if article_norm and any(token and token in article_norm for token in self.carve_out_tokens):
            return True
        if sku_norm and any(token and token in sku_norm for token in self.carve_out_tokens):
            return True
        for exception in self.exception_rows:
            if exception["action"] != "EXCLUDE_FROM_LEAK_SCORING":
                continue
            if exception["store"] and exception["store"] != store:
                continue
            if sku_norm and exception["sku_key"] == sku_norm:
                return True
            if article_norm and exception["sku_key"] and exception["sku_key"] in article_norm:
                return True
        return False

    def _match_sku_key(self, store_name: str, article: str, sku_key: str, match_type: str, source: str) -> PriceFloorMatch | None:
        sku_norm = _norm(sku_key)
        if not sku_norm or self._is_carved_out(store_name, article, sku_norm):
            return None
        floor = int(self.floors.get(sku_norm, 0))
        if floor <= 0:
            return None
        return PriceFloorMatch(
            sku_key=sku_norm,
            floor=floor,
            article=_clean_text(article) or sku_norm,
            match_type=match_type,
            source=source,
        )

    def floor_match_for_article(self, store_name: str, article: Any) -> PriceFloorMatch | None:
        article_clean = _clean_text(article)
        article_norm = _norm(article_clean)
        if not article_norm or self._is_carved_out(store_name, article_clean):
            return None
        if any(fnmatch.fnmatchcase(article_norm, pattern) for pattern in self.ambiguous_patterns):
            return None

        mapped = self.article_to_sku_key.get(article_norm)
        if mapped:
            return self._match_sku_key(store_name, article_clean, mapped[0], "article_map", mapped[1])

        alias = self.alias_to_sku_key.get(article_norm)
        if alias:
            return self._match_sku_key(store_name, article_clean, alias[0], "alias_map", alias[1])

        # Longest-prefix wins for canonical merchant articles like
        # CL_OC_MEN_LINE52_BLACK_108382478_42_(S).
        for sku_key in sorted(self.floors, key=lambda value: (-len(value), value)):
            if article_norm == sku_key or article_norm.startswith(f"{sku_key}_"):
                return self._match_sku_key(store_name, article_clean, sku_key, "sku_key_prefix", "floor_csv")
        return None

    def floor_match_for_row(self, store_name: str, row: Mapping[str, Any]) -> PriceFloorMatch | None:
        for field in SKU_KEY_FIELDS:
            sku_key = _clean_text(row.get(field, ""))
            if sku_key:
                match = self._match_sku_key(store_name, _clean_text(row.get("SKU", "")) or sku_key, sku_key, field, "row_sku_key")
                if match:
                    return match
        for field in ARTICLE_FIELDS:
            article = _clean_text(row.get(field, ""))
            if article:
                match = self.floor_match_for_article(store_name, article)
                if match:
                    return match
        return None


def default_guard() -> PriceFloorGuard:
    return PriceFloorGuard()


def floor_for_article(store_name: str, article: Any) -> int | None:
    match = default_guard().floor_match_for_article(store_name, article)
    return match.floor if match else None


def floor_for_row(store_name: str, row: Mapping[str, Any]) -> int | None:
    match = default_guard().floor_match_for_row(store_name, row)
    return match.floor if match else None


def _row_label(row: Mapping[str, Any], row_index: int, row_label_field: str) -> str:
    return _clean_text(row.get(row_label_field, "")) or _clean_text(row.get("SKU", "")) or f"row_{row_index}"


def below_floor_details(
    rows: Iterable[Mapping[str, Any]],
    *,
    store_name: str,
    price_field: str = "price",
    row_label_field: str = "SKU",
    guard: PriceFloorGuard | None = None,
) -> list[dict[str, str]]:
    active_guard = guard or default_guard()
    details: list[dict[str, str]] = []
    for row_index, row in enumerate(rows, start=1):
        match = active_guard.floor_match_for_row(store_name, row)
        if match is None:
            continue
        current_price = _parse_int(row.get(price_field))
        if current_price <= 0 or current_price >= match.floor:
            continue
        details.append(
            {
                "row_label": _row_label(row, row_index, row_label_field),
                "article": match.article,
                "sku_key": match.sku_key,
                "price_field": price_field,
                "price_before": str(current_price),
                "floor": str(match.floor),
                "match_type": match.match_type,
                "source": match.source,
                "reason": match.reason,
            }
        )
    return details


def clamp_rows_to_price_floors(
    rows: Iterable[Mapping[str, Any]],
    *,
    store_name: str,
    price_field: str = "price",
    row_label_field: str = "SKU",
    guard: PriceFloorGuard | None = None,
) -> dict[str, Any]:
    active_guard = guard or default_guard()
    out_rows: list[dict[str, Any]] = []
    clamps: list[dict[str, str]] = []
    for row_index, source_row in enumerate(rows, start=1):
        row = dict(source_row)
        match = active_guard.floor_match_for_row(store_name, row)
        current_price = _parse_int(row.get(price_field))
        if match is not None and 0 < current_price < match.floor:
            row[price_field] = str(match.floor)
            clamps.append(
                {
                    "row_label": _row_label(row, row_index, row_label_field),
                    "article": match.article,
                    "sku_key": match.sku_key,
                    "price_field": price_field,
                    "price_before": str(current_price),
                    "price_after": str(match.floor),
                    "floor": str(match.floor),
                    "match_type": match.match_type,
                    "source": match.source,
                    "reason": match.reason,
                }
            )
        out_rows.append(row)
    remaining = below_floor_details(
        out_rows,
        store_name=store_name,
        price_field=price_field,
        row_label_field=row_label_field,
        guard=active_guard,
    )
    return {"rows": out_rows, "clamps": clamps, "remaining_violations": remaining}


def write_price_floor_clamp_report(path: Path, rows: list[dict[str, Any]]) -> None:
    fieldnames = [
        "surface",
        "row_label",
        "article",
        "sku_key",
        "price_field",
        "price_before",
        "price_after",
        "floor",
        "match_type",
        "source",
        "reason",
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({key: row.get(key, "") for key in fieldnames})
