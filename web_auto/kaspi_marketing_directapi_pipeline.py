from __future__ import annotations

import csv
import json
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from .kaspi_marketing_directapi_controls import SCHEMA_VERSION as DIRECTAPI_PLAN_SCHEMA_VERSION


DEFAULT_PIPELINE_RUN_ROOT = Path("runs/kaspi_marketing_directapi_pipeline")
DEFAULT_MONITORING_RUN_ROOT = Path("runs/kaspi_marketing_monitoring")
PIPELINE_SCHEMA_VERSION = "web_auto.kaspi_marketing_directapi_pipeline.v1"
MONITORING_SCHEMA_VERSION = "web_auto.kaspi_marketing_monitoring.v1"
LIVE_APPROVAL_TEMPLATE = (
    "OWNER APPROVES KASPI MARKETING EXACT LIVE APPLY <YYYY-MM-DD>: In the verified <STORE> "
    "Kaspi Marketing account, apply only action plan <PLAN_PATH> with exact target campaign(s) "
    "<CAMPAIGN_IDS>, product SKU(s) <SKUS>, old gated values <OLD_VALUES>, and target values "
    "<TARGET_VALUES>."
)

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
)

CAMPAIGN_FIELDS = [
    "campaign_id",
    "campaign_name",
    "state",
    "daily_budget_kzt",
    "default_bid_kzt",
    "merchant_id",
    "store_code",
    "start_date",
    "record_date",
    "date",
    "views",
    "clicks",
    "favorites",
    "carts",
    "transactions",
    "cost",
    "crr",
    "target_location_ids",
    "source_path",
]

PRODUCT_FIELDS = [
    "campaign_id",
    "campaign_name",
    "campaign_state",
    "campaign_product_id",
    "product_sku",
    "merchant_sku",
    "product_name",
    "product_status",
    "bid_kzt",
    "price_kzt",
    "campaign_product_state",
    "record_date",
    "date",
    "views",
    "clicks",
    "favorites",
    "carts",
    "orders_total",
    "cost",
    "acos_share",
    "source_path",
]

CANDIDATE_FIELDS = [
    "campaign_id",
    "campaign_name",
    "campaign_state",
    "daily_budget_kzt",
    "product_sku",
    "merchant_sku",
    "product_name",
    "product_status",
    "bid_kzt",
    "price_kzt",
    "match_reasons",
]

METRIC_FIELDS = ["views", "clicks", "favorites", "carts", "transactions", "orders_total", "cost"]


class KaspiMarketingPipelineError(ValueError):
    pass


def _timestamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _write_csv(path: Path, rows: Sequence[Mapping[str, Any]], fieldnames: Sequence[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(fieldnames), extrasaction="ignore", lineterminator="\n")
        writer.writeheader()
        for row in rows:
            writer.writerow({key: row.get(key, "") for key in fieldnames})


def _is_secret_field(name: str) -> bool:
    normalized = str(name or "").strip().lower().replace("-", "_").replace(" ", "_")
    return any(needle in normalized for needle in SECRET_FIELD_NEEDLES)


def _read_csv_rows(path: Path) -> tuple[list[dict[str, str]], list[str]]:
    if not path.exists():
        raise KaspiMarketingPipelineError(f"csv_not_found:{path}")
    with path.open("r", newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames:
            return [], []
        dropped = [field for field in reader.fieldnames if _is_secret_field(field)]
        rows: list[dict[str, str]] = []
        for item in reader:
            rows.append(
                {
                    str(key): str(value or "")
                    for key, value in item.items()
                    if key is not None and not _is_secret_field(str(key))
                }
            )
    return rows, dropped


def _first(row: Mapping[str, Any], *keys: str) -> str:
    for key in keys:
        value = row.get(key)
        if value not in (None, ""):
            return str(value).strip()
    return ""


def _normalize_campaign_rows(
    rows: Sequence[Mapping[str, Any]],
    *,
    merchant_id: str,
    store_code: str,
    source_path: Path,
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for row in rows:
        out.append(
            {
                "campaign_id": _first(row, "campaign_id", "id"),
                "campaign_name": _first(row, "campaign_name", "name"),
                "state": _first(row, "state", "campaign_state", "report_state"),
                "daily_budget_kzt": _first(row, "daily_budget_kzt", "daily_budget", "dailyBudget"),
                "default_bid_kzt": _first(row, "default_bid_kzt", "default_bid", "defaultBid"),
                "merchant_id": _first(row, "merchant_id") or merchant_id,
                "store_code": _first(row, "store_code") or store_code,
                "start_date": _first(row, "start_date", "startDate"),
                "record_date": _first(row, "record_date", "record_timestamp", "ingested_at"),
                "date": _first(row, "date"),
                "views": _first(row, "views"),
                "clicks": _first(row, "clicks"),
                "favorites": _first(row, "favorites"),
                "carts": _first(row, "carts"),
                "transactions": _first(row, "transactions", "orders_total"),
                "cost": _first(row, "cost"),
                "crr": _first(row, "crr", "acos_share"),
                "target_location_ids": _first(row, "target_location_ids"),
                "source_path": str(source_path),
            }
        )
    return out


def _normalize_product_rows(
    rows: Sequence[Mapping[str, Any]],
    *,
    source_path: Path,
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for row in rows:
        out.append(
            {
                "campaign_id": _first(row, "campaign_id"),
                "campaign_name": _first(row, "campaign_name"),
                "campaign_state": _first(row, "campaign_state"),
                "campaign_product_id": _first(row, "campaign_product_id"),
                "product_sku": _first(row, "product_sku", "sku", "sku_key", "json_sku"),
                "merchant_sku": _first(row, "merchant_sku", "json_merchant_sku", "offerId"),
                "product_name": _first(row, "product_name", "name", "title"),
                "product_status": _first(row, "product_status", "state_or_row_status"),
                "bid_kzt": _first(row, "bid_kzt", "bid_cpc", "bid"),
                "price_kzt": _first(row, "price_kzt", "price"),
                "campaign_product_state": _first(row, "campaign_product_state"),
                "record_date": _first(row, "record_date", "record_timestamp", "ingested_at"),
                "date": _first(row, "date"),
                "views": _first(row, "views"),
                "clicks": _first(row, "clicks"),
                "favorites": _first(row, "favorites"),
                "carts": _first(row, "carts"),
                "orders_total": _first(row, "orders_total", "transactions"),
                "cost": _first(row, "cost"),
                "acos_share": _first(row, "acos_share", "crr"),
                "source_path": str(source_path),
            }
        )
    return out


def _matches_filters(
    joined: Mapping[str, Any],
    *,
    campaign_ids: Sequence[str],
    target_contains: Sequence[str],
    product_skus: Sequence[str],
    merchant_skus: Sequence[str],
) -> tuple[bool, list[str]]:
    reasons: list[str] = []
    if campaign_ids:
        if str(joined.get("campaign_id")) not in set(campaign_ids):
            return False, []
        reasons.append("campaign_id")
    haystack = " ".join(
        str(joined.get(key, ""))
        for key in ("campaign_name", "product_name", "product_sku", "merchant_sku")
    ).lower()
    if target_contains:
        matched = [term for term in target_contains if str(term).lower() in haystack]
        if not matched:
            return False, []
        reasons.extend([f"contains:{term}" for term in matched])
    if product_skus:
        if str(joined.get("product_sku")) not in set(product_skus):
            return False, []
        reasons.append("product_sku")
    if merchant_skus:
        if str(joined.get("merchant_sku")) not in set(merchant_skus):
            return False, []
        reasons.append("merchant_sku")
    return bool(reasons), reasons


def _candidate_rows(
    campaigns: Sequence[Mapping[str, Any]],
    products: Sequence[Mapping[str, Any]],
    *,
    campaign_ids: Sequence[str],
    target_contains: Sequence[str],
    product_skus: Sequence[str],
    merchant_skus: Sequence[str],
) -> list[dict[str, Any]]:
    by_campaign: dict[str, list[Mapping[str, Any]]] = {}
    for row in products:
        by_campaign.setdefault(str(row.get("campaign_id") or ""), []).append(row)
    candidates: list[dict[str, Any]] = []
    for campaign in campaigns:
        campaign_id = str(campaign.get("campaign_id") or "")
        product_rows = by_campaign.get(campaign_id) or [{}]
        for product in product_rows:
            joined = {
                "campaign_id": campaign_id,
                "campaign_name": campaign.get("campaign_name", ""),
                "campaign_state": campaign.get("state", ""),
                "daily_budget_kzt": campaign.get("daily_budget_kzt", ""),
                "product_sku": product.get("product_sku", ""),
                "merchant_sku": product.get("merchant_sku", ""),
                "product_name": product.get("product_name", ""),
                "product_status": product.get("product_status", ""),
                "bid_kzt": product.get("bid_kzt", ""),
                "price_kzt": product.get("price_kzt", ""),
            }
            matched, reasons = _matches_filters(
                joined,
                campaign_ids=campaign_ids,
                target_contains=target_contains,
                product_skus=product_skus,
                merchant_skus=merchant_skus,
            )
            if matched:
                candidates.append({**joined, "match_reasons": ";".join(reasons)})
    return candidates


def _build_action(
    *,
    operation: str,
    candidate: Mapping[str, Any],
    expected_current_bid: str | None,
    new_bid: str | None,
    expected_current_budget: str | None,
    new_daily_budget: str | None,
    expected_current_product_status: str | None,
    target_product_status: str | None,
    expected_current_campaign_state: str | None,
    issues: list[str],
) -> dict[str, Any] | None:
    campaign_id = str(candidate.get("campaign_id") or "").strip()
    sku = str(candidate.get("product_sku") or "").strip()
    action_id = f"{operation}_{campaign_id}"
    if operation == "product_bid":
        if not expected_current_bid:
            issues.append("missing_old_value_gate:expected_current_bid")
        if not new_bid:
            issues.append("missing_target_value:new_bid")
        if not sku:
            issues.append("missing_target_product_sku")
        if issues:
            return None
        return {
            "action_id": action_id,
            "operation": "product_bid",
            "campaign_id": campaign_id,
            "sku": sku,
            "expected_current_bid": expected_current_bid,
            "new_bid": new_bid,
        }
    if operation == "campaign_budget":
        if not expected_current_budget:
            issues.append("missing_old_value_gate:expected_current_budget")
        if not new_daily_budget:
            issues.append("missing_target_value:new_daily_budget")
        if issues:
            return None
        return {
            "action_id": action_id,
            "operation": "campaign_budget",
            "campaign_id": campaign_id,
            "expected_current_budget": expected_current_budget,
            "new_daily_budget": new_daily_budget,
        }
    if operation == "product_status":
        if not expected_current_product_status:
            issues.append("missing_old_value_gate:expected_current_product_status")
        if not target_product_status:
            issues.append("missing_target_value:target_product_status")
        if not sku:
            issues.append("missing_target_product_sku")
        if issues:
            return None
        return {
            "action_id": action_id,
            "operation": "product_status",
            "campaign_id": campaign_id,
            "sku": sku,
            "expected_current_product_status": expected_current_product_status,
            "target_product_status": target_product_status,
        }
    if operation == "campaign_resume":
        if not expected_current_campaign_state:
            issues.append("missing_old_value_gate:expected_current_campaign_state")
        if issues:
            return None
        return {
            "action_id": action_id,
            "operation": "campaign_resume",
            "campaign_id": campaign_id,
            "expected_current_campaign_state": expected_current_campaign_state,
            "target_campaign_state": "Enabled",
        }
    issues.append(f"unsupported_mapper_operation:{operation}")
    return None


def _render_account_identity(
    *,
    store: str,
    merchant_id: str,
    store_code: str,
    target_date: str,
    campaign_count: int,
    product_count: int,
) -> str:
    return "\n".join(
        [
            "# Kaspi Marketing Account Identity",
            "",
            f"- store: `{store}`",
            f"- merchant_id: `{merchant_id}`",
            f"- store_code: `{store_code}`",
            f"- target_date: `{target_date}`",
            f"- campaign_rows: `{campaign_count}`",
            f"- product_rows: `{product_count}`",
            "- live_write_authorized: `false`",
            "",
        ]
    )


def _render_mapper_closeout(summary: Mapping[str, Any]) -> str:
    return "\n".join(
        [
            "# Kaspi Marketing DirectAPI Mapper Closeout",
            "",
            f"Gate: {summary['gate']}",
            "",
            f"- status: `{summary['status']}`",
            f"- run dir: `{summary['run_dir']}`",
            f"- campaign rows: `{summary['campaign_rows']}`",
            f"- product rows: `{summary['product_rows']}`",
            f"- candidate rows: `{summary['candidate_rows']}`",
            f"- live write authorized: `{summary['live_write_authorized']}`",
            f"- secrets or tokens recorded: `{summary['secrets_or_tokens_recorded']}`",
            "",
            "## Issues",
            "",
            *(f"- `{issue}`" for issue in summary.get("issues", [])),
            "",
        ]
    )


def run_campaign_mapper(
    *,
    store: str,
    merchant_id: str,
    store_code: str,
    target_date: str,
    campaign_csv: Path,
    product_csv: Path,
    campaign_ids: Sequence[str] = (),
    target_contains: Sequence[str] = (),
    product_skus: Sequence[str] = (),
    merchant_skus: Sequence[str] = (),
    operation: str | None = None,
    operations: Sequence[str] = (),
    expected_current_bid: str | None = None,
    new_bid: str | None = None,
    expected_current_budget: str | None = None,
    new_daily_budget: str | None = None,
    expected_current_product_status: str | None = None,
    target_product_status: str | None = None,
    expected_current_campaign_state: str | None = None,
    owner_approval_text: str | None = None,
    run_root: Path = DEFAULT_PIPELINE_RUN_ROOT,
    timestamp: str | None = None,
) -> dict[str, Any]:
    run_dir = run_root / f"{timestamp or _timestamp()}_mapper"
    run_dir.mkdir(parents=True, exist_ok=False)

    raw_campaign_rows, campaign_dropped = _read_csv_rows(campaign_csv)
    raw_product_rows, product_dropped = _read_csv_rows(product_csv)
    campaigns = _normalize_campaign_rows(
        raw_campaign_rows,
        merchant_id=merchant_id,
        store_code=store_code,
        source_path=campaign_csv,
    )
    products = _normalize_product_rows(raw_product_rows, source_path=product_csv)

    issues: list[str] = []
    for row in campaigns:
        if str(row.get("merchant_id") or "") != str(merchant_id):
            issues.append(f"campaign_identity_mismatch:{row.get('campaign_id')}:merchant_id")
        if str(row.get("store_code") or "") != str(store_code):
            issues.append(f"campaign_identity_mismatch:{row.get('campaign_id')}:store_code")

    filters_supplied = bool(campaign_ids or target_contains or product_skus or merchant_skus)
    candidates = _candidate_rows(
        campaigns,
        products,
        campaign_ids=campaign_ids,
        target_contains=target_contains,
        product_skus=product_skus,
        merchant_skus=merchant_skus,
    )
    operation_list = [str(item).strip() for item in operations if str(item or "").strip()]
    if operation and not operation_list:
        operation_list = [operation]

    if operation_list and not filters_supplied:
        issues.append("operation_requires_target_filter")
    if operation_list and len(candidates) != 1:
        issues.append(f"target_uniqueness_failed:candidate_rows={len(candidates)}")

    actions: list[dict[str, Any]] = []
    if operation_list and len(candidates) == 1:
        for item in operation_list:
            action_issues: list[str] = []
            action = _build_action(
                operation=item,
                candidate=candidates[0],
                expected_current_bid=expected_current_bid,
                new_bid=new_bid,
                expected_current_budget=expected_current_budget,
                new_daily_budget=new_daily_budget,
                expected_current_product_status=expected_current_product_status,
                target_product_status=target_product_status,
                expected_current_campaign_state=expected_current_campaign_state,
                issues=action_issues,
            )
            issues.extend(action_issues)
            if action:
                actions.append(action)

    action_plan = {
        "schema_version": DIRECTAPI_PLAN_SCHEMA_VERSION,
        "pipeline_schema_version": PIPELINE_SCHEMA_VERSION,
        "store": store,
        "merchant_id": str(merchant_id),
        "store_code": str(store_code),
        "target_date": target_date,
        "source_campaign_csv": str(campaign_csv),
        "source_product_csv": str(product_csv),
        "live_write_authorized": False,
        "owner_approval_text_present": bool(owner_approval_text),
        "requires_future_live_apply_approval_phrase": LIVE_APPROVAL_TEMPLATE,
        "operations": operation_list,
        "actions": actions if actions and not issues else [],
        "candidate_rows": len(candidates),
        "issues": issues,
        "secrets_or_tokens_recorded": False,
    }
    gate = "GREEN" if not issues else "YELLOW"
    status = "mapper_ready" if gate == "GREEN" else "mapper_blocked"
    summary = {
        "schema_version": PIPELINE_SCHEMA_VERSION,
        "generated_at_local": datetime.now().isoformat(timespec="seconds"),
        "status": status,
        "gate": gate,
        "run_dir": str(run_dir),
        "store": store,
        "merchant_id": str(merchant_id),
        "store_code": str(store_code),
        "target_date": target_date,
        "campaign_rows": len(campaigns),
        "product_rows": len(products),
        "candidate_rows": len(candidates),
        "operation": operation_list[0] if len(operation_list) == 1 else ",".join(operation_list),
        "operations": operation_list,
        "live_write_authorized": False,
        "secrets_or_tokens_recorded": False,
        "secret_like_input_columns_dropped": sorted(set(campaign_dropped + product_dropped)),
        "issues": issues,
        "action_plan_path": str(run_dir / "ACTION_PLAN_DRAFT.json"),
    }

    (run_dir / "ACCOUNT_IDENTITY.md").write_text(
        _render_account_identity(
            store=store,
            merchant_id=str(merchant_id),
            store_code=str(store_code),
            target_date=target_date,
            campaign_count=len(campaigns),
            product_count=len(products),
        ),
        encoding="utf-8",
    )
    _write_csv(run_dir / "campaign_inventory.csv", campaigns, CAMPAIGN_FIELDS)
    _write_csv(run_dir / "campaign_product_rows.csv", products, PRODUCT_FIELDS)
    _write_csv(run_dir / "candidate_campaigns.csv", candidates, CANDIDATE_FIELDS)
    _write_json(run_dir / "ACTION_PLAN_DRAFT.json", action_plan)
    _write_json(run_dir / "MAPPER_SUMMARY.json", summary)
    (run_dir / "MAPPER_CLOSEOUT.md").write_text(_render_mapper_closeout(summary), encoding="utf-8")
    return summary


def _parse_date(value: str) -> date | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.strptime(text[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def _parse_datetime(value: str) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        parsed_date = _parse_date(text)
        if parsed_date:
            return datetime.combine(parsed_date, datetime.min.time())
    return None


def _comparable_datetime(value: datetime, reference: datetime) -> datetime:
    if value.tzinfo is None and reference.tzinfo is not None:
        return value.replace(tzinfo=reference.tzinfo)
    if value.tzinfo is not None and reference.tzinfo is None:
        return value.replace(tzinfo=None)
    return value


def _latest_record_datetime(
    rows: Sequence[Mapping[str, Any]],
    *,
    reference: datetime,
) -> datetime | None:
    parsed: list[datetime] = []
    for row in rows:
        for key in ("record_date", "record_timestamp", "ingested_at"):
            item = _parse_datetime(str(row.get(key) or ""))
            if item:
                parsed.append(_comparable_datetime(item, reference))
                break
    return max(parsed) if parsed else None


def _number(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(str(value).replace("\u00a0", "").replace(" ", "").replace(",", "."))
    except ValueError:
        return None


def _filter_rows(rows: Sequence[Mapping[str, Any]], campaign_ids: Sequence[str]) -> list[dict[str, Any]]:
    selected = set(str(item) for item in campaign_ids)
    return [dict(row) for row in rows if str(row.get("campaign_id")) in selected]


def _window_rows(
    *,
    windows: Sequence[str],
    source_dates: Sequence[str],
    current_date: date,
) -> list[dict[str, Any]]:
    parsed_dates = [_parse_date(item) for item in source_dates if _parse_date(item)]
    latest_source_date = max(parsed_dates).isoformat() if parsed_dates else ""
    rows: list[dict[str, Any]] = []
    for window in windows:
        window_date = _parse_date(window)
        if not parsed_dates:
            status = "missing"
            closed = "no"
        elif window_date and window_date >= current_date:
            status = "partial_same_day"
            closed = "no"
        elif window_date and window_date.isoformat() not in {item.isoformat() for item in parsed_dates}:
            status = "missing"
            closed = "no"
        else:
            status = "complete"
            closed = "yes"
        rows.append(
            {
                "window": window,
                "latest_source_date": latest_source_date,
                "closed_window": closed,
                "status": status,
            }
        )
    return rows


def _delta_rows(current_rows: Sequence[Mapping[str, Any]], baseline_rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    baseline_by_key = {
        (str(row.get("campaign_id")), str(row.get("product_sku") or "")): row for row in baseline_rows
    }
    out: list[dict[str, Any]] = []
    for row in current_rows:
        key = (str(row.get("campaign_id")), str(row.get("product_sku") or ""))
        baseline = baseline_by_key.get(key, {})
        result: dict[str, Any] = {
            "campaign_id": row.get("campaign_id", ""),
            "product_sku": row.get("product_sku", ""),
            "baseline_available": "yes" if baseline else "no",
        }
        for field in METRIC_FIELDS:
            current_value = _number(row.get(field))
            baseline_value = _number(baseline.get(field))
            result[f"current_{field}"] = "" if current_value is None else current_value
            result[f"baseline_{field}"] = "" if baseline_value is None else baseline_value
            result[f"delta_{field}"] = (
                "" if current_value is None or baseline_value is None else current_value - baseline_value
            )
        out.append(result)
    return out


def _render_monitoring_recommendation(summary: Mapping[str, Any]) -> str:
    warnings = summary.get("warnings", [])
    candidates = summary.get("owner_decision_candidates", [])
    lines = [
        "# Kaspi Marketing Monitoring Recommendation",
        "",
        f"Gate: {summary['gate']}",
        "",
        "## Read-Only Measured Facts",
        "",
        f"- campaign rows: `{summary['campaign_rows']}`",
        f"- product rows: `{summary['product_rows']}`",
        f"- campaign IDs: `{', '.join(summary['campaign_ids'])}`",
        "",
        "## Stale Or Partial Window Warnings",
        "",
    ]
    lines.extend([f"- `{item}`" for item in warnings] or ["- `none`"])
    lines.extend(["", "## Owner Decision Candidates", ""])
    lines.extend([f"- {item}" for item in candidates] or ["- No live change candidate is approved by this packet."])
    lines.extend(
        [
            "",
            "## Future Approval Required",
            "",
            "- No bid, budget, campaign-state, product-state, price, stock, Repricer, Meta, CRM, scheduler, or database change is authorized by this recommendation.",
            f"- Future live changes require an exact approval phrase such as: `{LIVE_APPROVAL_TEMPLATE}`",
            "",
        ]
    )
    return "\n".join(lines)


def run_monitoring_packet(
    *,
    store: str,
    merchant_id: str,
    store_code: str,
    campaign_ids: Sequence[str],
    change_timestamp: str,
    campaign_csv: Path,
    product_csv: Path,
    windows: Sequence[str] = (),
    baseline_campaign_csv: Path | None = None,
    baseline_product_csv: Path | None = None,
    run_root: Path = DEFAULT_MONITORING_RUN_ROOT,
    timestamp: str | None = None,
    current_date: date | None = None,
) -> dict[str, Any]:
    if not campaign_ids:
        raise KaspiMarketingPipelineError("at_least_one_campaign_id_required")
    scope = "_".join(str(item) for item in campaign_ids[:3])
    run_dir = run_root / f"{timestamp or _timestamp()}_{scope}"
    run_dir.mkdir(parents=True, exist_ok=False)

    raw_campaign_rows, campaign_dropped = _read_csv_rows(campaign_csv)
    raw_product_rows, product_dropped = _read_csv_rows(product_csv)
    campaigns = _filter_rows(
        _normalize_campaign_rows(raw_campaign_rows, merchant_id=merchant_id, store_code=store_code, source_path=campaign_csv),
        campaign_ids,
    )
    products = _filter_rows(_normalize_product_rows(raw_product_rows, source_path=product_csv), campaign_ids)
    baseline_products: list[dict[str, Any]] = []
    if baseline_product_csv:
        raw_baseline_products, _ = _read_csv_rows(baseline_product_csv)
        baseline_products = _filter_rows(_normalize_product_rows(raw_baseline_products, source_path=baseline_product_csv), campaign_ids)
    elif baseline_campaign_csv:
        raw_baseline_campaigns, _ = _read_csv_rows(baseline_campaign_csv)
        baseline_products = _filter_rows(
            _normalize_campaign_rows(
                raw_baseline_campaigns,
                merchant_id=merchant_id,
                store_code=store_code,
                source_path=baseline_campaign_csv,
            ),
            campaign_ids,
        )

    source_dates = [str(row.get("date") or "") for row in campaigns if row.get("date")]
    requested_windows = list(windows) if windows else [change_timestamp[:10]]
    completeness_rows = _window_rows(
        windows=requested_windows,
        source_dates=source_dates,
        current_date=current_date or date.today(),
    )
    warnings = [
        f"{row['window']}:{row['status']}"
        for row in completeness_rows
        if row["status"] != "complete"
    ]
    if not campaigns:
        warnings.append("missing_campaign_rows")
    if not products:
        warnings.append("missing_product_rows")
    change_dt = _parse_datetime(change_timestamp)
    latest_record_dt = _latest_record_datetime([*campaigns, *products], reference=change_dt) if change_dt else None
    if not change_dt:
        warnings.append("change_timestamp_unparseable")
    else:
        comparable_change_dt = _comparable_datetime(change_dt, change_dt)
        if campaigns or products:
            if latest_record_dt is None:
                warnings.append("post_change_record_timestamp_missing")
            elif latest_record_dt < comparable_change_dt:
                warnings.append(
                    "post_change_record_stale:"
                    f"latest_record={latest_record_dt.isoformat()}:"
                    f"change_timestamp={comparable_change_dt.isoformat()}"
                )

    gate = "GREEN" if not warnings else "YELLOW"
    summary = {
        "schema_version": MONITORING_SCHEMA_VERSION,
        "generated_at_local": datetime.now().isoformat(timespec="seconds"),
        "status": "monitoring_ready" if gate == "GREEN" else "monitoring_needs_review",
        "gate": gate,
        "run_dir": str(run_dir),
        "store": store,
        "merchant_id": str(merchant_id),
        "store_code": str(store_code),
        "campaign_ids": [str(item) for item in campaign_ids],
        "change_timestamp": change_timestamp,
        "latest_record_timestamp": latest_record_dt.isoformat() if latest_record_dt else "",
        "windows": requested_windows,
        "campaign_rows": len(campaigns),
        "product_rows": len(products),
        "warnings": warnings,
        "owner_decision_candidates": [
            "Review spend, clicks, orders, and cost deltas before preparing any owner-gated next action."
        ],
        "live_write_authorized": False,
        "secrets_or_tokens_recorded": False,
        "secret_like_input_columns_dropped": sorted(set(campaign_dropped + product_dropped)),
    }

    _write_csv(run_dir / "monitoring_campaign_metrics.csv", campaigns, CAMPAIGN_FIELDS)
    _write_csv(run_dir / "monitoring_product_metrics.csv", products, PRODUCT_FIELDS)
    _write_csv(run_dir / "window_completeness.csv", completeness_rows, ["window", "latest_source_date", "closed_window", "status"])
    delta_fields = ["campaign_id", "product_sku", "baseline_available"]
    for field in METRIC_FIELDS:
        delta_fields.extend([f"current_{field}", f"baseline_{field}", f"delta_{field}"])
    _write_csv(run_dir / "before_after_or_baseline_delta.csv", _delta_rows(products or campaigns, baseline_products), delta_fields)
    _write_json(run_dir / "MONITORING_SUMMARY.json", summary)
    (run_dir / "MONITORING_RECOMMENDATION.md").write_text(
        _render_monitoring_recommendation(summary),
        encoding="utf-8",
    )
    return summary
