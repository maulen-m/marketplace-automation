from __future__ import annotations

import csv
import json
import re
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd
import yaml

from .kaspi_pricelist_ops import (
    TEMPLATE_COLUMNS,
    WAREHOUSE_COLUMNS,
    _write_workbook_atomic,
    is_no_like,
    parse_int,
)


DEFAULT_CALENDAR_PATH = Path("config/experiments/acmewear_bundles_experiment_calendar.yaml")
DEFAULT_REGISTRY_PATH = Path("config/experiments/acmewear_bundles_registry.yaml")
DEFAULT_CAPTURE_PATH = Path("exports/acmewear_bundle_publication_capture/20260427_104746/bundle_publication_capture.csv")
DEFAULT_STOCK_WAREHOUSE = "PP1"
DEFAULT_PLATFORM_FACADE_STOCK = 500
DEFAULT_INTERNAL_STOCK_POLICY = "real_snapshot_draw_no_tiny_cap"
EXPECTED_WAVE2_LANDED_ST_ROWS = 80
DEFAULT_CANONICAL_SIZE_TOKENS = {
    "S": "S-42",
    "M": "M-44",
    "L": "L-46",
    "XL": "XL-50",
    "2XL": "2XL-52",
    "3XL": "3XL-54",
    "4XL": "4XL-56",
}


class BundleActivationError(RuntimeError):
    """Raised when bundle activation would not be safe to materialize."""


@dataclass(frozen=True)
class BundleLaunchRule:
    bundle_id: str
    child_bundle_id: str
    parent_family: str
    initial_price_kzt: int
    launch_mode: str
    stock_cap_by_size: dict[str, int]


def parse_stock_cap_string(value: str) -> dict[str, int]:
    out: dict[str, int] = {}
    for part in str(value or "").strip().split("/"):
        token = part.strip()
        if not token:
            continue
        match = re.fullmatch(r"([A-Z0-9]+)(\d+)", token)
        if not match:
            raise BundleActivationError(f"invalid_stock_cap_token:{token}")
        out[match.group(1)] = int(match.group(2))
    return out


def _read_yaml(path: Path) -> dict[str, Any]:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _read_capture(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def _load_template_sheet(path: Path) -> tuple[pd.DataFrame, pd.DataFrame]:
    l1 = pd.read_excel(path, sheet_name="Лист1", dtype=str).fillna("")
    l2 = pd.read_excel(path, sheet_name="Лист2", dtype=str).fillna("")
    for column in TEMPLATE_COLUMNS:
        if column not in l1.columns:
            raise BundleActivationError(f"missing_template_column:{path}:{column}")
    return l1[TEMPLATE_COLUMNS].copy(), l2.copy()


def _build_sku_index(df: pd.DataFrame, *, state: str) -> dict[str, pd.Series]:
    counts = Counter(str(value or "").strip() for value in df["SKU"])
    duplicate_skus = sorted(sku for sku, count in counts.items() if sku and count > 1)
    if duplicate_skus:
        raise BundleActivationError(f"duplicate_{state.lower()}_sku_rows:{','.join(duplicate_skus[:10])}")
    return {
        str(row.get("SKU", "") or "").strip(): row
        for _, row in df.iterrows()
        if str(row.get("SKU", "") or "").strip()
    }


def _load_launch_rules(calendar_path: Path, registry_path: Path) -> dict[str, BundleLaunchRule]:
    calendar = _read_yaml(calendar_path)
    registry = _read_yaml(registry_path)
    registry_by_bundle = {row["bundle_id"]: row for row in registry["bundle_registry"]}
    rules: dict[str, BundleLaunchRule] = {}
    for row in calendar["bundle_launch_rows"]:
        bundle_id = str(row["bundle_id"])
        child_bundle_id = str(row["stage2_sku_key"])
        registry_row = registry_by_bundle.get(bundle_id)
        if not registry_row:
            raise BundleActivationError(f"calendar_bundle_missing_registry:{bundle_id}")
        if str(registry_row.get("child_bundle_id")) != child_bundle_id:
            raise BundleActivationError(f"calendar_registry_child_mismatch:{bundle_id}")
        rules[child_bundle_id] = BundleLaunchRule(
            bundle_id=bundle_id,
            child_bundle_id=child_bundle_id,
            parent_family=str(row["parent_family"]),
            initial_price_kzt=int(row["initial_price_kzt"]),
            launch_mode=str(row["launch_mode"]),
            stock_cap_by_size=parse_stock_cap_string(str(row["stock_cap_by_size"])),
        )
    return rules


def _has_positive_stock(row: pd.Series) -> bool:
    return any(parse_int(row.get(column, "")) > 0 for column in WAREHOUSE_COLUMNS)


def _has_no_price_or_stock(row: pd.Series) -> bool:
    return parse_int(row.get("price", "")) == 0 and not _has_positive_stock(row)


def _normalize_archive_no_stock(row: pd.Series) -> dict[str, Any]:
    out = {column: str(row.get(column, "") or "") for column in TEMPLATE_COLUMNS}
    for column in WAREHOUSE_COLUMNS:
        out[column] = "no"
    return out


def _activate_row(row: pd.Series, *, price: int, stock_warehouse: str, stock_value: int) -> dict[str, Any]:
    out = {column: str(row.get(column, "") or "") for column in TEMPLATE_COLUMNS}
    out["price"] = int(price)
    for column in WAREHOUSE_COLUMNS:
        out[column] = int(stock_value) if column == stock_warehouse else "no"
    out["preorder"] = int(parse_int(out.get("preorder", "")))
    return out


def _write_csv(path: Path, rows: list[dict[str, Any]], fieldnames: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def _write_review_markdown(path: Path, *, summary: dict[str, Any], review_rows: list[dict[str, Any]]) -> None:
    active_preview = [row for row in review_rows if row["planned_state"] == "ACTIVE"][:12]
    lines = [
        "# ACMEWEAR Bundle ST Activation Review",
        "",
        f"- Generated: {summary['generated_at_local']}",
        f"- Status: {summary['status']}",
        f"- Store: {summary['store']}",
        f"- Scope: landed ST card groups only; TRM/tank deferred rows excluded.",
        f"- Rows to activate: {summary['rows_to_activate']}",
        f"- Captured rows kept archive/no-stock: {summary['captured_rows_kept_archive_no_stock']}",
        f"- Deferred rows excluded: {summary['deferred_rows_excluded']}",
        f"- Platform facade stock: PP1={summary['platform_facade_pp1']}",
        f"- Internal stock policy: {summary['internal_stock_policy']}",
        f"- Repricer required: {'yes' if summary['repricer_required'] else 'no'}",
        "",
        "## Open Gates",
        "",
    ]
    if summary["open_gates"]:
        lines.extend(f"- {gate}" for gate in summary["open_gates"])
    else:
        lines.append("- none recorded by builder; owner approval is still required before live upload")
    lines.extend(
        [
            "",
            "## Active Row Preview",
            "",
            "| merchant_article | child_bundle_id | size | token | price | PP1 | reason |",
            "|---|---|---:|---|---:|---:|---|",
        ]
    )
    for row in active_preview:
        lines.append(
            "| {merchant_article} | {child_bundle_id} | {internal_size} | {kaspi_size_token} | {planned_price} | {planned_pp1} | {classification_reason} |".format(
                **row
            )
        )
    lines.extend(
        [
            "",
            "## Safety Notes",
            "",
            "- This pack does not upload anything.",
            "- ACMEWEAR is explicitly outside the Repricer red-flag/dumping workflow.",
            "- Wave 2 activates every landed ST platform row, including alias platform tokens such as XL-48 and 4XL variants.",
            "- Old calendar stock-cap values are not used as platform stock quantities; launched rows use PP1=500.",
            "- TRM/tank rows remain excluded; no missing rows are invented into upload workbooks.",
        ]
    )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def build_acmewear_bundle_activation_pack(
    *,
    active_path: Path,
    archive_path: Path,
    calendar_path: Path = DEFAULT_CALENDAR_PATH,
    registry_path: Path = DEFAULT_REGISTRY_PATH,
    capture_path: Path = DEFAULT_CAPTURE_PATH,
    output_dir: Path,
    image_moderation_cleared: bool = False,
    stock_warehouse: str = DEFAULT_STOCK_WAREHOUSE,
    canonical_size_tokens: dict[str, str] | None = None,
) -> dict[str, Any]:
    if stock_warehouse not in WAREHOUSE_COLUMNS:
        raise BundleActivationError(f"invalid_stock_warehouse:{stock_warehouse}")

    canonical_tokens = dict(DEFAULT_CANONICAL_SIZE_TOKENS)
    if canonical_size_tokens:
        canonical_tokens.update(canonical_size_tokens)

    active_df, active_l2 = _load_template_sheet(active_path)
    archive_df, archive_l2 = _load_template_sheet(archive_path)
    active_index = _build_sku_index(active_df, state="ACTIVE")
    archive_index = _build_sku_index(archive_df, state="ARCHIVE")
    rules = _load_launch_rules(calendar_path, registry_path)
    capture_rows = _read_capture(capture_path)

    captured_st_rows = [
        row
        for row in capture_rows
        if row.get("captured") == "yes"
        and row.get("category_code") == "ST"
        and row.get("child_bundle_id") in rules
    ]
    deferred_rows = [row for row in capture_rows if row.get("captured") == "no"]
    if len(captured_st_rows) != EXPECTED_WAVE2_LANDED_ST_ROWS:
        raise BundleActivationError(f"expected_80_landed_st_rows:{len(captured_st_rows)}")

    duplicated_selected = [
        row["merchant_article"]
        for row in captured_st_rows
        if row["merchant_article"] in active_index and row["merchant_article"] in archive_index
    ]
    if duplicated_selected:
        raise BundleActivationError(f"duplicate_active_archive_target:{','.join(duplicated_selected[:10])}")

    missing_selected = [
        row["merchant_article"]
        for row in captured_st_rows
        if row["merchant_article"] not in active_index and row["merchant_article"] not in archive_index
    ]
    if missing_selected:
        raise BundleActivationError(f"missing_selected_rows:{','.join(missing_selected[:10])}")

    activated_skus: set[str] = set()
    active_activation_by_sku: dict[str, dict[str, Any]] = {}
    archive_activation_by_sku: dict[str, dict[str, Any]] = {}
    review_rows: list[dict[str, Any]] = []

    for capture_row in captured_st_rows:
        sku = capture_row["merchant_article"]
        rule = rules[capture_row["child_bundle_id"]]
        if sku in active_index:
            source_state = "ACTIVE"
            source_row = active_index[sku]
            if not _has_no_price_or_stock(source_row):
                source_price = parse_int(source_row.get("price", ""))
                raise BundleActivationError(f"unexpected_active_price_stock:{sku}:price={source_price}")
        else:
            source_state = "ARCHIVE"
            source_row = archive_index[sku]
            if _has_positive_stock(source_row):
                raise BundleActivationError(f"unexpected_archive_stock:{sku}")
            source_price = parse_int(source_row.get("price", ""))
            if source_price not in {0, rule.initial_price_kzt}:
                raise BundleActivationError(f"unexpected_archive_price:{sku}:{source_price}")
        internal_size = capture_row["internal_size"]
        kaspi_size_token = capture_row["kaspi_size_token"]
        canonical_token = canonical_tokens.get(internal_size, "")
        if not canonical_token:
            raise BundleActivationError(f"missing_canonical_size_token:{internal_size}")
        stock_cap = rule.stock_cap_by_size.get(internal_size)
        if stock_cap is None:
            raise BundleActivationError(f"missing_stock_cap:{rule.child_bundle_id}:{internal_size}")
        activated_skus.add(sku)
        activation_payload = {
            "price": rule.initial_price_kzt,
            "stock_warehouse": stock_warehouse,
            "stock_value": DEFAULT_PLATFORM_FACADE_STOCK,
        }
        if source_state == "ACTIVE":
            active_activation_by_sku[sku] = activation_payload
        else:
            archive_activation_by_sku[sku] = activation_payload
        reason = "owner_approved_wave2_landed_st_platform_row"
        review_rows.append(
            {
                "card_group_key": f"{capture_row['parent_family']}|{capture_row['child_bundle_id']}|ST",
                "parent_family": capture_row["parent_family"],
                "bundle_id": rule.bundle_id,
                "child_bundle_id": rule.child_bundle_id,
                "launch_mode": rule.launch_mode,
                "merchant_article": sku,
                "internal_size": internal_size,
                "kaspi_size_token": kaspi_size_token,
                "canonical_size_token": canonical_token,
                "calendar_stock_hint": stock_cap,
                "internal_stock_available": "owner_approved_sellable_not_calendar_gated",
                "internal_stock_policy": DEFAULT_INTERNAL_STOCK_POLICY,
                "source_state": source_state,
                "planned_state": "ACTIVE",
                "planned_price": rule.initial_price_kzt,
                "platform_facade_pp1": DEFAULT_PLATFORM_FACADE_STOCK if stock_warehouse == "PP1" else "",
                "planned_pp1": DEFAULT_PLATFORM_FACADE_STOCK if stock_warehouse == "PP1" else "",
                "stock_warehouse": stock_warehouse,
                "classification_reason": reason,
            }
        )

    if len(activated_skus) != EXPECTED_WAVE2_LANDED_ST_ROWS:
        raise BundleActivationError(f"rows_to_activate_not_80:{len(activated_skus)}")
    planned_archive = [row["merchant_article"] for row in review_rows if row["planned_state"] != "ACTIVE"]
    if planned_archive:
        raise BundleActivationError(f"landed_st_rows_not_active:{','.join(planned_archive[:10])}")
    bad_launched = [
        row["merchant_article"]
        for row in review_rows
        if str(row.get("planned_price", "") or "").strip() == ""
        or str(row.get("planned_pp1", "") or "").strip() != str(DEFAULT_PLATFORM_FACADE_STOCK)
    ]
    if bad_launched:
        raise BundleActivationError(f"invalid_wave2_launched_row_values:{','.join(bad_launched[:10])}")

    output_dir.mkdir(parents=True, exist_ok=True)

    selected_skus = {row["merchant_article"] for row in captured_st_rows}
    final_active_rows: list[dict[str, Any]] = []
    for _, row in active_df.iterrows():
        sku = str(row.get("SKU", "") or "").strip()
        activation_payload = active_activation_by_sku.get(sku)
        if activation_payload:
            final_active_rows.append(
                _activate_row(
                    row,
                    price=int(activation_payload["price"]),
                    stock_warehouse=str(activation_payload["stock_warehouse"]),
                    stock_value=int(activation_payload["stock_value"]),
                )
            )
        else:
            final_active_rows.append({column: str(row.get(column, "") or "") for column in TEMPLATE_COLUMNS})
    final_archive_rows: list[dict[str, Any]] = []
    for _, row in archive_df.iterrows():
        sku = str(row.get("SKU", "") or "").strip()
        if sku in activated_skus:
            continue
        if sku in selected_skus:
            final_archive_rows.append(_normalize_archive_no_stock(row))
        else:
            final_archive_rows.append({column: str(row.get(column, "") or "") for column in TEMPLATE_COLUMNS})

    for review in review_rows:
        if review["planned_state"] != "ACTIVE":
            continue
        if review["source_state"] != "ARCHIVE":
            continue
        source_row = archive_index[review["merchant_article"]]
        activation_payload = archive_activation_by_sku[review["merchant_article"]]
        final_active_rows.append(
            _activate_row(
                source_row,
                price=int(activation_payload["price"]),
                stock_warehouse=str(activation_payload["stock_warehouse"]),
                stock_value=int(activation_payload["stock_value"]),
            )
        )

    active_out = output_dir / "acmewear_bundle_upload_ACTIVE.xlsx"
    archive_out = output_dir / "acmewear_bundle_upload_ARCHIVE.xlsx"
    diff_out = output_dir / "acmewear_bundle_activation_diff.csv"
    stoplines_out = output_dir / "acmewear_bundle_activation_open_gates.csv"
    summary_out = output_dir / "acmewear_bundle_activation_summary.json"
    review_md = output_dir / "acmewear_bundle_activation_review.md"

    _write_workbook_atomic(active_out, pd.DataFrame(final_active_rows, columns=TEMPLATE_COLUMNS), active_l2)
    _write_workbook_atomic(archive_out, pd.DataFrame(final_archive_rows, columns=TEMPLATE_COLUMNS), archive_l2)

    review_fieldnames = [
        "card_group_key",
        "parent_family",
        "bundle_id",
        "child_bundle_id",
        "launch_mode",
        "merchant_article",
        "internal_size",
        "kaspi_size_token",
        "canonical_size_token",
        "calendar_stock_hint",
        "internal_stock_available",
        "internal_stock_policy",
        "source_state",
        "planned_state",
        "planned_price",
        "platform_facade_pp1",
        "planned_pp1",
        "stock_warehouse",
        "classification_reason",
    ]
    _write_csv(diff_out, review_rows, review_fieldnames)

    open_gates = []
    if not image_moderation_cleared:
        open_gates.append("image_moderation_not_live_verified")
    open_gates.append("owner_live_upload_approval_required")
    stopline_rows = [{"gate": gate, "status": "open"} for gate in open_gates]
    _write_csv(stoplines_out, stopline_rows, ["gate", "status"])

    status = "review_ready" if image_moderation_cleared else "review_ready_with_open_gates"
    card_groups = sorted({row["card_group_key"] for row in review_rows})
    summary: dict[str, Any] = {
        "status": status,
        "store": "ACMEWEAR",
        "generated_at_local": datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %z"),
        "active_source_path": str(active_path),
        "archive_source_path": str(archive_path),
        "calendar_path": str(calendar_path),
        "registry_path": str(registry_path),
        "capture_path": str(capture_path),
        "stock_warehouse": stock_warehouse,
        "platform_facade_pp1": DEFAULT_PLATFORM_FACADE_STOCK,
        "internal_stock_policy": DEFAULT_INTERNAL_STOCK_POLICY,
        "stock_quantity_policy": "calendar_stock_cap_by_size_not_used_as_platform_quantity",
        "repricer_required": False,
        "landed_st_card_groups": len(card_groups),
        "captured_rows": len(captured_st_rows),
        "rows_to_activate": len(activated_skus),
        "captured_rows_kept_archive_no_stock": len(captured_st_rows) - len(activated_skus),
        "deferred_rows_excluded": len(deferred_rows),
        "active_output": str(active_out),
        "archive_output": str(archive_out),
        "diff_output": str(diff_out),
        "open_gates_output": str(stoplines_out),
        "review_markdown": str(review_md),
        "summary_output": str(summary_out),
        "open_gates": open_gates,
        "owner_approval_required": True,
        "live_upload_allowed_by_builder": False,
        "card_groups": card_groups,
    }
    _write_review_markdown(review_md, summary=summary, review_rows=review_rows)
    summary_out.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    return summary
