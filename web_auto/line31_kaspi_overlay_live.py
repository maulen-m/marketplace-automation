from __future__ import annotations

import argparse
import csv
import json
import time
from datetime import date, datetime
from pathlib import Path
from typing import Any, Mapping, Sequence
from zoneinfo import ZoneInfo

from playwright.sync_api import BrowserContext, Page, sync_playwright

from .kaspi_marketing import (
    MARKETING_CAMPAIGNS_URL,
    build_marketing_headers,
    login_kaspi_marketing,
    resolve_marketing_credentials,
)
from .kaspi_merchant_common import merchant_browser_launch_kwargs


API_BASE = "https://marketing.kaspi.kz/advertising/products/api"
STORE = "ACMEWEAR"
MERCHANT_ID = "759051"
STORE_CODE = "30137883"
ALMATY = ZoneInfo("Asia/Almaty")

FORBIDDEN_WRITES = [
    "Meta",
    "LINE61",
    "CRM",
    "website",
    "Telegram",
    "workbook",
    "DB",
    "price",
    "stock",
]

TARGETS = [
    {
        "route_id": "line31_olive_cardamom",
        "label": "LINE31 Olive/Cardamom Green",
        "product_sku": "20887378b",
        "merchant_sku_contains": "CARDAMOM-GREEN",
        "target_daily_budget_kzt": 5000,
        "target_bid_kzt": 50,
        "target_price_kzt": 24990,
    },
    {
        "route_id": "line31_iws_mixed",
        "label": "LINE31 IWS Mixed",
        "product_sku": "20970889b",
        "merchant_sku_contains": "IWSB",
        "preferred_campaign_id": "2862392",
        "target_daily_budget_kzt": 5000,
        "target_bid_kzt": 50,
        "target_price_kzt": 24990,
    },
    {
        "route_id": "line31_starry_black",
        "label": "LINE31 Starry Black",
        "product_sku": "20754945b",
        "merchant_sku_contains": "STARRY-BLACK",
        "preferred_campaign_id": "2695637",
        "target_daily_budget_kzt": 3000,
        "target_bid_kzt": 120,
        "target_price_kzt": 24990,
    },
]


def now_local() -> str:
    return datetime.now(ALMATY).isoformat(timespec="seconds")


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def write_csv(path: Path, rows: Sequence[Mapping[str, Any]], fieldnames: Sequence[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(fieldnames), extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({key: row.get(key, "") for key in fieldnames})


def pick(data: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        value = data.get(key)
        if value not in (None, ""):
            return value
    return None


def unwrap(data: Any) -> Any:
    if isinstance(data, dict) and "data" in data and ("result" in data or "message" in data):
        return data["data"]
    if isinstance(data, dict) and "data" in data and isinstance(data["data"], (list, dict)):
        return data["data"]
    return data


def list_body(data: Any) -> list[dict[str, Any]]:
    body = unwrap(data)
    if isinstance(body, list):
        return [item for item in body if isinstance(item, dict)]
    if isinstance(body, dict):
        for key in ("items", "products", "rows", "data", "campaigns"):
            value = body.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
    return []


def as_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(round(float(value)))
    text = str(value).replace("\u00a0", " ").replace("₸", "").replace(" ", "").replace(",", ".").strip()
    if not text:
        return None
    try:
        return int(round(float(text)))
    except Exception:
        return None


def _campaign_id(row: Mapping[str, Any]) -> str:
    return str(pick(row, "campaign_id", "id", "Id") or "").strip()


def _campaign_name(row: Mapping[str, Any]) -> str:
    return str(pick(row, "campaign_name", "name", "Name") or "").strip()


def _campaign_state(row: Mapping[str, Any]) -> str:
    return str(pick(row, "campaign_state", "state", "State", "report_state") or "").strip()


def _campaign_budget(row: Mapping[str, Any]) -> int | None:
    return as_int(pick(row, "daily_budget_kzt", "daily_budget", "dailyBudget", "DailyBudget"))


def _product_campaign_id(row: Mapping[str, Any]) -> str:
    return str(pick(row, "campaign_id", "campaignId", "campaignID") or "").strip()


def _product_sku(row: Mapping[str, Any]) -> str:
    return str(pick(row, "sku", "product_sku", "sku_key", "json_sku", "id", "productId") or "").strip()


def _product_merchant_sku(row: Mapping[str, Any]) -> str:
    return str(pick(row, "merchant_sku", "merchantSku", "merchantSKU", "json_merchant_sku", "offerId") or "").strip()


def _product_state(row: Mapping[str, Any]) -> str:
    return str(pick(row, "product_state", "product_status", "state", "status") or "").strip()


def _product_bid(row: Mapping[str, Any]) -> int | None:
    return as_int(pick(row, "bid", "bid_kzt", "bid_cpc", "bidCpc", "avgBid"))


def _product_price(row: Mapping[str, Any]) -> int | None:
    return as_int(pick(row, "price", "price_kzt"))


def _normalize_campaign(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "campaign_id": _campaign_id(row),
        "campaign_name": _campaign_name(row),
        "campaign_state": _campaign_state(row),
        "daily_budget_kzt": _campaign_budget(row),
        "start_date": pick(row, "start_date", "startDate", "StartDate"),
        "end_date": pick(row, "end_date", "endDate", "EndDate"),
        "default_bid_kzt": as_int(pick(row, "default_bid", "default_bid_kzt", "defaultBid", "DefaultBid")),
        "target_location_ids": pick(row, "target_location_ids", "targetLocationIds") or [],
        "raw": dict(row),
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
        "raw": dict(row),
    }


def _state_is_paused(value: str) -> bool:
    return value.strip().lower() in {"paused", "suspended", "suspendedbyyou"}


def _state_is_enabled(value: str) -> bool:
    return value.strip().lower() in {"enabled", "outofbudget"}


def _product_is_active(value: str) -> bool:
    return value.strip().lower() in {"active", "enabled"}


def _campaign_product_pairs(
    campaign_rows: Sequence[Mapping[str, Any]],
    product_rows: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    campaigns = {_campaign_id(row): _normalize_campaign(row) for row in campaign_rows if _campaign_id(row)}
    out: list[dict[str, Any]] = []
    for product_row in product_rows:
        product = _normalize_product(product_row)
        campaign = campaigns.get(product["campaign_id"])
        if not campaign:
            continue
        out.append({**campaign, **{f"product_{key}": value for key, value in product.items() if key != "raw"}})
    return out


def _product_search_exact(product_search_rows: Sequence[Mapping[str, Any]], sku: str) -> dict[str, Any] | None:
    matches = [dict(row) for row in product_search_rows if _product_sku(row) == sku]
    if len(matches) != 1:
        return None
    return matches[0]


def _target_campaign_name(route_id: str, target_date: str, created_at_id: str | None) -> str:
    stamp = created_at_id or f"{target_date.replace('-', '')}_overlay"
    if route_id == "line31_olive_cardamom":
        return f"LINE31_CGOG_ST_{stamp}"
    return f"LINE31_{route_id}_{stamp}"


def build_overlay_plan(
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
        candidates = [row for row in pairs if row["product_sku"] == target["product_sku"]]
        route_id = target["route_id"]
        preferred = str(target.get("preferred_campaign_id") or "")
        chosen: dict[str, Any] | None = None
        duplicate_ids: list[str] = []
        if candidates:
            if preferred:
                preferred_matches = [row for row in candidates if row["campaign_id"] == preferred]
                if len(preferred_matches) == 1:
                    chosen = preferred_matches[0]
                    duplicate_ids = sorted(row["campaign_id"] for row in candidates if row["campaign_id"] != preferred)
                elif len(candidates) == 1:
                    chosen = candidates[0]
                else:
                    raise ValueError(f"ambiguous_existing_campaigns:{route_id}:{[row['campaign_id'] for row in candidates]}")
            elif len(candidates) == 1:
                chosen = candidates[0]
            else:
                raise ValueError(f"ambiguous_existing_campaigns:{route_id}:{[row['campaign_id'] for row in candidates]}")

        if chosen is None:
            product = _product_search_exact(product_search_rows, str(target["product_sku"]))
            if product is None:
                raise ValueError(f"missing_exact_product_search:{route_id}:{target['product_sku']}")
            if product.get("advertisable") is not True:
                raise ValueError(f"product_not_advertisable:{route_id}:{product.get('advertisable')}")
            targets.append(
                {
                    "route_id": route_id,
                    "label": target["label"],
                    "action": "create_campaign",
                    "campaign_name": _target_campaign_name(route_id, target_date, created_at_id),
                    "product_sku": target["product_sku"],
                    "merchant_sku": _product_merchant_sku(product),
                    "target_bid_kzt": target["target_bid_kzt"],
                    "target_daily_budget_kzt": target["target_daily_budget_kzt"],
                    "operations": ["campaign_create"],
                }
            )
            continue

        if not _product_is_active(str(chosen.get("product_product_state") or "")):
            raise ValueError(f"target_product_not_active:{route_id}:{chosen['campaign_id']}:{chosen.get('product_product_state')}")
        expected_price = as_int(target.get("target_price_kzt"))
        if expected_price and as_int(chosen.get("product_price_kzt")) != expected_price:
            raise ValueError(
                f"target_price_not_expected:{route_id}:{chosen['campaign_id']}:"
                f"{chosen.get('product_price_kzt')}:{expected_price}"
            )
        if as_int(chosen.get("product_bid_kzt")) != target["target_bid_kzt"]:
            raise ValueError(f"target_bid_not_expected:{route_id}:{chosen['campaign_id']}:{chosen.get('product_bid_kzt')}")

        operations: list[str] = []
        if as_int(chosen.get("daily_budget_kzt")) != target["target_daily_budget_kzt"]:
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
                "target_bid_kzt": target["target_bid_kzt"],
                "target_daily_budget_kzt": target["target_daily_budget_kzt"],
                "operations": operations,
                "legacy_duplicate_campaign_ids_kept_paused": duplicate_ids,
            }
        )

    total_budget = sum(as_int(target["target_daily_budget_kzt"]) or 0 for target in targets)
    if total_budget < 10000 or total_budget > 15000:
        raise ValueError(f"total_budget_outside_cap:{total_budget}")
    return {
        "schema_version": "web_auto.line31_kaspi_overlay_live.plan.v1",
        "gate": "GREEN",
        "store": STORE,
        "merchant_id": MERCHANT_ID,
        "store_code": STORE_CODE,
        "target_date": target_date,
        "targets": targets,
        "total_target_daily_budget_kzt": total_budget,
        "forbidden_writes": FORBIDDEN_WRITES,
    }


def validate_campaign_add_payload(
    payload: Mapping[str, Any],
    *,
    expected_name: str,
    expected_sku: str,
    expected_bid: int,
    expected_budget: int,
    expected_start_date: str,
) -> list[str]:
    errors: list[str] = []
    if payload.get("name") != expected_name:
        errors.append(f"name_mismatch:{payload.get('name')!r}")
    if as_int(payload.get("dailyBudget")) != expected_budget:
        errors.append(f"daily_budget_mismatch:{payload.get('dailyBudget')!r}")
    products = payload.get("products")
    if not isinstance(products, list) or len(products) != 1:
        errors.append(f"product_count_mismatch:{products!r}")
    else:
        product = products[0]
        if not isinstance(product, dict):
            errors.append("product_payload_not_dict")
        else:
            if str(product.get("sku") or "") != expected_sku:
                errors.append(f"product_sku_mismatch:{product.get('sku')!r}")
            if as_int(product.get("bid")) != expected_bid:
                errors.append(f"product_bid_mismatch:{product.get('bid')!r}")
    if not str(payload.get("startDate") or "").startswith(expected_start_date):
        errors.append(f"start_date_not_target_date:{payload.get('startDate')!r}")
    if payload.get("endDate") not in (None, "", []):
        errors.append(f"unexpected_end_date:{payload.get('endDate')!r}")
    if payload.get("targetLocationIds", []) not in ([], None):
        errors.append(f"unexpected_target_locations:{payload.get('targetLocationIds')!r}")
    return errors


def safe_response_body(resp: Any) -> Any:
    try:
        return resp.json()
    except Exception:
        try:
            return {"raw_text_preview": resp.text()[:2000]}
        except Exception as exc:
            return {"raw_text_error": type(exc).__name__}


def request_json(context: BrowserContext, *, url: str, headers: dict[str, str]) -> dict[str, Any]:
    started = time.time()
    resp = context.request.get(url, headers=headers)
    return {
        "url_path": url.replace(API_BASE, ""),
        "status": resp.status,
        "ok": 200 <= resp.status < 300,
        "elapsed_ms": int((time.time() - started) * 1000),
        "body": safe_response_body(resp),
    }


def xsrf_from_cookies(cookies: list[dict[str, Any]]) -> str:
    for cookie in cookies:
        if cookie.get("name") == "XSRF-TOKEN":
            return str(cookie.get("value") or "")
    return ""


def js_fetch_json(page: Page, *, method: str, url: str, xsrf_token: str, payload: dict[str, Any]) -> dict[str, Any]:
    return page.evaluate(
        """
        async ({method, url, xsrfToken, payload}) => {
          const headers = {
            "accept": "application/json, text/plain, */*",
            "content-type": "application/json",
            "x-requested-with": "XMLHttpRequest"
          };
          if (xsrfToken) headers["x-xsrf-token"] = xsrfToken;
          const started = performance.now();
          const resp = await fetch(url, {
            method,
            credentials: "include",
            headers,
            body: JSON.stringify(payload)
          });
          const text = await resp.text();
          let body;
          try { body = JSON.parse(text); } catch { body = {raw_text_preview: text.slice(0, 2000)}; }
          return {
            url_path: url.replace("https://marketing.kaspi.kz/advertising/products/api", ""),
            status: resp.status,
            ok: resp.ok,
            elapsed_ms: Math.round(performance.now() - started),
            body
          };
        }
        """,
        {"method": method, "url": url, "xsrfToken": xsrf_token, "payload": payload},
    )


def js_put_form(page: Page, *, url: str, xsrf_token: str, fields: dict[str, Any]) -> dict[str, Any]:
    return page.evaluate(
        """
        async ({url, xsrfToken, fields}) => {
          const fd = new FormData();
          for (const [key, value] of Object.entries(fields)) {
            if (Array.isArray(value)) {
              for (const item of value) fd.append(`${key}[]`, item);
            } else if (value !== undefined && value !== null) {
              fd.append(key, value);
            }
          }
          const headers = {
            "accept": "application/json, text/plain, */*",
            "x-requested-with": "XMLHttpRequest"
          };
          if (xsrfToken) headers["x-xsrf-token"] = xsrfToken;
          const started = performance.now();
          const resp = await fetch(url, {
            method: "PUT",
            credentials: "include",
            headers,
            body: fd
          });
          const text = await resp.text();
          let body;
          try { body = JSON.parse(text); } catch { body = {raw_text_preview: text.slice(0, 2000)}; }
          return {
            url_path: url.replace("https://marketing.kaspi.kz/advertising/products/api", ""),
            status: resp.status,
            ok: resp.ok,
            elapsed_ms: Math.round(performance.now() - started),
            body
          };
        }
        """,
        {"url": url, "xsrfToken": xsrf_token, "fields": fields},
    )


def campaign_summary(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "campaign_id": str(pick(row, "id", "Id") or ""),
        "campaign_name": str(pick(row, "name", "Name") or ""),
        "campaign_state": str(pick(row, "state", "State") or ""),
        "daily_budget": pick(row, "dailyBudget", "DailyBudget"),
        "default_bid": pick(row, "defaultBid", "DefaultBid"),
        "start_date": pick(row, "startDate", "StartDate"),
        "end_date": pick(row, "endDate", "EndDate"),
        "target_location_ids": pick(row, "targetLocationIds") or [],
        "merchant_id": str(pick(row, "merchantId") or MERCHANT_ID),
        "store_code": str(pick(row, "merchantBusinessId") or ""),
    }


def product_summary(row: Mapping[str, Any], *, campaign_id: str) -> dict[str, Any]:
    return {
        "campaign_id": str(pick(row, "campaignId", "campaignID") or campaign_id),
        "sku": str(pick(row, "sku", "id", "productId") or ""),
        "merchant_sku": str(pick(row, "merchantSku", "merchantSKU", "offerId") or ""),
        "title": str(pick(row, "title", "name", "productName") or ""),
        "product_state": str(pick(row, "productState", "state", "status") or ""),
        "bid": pick(row, "bid", "bidCpc", "avgBid"),
        "price": pick(row, "price"),
    }


def fetch_state(
    context: BrowserContext,
    headers: dict[str, str],
    *,
    run_dir: Path,
    label: str,
    target_date: str,
) -> dict[str, Any]:
    raw_dir = run_dir / "raw_api"
    list_url = f"{API_BASE}/v5/merchant/{MERCHANT_ID}/Campaigns?StartDate={target_date}&EndDate={target_date}"
    list_result = request_json(context, url=list_url, headers=headers)
    write_json(raw_dir / f"{label}_campaign_list.json", list_result)
    campaign_rows = [campaign_summary(row) for row in list_body(list_result.get("body"))]
    product_rows: list[dict[str, Any]] = []
    for campaign in campaign_rows:
        campaign_id = str(campaign["campaign_id"])
        products_url = (
            f"{API_BASE}/v5/merchant/{MERCHANT_ID}/campaign/{campaign_id}/products"
            f"?StartDate={target_date}&EndDate={target_date}"
        )
        products_result = request_json(context, url=products_url, headers=headers)
        write_json(raw_dir / f"{label}_{campaign_id}_products.json", products_result)
        for row in list_body(products_result.get("body")):
            product_rows.append(product_summary(row, campaign_id=campaign_id))
    write_json(run_dir / f"{label}_campaign_rows.json", campaign_rows)
    write_json(run_dir / f"{label}_product_rows.json", product_rows)
    return {"campaign_list": list_result, "campaign_rows": campaign_rows, "product_rows": product_rows}


def fetch_product_search(context: BrowserContext, headers: dict[str, str], *, run_dir: Path) -> list[dict[str, Any]]:
    raw_dir = run_dir / "raw_api"
    out: list[dict[str, Any]] = []
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


def build_budget_payload(campaign: Mapping[str, Any], new_budget: int) -> dict[str, Any]:
    default_bid = as_int(campaign.get("default_bid"))
    payload: dict[str, Any] = {
        "Id": str(campaign["campaign_id"]),
        "Name": str(campaign.get("campaign_name") or ""),
        "State": str(campaign.get("campaign_state") or ""),
        "DailyBudget": str(new_budget),
        "StartDate": str(campaign.get("start_date") or ""),
        "DefaultBid": str(default_bid if default_bid is not None else campaign.get("default_bid")),
        "targetLocationIds": campaign.get("target_location_ids") or [],
    }
    if campaign.get("end_date"):
        payload["EndDate"] = str(campaign["end_date"])
    return payload


def build_resume_form_fields(campaign_id: str) -> dict[str, list[str]]:
    return {"campaignIds": [str(campaign_id)]}


def response_success(result: Mapping[str, Any]) -> bool:
    if not result.get("ok"):
        return False
    body = result.get("body")
    if isinstance(body, dict):
        marker = body.get("result")
        if marker and marker != "Ok":
            return False
    return True


def execute_live_plan(page: Page, xsrf_token: str, plan: Mapping[str, Any], pre_state: Mapping[str, Any]) -> list[dict[str, Any]]:
    campaign_by_id = {str(row["campaign_id"]): row for row in pre_state["campaign_rows"]}
    results: list[dict[str, Any]] = []
    for target in plan["targets"]:
        if target["action"] != "resume_existing":
            continue
        campaign_id = str(target["campaign_id"])
        if "campaign_budget_update" in target["operations"]:
            payload = build_budget_payload(campaign_by_id[campaign_id], as_int(target["target_daily_budget_kzt"]) or 0)
            result = js_put_form(
                page,
                url=f"{API_BASE}/v3/merchant/{MERCHANT_ID}/Campaign/update",
                xsrf_token=xsrf_token,
                fields=payload,
            )
            results.append(
                {
                    "route_id": target["route_id"],
                    "operation": "campaign_budget_update",
                    "campaign_id": campaign_id,
                    "endpoint": result["url_path"],
                    "status": result["status"],
                    "ok": result["ok"],
                    "payload_redacted": payload,
                    "response_body_redacted": result["body"],
                }
            )
            if not response_success(result):
                return results

    create_targets = [target for target in plan["targets"] if target["action"] == "create_campaign"]
    for target in create_targets:
        payload = {
            "name": target["campaign_name"],
            "dailyBudget": target["target_daily_budget_kzt"],
            "products": [{"sku": target["product_sku"], "bid": target["target_bid_kzt"]}],
            "startDate": plan["target_date"],
            "targetLocationIds": [],
        }
        guard_errors = validate_campaign_add_payload(
            payload,
            expected_name=str(target["campaign_name"]),
            expected_sku=str(target["product_sku"]),
            expected_bid=as_int(target["target_bid_kzt"]) or 0,
            expected_budget=as_int(target["target_daily_budget_kzt"]) or 0,
            expected_start_date=str(plan["target_date"]),
        )
        if guard_errors:
            results.append(
                {
                    "route_id": target["route_id"],
                    "operation": "campaign_create",
                    "ok": False,
                    "status": "guard_blocked",
                    "guard_errors": guard_errors,
                    "payload_redacted": payload,
                }
            )
            return results
        result = js_fetch_json(
            page,
            method="POST",
            url=f"{API_BASE}/v3/merchant/{MERCHANT_ID}/Campaign/add",
            xsrf_token=xsrf_token,
            payload=payload,
        )
        results.append(
            {
                "route_id": target["route_id"],
                "operation": "campaign_create",
                "campaign_id": pick(unwrap(result["body"]) if isinstance(result.get("body"), dict) else {}, "id", "Id"),
                "endpoint": result["url_path"],
                "status": result["status"],
                "ok": result["ok"],
                "payload_redacted": payload,
                "response_body_redacted": result["body"],
            }
        )
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
        results.append(
            {
                "route_id": target["route_id"],
                "operation": "campaign_resume",
                "campaign_id": campaign_id,
                "endpoint": result["url_path"],
                "status": result["status"],
                "ok": result["ok"],
                "payload_redacted": payload,
                "response_body_redacted": result["body"],
            }
        )
        if not response_success(result):
            return results
    return results


def summarize_final_state(post_state: Mapping[str, Any], target_date: str) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []
    errors: list[str] = []
    try:
        plan = build_overlay_plan(
            campaign_rows=post_state["campaign_rows"],
            product_rows=post_state["product_rows"],
            product_search_rows=[],
            target_date=target_date,
        )
    except ValueError as exc:
        return {"plan": {}, "checks": checks, "errors": [f"post_verify_plan_unresolved:{exc}"]}
    for target in plan["targets"]:
        if target["action"] != "resume_existing":
            errors.append(f"target_not_existing_after_apply:{target['route_id']}")
            continue
        state_ok = _state_is_enabled(str(target["current_campaign_state"]))
        budget_ok = as_int(target["current_daily_budget_kzt"]) == as_int(target["target_daily_budget_kzt"])
        bid_ok = as_int(target["current_bid_kzt"]) == as_int(target["target_bid_kzt"])
        checks.append(
            {
                "route_id": target["route_id"],
                "campaign_id": target["campaign_id"],
                "campaign_state": target["current_campaign_state"],
                "daily_budget_kzt": target["current_daily_budget_kzt"],
                "bid_kzt": target["current_bid_kzt"],
                "state_ok": state_ok,
                "budget_ok": budget_ok,
                "bid_ok": bid_ok,
                "legacy_duplicate_campaign_ids_kept_paused": target.get("legacy_duplicate_campaign_ids_kept_paused") or [],
            }
        )
        if not state_ok:
            errors.append(f"post_state_not_enabled:{target['route_id']}:{target['current_campaign_state']}")
        if not budget_ok:
            errors.append(f"post_budget_mismatch:{target['route_id']}:{target['current_daily_budget_kzt']}")
        if not bid_ok:
            errors.append(f"post_bid_mismatch:{target['route_id']}:{target['current_bid_kzt']}")
        for duplicate_id in target.get("legacy_duplicate_campaign_ids_kept_paused") or []:
            duplicate_rows = [row for row in post_state["campaign_rows"] if str(row.get("campaign_id")) == str(duplicate_id)]
            if len(duplicate_rows) != 1 or not _state_is_paused(str(duplicate_rows[0].get("campaign_state") or "")):
                errors.append(f"legacy_duplicate_not_paused:{target['route_id']}:{duplicate_id}")
    return {"plan": plan, "checks": checks, "errors": errors}


def render_closeout(summary: Mapping[str, Any]) -> str:
    gate = str(summary.get("gate") or "YELLOW")
    lines = [
        f"Gate: {gate}",
        "",
        "# LINE31 Kaspi Internal Marketing Overlay Live Apply",
        "",
        f"- Generated at: `{summary.get('generated_at_local')}` Asia/Almaty.",
        f"- Store: `{STORE}` / merchant `{MERCHANT_ID}` / store code `{STORE_CODE}`.",
        f"- Mode: `{summary.get('mode')}`.",
        "",
        "Kaspi internal marketing is a cash-flow overlay and must be annotated as active background in Meta/LINE31 reads.",
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
    final = summary.get("final_state", {})
    for check in final.get("checks", []) if isinstance(final, dict) else []:
        lines.append(
            f"- `{check['route_id']}` campaign `{check['campaign_id']}`: "
            f"state `{check['campaign_state']}`, budget `{check['daily_budget_kzt']}`, BID `{check['bid_kzt']}`."
        )
    if summary.get("apply_results"):
        lines.extend(["", "## Apply Results", ""])
        for result in summary["apply_results"]:
            lines.append(
                f"- `{result.get('route_id')}` `{result.get('operation')}` "
                f"campaign `{result.get('campaign_id', '')}`: status `{result.get('status')}`, ok `{result.get('ok')}`."
            )
    artifacts = summary.get("artifact_paths") or {}
    if artifacts:
        lines.extend(["", "## Evidence Artifacts", ""])
        for label, path in artifacts.items():
            lines.append(f"- `{label}`: `{path}`")
    if summary.get("validation_errors"):
        lines.extend(["", "## Validation Errors", ""])
        for error in summary["validation_errors"]:
            lines.append(f"- `{error}`")
    lines.extend(
        [
            "",
            "## Boundaries",
            "",
            "- No Meta/LINE61/web/CRM/Telegram/workbook/DB/price/stock changes were made by this lane.",
            "- Non-target LINE31 campaigns were not resumed or duplicated.",
            "- Rollback/stop instruction: use the same merchant-scoped campaign suspend/pause control on only the listed LINE31 campaign IDs if emergency stop is needed.",
            "- Settings freeze: `72` hours after launch except emergency stop.",
            "",
        ]
    )
    return "\n".join(lines)


def run_overlay(args: argparse.Namespace) -> dict[str, Any]:
    run_dir = args.run_dir
    run_dir.mkdir(parents=True, exist_ok=True)
    summary: dict[str, Any] = {
        "schema_version": "web_auto.line31_kaspi_overlay_live.v1",
        "generated_at_local": now_local(),
        "mode": args.mode,
        "target_date": args.date,
        "store": STORE,
        "merchant_id": MERCHANT_ID,
        "store_code": STORE_CODE,
        "owner_approval_text": args.owner_approval_text,
        "secrets_or_tokens_recorded": False,
        "live_writes_executed": False,
        "launch_timestamp_almaty": "NOT_LAUNCHED",
        "preflight_plan": None,
        "apply_results": [],
        "final_state": {},
        "validation_errors": [],
        "artifact_paths": {
            "action_plan": str(run_dir / "ACTION_PLAN.json"),
            "pre_campaign_rows": str(run_dir / "pre_campaign_rows.json"),
            "pre_product_rows": str(run_dir / "pre_product_rows.json"),
            "product_search_rows": str(run_dir / "product_search_rows.json"),
            "post_campaign_rows": str(run_dir / "post_campaign_rows.json"),
            "post_product_rows": str(run_dir / "post_product_rows.json"),
            "summary": str(run_dir / "live_apply_summary.json"),
            "closeout": str(run_dir / "CLOSEOUT.md"),
        },
        "status": "UNKNOWN",
        "gate": "YELLOW",
    }

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
        cookie_names = {str(cookie.get("name")) for cookie in cookies}
        summary["auth"] = {
            "cookie_count": len(cookies),
            "has_auth_cookie": any(
                name.strip().lower().endswith("token") or name.strip().lower().endswith("session-id")
                for name in cookie_names
            ),
            "has_xsrf_cookie": bool(xsrf_token),
            "request_header_names": sorted(headers.keys()),
        }

        pre_state = fetch_state(context, headers, run_dir=run_dir, label="pre", target_date=args.date)
        product_search_rows = fetch_product_search(context, headers, run_dir=run_dir)
        try:
            plan = build_overlay_plan(
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
        write_json(run_dir / "ACTION_PLAN.json", plan)

        if args.mode == "live":
            results = execute_live_plan(page, xsrf_token, plan, pre_state)
            summary["apply_results"] = results
            summary["live_writes_executed"] = bool(results)
            failed = [result for result in results if not result.get("ok")]
            if failed:
                summary["validation_errors"].append(f"apply_failed:{failed[-1].get('operation')}:{failed[-1].get('status')}")
            time.sleep(3.0)
            post_state = fetch_state(context, headers, run_dir=run_dir, label="post", target_date=args.date)
            final_state = summarize_final_state(post_state, args.date)
            summary["final_state"] = final_state
            summary["validation_errors"].extend(final_state.get("errors") or [])
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
    parser = argparse.ArgumentParser(description="Launch/verify the LINE31 Kaspi internal marketing overlay.")
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--date", default=date.today().isoformat())
    parser.add_argument("--created-at-id", default=datetime.now(ALMATY).strftime("%Y%m%d_%H%M%S"))
    parser.add_argument("--mode", choices=["preflight", "live"], default="preflight")
    parser.add_argument("--owner-approval-text", required=True)
    parser.add_argument("--headless", action="store_true", default=True)
    parser.add_argument("--headed", action="store_false", dest="headless")
    args = parser.parse_args(argv)
    summary = run_overlay(args)
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
