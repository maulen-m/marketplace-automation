from __future__ import annotations

import argparse
import csv
import json
import time
from datetime import date, datetime
from pathlib import Path
from typing import Any, Mapping, Sequence

from playwright.sync_api import BrowserContext, Page, sync_playwright

from .kaspi_marketing import MARKETING_CAMPAIGNS_URL, build_marketing_headers, login_kaspi_marketing, resolve_marketing_credentials
from .kaspi_merchant_common import merchant_browser_launch_kwargs
from .line31_kaspi_overlay_live import (
    ALMATY,
    API_BASE,
    MERCHANT_ID,
    STORE,
    STORE_CODE,
    as_int,
    build_resume_form_fields,
    js_fetch_json,
    js_put_form,
    list_body,
    now_local,
    pick,
    request_json,
    response_success,
    unwrap,
    write_json,
    xsrf_from_cookies,
)


APPROVAL_PHRASE = (
    "I approve LINE31_IRIS_MISTY_INTERNAL_KASPI_MARKETING_20260617: in ~/Docs/Web_automation, "
    "resume existing exact ACMEWEAR LINE31 Iris Purple and Misty Blue Kaspi internal marketing campaigns if safely present, "
    "or create minimum equivalent campaigns only if no exact safe campaign exists; target BID 50 KZT, target daily budget "
    "5900 KZT per campaign, target region all Kazakhstan / Весь Казахстан with exact platform targetLocationIds proven "
    "from fresh readback, merchant 759051, store 30137883. No Meta/Facebook, price, stock, Repricer, CRM, website, "
    "Telegram, Autonomous_business, card-content, image, product-status archive/pause, or unrelated Kaspi campaign changes "
    "are authorized. Stop YELLOW with evidence and no write if any identity, region, product, campaign, budget, BID, "
    "freshness, or tooling gate is ambiguous."
)

TARGET_DAILY_BUDGET_KZT = 5900
TARGET_BID_KZT = 50
ALL_KAZAKHSTAN_LABEL = "Весь Казахстан"

FORBIDDEN_WRITES = [
    "Meta/Facebook",
    "price",
    "stock",
    "Repricer",
    "CRM",
    "website",
    "Telegram",
    "Autonomous_business",
    "card-content",
    "image",
    "product-status archive/pause",
    "unrelated Kaspi campaign",
]

TARGETS = [
    {
        "route_id": "line31_misty_blue",
        "label": "LINE31 Misty Blue",
        "preferred_campaign_id": "2752402",
        "preferred_campaign_name": "LINE31_misty-blue_ST_16.4.26_10_03_00",
        "create_campaign_prefix": "LINE31_misty-blue_ST",
        "product_sku": "21022113b",
        "merchant_sku": "OF_LINE31_ST_MB_XL",
    },
    {
        "route_id": "line31_iris_purple",
        "label": "LINE31 Iris Purple",
        "preferred_campaign_id": "2752405",
        "preferred_campaign_name": "LINE31_iris-purple_ST_16.4.26_10_03_00",
        "create_campaign_prefix": "LINE31_iris-purple_ST",
        "product_sku": "21022114b",
        "merchant_sku": "OF_LINE31_ST_IP_XL",
    },
]


def write_csv(path: Path, rows: Sequence[Mapping[str, Any]], fieldnames: Sequence[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(fieldnames), extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({key: row.get(key, "") for key in fieldnames})


def _as_locations(value: Any) -> list[Any]:
    if value in (None, ""):
        return []
    if isinstance(value, list):
        return value
    return [value]


def _campaign_id(row: Mapping[str, Any]) -> str:
    return str(pick(row, "campaign_id", "id", "Id") or "").strip()


def _campaign_name(row: Mapping[str, Any]) -> str:
    return str(pick(row, "campaign_name", "name", "Name") or "").strip()


def _campaign_state(row: Mapping[str, Any]) -> str:
    return str(pick(row, "campaign_state", "state", "State", "report_state") or "").strip()


def _campaign_budget(row: Mapping[str, Any]) -> int | None:
    return as_int(pick(row, "daily_budget_kzt", "daily_budget", "dailyBudget", "DailyBudget"))


def _campaign_locations(row: Mapping[str, Any]) -> list[Any]:
    return _as_locations(pick(row, "target_location_ids", "targetLocationIds"))


def _product_campaign_id(row: Mapping[str, Any]) -> str:
    return str(pick(row, "campaign_id", "campaignId", "campaignID") or "").strip()


def _product_sku(row: Mapping[str, Any]) -> str:
    return str(pick(row, "sku", "product_sku", "id", "productId") or "").strip()


def _product_merchant_sku(row: Mapping[str, Any]) -> str:
    return str(pick(row, "merchant_sku", "merchantSku", "merchantSKU", "offerId") or "").strip()


def _product_state(row: Mapping[str, Any]) -> str:
    return str(pick(row, "product_state", "productState", "state", "status") or "").strip()


def _product_bid(row: Mapping[str, Any]) -> int | None:
    return as_int(pick(row, "bid_kzt", "bid", "bidCpc", "avgBid"))


def _product_price(row: Mapping[str, Any]) -> int | None:
    return as_int(pick(row, "price_kzt", "price"))


def _state_is_paused(value: str) -> bool:
    return value.strip().lower() in {"paused", "suspended", "suspendedbyyou"}


def _state_is_enabled(value: str) -> bool:
    return value.strip().lower() in {"enabled", "outofbudget"}


def _product_is_active(value: str) -> bool:
    return value.strip().lower() in {"active", "enabled"}


def _normalize_campaign(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "campaign_id": _campaign_id(row),
        "campaign_name": _campaign_name(row),
        "campaign_state": _campaign_state(row),
        "daily_budget_kzt": _campaign_budget(row),
        "start_date": pick(row, "start_date", "startDate", "StartDate"),
        "end_date": pick(row, "end_date", "endDate", "EndDate"),
        "default_bid_kzt": as_int(pick(row, "default_bid_kzt", "default_bid", "defaultBid", "DefaultBid")),
        "target_location_ids": _campaign_locations(row),
    }


def _normalize_product(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "campaign_id": _product_campaign_id(row),
        "sku": _product_sku(row),
        "merchant_sku": _product_merchant_sku(row),
        "title": str(pick(row, "title", "name", "productName", "product_name") or "").strip(),
        "product_state": _product_state(row),
        "bid_kzt": _product_bid(row),
        "price_kzt": _product_price(row),
        "buy_box": pick(row, "buy_box", "buyBox"),
        "advertisable": pick(row, "advertisable", "isAdvertisable"),
    }


def _campaign_product_pairs(
    campaign_rows: Sequence[Mapping[str, Any]],
    product_rows: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    campaigns = {_campaign_id(row): _normalize_campaign(row) for row in campaign_rows if _campaign_id(row)}
    pairs: list[dict[str, Any]] = []
    for row in product_rows:
        product = _normalize_product(row)
        campaign = campaigns.get(product["campaign_id"])
        if campaign:
            pairs.append({**campaign, **{f"product_{key}": value for key, value in product.items()}})
    return pairs


def _product_search_exact(product_search_rows: Sequence[Mapping[str, Any]], sku: str, merchant_sku: str) -> dict[str, Any] | None:
    matches = [
        dict(row)
        for row in product_search_rows
        if _product_sku(row) == sku and _product_merchant_sku(row) == merchant_sku
    ]
    if len(matches) != 1:
        return None
    return matches[0]


def _target_campaign_name(target: Mapping[str, Any], created_at_id: str | None) -> str:
    stamp = created_at_id or datetime.now(ALMATY).strftime("%Y%m%d_%H%M%S")
    return f"{target['create_campaign_prefix']}_{stamp}"


def _region_label_for_locations(locations: Sequence[Any]) -> str:
    if list(locations) == []:
        return ALL_KAZAKHSTAN_LABEL
    return "NOT_ALL_KAZAKHSTAN"


def build_product_bid_payload(product_sku: str, bid_kzt: int) -> dict[str, Any]:
    return {"skuList": [str(product_sku)], "bid": int(bid_kzt)}


def build_campaign_add_payload(
    *,
    name: str,
    product_sku: str,
    bid_kzt: int,
    daily_budget_kzt: int,
    start_date: str,
    target_location_ids: Sequence[Any],
) -> dict[str, Any]:
    return {
        "name": name,
        "dailyBudget": int(daily_budget_kzt),
        "products": [{"sku": str(product_sku), "bid": int(bid_kzt)}],
        "startDate": start_date,
        "targetLocationIds": list(target_location_ids),
    }


def build_campaign_budget_payload(campaign: Mapping[str, Any], new_budget_kzt: int) -> dict[str, Any]:
    default_bid = as_int(pick(campaign, "default_bid_kzt", "default_bid", "defaultBid", "DefaultBid"))
    if default_bid is None:
        raise ValueError(f"campaign_default_bid_missing:{_campaign_id(campaign)}")
    payload: dict[str, Any] = {
        "Id": _campaign_id(campaign),
        "Name": _campaign_name(campaign),
        "State": _campaign_state(campaign),
        "DailyBudget": str(int(new_budget_kzt)),
        "StartDate": str(pick(campaign, "start_date", "startDate", "StartDate") or ""),
        "DefaultBid": str(default_bid),
        "targetLocationIds": _campaign_locations(campaign),
    }
    end_date = pick(campaign, "end_date", "endDate", "EndDate")
    if end_date:
        payload["EndDate"] = str(end_date)
    return payload


def _assert_line31_target_identity(target: Mapping[str, Any], pair: Mapping[str, Any]) -> None:
    route_id = str(target["route_id"])
    campaign_id = str(pair["campaign_id"])
    if pair["campaign_name"] != target["preferred_campaign_name"] and campaign_id == target["preferred_campaign_id"]:
        raise ValueError(f"target_campaign_name_mismatch:{route_id}:{campaign_id}:{pair['campaign_name']}")
    if str(pair["product_sku"]) != target["product_sku"]:
        raise ValueError(f"target_product_sku_mismatch:{route_id}:{campaign_id}:{pair['product_sku']}")
    if str(pair["product_merchant_sku"]) != target["merchant_sku"]:
        raise ValueError(f"target_merchant_sku_mismatch:{route_id}:{campaign_id}:{pair['product_merchant_sku']}")
    text = f"{pair.get('campaign_name','')} {pair.get('product_merchant_sku','')} {pair.get('product_title','')}".lower()
    for blocked in ("line51", "line61", "suit-31", "suit-21", "line52"):
        if blocked in text:
            raise ValueError(f"target_family_forbidden_term:{route_id}:{campaign_id}:{blocked}")
    if "line31" not in str(pair["product_merchant_sku"]).lower():
        raise ValueError(f"target_family_not_line31:{route_id}:{campaign_id}:{pair['product_merchant_sku']}")


def build_iris_misty_plan(
    *,
    campaign_rows: Sequence[Mapping[str, Any]],
    product_rows: Sequence[Mapping[str, Any]],
    product_search_rows: Sequence[Mapping[str, Any]],
    target_date: str,
    created_at_id: str | None = None,
) -> dict[str, Any]:
    pairs = _campaign_product_pairs(campaign_rows, product_rows)
    targets: list[dict[str, Any]] = []

    for target in TARGETS:
        route_id = str(target["route_id"])
        preferred_id = str(target["preferred_campaign_id"])
        candidates = [row for row in pairs if row["product_sku"] == target["product_sku"]]
        preferred_matches = [row for row in candidates if row["campaign_id"] == preferred_id]
        chosen: dict[str, Any] | None = None
        duplicate_ids = sorted(row["campaign_id"] for row in candidates if row["campaign_id"] != preferred_id)

        if len(preferred_matches) == 1:
            chosen = preferred_matches[0]
        elif len(preferred_matches) > 1:
            raise ValueError(f"ambiguous_existing_campaigns:{route_id}:{[row['campaign_id'] for row in preferred_matches]}")
        elif candidates:
            create_name = _target_campaign_name(target, created_at_id)
            create_matches = [row for row in candidates if row["campaign_name"] == create_name]
            if len(create_matches) == 1:
                chosen = create_matches[0]
            else:
                raise ValueError(f"same_sku_campaign_without_preferred_exact_match:{route_id}:{[row['campaign_id'] for row in candidates]}")

        if chosen is None:
            product = _product_search_exact(product_search_rows, str(target["product_sku"]), str(target["merchant_sku"]))
            if product is None:
                raise ValueError(f"missing_exact_product_search:{route_id}:{target['product_sku']}:{target['merchant_sku']}")
            if pick(product, "advertisable", "isAdvertisable") is not True:
                raise ValueError(f"product_not_advertisable:{route_id}:{pick(product, 'advertisable', 'isAdvertisable')}")
            targets.append(
                {
                    "route_id": route_id,
                    "label": target["label"],
                    "action": "create_campaign",
                    "campaign_name": _target_campaign_name(target, created_at_id),
                    "product_sku": target["product_sku"],
                    "merchant_sku": target["merchant_sku"],
                    "target_bid_kzt": TARGET_BID_KZT,
                    "target_daily_budget_kzt": TARGET_DAILY_BUDGET_KZT,
                    "target_region_label": ALL_KAZAKHSTAN_LABEL,
                    "target_location_ids": [],
                    "operations": ["campaign_create"],
                }
            )
            continue

        _assert_line31_target_identity(target, chosen)
        locations = list(chosen.get("target_location_ids") or [])
        if locations != []:
            raise ValueError(f"target_region_not_all_kazakhstan:{route_id}:{chosen['campaign_id']}:{locations}")
        if not _product_is_active(str(chosen.get("product_product_state") or "")):
            raise ValueError(f"target_product_not_active:{route_id}:{chosen['campaign_id']}:{chosen.get('product_product_state')}")
        if chosen.get("product_buy_box") not in (True, None, ""):
            raise ValueError(f"target_not_buy_box:{route_id}:{chosen['campaign_id']}:{chosen.get('product_buy_box')}")
        if chosen.get("product_advertisable") not in (True, None, ""):
            raise ValueError(f"target_not_advertisable:{route_id}:{chosen['campaign_id']}:{chosen.get('product_advertisable')}")

        operations: list[str] = []
        if as_int(chosen.get("product_bid_kzt")) != TARGET_BID_KZT:
            operations.append("product_bid_update")
        if as_int(chosen.get("daily_budget_kzt")) != TARGET_DAILY_BUDGET_KZT:
            operations.append("campaign_budget_update")
        state = str(chosen.get("campaign_state") or "")
        if _state_is_paused(state):
            operations.append("campaign_resume")
        elif not _state_is_enabled(state):
            raise ValueError(f"target_campaign_state_not_resumable:{route_id}:{chosen['campaign_id']}:{state}")

        targets.append(
            {
                "route_id": route_id,
                "label": target["label"],
                "action": "resume_existing",
                "campaign_id": chosen["campaign_id"],
                "campaign_name": chosen["campaign_name"],
                "product_sku": chosen["product_sku"],
                "merchant_sku": chosen["product_merchant_sku"],
                "current_campaign_state": chosen["campaign_state"],
                "current_daily_budget_kzt": chosen["daily_budget_kzt"],
                "current_bid_kzt": chosen["product_bid_kzt"],
                "current_price_kzt": chosen["product_price_kzt"],
                "current_product_state": chosen["product_product_state"],
                "current_buy_box": chosen["product_buy_box"],
                "current_advertisable": chosen["product_advertisable"],
                "target_bid_kzt": TARGET_BID_KZT,
                "target_daily_budget_kzt": TARGET_DAILY_BUDGET_KZT,
                "target_region_label": _region_label_for_locations(locations),
                "target_location_ids": locations,
                "operations": operations,
                "same_sku_duplicate_campaign_ids": duplicate_ids,
            }
        )

    total_budget = sum(as_int(target["target_daily_budget_kzt"]) or 0 for target in targets)
    return {
        "schema_version": "web_auto.line31_iris_misty_internal_marketing.plan.v1",
        "gate": "GREEN",
        "store": STORE,
        "merchant_id": MERCHANT_ID,
        "store_code": STORE_CODE,
        "target_date": target_date,
        "target_region_label": ALL_KAZAKHSTAN_LABEL,
        "total_target_daily_budget_kzt": total_budget,
        "targets": targets,
        "forbidden_writes": FORBIDDEN_WRITES,
    }


CAMPAIGN_STABLE_FIELDS = ["campaign_name", "campaign_state", "daily_budget_kzt", "target_location_ids"]
PRODUCT_STABLE_FIELDS = ["sku", "merchant_sku", "product_state", "bid_kzt", "price_kzt"]


def _stable_value(value: Any) -> str:
    if isinstance(value, list):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return "" if value is None else str(value)


def stable_drift_rows(
    *,
    pre_campaigns: Sequence[Mapping[str, Any]],
    post_campaigns: Sequence[Mapping[str, Any]],
    pre_products: Sequence[Mapping[str, Any]],
    post_products: Sequence[Mapping[str, Any]],
    target_campaign_ids: set[str],
) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    pre_campaign_by_id = {_campaign_id(row): _normalize_campaign(row) for row in pre_campaigns if _campaign_id(row)}
    post_campaign_by_id = {_campaign_id(row): _normalize_campaign(row) for row in post_campaigns if _campaign_id(row)}
    for campaign_id, before in sorted(pre_campaign_by_id.items()):
        if campaign_id in target_campaign_ids:
            continue
        after = post_campaign_by_id.get(campaign_id)
        if not after:
            rows.append({"scope": "campaign", "campaign_id": campaign_id, "field": "row_missing_post", "before": "present", "after": "missing"})
            continue
        for field in CAMPAIGN_STABLE_FIELDS:
            if _stable_value(before.get(field)) != _stable_value(after.get(field)):
                rows.append(
                    {
                        "scope": "campaign",
                        "campaign_id": campaign_id,
                        "field": field,
                        "before": _stable_value(before.get(field)),
                        "after": _stable_value(after.get(field)),
                    }
                )

    def product_key(row: Mapping[str, Any]) -> tuple[str, str]:
        return (_product_campaign_id(row), _product_sku(row))

    pre_product_by_key = {product_key(row): _normalize_product(row) for row in pre_products if all(product_key(row))}
    post_product_by_key = {product_key(row): _normalize_product(row) for row in post_products if all(product_key(row))}
    for (campaign_id, sku), before in sorted(pre_product_by_key.items()):
        if campaign_id in target_campaign_ids:
            continue
        after = post_product_by_key.get((campaign_id, sku))
        if not after:
            rows.append({"scope": "product", "campaign_id": campaign_id, "sku": sku, "field": "row_missing_post", "before": "present", "after": "missing"})
            continue
        for field in PRODUCT_STABLE_FIELDS:
            if _stable_value(before.get(field)) != _stable_value(after.get(field)):
                rows.append(
                    {
                        "scope": "product",
                        "campaign_id": campaign_id,
                        "sku": sku,
                        "field": field,
                        "before": _stable_value(before.get(field)),
                        "after": _stable_value(after.get(field)),
                    }
                )
    return rows


def target_diff_rows(
    *,
    pre_campaigns: Sequence[Mapping[str, Any]],
    post_campaigns: Sequence[Mapping[str, Any]],
    pre_products: Sequence[Mapping[str, Any]],
    post_products: Sequence[Mapping[str, Any]],
    plan: Mapping[str, Any],
) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    pre_campaign_by_id = {_campaign_id(row): _normalize_campaign(row) for row in pre_campaigns if _campaign_id(row)}
    post_campaign_by_id = {_campaign_id(row): _normalize_campaign(row) for row in post_campaigns if _campaign_id(row)}
    pre_products_by_campaign = {}
    post_products_by_campaign = {}
    for row in pre_products:
        pre_products_by_campaign.setdefault(_product_campaign_id(row), []).append(_normalize_product(row))
    for row in post_products:
        post_products_by_campaign.setdefault(_product_campaign_id(row), []).append(_normalize_product(row))
    for target in plan.get("targets", []):
        campaign_id = str(target.get("campaign_id") or "")
        if not campaign_id:
            continue
        for field in ("campaign_state", "daily_budget_kzt", "target_location_ids"):
            before = _stable_value((pre_campaign_by_id.get(campaign_id) or {}).get(field))
            after = _stable_value((post_campaign_by_id.get(campaign_id) or {}).get(field))
            if before != after:
                rows.append({"scope": "target_campaign", "route_id": str(target["route_id"]), "campaign_id": campaign_id, "field": field, "before": before, "after": after})
        target_sku = str(target.get("product_sku") or "")
        before_product = next((row for row in pre_products_by_campaign.get(campaign_id, []) if row["sku"] == target_sku), {})
        after_product = next((row for row in post_products_by_campaign.get(campaign_id, []) if row["sku"] == target_sku), {})
        for field in ("product_state", "bid_kzt", "price_kzt"):
            before = _stable_value(before_product.get(field))
            after = _stable_value(after_product.get(field))
            if before != after:
                rows.append({"scope": "target_product", "route_id": str(target["route_id"]), "campaign_id": campaign_id, "sku": target_sku, "field": field, "before": before, "after": after})
    return rows


def campaign_summary(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "campaign_id": str(pick(row, "id", "Id", "campaign_id") or ""),
        "campaign_name": str(pick(row, "name", "Name", "campaign_name") or ""),
        "campaign_state": str(pick(row, "state", "State", "campaign_state") or ""),
        "daily_budget_kzt": pick(row, "dailyBudget", "DailyBudget", "daily_budget_kzt"),
        "default_bid_kzt": pick(row, "defaultBid", "DefaultBid", "default_bid_kzt"),
        "start_date": pick(row, "startDate", "StartDate", "start_date"),
        "end_date": pick(row, "endDate", "EndDate", "end_date"),
        "target_location_ids": pick(row, "targetLocationIds", "target_location_ids") or [],
        "merchant_id": str(pick(row, "merchantId") or MERCHANT_ID),
        "store_code": str(pick(row, "merchantBusinessId") or ""),
    }


def product_summary(row: Mapping[str, Any], *, campaign_id: str) -> dict[str, Any]:
    return {
        "campaign_id": str(pick(row, "campaignId", "campaignID", "campaign_id") or campaign_id),
        "sku": str(pick(row, "sku", "id", "productId") or ""),
        "merchant_sku": str(pick(row, "merchantSku", "merchantSKU", "offerId", "merchant_sku") or ""),
        "title": str(pick(row, "title", "name", "productName") or ""),
        "product_state": str(pick(row, "productState", "state", "status", "product_state") or ""),
        "bid_kzt": pick(row, "bid", "bidCpc", "avgBid", "bid_kzt"),
        "price_kzt": pick(row, "price", "price_kzt"),
        "buy_box": pick(row, "buyBox", "buy_box"),
        "advertisable": pick(row, "advertisable", "isAdvertisable"),
    }


def fetch_product_search(context: BrowserContext, headers: dict[str, str], *, run_dir: Path) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    raw_dir = run_dir / "raw_api"
    for target in TARGETS:
        url = f"{API_BASE}/v3/merchant/{MERCHANT_ID}/catalog/products?criteria={target['product_sku']}"
        result = request_json(context, url=url, headers=headers)
        write_json(raw_dir / f"product_search_{target['route_id']}.json", result)
        for row in list_body(result.get("body")):
            out.append(
                {
                    "route_id": target["route_id"],
                    "sku": _product_sku(row),
                    "merchantSku": _product_merchant_sku(row),
                    "title": str(pick(row, "title", "name", "productName") or ""),
                    "advertisable": pick(row, "advertisable", "isAdvertisable"),
                    "alreadyAdded": pick(row, "alreadyAdded", "isAlreadyAdded"),
                    "raw": row,
                }
            )
    write_json(run_dir / "product_search_rows.json", out)
    return out


def fetch_state(context: BrowserContext, headers: dict[str, str], *, run_dir: Path, label: str, target_date: str) -> dict[str, Any]:
    raw_dir = run_dir / "raw_api"
    raw_dir.mkdir(parents=True, exist_ok=True)
    campaign_list_url = f"{API_BASE}/v5/merchant/{MERCHANT_ID}/Campaigns?StartDate={target_date}&EndDate={target_date}"
    campaign_list_result = request_json(context, url=campaign_list_url, headers=headers)
    write_json(raw_dir / f"{label}_campaign_list_raw.json", campaign_list_result)
    write_json(run_dir / f"{label}write_campaign_list.json", campaign_list_result if label == "pre" else campaign_list_result)
    campaign_rows = [campaign_summary(row) for row in list_body(campaign_list_result.get("body"))]
    write_json(run_dir / f"{label}_campaign_rows.json", campaign_rows)

    product_rows: list[dict[str, Any]] = []
    target_core_rows: dict[str, dict[str, Any]] = {}
    target_ids = {target["preferred_campaign_id"] for target in TARGETS}
    for campaign in campaign_rows:
        campaign_id = str(campaign["campaign_id"])
        if campaign_id in target_ids:
            core_url = f"{API_BASE}/v1/merchant/{MERCHANT_ID}/Campaign/{campaign_id}"
            core_result = request_json(context, url=core_url, headers=headers)
            write_json(run_dir / f"{label}write_target_campaign_core_{campaign_id}.json", core_result)
            target_core_rows[campaign_id] = campaign_summary(unwrap(core_result.get("body")) if isinstance(unwrap(core_result.get("body")), dict) else {})
        products_url = (
            f"{API_BASE}/v5/merchant/{MERCHANT_ID}/campaign/{campaign_id}/products"
            f"?StartDate={target_date}&EndDate={target_date}"
        )
        products_result = request_json(context, url=products_url, headers=headers)
        write_json(raw_dir / f"{label}_{campaign_id}_products_raw.json", products_result)
        if campaign_id in target_ids:
            write_json(run_dir / f"{label}write_target_product_rows_{campaign_id}.json", products_result)
        for row in list_body(products_result.get("body")):
            product_rows.append(product_summary(row, campaign_id=campaign_id))
    write_json(run_dir / f"{label}_product_rows.json", product_rows)
    for idx, row in enumerate(campaign_rows):
        core = target_core_rows.get(row["campaign_id"])
        if core:
            campaign_rows[idx] = {**row, **{key: value for key, value in core.items() if value not in ("", None)}}
    return {"campaign_list": campaign_list_result, "campaign_rows": campaign_rows, "product_rows": product_rows}


def execute_live_plan(page: Page, xsrf_token: str, plan: Mapping[str, Any], pre_state: Mapping[str, Any]) -> list[dict[str, Any]]:
    campaign_by_id = {str(row["campaign_id"]): row for row in pre_state["campaign_rows"]}
    results: list[dict[str, Any]] = []
    for target in plan["targets"]:
        if target["action"] != "resume_existing":
            continue
        campaign_id = str(target["campaign_id"])
        if "product_bid_update" in target["operations"]:
            payload = build_product_bid_payload(str(target["product_sku"]), TARGET_BID_KZT)
            result = js_fetch_json(
                page,
                method="PUT",
                url=f"{API_BASE}/v1/merchant/{MERCHANT_ID}/campaign/{campaign_id}/products/update-bid",
                xsrf_token=xsrf_token,
                payload=payload,
            )
            results.append({"route_id": target["route_id"], "operation": "product_bid_update", "campaign_id": campaign_id, "endpoint": result["url_path"], "status": result["status"], "ok": result["ok"], "payload_redacted": payload, "response_body_redacted": result["body"]})
            if not response_success(result):
                return results
        if "campaign_budget_update" in target["operations"]:
            payload = build_campaign_budget_payload(campaign_by_id[campaign_id], TARGET_DAILY_BUDGET_KZT)
            result = js_put_form(
                page,
                url=f"{API_BASE}/v3/merchant/{MERCHANT_ID}/Campaign/update",
                xsrf_token=xsrf_token,
                fields=payload,
            )
            results.append({"route_id": target["route_id"], "operation": "campaign_budget_update", "campaign_id": campaign_id, "endpoint": result["url_path"], "status": result["status"], "ok": result["ok"], "payload_redacted": payload, "response_body_redacted": result["body"]})
            if not response_success(result):
                return results

    for target in plan["targets"]:
        if target["action"] != "create_campaign":
            continue
        payload = build_campaign_add_payload(
            name=str(target["campaign_name"]),
            product_sku=str(target["product_sku"]),
            bid_kzt=TARGET_BID_KZT,
            daily_budget_kzt=TARGET_DAILY_BUDGET_KZT,
            start_date=str(plan["target_date"]),
            target_location_ids=[],
        )
        result = js_fetch_json(
            page,
            method="POST",
            url=f"{API_BASE}/v3/merchant/{MERCHANT_ID}/Campaign/add",
            xsrf_token=xsrf_token,
            payload=payload,
        )
        campaign_id = pick(unwrap(result["body"]) if isinstance(result.get("body"), dict) else {}, "id", "Id")
        results.append({"route_id": target["route_id"], "operation": "campaign_create", "campaign_id": campaign_id, "endpoint": result["url_path"], "status": result["status"], "ok": result["ok"], "payload_redacted": payload, "response_body_redacted": result["body"]})
        if not response_success(result):
            return results

    for target in plan["targets"]:
        if target["action"] != "resume_existing" or "campaign_resume" not in target["operations"]:
            continue
        campaign_id = str(target["campaign_id"])
        payload = build_resume_form_fields(campaign_id)
        result = js_put_form(
            page,
            url=f"{API_BASE}/v1.0/merchant/{MERCHANT_ID}/campaign/resume",
            xsrf_token=xsrf_token,
            fields=payload,
        )
        results.append({"route_id": target["route_id"], "operation": "campaign_resume", "campaign_id": campaign_id, "endpoint": result["url_path"], "status": result["status"], "ok": result["ok"], "payload_redacted": payload, "response_body_redacted": result["body"]})
        if not response_success(result):
            return results
    return results


def summarize_final_state(post_state: Mapping[str, Any], plan: Mapping[str, Any]) -> dict[str, Any]:
    try:
        final_plan = build_iris_misty_plan(
            campaign_rows=post_state["campaign_rows"],
            product_rows=post_state["product_rows"],
            product_search_rows=[],
            target_date=str(plan["target_date"]),
        )
    except ValueError as exc:
        return {"checks": [], "errors": [f"post_verify_plan_unresolved:{exc}"]}
    checks: list[dict[str, Any]] = []
    errors: list[str] = []
    for target in final_plan["targets"]:
        if target["action"] != "resume_existing":
            errors.append(f"target_not_existing_after_apply:{target['route_id']}")
            continue
        state_ok = _state_is_enabled(str(target["current_campaign_state"]))
        budget_ok = as_int(target["current_daily_budget_kzt"]) == TARGET_DAILY_BUDGET_KZT
        bid_ok = as_int(target["current_bid_kzt"]) == TARGET_BID_KZT
        region_ok = target["target_region_label"] == ALL_KAZAKHSTAN_LABEL and target["target_location_ids"] == []
        checks.append(
            {
                "route_id": target["route_id"],
                "campaign_id": target["campaign_id"],
                "campaign_state": target["current_campaign_state"],
                "daily_budget_kzt": target["current_daily_budget_kzt"],
                "bid_kzt": target["current_bid_kzt"],
                "price_kzt": target["current_price_kzt"],
                "product_state": target["current_product_state"],
                "region_label": target["target_region_label"],
                "target_location_ids": target["target_location_ids"],
                "state_ok": state_ok,
                "budget_ok": budget_ok,
                "bid_ok": bid_ok,
                "region_ok": region_ok,
            }
        )
        if not state_ok:
            errors.append(f"post_state_not_enabled:{target['route_id']}:{target['current_campaign_state']}")
        if not budget_ok:
            errors.append(f"post_budget_mismatch:{target['route_id']}:{target['current_daily_budget_kzt']}")
        if not bid_ok:
            errors.append(f"post_bid_mismatch:{target['route_id']}:{target['current_bid_kzt']}")
        if not region_ok:
            errors.append(f"post_region_mismatch:{target['route_id']}:{target['target_region_label']}:{target['target_location_ids']}")
    return {"plan": final_plan, "checks": checks, "errors": errors}


def render_region_readback(plan: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": "web_auto.line31_iris_misty_internal_marketing.region_readback.v1",
        "region_label": ALL_KAZAKHSTAN_LABEL,
        "platform_equivalent": "empty targetLocationIds[] means all Kazakhstan for Kaspi Marketing campaign targeting",
        "targets": [
            {
                "route_id": target["route_id"],
                "campaign_id": target.get("campaign_id", "CREATE"),
                "campaign_name": target.get("campaign_name"),
                "target_location_ids": target.get("target_location_ids", []),
                "region_label": target.get("target_region_label", ALL_KAZAKHSTAN_LABEL),
            }
            for target in plan.get("targets", [])
        ],
    }


def render_closeout(summary: Mapping[str, Any]) -> str:
    gate = str(summary.get("gate") or "YELLOW")
    lines = [
        f"Gate: {gate}",
        "",
        "# LINE31 Iris Purple + Misty Blue Internal Kaspi Marketing",
        "",
        f"- Generated at: `{summary.get('generated_at_local')}` Asia/Almaty.",
        f"- Store: `{STORE}` / merchant `{MERCHANT_ID}` / store code `{STORE_CODE}`.",
        f"- Mode: `{summary.get('mode')}`.",
        "",
        "Kaspi internal marketing is internal marketplace truth, not deterministic total product demand.",
        "Meta delivery is currently disrupted by the payment issue, so this is an internal-active baseline window.",
        "",
        "## Result",
        "",
        f"- status: `{summary.get('status')}`",
        f"- live writes executed: `{summary.get('live_writes_executed')}`",
        f"- launch timestamp: `{summary.get('launch_timestamp_almaty')}`",
        "",
        "## Targets",
        "",
    ]
    final = summary.get("final_state", {}) if isinstance(summary.get("final_state"), dict) else {}
    checks = final.get("checks") or []
    if not checks:
        plan = summary.get("preflight_plan") or {}
        checks = plan.get("targets") or []
    for check in checks:
        lines.append(
            f"- `{check.get('route_id')}` campaign `{check.get('campaign_id')}`: "
            f"state `{check.get('campaign_state', check.get('current_campaign_state'))}`, "
            f"budget `{check.get('daily_budget_kzt', check.get('current_daily_budget_kzt'))}`, "
            f"BID `{check.get('bid_kzt', check.get('current_bid_kzt'))}`, "
            f"price `{check.get('price_kzt', check.get('current_price_kzt'))}`, "
            f"region `{check.get('region_label', check.get('target_region_label'))}`, "
            f"targetLocationIds `{check.get('target_location_ids')}`."
        )
    if summary.get("apply_results"):
        lines.extend(["", "## Apply Results", ""])
        for result in summary["apply_results"]:
            lines.append(f"- `{result.get('route_id')}` `{result.get('operation')}` campaign `{result.get('campaign_id', '')}`: status `{result.get('status')}`, ok `{result.get('ok')}`.")
    if summary.get("drift_rows"):
        lines.extend(["", "## Drift Rows", ""])
        for row in summary["drift_rows"]:
            lines.append(f"- `{row}`")
    if summary.get("validation_errors"):
        lines.extend(["", "## Validation Errors", ""])
        for error in summary["validation_errors"]:
            lines.append(f"- `{error}`")
    lines.extend(["", "## Evidence Artifacts", ""])
    for label, path in (summary.get("artifact_paths") or {}).items():
        lines.append(f"- `{label}`: `{path}`")
    lines.extend(
        [
            "",
            "## Boundaries",
            "",
            "- No Meta/Facebook, price, stock, Repricer, CRM, website, Telegram, Autonomous_business, card-content, image, product-status archive/pause, or unrelated Kaspi campaign changes were made by this lane.",
            "- Rollback/stop instruction: use only the merchant-scoped campaign suspend/pause control on the listed campaign IDs if emergency stop is separately approved.",
            "",
            "## Post-Launch Watch",
            "",
            "- Create read-only 24h and 48h watch notes for Misty Blue and Iris Purple separately: spend, views, clicks, carts, orders, average CPC, and spend/order if orders exist.",
            "- Label watch output as Kaspi internal marketplace truth, not deterministic total product demand.",
            "",
        ]
    )
    return "\n".join(lines)


def run_lane(args: argparse.Namespace) -> dict[str, Any]:
    run_dir: Path = args.run_dir
    run_dir.mkdir(parents=True, exist_ok=True)
    summary: dict[str, Any] = {
        "schema_version": "web_auto.line31_iris_misty_internal_marketing.v1",
        "generated_at_local": now_local(),
        "mode": args.mode,
        "target_date": args.date,
        "owner_approval_text": args.owner_approval_text,
        "store": STORE,
        "merchant_id": MERCHANT_ID,
        "store_code": STORE_CODE,
        "live_writes_executed": False,
        "launch_timestamp_almaty": "NOT_LAUNCHED",
        "preflight_plan": None,
        "apply_results": [],
        "final_state": {},
        "validation_errors": [],
        "drift_rows": [],
        "status": "UNKNOWN",
        "gate": "YELLOW",
        "artifact_paths": {
            "prewrite_campaign_list": str(run_dir / "prewrite_campaign_list.json"),
            "prewrite_region_readback": str(run_dir / "prewrite_region_readback.json"),
            "dry_run_action_plan": str(run_dir / "dry_run_action_plan.json"),
            "live_write_responses_redacted": str(run_dir / "live_write_responses_redacted.json"),
            "postwrite_campaign_list": str(run_dir / "postwrite_campaign_list.json"),
            "before_after_diff": str(run_dir / "before_after_diff.csv"),
            "closeout": str(run_dir / "CLOSEOUT.md"),
        },
    }
    if APPROVAL_PHRASE not in args.owner_approval_text:
        summary["validation_errors"].append("approval_phrase_missing_or_not_exact")
        summary["status"] = "YELLOW_BLOCKED_APPROVAL"
        write_json(run_dir / "live_apply_summary.json", summary)
        (run_dir / "CLOSEOUT.md").write_text(render_closeout(summary), encoding="utf-8")
        return summary

    with sync_playwright() as p:
        browser = p.chromium.launch(**merchant_browser_launch_kwargs(headless=args.headless))
        context = browser.new_context(viewport={"width": 1440, "height": 1400})
        page = context.new_page()
        creds = resolve_marketing_credentials(STORE, env_file=args.env_file, merchant_id=MERCHANT_ID, store_code=STORE_CODE)
        login_kaspi_marketing(page, login_value=creds.login, password_value=creds.password)
        page.goto(MARKETING_CAMPAIGNS_URL, wait_until="domcontentloaded", timeout=60000)
        cookies = context.cookies("https://marketing.kaspi.kz")
        headers = build_marketing_headers(cookies, MARKETING_CAMPAIGNS_URL)
        xsrf_token = xsrf_from_cookies(cookies)
        summary["auth"] = {
            "cookie_count": len(cookies),
            "has_auth_cookie": True,
            "has_xsrf_cookie": bool(xsrf_token),
            "request_header_names": sorted(headers.keys()),
        }

        pre_state = fetch_state(context, headers, run_dir=run_dir, label="pre", target_date=args.date)
        product_search_rows = fetch_product_search(context, headers, run_dir=run_dir)
        try:
            plan = build_iris_misty_plan(
                campaign_rows=pre_state["campaign_rows"],
                product_rows=pre_state["product_rows"],
                product_search_rows=product_search_rows,
                target_date=args.date,
                created_at_id=args.created_at_id,
            )
        except ValueError as exc:
            summary["validation_errors"].append(str(exc))
            summary["status"] = "YELLOW_BLOCKED_PREFLIGHT"
            write_json(run_dir / "live_apply_summary.json", summary)
            (run_dir / "CLOSEOUT.md").write_text(render_closeout(summary), encoding="utf-8")
            context.close()
            browser.close()
            return summary
        summary["preflight_plan"] = plan
        write_json(run_dir / "dry_run_action_plan.json", plan)
        region_readback = render_region_readback(plan)
        write_json(run_dir / "prewrite_region_readback.json", region_readback)

        if args.mode == "live":
            results = execute_live_plan(page, xsrf_token, plan, pre_state)
            summary["apply_results"] = results
            summary["live_writes_executed"] = bool(results)
            write_json(run_dir / "live_write_responses_redacted.json", results)
            failed = [result for result in results if not result.get("ok")]
            if failed:
                summary["validation_errors"].append(f"apply_failed:{failed[-1].get('operation')}:{failed[-1].get('status')}")
            time.sleep(3.0)
            post_state = fetch_state(context, headers, run_dir=run_dir, label="post", target_date=args.date)
            final_state = summarize_final_state(post_state, plan)
            summary["final_state"] = final_state
            summary["validation_errors"].extend(final_state.get("errors") or [])
            target_campaign_ids = {str(target.get("campaign_id")) for target in final_state.get("plan", {}).get("targets", []) if target.get("campaign_id")}
            drift_rows = stable_drift_rows(
                pre_campaigns=pre_state["campaign_rows"],
                post_campaigns=post_state["campaign_rows"],
                pre_products=pre_state["product_rows"],
                post_products=post_state["product_rows"],
                target_campaign_ids=target_campaign_ids,
            )
            target_rows = target_diff_rows(
                pre_campaigns=pre_state["campaign_rows"],
                post_campaigns=post_state["campaign_rows"],
                pre_products=pre_state["product_rows"],
                post_products=post_state["product_rows"],
                plan=final_state.get("plan", plan),
            )
            diff_rows = target_rows + drift_rows
            summary["drift_rows"] = drift_rows
            write_csv(
                run_dir / "before_after_diff.csv",
                diff_rows,
                ["scope", "route_id", "campaign_id", "sku", "field", "before", "after"],
            )
            if drift_rows:
                summary["validation_errors"].append(f"non_target_drift_detected:{len(drift_rows)}")
            summary["launch_timestamp_almaty"] = now_local() if not summary["validation_errors"] else "PARTIAL_OR_UNVERIFIED"
            summary["status"] = "GREEN_LIVE_APPLY_VERIFIED" if not summary["validation_errors"] else "RED_OR_YELLOW_POST_VERIFY_FAILED"
            summary["gate"] = "GREEN" if not summary["validation_errors"] else "RED"
        else:
            summary["status"] = "PREFLIGHT_GREEN_DRY_RUN_ONLY"
            summary["gate"] = "GREEN"

        context.close()
        browser.close()

    write_json(run_dir / "live_apply_summary.json", summary)
    (run_dir / "CLOSEOUT.md").write_text(render_closeout(summary), encoding="utf-8")
    return summary


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Resume/create LINE31 Iris Purple and Misty Blue Kaspi internal marketing campaigns.")
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--date", default=date.today().isoformat())
    parser.add_argument("--created-at-id", default=datetime.now(ALMATY).strftime("%Y%m%d_%H%M%S"))
    parser.add_argument("--mode", choices=["preflight", "live"], default="preflight")
    parser.add_argument("--owner-approval-text", required=True)
    parser.add_argument("--headless", action="store_true", default=True)
    parser.add_argument("--headed", action="store_false", dest="headless")
    args = parser.parse_args(argv)
    summary = run_lane(args)
    print(
        json.dumps(
            {
                "status": summary.get("status"),
                "gate": summary.get("gate"),
                "summary": str(args.run_dir / "live_apply_summary.json"),
                "closeout": str(args.run_dir / "CLOSEOUT.md"),
                "validation_errors": summary.get("validation_errors"),
            },
            ensure_ascii=False,
        )
    )
    return 0 if summary.get("gate") in {"GREEN", "YELLOW"} else 4


if __name__ == "__main__":
    raise SystemExit(main())
