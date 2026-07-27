from __future__ import annotations

import json
import os
import random
import sqlite3
import time
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable, Sequence
from urllib.parse import parse_qs, urlsplit

from playwright.sync_api import BrowserContext, Page, sync_playwright

from .kaspi_merchant_common import merchant_browser_launch_kwargs, normalize_store_name


ALMATY_TZ = "Asia/Almaty"
DEFAULT_MARKETING_DB = Path("data/kaspi_marketing.sqlite")
MARKETING_HOME_URL = "https://marketing.kaspi.kz/advertising/"
MARKETING_CAMPAIGNS_URL = "https://marketing.kaspi.kz/advertising/campaigns?tab=campaigns&activeTab=Enabled"
CAMPAIGN_DETAIL_URL = "https://marketing.kaspi.kz/advertising/campaigns/{campaign_id}?startDate={date}&endDate={date}"
CAMPAIGN_OVERVIEW_URL = (
    "https://marketing.kaspi.kz/advertising/products/api/v4/merchant/{merchant_id}/Overview/{campaign_id}"
)
CAMPAIGN_CORE_URL = (
    "https://marketing.kaspi.kz/advertising/products/api/v1/merchant/{merchant_id}/Campaign/{campaign_id}"
)
CAMPAIGN_PRODUCTS_URL = (
    "https://marketing.kaspi.kz/advertising/products/api/v5/merchant/{merchant_id}"
    "/campaign/{campaign_id}/products?StartDate={date}&EndDate={date}"
)
CAMPAIGN_CATEGORIES_URL = (
    "https://marketing.kaspi.kz/advertising/products/api/v4/merchant/{merchant_id}"
    "/campaign/{campaign_id}/products-categories?StartDate={date}&EndDate={date}"
)
CAMPAIGN_DAILY_VIEWS_URL = (
    "https://marketing.kaspi.kz/advertising/products/api/v3/merchant/{merchant_id}"
    "/overview/daily/{campaign_id}/views"
)

DEFAULT_MARKETING_MERCHANT_IDS = {
    "ACMEWEAR": "759051",
}
DEFAULT_MARKETING_STORE_CODES = {
    "ACMEWEAR": "30137883",
}


@dataclass(frozen=True)
class MarketingCredentials:
    store_name: str
    login: str
    password: str
    merchant_id: str
    store_code: str


def _load_env_assignments(path: Path | None) -> dict[str, str]:
    if path is None or not path.exists():
        return {}
    out: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if "=" not in line or line.strip().startswith("#"):
            continue
        key, value = line.split("=", 1)
        out[key.strip()] = value.strip().strip('"').strip("'")
    return out


def _merged_env(path: Path | None) -> dict[str, str]:
    out = _load_env_assignments(path)
    for key, value in os.environ.items():
        if value:
            out[key] = value
    return out


def _first_nonblank(env: dict[str, str], keys: Iterable[str]) -> str:
    for key in keys:
        value = str(env.get(key, "") or "").strip()
        if value:
            return value
    return ""


def resolve_marketing_credentials(
    store_name: str,
    *,
    env_file: Path | None = None,
    merchant_id: str | None = None,
    store_code: str | None = None,
) -> MarketingCredentials:
    store = normalize_store_name(store_name)
    env = _merged_env(env_file)
    login = _first_nonblank(
        env,
        [
            f"Kaspi_marketing_login_{store}",
            f"KASPI_MARKETING_LOGIN_{store}",
            "Kaspi_marketing_login",
            "KASPI_MARKETING_LOGIN",
        ],
    )
    password_value = _first_nonblank(
        env,
        [
            f"Kaspi_marketing_Password_{store}",
            f"KASPI_MARKETING_PASSWORD_{store}",
            "Kaspi_marketing_Password",
            "KASPI_MARKETING_PASSWORD",
        ],
    )
    resolved_merchant_id = str(
        merchant_id
        or _first_nonblank(
            env,
            [
                f"KASPI_MARKETING_MERCHANT_ID_{store}",
                "KASPI_MARKETING_MERCHANT_ID",
            ],
        )
        or DEFAULT_MARKETING_MERCHANT_IDS.get(store, "")
    ).strip()
    resolved_store_code = str(
        store_code
        or _first_nonblank(
            env,
            [
                f"KASPI_MARKETING_STORE_CODE_{store}",
                "KASPI_MARKETING_STORE_CODE",
            ],
        )
        or DEFAULT_MARKETING_STORE_CODES.get(store, store)
    ).strip()
    if not login or not password_value:
        raise ValueError(f"missing marketing credentials for {store}")
    if not resolved_merchant_id:
        raise ValueError(f"missing marketing merchant_id for {store}")
    if not resolved_store_code:
        raise ValueError(f"missing marketing store_code for {store}")
    return MarketingCredentials(
        store_name=store,
        login=login,
        password=password_value,
        merchant_id=resolved_merchant_id,
        store_code=resolved_store_code,
    )


def build_marketing_headers(cookies: list[dict[str, Any]], referer: str) -> dict[str, str]:
    token = ""
    for cookie in cookies or []:
        if cookie.get("name") == "XSRF-TOKEN":
            token = str(cookie.get("value") or "").strip()
            break
    headers = {
        "accept": "application/json, text/plain, */*",
        "referer": referer,
        "x-requested-with": "XMLHttpRequest",
    }
    if token:
        headers["x-xsrf-token"] = token
    return headers


def _pick(data: Any, *paths: str) -> Any:
    for path in paths:
        current = data
        ok = True
        for part in path.split("."):
            if isinstance(current, dict) and part in current:
                current = current[part]
            else:
                ok = False
                break
        if ok and current not in (None, ""):
            return current
    return None


def _parse_number(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if not text:
        return None
    text = text.replace("\u00a0", " ").replace("₸", "").replace("%", "").replace(" ", "").replace(",", ".")
    if not text:
        return None
    try:
        return float(text)
    except Exception:
        return None


def _parse_int(value: Any) -> int | None:
    num = _parse_number(value)
    if num is None:
        return None
    try:
        return int(round(num))
    except Exception:
        return None


def _login_required(page: Page) -> bool:
    try:
        if "sign-in" in page.url:
            return True
    except Exception:
        return True
    for selector in (
        "input[type='password']",
        "input[type='tel']",
        "a:has-text('Вход')",
        "button:has-text('Вход')",
    ):
        try:
            if page.locator(selector).count() > 0:
                return True
        except Exception:
            continue
    return False


def login_kaspi_marketing(page: Page, *, login_value: str, password_value: str) -> None:
    login_compact = "".join(ch for ch in str(login_value or "").strip() if ch.isdigit() or ch == "+")
    page.goto(MARKETING_HOME_URL, wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(1500)
    if not _login_required(page):
        page.goto(MARKETING_CAMPAIGNS_URL, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(1500)
        return

    if "sign-in" not in page.url:
        page.goto("https://marketing.kaspi.kz/sign-in", wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(1500)

    login_selectors = [
        "input[type='tel']",
        "input[name='login']",
        "input[name='phone']",
        "input[autocomplete='username']",
        "input[placeholder*='Телефон']",
        "input[placeholder*='телефон']",
        "input[type='text']",
    ]
    login_filled = False
    for candidate in (str(login_value or "").strip(), login_compact):
        if not candidate:
            continue
        for selector in login_selectors:
            inputs = page.locator(selector)
            for idx in range(min(inputs.count(), 5)):
                field = inputs.nth(idx)
                if not field.is_visible():
                    continue
                try:
                    field.fill(candidate)
                    login_filled = True
                    break
                except Exception:
                    continue
            if login_filled:
                break
        if login_filled:
            break
    if not login_filled:
        raise RuntimeError("marketing_login_field_not_found")

    if page.locator("input[type='password']").count() == 0:
        for label in ("Продолжить", "Далее", "Войти"):
            try:
                page.locator(f"button:has-text('{label}')").first.click(timeout=3000)
                break
            except Exception:
                continue
        page.wait_for_timeout(1200)

    page.locator("input[type='password']").first.fill(password_value)
    for selector in (
        "button[type='submit']",
        "button:has-text('Войти')",
        "button:has-text('Продолжить')",
        "button:has-text('Далее')",
    ):
        try:
            page.locator(selector).first.click(timeout=5000)
            break
        except Exception:
            continue
    page.wait_for_timeout(3500)
    if _login_required(page):
        raise RuntimeError("marketing_login_failed")
    page.goto(MARKETING_CAMPAIGNS_URL, wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(1500)


def _request_json(
    context: BrowserContext,
    *,
    url: str,
    headers: dict[str, str],
    max_attempts: int = 3,
    retry_statuses: Sequence[int] = (429, 500, 502, 503),
) -> Any:
    last_error: str = "request_failed"
    for attempt in range(1, max_attempts + 1):
        resp = context.request.get(url, headers=headers)
        if resp.status == 200:
            return resp.json()
        last_error = f"status_{resp.status}"
        if resp.status not in retry_statuses or attempt == max_attempts:
            try:
                body = resp.text()[:1000]
            except Exception:
                body = ""
            raise RuntimeError(f"{last_error}:{body}")
        time.sleep((2 ** attempt) + random.uniform(0, 1))
    raise RuntimeError(last_error)


def _collect_campaign_payloads(
    page: Page,
    *,
    merchant_id: str,
    campaign_id: str,
    target_date: str,
    timeout_seconds: float = 20.0,
) -> dict[str, Any]:
    target_detail = CAMPAIGN_DETAIL_URL.format(campaign_id=campaign_id, date=target_date)
    expected_fragments = {
        "overview": f"/api/v4/merchant/{merchant_id}/Overview/{campaign_id}",
        "core": f"/api/v1/merchant/{merchant_id}/Campaign/{campaign_id}",
        "daily_views": f"/api/v3/merchant/{merchant_id}/overview/daily/{campaign_id}/views",
    }
    dated_path_prefixes = {
        "products": f"/api/v5/merchant/{merchant_id}/campaign/{campaign_id}/products",
        "categories": f"/api/v4/merchant/{merchant_id}/campaign/{campaign_id}/products-categories",
    }
    payloads: dict[str, Any] = {}
    errors: list[dict[str, Any]] = []
    effective_ranges: dict[str, dict[str, str]] = {}

    def on_response(resp) -> None:
        url = resp.url
        parsed_url = urlsplit(url)
        matched_key = ""
        for key, fragment in expected_fragments.items():
            if fragment in url:
                matched_key = key
                break
        if not matched_key:
            for key, path_prefix in dated_path_prefixes.items():
                if parsed_url.path.endswith(path_prefix):
                    matched_key = key
                    break
        if not matched_key:
            return
        try:
            status = resp.status
        except Exception:
            status = 0
        if status != 200:
            errors.append({"key": matched_key, "status": status, "url": url})
            return
        try:
            payloads[matched_key] = resp.json()
        except Exception:
            try:
                payloads[matched_key] = json.loads(resp.text())
            except Exception:
                payloads[matched_key] = {"raw_text": resp.text()}
        if matched_key in dated_path_prefixes:
            query = {
                key.lower(): values
                for key, values in parse_qs(parsed_url.query, keep_blank_values=True).items()
            }
            start_date = (query.get("startdate") or [""])[0]
            end_date = (query.get("enddate") or [""])[0]
            if start_date and end_date:
                effective_ranges[matched_key] = {
                    "start_date": start_date,
                    "end_date": end_date,
                }

    page.on("response", on_response)
    try:
        page.goto(target_detail, wait_until="domcontentloaded", timeout=60000)
        deadline = time.time() + timeout_seconds
        while time.time() < deadline:
            if all(key in payloads for key in ("overview", "core", "products")):
                break
            page.wait_for_timeout(500)
    finally:
        try:
            page.remove_listener("response", on_response)
        except Exception:
            pass
    missing = [key for key in ("overview", "core", "products") if key not in payloads]
    if missing:
        raise RuntimeError(f"marketing_payloads_missing:{campaign_id}:{','.join(missing)}")
    payloads["_meta"] = {"detail_url": target_detail, "errors": errors}
    for key, effective_range in effective_ranges.items():
        payloads["_meta"][f"effective_{key}_range"] = effective_range
    return payloads


def _resolve_effective_ingestion_date(
    *,
    target_date: str,
    effective_products_range: Any,
) -> str:
    if not isinstance(effective_products_range, dict):
        return target_date
    start_date = str(effective_products_range.get("start_date") or "").strip()
    end_date = str(effective_products_range.get("end_date") or "").strip()
    if not start_date or not end_date:
        return target_date
    if start_date != end_date:
        raise RuntimeError(
            f"marketing_effective_products_range_not_single_day:{start_date}:{end_date}"
        )
    return start_date


def _list_payload(data: Any) -> list[dict[str, Any]]:
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    if isinstance(data, dict):
        for key in ("data", "items", "products", "rows"):
            value = data.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
    return []


def normalize_campaign_daily_row(
    *,
    target_date: str,
    merchant_id: str,
    store_code: str,
    campaign_id: str,
    core_payload: dict[str, Any] | None,
    overview_payload: dict[str, Any] | None,
    fetched_at: str,
) -> dict[str, Any]:
    core = core_payload or {}
    overview = overview_payload or {}
    campaign_name = str(
        _pick(core, "name", "campaignName", "title", "data.name", "data.campaignName", "data.title")
        or _pick(overview, "name", "campaignName", "title", "data.name", "data.campaignName", "data.title")
        or campaign_id
    ).strip()
    return {
        "date": target_date,
        "merchant_id": str(merchant_id).strip(),
        "store_code": str(store_code).strip(),
        "campaign_id": str(campaign_id).strip(),
        "campaign_name": campaign_name,
        "state": str(_pick(core, "state", "status", "data.state", "data.status") or "").strip(),
        "daily_budget": _parse_number(_pick(core, "dailyBudget", "dailyBudget.amount", "budget", "budget.amount", "data.dailyBudget", "data.budget")),
        "default_bid": _parse_number(_pick(core, "defaultBid", "bid", "bid.amount", "data.defaultBid", "data.bid")),
        "views": _parse_int(_pick(overview, "views", "viewCount", "totalViews", "data.views", "data.viewCount", "data.totalViews")),
        "clicks": _parse_int(_pick(overview, "clicks", "clickCount", "totalClicks", "data.clicks", "data.clickCount", "data.totalClicks")),
        "favorites": _parse_int(_pick(overview, "favorites", "favoriteCount", "data.favorites", "data.favoriteCount")),
        "carts": _parse_int(_pick(overview, "carts", "cartCount", "data.carts", "data.cartCount")),
        "ctr": _parse_number(_pick(overview, "ctr", "clickThroughRate", "data.ctr", "data.clickThroughRate")),
        "gmv": _parse_number(_pick(overview, "gmv", "orderAmount", "ordersAmount", "sumOrders", "data.gmv", "data.orderAmount", "data.ordersAmount")),
        "transactions": _parse_int(_pick(overview, "transactions", "ordersCount", "orderCount", "data.transactions", "data.ordersCount", "data.orderCount")),
        "cost": _parse_number(_pick(overview, "cost", "spend", "spent", "data.cost", "data.spend", "data.spent")),
        "crr": _parse_number(_pick(overview, "crr", "acos", "drra", "data.crr", "data.acos", "data.drra")),
        "report_state": str(_pick(core, "stateName", "statusName", "data.stateName", "data.statusName", "data.state") or "").strip(),
        "record_timestamp": fetched_at,
        "ingested_at": fetched_at,
    }


def normalize_campaign_product_rows(
    *,
    target_date: str,
    merchant_id: str,
    store_code: str,
    campaign_id: str,
    campaign_name: str,
    products_payload: Any,
    fetched_at: str,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for item in _list_payload(products_payload):
        rows.append(
            {
                "date": target_date,
                "merchant_id": str(merchant_id).strip(),
                "store_code": str(store_code).strip(),
                "campaign_id": str(campaign_id).strip(),
                "campaign_name": str(campaign_name or campaign_id).strip(),
                "sku_key": str(_pick(item, "sku", "skuKey", "id", "productId") or "").strip(),
                "product_name": str(_pick(item, "name", "productName", "title") or "").strip(),
                "product_status": str(_pick(item, "status", "state", "productState", "campaignState") or "").strip(),
                "bid_cpc": _parse_number(_pick(item, "bidCpc", "bid", "avgBid")),
                "avg_cpc": _parse_number(_pick(item, "avgCpc", "averageCpc")),
                "views": _parse_int(_pick(item, "views", "viewCount")),
                "clicks": _parse_int(_pick(item, "clicks", "clickCount")),
                "favorites": _parse_int(_pick(item, "favorites", "favoriteCount")),
                "carts": _parse_int(_pick(item, "carts", "cartCount")),
                "ctr": _parse_number(_pick(item, "ctr", "clickThroughRate")),
                "gmv": _parse_number(_pick(item, "gmv", "orderAmount", "ordersAmount")),
                "orders_total": _parse_int(_pick(item, "ordersTotal", "orders", "orderCount", "transactions")),
                "orders_direct": _parse_int(_pick(item, "ordersDirect", "directOrders", "directTransactions")),
                "orders_assisted": _parse_int(_pick(item, "ordersAssisted", "assistedOrders", "inDirectTransactions")),
                "conversion_order": _parse_number(_pick(item, "conversionOrder", "orderConversion", "cr")),
                "cost": _parse_number(_pick(item, "cost", "spend", "spent")),
                "acos_share": _parse_number(_pick(item, "acosShare", "acos", "drra")),
                "json_sku": str(_pick(item, "sku", "jsonSku", "productId") or "").strip(),
                "json_merchant_sku": str(_pick(item, "merchantSku", "merchantSKU", "offerId") or "").strip(),
                "bid_cpc_source": "api_current",
                "ingested_at": fetched_at,
            }
        )
    return rows


def ensure_marketing_tables(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS campaign_daily_history (
            run_id TEXT,
            ingested_at TEXT,
            date TEXT,
            merchant_id TEXT,
            store_code TEXT,
            campaign_id TEXT,
            campaign_name TEXT,
            state TEXT,
            daily_budget REAL,
            default_bid REAL,
            views INTEGER,
            clicks INTEGER,
            favorites INTEGER,
            carts INTEGER,
            ctr REAL,
            gmv REAL,
            transactions INTEGER,
            cost REAL,
            crr REAL,
            report_state TEXT,
            record_timestamp TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS campaign_daily_current (
            date TEXT,
            merchant_id TEXT,
            store_code TEXT,
            campaign_id TEXT,
            campaign_name TEXT,
            state TEXT,
            daily_budget REAL,
            default_bid REAL,
            views INTEGER,
            clicks INTEGER,
            favorites INTEGER,
            carts INTEGER,
            ctr REAL,
            gmv REAL,
            transactions INTEGER,
            cost REAL,
            crr REAL,
            report_state TEXT,
            record_timestamp TEXT,
            ingested_at TEXT,
            PRIMARY KEY (date, merchant_id, campaign_id)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS campaign_product_daily_history (
            run_id TEXT,
            ingested_at TEXT,
            date TEXT,
            merchant_id TEXT,
            store_code TEXT,
            campaign_id TEXT,
            campaign_name TEXT,
            sku_key TEXT,
            product_name TEXT,
            product_status TEXT,
            bid_cpc REAL,
            avg_cpc REAL,
            views INTEGER,
            clicks INTEGER,
            favorites INTEGER,
            carts INTEGER,
            ctr REAL,
            gmv REAL,
            orders_total INTEGER,
            orders_direct INTEGER,
            orders_assisted INTEGER,
            conversion_order REAL,
            cost REAL,
            acos_share REAL,
            json_sku TEXT,
            json_merchant_sku TEXT,
            bid_cpc_source TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS campaign_product_daily_current (
            date TEXT,
            merchant_id TEXT,
            store_code TEXT,
            campaign_id TEXT,
            campaign_name TEXT,
            sku_key TEXT,
            product_name TEXT,
            product_status TEXT,
            bid_cpc REAL,
            avg_cpc REAL,
            views INTEGER,
            clicks INTEGER,
            favorites INTEGER,
            carts INTEGER,
            ctr REAL,
            gmv REAL,
            orders_total INTEGER,
            orders_direct INTEGER,
            orders_assisted INTEGER,
            conversion_order REAL,
            cost REAL,
            acos_share REAL,
            json_sku TEXT,
            json_merchant_sku TEXT,
            ingested_at TEXT,
            bid_cpc_source TEXT,
            PRIMARY KEY (date, merchant_id, campaign_id, sku_key, json_merchant_sku)
        )
        """
    )
    conn.commit()


def persist_marketing_snapshot(
    *,
    db_path: Path,
    run_id: str,
    campaign_rows: list[dict[str, Any]],
    product_rows: list[dict[str, Any]],
) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    try:
        ensure_marketing_tables(conn)
        if campaign_rows:
            date_key = campaign_rows[0]["date"]
            merchant_id = campaign_rows[0]["merchant_id"]
            campaign_ids = [row["campaign_id"] for row in campaign_rows]
            placeholders = ",".join("?" for _ in campaign_ids)
            conn.execute(
                f"DELETE FROM campaign_daily_current WHERE date = ? AND merchant_id = ? AND campaign_id IN ({placeholders})",
                [date_key, merchant_id, *campaign_ids],
            )
            conn.executemany(
                """
                INSERT INTO campaign_daily_current (
                    date, merchant_id, store_code, campaign_id, campaign_name, state,
                    daily_budget, default_bid, views, clicks, favorites, carts, ctr,
                    gmv, transactions, cost, crr, report_state, record_timestamp, ingested_at
                ) VALUES (
                    :date, :merchant_id, :store_code, :campaign_id, :campaign_name, :state,
                    :daily_budget, :default_bid, :views, :clicks, :favorites, :carts, :ctr,
                    :gmv, :transactions, :cost, :crr, :report_state, :record_timestamp, :ingested_at
                )
                """,
                campaign_rows,
            )
            conn.executemany(
                """
                INSERT INTO campaign_daily_history (
                    run_id, ingested_at, date, merchant_id, store_code, campaign_id,
                    campaign_name, state, daily_budget, default_bid, views, clicks, favorites,
                    carts, ctr, gmv, transactions, cost, crr, report_state, record_timestamp
                ) VALUES (
                    :run_id, :ingested_at, :date, :merchant_id, :store_code, :campaign_id,
                    :campaign_name, :state, :daily_budget, :default_bid, :views, :clicks, :favorites,
                    :carts, :ctr, :gmv, :transactions, :cost, :crr, :report_state, :record_timestamp
                )
                """,
                [{**row, "run_id": run_id} for row in campaign_rows],
            )
        if product_rows:
            date_key = product_rows[0]["date"]
            merchant_id = product_rows[0]["merchant_id"]
            campaign_ids = sorted({row["campaign_id"] for row in product_rows})
            placeholders = ",".join("?" for _ in campaign_ids)
            conn.execute(
                f"DELETE FROM campaign_product_daily_current WHERE date = ? AND merchant_id = ? AND campaign_id IN ({placeholders})",
                [date_key, merchant_id, *campaign_ids],
            )
            conn.executemany(
                """
                INSERT INTO campaign_product_daily_current (
                    date, merchant_id, store_code, campaign_id, campaign_name, sku_key,
                    product_name, product_status, bid_cpc, avg_cpc, views, clicks, favorites, carts,
                    ctr, gmv, orders_total, orders_direct, orders_assisted, conversion_order,
                    cost, acos_share, json_sku, json_merchant_sku, ingested_at, bid_cpc_source
                ) VALUES (
                    :date, :merchant_id, :store_code, :campaign_id, :campaign_name, :sku_key,
                    :product_name, :product_status, :bid_cpc, :avg_cpc, :views, :clicks, :favorites, :carts,
                    :ctr, :gmv, :orders_total, :orders_direct, :orders_assisted, :conversion_order,
                    :cost, :acos_share, :json_sku, :json_merchant_sku, :ingested_at, :bid_cpc_source
                )
                """,
                product_rows,
            )
            conn.executemany(
                """
                INSERT INTO campaign_product_daily_history (
                    run_id, ingested_at, date, merchant_id, store_code, campaign_id, campaign_name,
                    sku_key, product_name, product_status, bid_cpc, avg_cpc, views, clicks,
                    favorites, carts, ctr, gmv, orders_total, orders_direct, orders_assisted,
                    conversion_order, cost, acos_share, json_sku, json_merchant_sku, bid_cpc_source
                ) VALUES (
                    :run_id, :ingested_at, :date, :merchant_id, :store_code, :campaign_id, :campaign_name,
                    :sku_key, :product_name, :product_status, :bid_cpc, :avg_cpc, :views, :clicks,
                    :favorites, :carts, :ctr, :gmv, :orders_total, :orders_direct, :orders_assisted,
                    :conversion_order, :cost, :acos_share, :json_sku, :json_merchant_sku, :bid_cpc_source
                )
                """,
                [{**row, "run_id": run_id} for row in product_rows],
            )
        conn.commit()
    finally:
        conn.close()


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def default_marketing_run_dir(store_name: str) -> Path:
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    return Path("runs/kaspi_marketing") / f"{ts}_{normalize_store_name(store_name).replace('-', '').lower()}"


def run_kaspi_marketing_fetch(
    *,
    creds: MarketingCredentials,
    campaign_ids: Sequence[str],
    target_date: str | None,
    run_dir: Path,
    db_path: Path,
    headless: bool,
) -> dict[str, Any]:
    campaign_ids = [str(cid).strip() for cid in campaign_ids if str(cid).strip()]
    if not campaign_ids:
        raise ValueError("at least one campaign_id is required")
    resolved_date = _resolve_target_date(target_date)
    run_dir.mkdir(parents=True, exist_ok=True)
    raw_dir = run_dir / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    fetched_at = datetime.now().isoformat()
    campaign_rows: list[dict[str, Any]] = []
    product_rows: list[dict[str, Any]] = []
    campaigns_summary: list[dict[str, Any]] = []
    with sync_playwright() as p:
        browser = p.chromium.launch(**merchant_browser_launch_kwargs(headless=headless))
        context = browser.new_context(viewport={"width": 1440, "height": 1400})
        page = context.new_page()
        login_kaspi_marketing(page, login_value=creds.login, password_value=creds.password)
        page.screenshot(path=str(run_dir / "marketing_home.png"), full_page=True)
        for campaign_id in campaign_ids:
            payloads = _collect_campaign_payloads(
                page,
                merchant_id=creds.merchant_id,
                campaign_id=campaign_id,
                target_date=resolved_date,
            )
            overview = payloads["overview"]
            core = payloads["core"]
            products = payloads["products"]
            categories = payloads.get("categories", {})
            daily_views = payloads.get("daily_views", {})
            payload_meta = payloads.get("_meta", {})
            effective_products_range = payload_meta.get("effective_products_range")
            effective_date = _resolve_effective_ingestion_date(
                target_date=resolved_date,
                effective_products_range=effective_products_range,
            )
            payload = {
                "campaign_id": campaign_id,
                "target_date": resolved_date,
                "effective_date": effective_date,
                "merchant_id": creds.merchant_id,
                "store_code": creds.store_code,
                "detail_url": payload_meta.get("detail_url", ""),
                "effective_products_range": effective_products_range,
                "effective_categories_range": payload_meta.get("effective_categories_range"),
                "cookies_seen": [cookie.get("name") for cookie in context.cookies("https://marketing.kaspi.kz")],
                "network_errors": payload_meta.get("errors", []),
                "overview": overview,
                "core": core,
                "products": products,
                "categories": categories,
                "daily_views": daily_views,
            }
            _write_json(raw_dir / f"campaign_{campaign_id}.json", payload)
            campaign_row = normalize_campaign_daily_row(
                target_date=effective_date,
                merchant_id=creds.merchant_id,
                store_code=creds.store_code,
                campaign_id=campaign_id,
                core_payload=core if isinstance(core, dict) else {},
                overview_payload=overview if isinstance(overview, dict) else {},
                fetched_at=fetched_at,
            )
            campaign_rows.append(campaign_row)
            normalized_products = normalize_campaign_product_rows(
                target_date=effective_date,
                merchant_id=creds.merchant_id,
                store_code=creds.store_code,
                campaign_id=campaign_id,
                campaign_name=campaign_row["campaign_name"],
                products_payload=products,
                fetched_at=fetched_at,
            )
            product_rows.extend(normalized_products)
            campaigns_summary.append(
                {
                    "campaign_id": campaign_id,
                    "campaign_name": campaign_row["campaign_name"],
                    "state": campaign_row["state"],
                    "views": campaign_row["views"],
                    "clicks": campaign_row["clicks"],
                    "transactions": campaign_row["transactions"],
                    "products_rows": len(normalized_products),
                }
            )
        context.close()
        browser.close()
    persist_marketing_snapshot(
        db_path=db_path,
        run_id=run_dir.name,
        campaign_rows=campaign_rows,
        product_rows=product_rows,
    )
    import pandas as pd

    campaign_csv = run_dir / "campaign_daily_current.csv"
    product_csv = run_dir / "campaign_product_daily_current.csv"
    pd.DataFrame(campaign_rows).to_csv(campaign_csv, index=False)
    pd.DataFrame(product_rows).to_csv(product_csv, index=False)
    summary = {
        "status": "success",
        "run_dir": str(run_dir),
        "store_name": creds.store_name,
        "merchant_id": creds.merchant_id,
        "store_code": creds.store_code,
        "target_date": resolved_date,
        "campaign_ids": campaign_ids,
        "campaign_count": len(campaign_rows),
        "product_rows": len(product_rows),
        "campaign_csv": str(campaign_csv),
        "product_csv": str(product_csv),
        "db_path": str(db_path),
        "campaigns": campaigns_summary,
    }
    _write_json(run_dir / "summary.json", summary)
    return summary


def _resolve_target_date(value: str | None) -> str:
    if value:
        return str(value).strip()
    return date.today().isoformat()
