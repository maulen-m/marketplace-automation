from __future__ import annotations

import csv
import hashlib
import http.server
import json
import logging
import os
import posixpath
import re
import sqlite3
import threading
import urllib.parse
import webbrowser
from datetime import datetime, timedelta
from pathlib import Path
from statistics import mean
from typing import Any, Sequence

from .autonomous_business_readonly import (
    REALIZED_ORDER_STATUSES,
    build_app_db_uri,
    connect_app_db_ro,
    fetch_daily_created_metrics,
    fetch_daily_shipped_metrics,
)
from .delivery_promise import DEFAULT_DELIVERY_PROMISE_ROOT, normalize_delivery_capture_health
from .kaspi_marketing import run_kaspi_marketing_fetch
from .marketing_experiments import (
    DEFAULT_AB_ROOT,
    DEFAULT_CHANGE_LOG,
    DEFAULT_EXPERIMENT_ROOT,
    DEFAULT_MARKETING_DB,
    DEFAULT_WATCH_ROOT,
    classify_marketing_category,
    dedupe_campaign_ids,
    fetch_order_period_rows,
    load_events,
    normalize_local_timestamp,
    now_local_text,
    summarize_orders,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEFAULT_DASHBOARD_CACHE_DIR = Path("runs/experiment_dashboard")
DEFAULT_DASHBOARD_PORT = 8765
LIVE_DASHBOARD_DIR_NAME = "live"
DEFAULT_STALE_MINUTES = 75
MARKETING_BASELINE_CACHE_FILE = "marketing_baseline_cache.json"
DEFAULT_EXTERNAL_MARKETING_DB = Path(
    "~/Documents/useful tables/Main crm spreadsheets/main tables/"
    "External_database/Kaspi_marketing/db/kaspi_marketing.db"
)

# Line61 economics defaults (from handoff doc)
LINE61_COGS = 5919
LINE61_COMMISSION = 0.125
LINE61_VAT = 0.04
LINE61_DELIVERY_FALLBACK = 1099.14
LINE61_LEAD_TIME = 22
LINE61_REORDER_CYCLE = 10
LINE61_BUFFER_DAYS = 14

# v2 consolidation thresholds
MIN_MAIN_ORDERS = 5
MIN_MAIN_UNITS = 5
MIN_REALIZED_SHIP_RATE = 0.10
MIN_CONFIDENT_AD_COVERAGE = 0.80
CAMPAIGN_LABELS = {
    "2545773": "ST",
    "2629982": "TRM",
}
LINE61_CANONICAL_SKU_KEY = "CL_NEW-CLO2_MEN_SUIT-61_BLACK"
DEFAULT_LINE61_STOCK_AUDIT_ROWS = [
    {
        "internal_size": "4XL",
        "stock_status": "out_of_stock",
        "first_verified_oos_at": "2026-04-11 21:03:55 +05",
        "evidence_path": (
            "runs/kaspi_pricelist_ops/20260411_210355_acmewear/"
            "downloads/acmewear_ARCHIVE.xlsx"
        ),
        "evidence_note": (
            "Earliest repo-held ACMEWEAR merchant download where all six "
            "Line61 4XL rows are in ARCHIVE with PP1..PP5=no."
        ),
        "reorder_policy_status": (
            "do_not_order_30_60_days_due_shr_supplier_repayment"
        ),
    }
]

SCENARIO_FIELDS = [
    "scenario_id",
    "scenario_label",
    "scenario_source",
    "window_start_at",
    "window_end_at",
    "price_policy",
    "bid_policy",
    "categories_included",
    "campaign_ids",
    "orders_count",
    "units_sold",
    "shipped_units",
    "shipped_revenue",
    "realized_avg_sell_price",
    "financial_units",
    "financial_basis",
    "gross_revenue",
    "avg_sell_price",
    "ad_spend",
    "ad_spend_allocated",
    "ad_spend_status",
    "ad_coverage_pct",
    "avg_cpc",
    "clicks",
    "days_in_window",
    "avg_daily_demand",
    "avg_daily_ad_cost",
    "ads_per_unit",
    "sales_truth_source",
    "product_net_revenue_total",
    "product_cogs_total",
    "product_profit_total",
    "ad_spend_raw",
    "ad_allocation_basis",
    "unit_profit_before_ads",
    "unit_profit_after_ads",
    "monthly_profit_estimate",
    "capital",
    "roic_estimate",
    "decision_gate",
    "confidence",
    "evidence_ids",
    "observation_note",
    "marketing_warnings",
    "order_warnings",
]


# ---------------------------------------------------------------------------
# Pure economics
# ---------------------------------------------------------------------------


def compute_economics(
    *,
    gross_price: float | None,
    units_sold: int,
    days_in_window: float,
    ad_spend: float | None = 0.0,
    cogs: float = LINE61_COGS,
    commission: float = LINE61_COMMISSION,
    vat: float = LINE61_VAT,
    delivery: float = LINE61_DELIVERY_FALLBACK,
    lead_time: int = LINE61_LEAD_TIME,
    reorder_cycle: int = LINE61_REORDER_CYCLE,
    buffer_days: int = LINE61_BUFFER_DAYS,
) -> dict[str, Any]:
    if gross_price is None or gross_price <= 0:
        return {
            "net_revenue": None,
            "unit_profit_before_ads": None,
            "unit_profit_after_ads": None,
            "ads_per_unit": None,
            "monthly_profit_estimate": None,
            "capital": None,
            "roic_estimate": None,
        }
    net_revenue = (gross_price * (1 - commission) - delivery) * (1 - vat)
    unit_profit_before_ads = net_revenue - cogs
    ads_per_unit = ad_spend / units_sold if ad_spend is not None and units_sold > 0 else None
    unit_profit_after_ads = (
        unit_profit_before_ads - ads_per_unit if ads_per_unit is not None else unit_profit_before_ads
    )
    if ad_spend is None:
        unit_profit_after_ads = None
    demand_per_day = units_sold / days_in_window if days_in_window > 0 else 0
    monthly_profit = unit_profit_after_ads * demand_per_day * 30 if demand_per_day > 0 and unit_profit_after_ads is not None else None
    safety_stock = demand_per_day * buffer_days
    cycle_stock = demand_per_day * (lead_time + reorder_cycle / 2)
    capital = (cycle_stock + safety_stock) * cogs if demand_per_day > 0 else None
    roic = monthly_profit / capital if capital and capital > 0 and monthly_profit is not None else None
    return {
        "net_revenue": _r2(net_revenue),
        "unit_profit_before_ads": _r2(unit_profit_before_ads),
        "unit_profit_after_ads": _r2(unit_profit_after_ads),
        "ads_per_unit": _r2(ads_per_unit),
        "monthly_profit_estimate": _r2(monthly_profit),
        "capital": _r2(capital),
        "roic_estimate": _r4(roic),
    }


def compute_economics_from_product_profit(
    *,
    product_profit_total: float | None,
    product_cogs_total: float | None,
    units_sold: int,
    days_in_window: float,
    ad_spend: float | None = 0.0,
    lead_time: int = LINE61_LEAD_TIME,
    reorder_cycle: int = LINE61_REORDER_CYCLE,
    buffer_days: int = LINE61_BUFFER_DAYS,
) -> dict[str, Any]:
    if product_profit_total is None or units_sold <= 0:
        return {
            "net_revenue": None,
            "unit_profit_before_ads": None,
            "unit_profit_after_ads": None,
            "ads_per_unit": None,
            "monthly_profit_estimate": None,
            "capital": None,
            "roic_estimate": None,
        }
    unit_profit_before_ads = product_profit_total / units_sold
    ads_per_unit = ad_spend / units_sold if ad_spend is not None else None
    unit_profit_after_ads = (
        unit_profit_before_ads - ads_per_unit if ads_per_unit is not None else None
    )
    demand_per_day = units_sold / days_in_window if days_in_window > 0 else 0
    monthly_profit = unit_profit_after_ads * demand_per_day * 30 if demand_per_day > 0 and unit_profit_after_ads is not None else None
    avg_cogs = (
        product_cogs_total / units_sold
        if product_cogs_total is not None and product_cogs_total > 0
        else LINE61_COGS
    )
    safety_stock = demand_per_day * buffer_days
    cycle_stock = demand_per_day * (lead_time + reorder_cycle / 2)
    capital = (cycle_stock + safety_stock) * avg_cogs if demand_per_day > 0 else None
    roic = monthly_profit / capital if capital and capital > 0 and monthly_profit is not None else None
    return {
        "net_revenue": None,
        "unit_profit_before_ads": _r2(unit_profit_before_ads),
        "unit_profit_after_ads": _r2(unit_profit_after_ads),
        "ads_per_unit": _r2(ads_per_unit),
        "monthly_profit_estimate": _r2(monthly_profit),
        "capital": _r2(capital),
        "roic_estimate": _r4(roic),
    }


def apply_decision_gate(
    *,
    orders_count: int,
    units_sold: int,
    unit_profit_after_ads: float | None,
    roic_estimate: float | None,
    source_statuses: Sequence[dict[str, Any]],
) -> tuple[str, str]:
    critical_stale = any(
        s.get("source_name") in {"marketing_db", "ab_order_db"}
        and s.get("freshness_status") not in ("OK",)
        for s in source_statuses
    )
    if orders_count == 0 or units_sold == 0:
        return "INSUFFICIENT_DATA", "low"
    if unit_profit_after_ads is None:
        return "REVIEW", "low"
    if unit_profit_after_ads < 0:
        return "FLAG", "high" if not critical_stale else "medium"
    if critical_stale:
        return "REVIEW", "low"
    if roic_estimate is not None and roic_estimate >= 0.20:
        return "ORDER_FULL", "high"
    if roic_estimate is not None and roic_estimate >= 0.10:
        return "REVIEW", "medium"
    if orders_count < 5:
        return "INSUFFICIENT_DATA", "low"
    return "ORDER_FULL", "medium"


# ---------------------------------------------------------------------------
# Timeline algorithm
# ---------------------------------------------------------------------------


def _parse_dt(text: str) -> datetime | None:
    text = _resolve_relative_date(text)
    if not text:
        return None
    # Strip fractional seconds if present (e.g. .598596)
    if "." in text:
        text = text.split(".")[0]
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def _resolve_relative_date(value: Any) -> str:
    text = str(value or "").strip()
    if text.lower() in {"today", "now"}:
        return datetime.now().strftime("%Y-%m-%d")
    return text


def _parse_boundary(text: str, *, is_end: bool) -> datetime | None:
    raw = _resolve_relative_date(text)
    dt = _parse_dt(raw)
    if dt is None:
        return None
    if len(raw) == 10 and raw[4:5] == "-" and raw[7:8] == "-":
        # Dashboard date filters are inclusive; internal windows are end-exclusive.
        if is_end:
            return dt + timedelta(days=1)
        return dt.replace(hour=0, minute=0, second=0, microsecond=0)
    return dt


def _dt_text(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def _scenario_key(price_policy: str, bid_policy: str) -> str:
    canonical = f"{price_policy}|{bid_policy}"
    return hashlib.sha1(canonical.encode()).hexdigest()[:12]


def _window_key(scenario_id: str, window_start_at: str) -> str:
    return f"{scenario_id}_{window_start_at}"


def _walk_change_timeline(
    events: Sequence[dict[str, Any]],
    date_from: str,
    date_to: str,
    campaign_ids: Sequence[str],
) -> list[dict[str, Any]]:
    dt_from = _parse_boundary(date_from, is_end=False)
    dt_to = _parse_boundary(date_to, is_end=True)
    if dt_from is None or dt_to is None:
        return []

    # Filter events relevant to our campaigns
    relevant = []
    for ev in events:
        cid = str(ev.get("campaign_id") or "").strip()
        if campaign_ids and cid not in campaign_ids:
            continue
        relevant.append(ev)

    # Sort by effective_at
    relevant.sort(key=lambda e: str(e.get("effective_at") or ""))

    # Initialize state from old_value of earliest events (pre-window state)
    state: dict[tuple[str, str], str] = {}
    for ev in relevant:
        key = (str(ev.get("campaign_id") or ""), str(ev.get("metric_name") or ""))
        ev_dt = _parse_dt(str(ev.get("effective_at") or ""))
        if ev_dt and ev_dt <= dt_from and key not in state:
            state[key] = str(ev.get("new_value") or "")
        elif key not in state:
            old_val = str(ev.get("old_value") or "").strip()
            if old_val:
                state[key] = old_val

    # Collect breakpoints within [dt_from, dt_to]
    breakpoints: list[datetime] = [dt_from]
    for ev in relevant:
        ev_dt = _parse_dt(str(ev.get("effective_at") or ""))
        if ev_dt and dt_from < ev_dt < dt_to:
            breakpoints.append(ev_dt)
    breakpoints.append(dt_to)
    breakpoints = sorted(set(breakpoints))

    # Walk timeline
    windows: list[dict[str, Any]] = []
    for i in range(len(breakpoints) - 1):
        bp = breakpoints[i]
        bp_next = breakpoints[i + 1]

        # Apply events at this breakpoint
        for ev in relevant:
            ev_dt = _parse_dt(str(ev.get("effective_at") or ""))
            if ev_dt == bp:
                key = (str(ev.get("campaign_id") or ""), str(ev.get("metric_name") or ""))
                state[key] = str(ev.get("new_value") or "")

        price_policy = _derive_price_policy(state)
        bid_policy = _derive_bid_policy(state, campaign_ids)
        windows.append({
            "window_start_at": _dt_text(bp),
            "window_end_at": _dt_text(bp_next),
            "price_policy": price_policy,
            "bid_policy": bid_policy,
            "state_snapshot": dict(state),
        })

    # Merge consecutive windows with same scenario key
    return _merge_same_scenario_windows(windows)


def _derive_price_policy(state: dict[tuple[str, str], str]) -> str:
    prices = []
    for (_, metric), val in state.items():
        if metric in ("price", "sell_price"):
            prices.append(val)
    if prices:
        return "/".join(sorted(set(prices)))
    return "unknown"


def _derive_bid_policy(state: dict[tuple[str, str], str], campaign_ids: Sequence[str]) -> str:
    parts = []
    for cid in sorted(campaign_ids):
        bid_val = state.get((cid, "bid"), "")
        if not bid_val:
            bid_val = state.get((cid, "bid_cpc"), "")
        if bid_val:
            parts.append(f"{cid}:{bid_val}")
    if parts:
        return "/".join(parts)
    return "unknown"


def _merge_same_scenario_windows(windows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not windows:
        return []
    merged: list[dict[str, Any]] = [windows[0]]
    for w in windows[1:]:
        prev = merged[-1]
        if prev["price_policy"] == w["price_policy"] and prev["bid_policy"] == w["bid_policy"]:
            prev["window_end_at"] = w["window_end_at"]
        else:
            merged.append(w)
    return merged


# ---------------------------------------------------------------------------
# Source status
# ---------------------------------------------------------------------------


def build_source_status(
    *,
    marketing_db: Path = DEFAULT_MARKETING_DB,
    external_marketing_db: Path = DEFAULT_EXTERNAL_MARKETING_DB,
    change_log: Path = DEFAULT_CHANGE_LOG,
    ab_root: Path = DEFAULT_AB_ROOT,
    watcher_root: Path = DEFAULT_WATCH_ROOT,
    delivery_watch_root: Path = DEFAULT_DELIVERY_PROMISE_ROOT,
) -> list[dict[str, Any]]:
    now = datetime.now()
    rows: list[dict[str, Any]] = []

    # Marketing DB
    rows.append(_probe_sqlite_source(
        source_name="marketing_db",
        source_type="sqlite",
        path=marketing_db,
        table="campaign_daily_history",
        time_col="ingested_at",
        now=now,
    ))

    # External marketing history is optional read-only context for historical baselines.
    external_row = _probe_sqlite_source(
        source_name="external_marketing_db",
        source_type="sqlite_readonly",
        path=external_marketing_db,
        table="campaign_product_daily_history",
        time_col="ingested_at",
        now=now,
        readonly=True,
    )
    external_row["required"] = False
    if external_row["freshness_status"] in {"MISSING", "FETCH_FAILED", "STALE"}:
        external_row["freshness_status"] = "OPTIONAL_UNAVAILABLE"
        external_row["failure_step"] = external_row.get("failure_step") or "optional_context"
        external_row["failure_message"] = external_row.get("failure_message") or "optional external marketing context unavailable"
    rows.append(external_row)

    # Change log
    rows.append(_probe_csv_source(
        source_name="change_log",
        source_type="csv",
        path=change_log,
        time_col="effective_at",
        now=now,
    ))

    # AB order DB
    ab_db = Path(str(ab_root)) / "db" / "app.db"
    rows.append(_probe_sqlite_source(
        source_name="ab_order_db",
        source_type="sqlite_readonly",
        path=ab_db,
        table="fact_orders_kaspi",
        time_col="created_at",
        now=now,
        readonly=True,
    ))

    # Watcher heartbeat
    rows.append(_probe_heartbeat(
        source_name="watcher_heartbeat",
        watcher_root=watcher_root,
        now=now,
    ))

    rows.append(_probe_heartbeat(
        source_name="delivery_promise_watch",
        watcher_root=delivery_watch_root,
        now=now,
    ))

    return rows


def _probe_sqlite_source(
    *,
    source_name: str,
    source_type: str,
    path: Path,
    table: str,
    time_col: str,
    now: datetime,
    readonly: bool = False,
) -> dict[str, Any]:
    row: dict[str, Any] = {
        "source_name": source_name,
        "source_type": source_type,
        "path_or_endpoint": str(path),
        "required": True,
        "last_success_at": None,
        "freshness_status": "MISSING",
        "row_count": 0,
        "max_event_at": None,
        "failure_step": None,
        "failure_message": None,
    }
    if not path.exists():
        return row
    try:
        if readonly:
            uri = f"file:{path}?mode=ro"
            conn = sqlite3.connect(uri, uri=True)
        else:
            conn = sqlite3.connect(path)
        with conn:
            count = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            max_ts = conn.execute(f"SELECT MAX({time_col}) FROM {table}").fetchone()[0] or ""
        conn.close()
        row["row_count"] = count
        row["max_event_at"] = str(max_ts)
        row["last_success_at"] = str(max_ts)
        max_dt = _parse_dt(str(max_ts))
        if max_dt and (now - max_dt).total_seconds() < DEFAULT_STALE_MINUTES * 60:
            row["freshness_status"] = "OK"
        elif max_dt:
            row["freshness_status"] = "STALE"
        else:
            row["freshness_status"] = "SCHEMA_FAILED"
    except sqlite3.DatabaseError as exc:
        row["freshness_status"] = "FETCH_FAILED"
        row["failure_step"] = "sqlite_query"
        row["failure_message"] = str(exc)
    return row


def _probe_csv_source(
    *,
    source_name: str,
    source_type: str,
    path: Path,
    time_col: str,
    now: datetime,
) -> dict[str, Any]:
    row: dict[str, Any] = {
        "source_name": source_name,
        "source_type": source_type,
        "path_or_endpoint": str(path),
        "required": True,
        "last_success_at": None,
        "freshness_status": "MISSING",
        "row_count": 0,
        "max_event_at": None,
        "failure_step": None,
        "failure_message": None,
    }
    if not path.exists():
        return row
    try:
        with path.open("r", encoding="utf-8") as fh:
            reader = csv.DictReader(fh)
            rows = list(reader)
        row["row_count"] = len(rows)
        max_ts = ""
        for r in rows:
            ts = str(r.get(time_col) or "")
            if ts > max_ts:
                max_ts = ts
        row["max_event_at"] = max_ts or None
        row["last_success_at"] = max_ts or None
        row["freshness_status"] = "OK" if rows else "STALE"
    except Exception as exc:
        row["freshness_status"] = "FETCH_FAILED"
        row["failure_step"] = "csv_read"
        row["failure_message"] = str(exc)
    return row


def _probe_heartbeat(
    *,
    source_name: str,
    watcher_root: Path,
    now: datetime,
) -> dict[str, Any]:
    row: dict[str, Any] = {
        "source_name": source_name,
        "source_type": "heartbeat_json",
        "path_or_endpoint": str(watcher_root / "latest_heartbeat.json"),
        "required": True,
        "last_success_at": None,
        "freshness_status": "MISSING",
        "row_count": 0,
        "max_event_at": None,
        "failure_step": None,
        "failure_message": None,
    }
    hb_path = watcher_root / "latest_heartbeat.json"
    if not hb_path.exists():
        return row
    try:
        data = json.loads(hb_path.read_text(encoding="utf-8"))
        last_at = str(data.get("generated_at") or data.get("last_success_at") or "")
        row["last_success_at"] = last_at or None
        row["max_event_at"] = last_at or None
        row["row_count"] = 1
        last_dt = _parse_dt(last_at)
        reported_status = str(data.get("heartbeat_status") or data.get("freshness_status") or "").strip().lower()
        run_status = str(data.get("status") or "").strip().lower()
        reported_failure = reported_status in {"red", "failed", "failure"} or run_status.endswith("_failed")
        if reported_failure:
            row["freshness_status"] = "FETCH_FAILED"
            row["failure_step"] = "heartbeat_reported_failure"
            row["failure_message"] = str(data.get("error") or data.get("failure_message") or run_status or reported_status)
        elif last_dt and (now - last_dt).total_seconds() < DEFAULT_STALE_MINUTES * 60:
            row["freshness_status"] = "OK"
        elif last_dt:
            row["freshness_status"] = "STALE"
        else:
            row["freshness_status"] = "SCHEMA_FAILED"
    except Exception as exc:
        row["freshness_status"] = "FETCH_FAILED"
        row["failure_step"] = "heartbeat_read"
        row["failure_message"] = str(exc)
    return row


# ---------------------------------------------------------------------------
# Marketing metrics query
# ---------------------------------------------------------------------------


def _fetch_window_marketing_metrics(
    *,
    marketing_db: Path,
    campaign_ids: Sequence[str],
    date_from: str,
    date_to: str,
) -> dict[str, Any]:
    result = {
        "ad_spend": None,
        "ad_spend_allocated": 0.0,
        "ad_spend_status": "MISSING",
        "clicks": 0,
        "views": 0,
        "avg_cpc": None,
        "rows": 0,
        "warnings": [],
    }
    if not marketing_db.exists() or not campaign_ids:
        result["warnings"].append("marketing_db_missing_or_no_campaigns")
        return result
    dt_start = _parse_dt(str(date_from))
    dt_end = _parse_dt(str(date_to))
    if dt_start is None or dt_end is None or dt_end <= dt_start:
        result["warnings"].append("invalid_marketing_window")
        result["ad_spend_status"] = "SCHEMA_FAILED"
        return result

    full_dates, partial_dates = _full_calendar_dates_for_window(dt_start, dt_end)
    if partial_dates:
        result["warnings"].append("partial_day_marketing_unallocated:" + ",".join(partial_dates))
        result["ad_spend_status"] = "PARTIAL_DAY_UNALLOCATED"
    if not full_dates:
        return result

    placeholders = ",".join("?" for _ in campaign_ids)
    date_placeholders = ",".join("?" for _ in full_dates)
    sql = f"""
        WITH ranked AS (
            SELECT *, ROW_NUMBER() OVER (
                PARTITION BY date, campaign_id ORDER BY ingested_at DESC
            ) AS rn
            FROM campaign_daily_history
            WHERE campaign_id IN ({placeholders})
              AND date IN ({date_placeholders})
        )
        SELECT date, campaign_id, cost, clicks, views
        FROM ranked WHERE rn = 1
    """
    try:
        with sqlite3.connect(marketing_db) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(sql, [*campaign_ids, *full_dates]).fetchall()
        total_cost = 0.0
        for r in rows:
            try:
                total_cost += float(r["cost"] or 0)
            except (ValueError, TypeError):
                pass
            try:
                result["clicks"] += int(r["clicks"] or 0)
            except (ValueError, TypeError):
                pass
            try:
                result["views"] += int(r["views"] or 0)
            except (ValueError, TypeError):
                pass
        result["rows"] = len(rows)
        result["ad_spend_allocated"] = round(total_cost, 2)
        if result["clicks"] > 0:
            result["avg_cpc"] = round(total_cost / result["clicks"], 2)
        expected_rows = len(full_dates) * len(campaign_ids)
        result["expected_rows"] = expected_rows
        result["ad_coverage_pct"] = _r4(len(rows) / expected_rows) if expected_rows else None
        if len(rows) < expected_rows:
            result["warnings"].append(f"marketing_daily_coverage_incomplete:rows={len(rows)}/expected={expected_rows}")
            if result["ad_spend_status"] == "MISSING":
                result["ad_spend_status"] = "INCOMPLETE_COVERAGE"
        elif result["ad_spend_status"] == "MISSING":
            result["ad_spend_status"] = "OK"
            result["ad_spend"] = round(total_cost, 2)
    except sqlite3.DatabaseError as exc:
        result["warnings"].append(f"marketing_db_error:{exc}")
        result["ad_spend_status"] = "FETCH_FAILED"
    return result


def _full_calendar_dates_for_window(dt_start: datetime, dt_end: datetime) -> tuple[list[str], list[str]]:
    full_dates: list[str] = []
    partial_dates: list[str] = []
    cursor = dt_start.date()
    final = (dt_end - timedelta(microseconds=1)).date()
    while cursor <= final:
        day_start = datetime.combine(cursor, datetime.min.time())
        day_end = day_start + timedelta(days=1)
        overlap_start = max(dt_start, day_start)
        overlap_end = min(dt_end, day_end)
        if overlap_start < overlap_end:
            if overlap_start == day_start and overlap_end == day_end:
                full_dates.append(cursor.isoformat())
            else:
                partial_dates.append(cursor.isoformat())
        cursor += timedelta(days=1)
    return full_dates, partial_dates


def _calendar_dates_touched(date_from: str, date_to: str) -> list[str]:
    start = _parse_dt(date_from)
    end = _parse_dt(date_to)
    if start is None or end is None:
        return []
    if end < start:
        return []
    cursor = start.date()
    final = end.date()
    out: list[str] = []
    while cursor <= final:
        out.append(cursor.isoformat())
        cursor += timedelta(days=1)
    return out


def _summarize_shipped_orders(rows: Sequence[dict[str, Any]]) -> tuple[int, float]:
    shipped_units = 0
    shipped_revenue = 0.0
    for row in rows:
        if not row.get("is_shipped"):
            continue
        try:
            qty = int(row.get("quantity") or 1)
        except Exception:
            qty = 1
        try:
            price = float(row.get("unit_price_kzt") or 0)
        except Exception:
            price = 0.0
        shipped_units += qty
        shipped_revenue += qty * price
    return shipped_units, shipped_revenue


def _evidence_ids_for_window(
    events: Sequence[dict[str, Any]],
    window: dict[str, Any],
    campaign_ids: Sequence[str],
) -> list[str]:
    w_start = _parse_dt(str(window.get("window_start_at") or ""))
    w_end = _parse_dt(str(window.get("window_end_at") or ""))
    out: list[str] = []
    for ev in events:
        cid = str(ev.get("campaign_id") or "").strip()
        event_id = str(ev.get("event_id") or "").strip()
        if not event_id or (campaign_ids and cid not in campaign_ids):
            continue
        ev_dt = _parse_dt(str(ev.get("effective_at") or ""))
        if ev_dt is None:
            continue
        is_boundary = (w_start and ev_dt == w_start) or (w_end and ev_dt == w_end)
        is_inside = bool(w_start and w_end and w_start < ev_dt < w_end)
        defines_prior_state = bool(w_end and ev_dt == w_end and str(ev.get("old_value") or "").strip())
        if is_boundary or is_inside or defines_prior_state:
            if event_id not in out:
                out.append(event_id)
    return out


# ---------------------------------------------------------------------------
# Observed price baselines
# ---------------------------------------------------------------------------


def _safe_float(value: Any) -> float | None:
    try:
        if value is None or str(value).strip() == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        if value is None or str(value).strip() == "":
            return default
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _median(values: Sequence[float | int | None]) -> float | None:
    clean = sorted(float(v) for v in values if v is not None)
    if not clean:
        return None
    return _r2(_percentile(clean, 0.5))


def _percentile(values: Sequence[float | int | None], q: float) -> float | None:
    clean = sorted(float(v) for v in values if v is not None)
    if not clean:
        return None
    if len(clean) == 1:
        return clean[0]
    q = max(0.0, min(1.0, q))
    pos = (len(clean) - 1) * q
    lower = int(pos)
    upper = min(lower + 1, len(clean) - 1)
    weight = pos - lower
    return clean[lower] * (1 - weight) + clean[upper] * weight


def _price_policy_from_order(row: dict[str, Any]) -> str | None:
    price = _safe_float(row.get("unit_price_kzt"))
    if price is None or price <= 0:
        return None
    return str(int(round(price)))


def _marketing_candidate_paths(
    marketing_db: Path,
    external_marketing_db: Path,
) -> list[Path]:
    paths: list[Path] = []
    for path in (external_marketing_db, marketing_db):
        p = Path(path)
        if p.exists() and p not in paths:
            paths.append(p)
    return paths


def _sqlite_relation_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE name = ? AND type IN ('table', 'view') LIMIT 1",
        [name],
    ).fetchone()
    return row is not None


def _safe_qty(row: dict[str, Any]) -> int:
    qty = _safe_float(row.get("quantity") if "quantity" in row else row.get("units"))
    if qty is None or qty <= 0:
        return 1
    return max(int(round(qty)), 1)


def _fallback_product_profit_for_row(row: dict[str, Any]) -> tuple[float, float, float]:
    qty = _safe_qty(row)
    gross_price = _safe_float(row.get("unit_price_kzt"))
    if gross_price is None or gross_price <= 0:
        return 0.0, LINE61_COGS * qty, 0.0
    econ = compute_economics(
        gross_price=gross_price,
        units_sold=qty,
        days_in_window=1,
        ad_spend=0,
    )
    unit_profit = _safe_float(econ.get("unit_profit_before_ads")) or 0.0
    net_revenue = _safe_float(econ.get("net_revenue")) or 0.0
    return net_revenue * qty, LINE61_COGS * qty, unit_profit * qty


def _product_profit_totals(rows: Sequence[dict[str, Any]]) -> dict[str, float | None]:
    if not rows:
        return {
            "product_net_revenue_total": None,
            "product_cogs_total": None,
            "product_profit_total": None,
        }
    net_total = 0.0
    cogs_total = 0.0
    profit_total = 0.0
    for row in rows:
        qty = _safe_qty(row)
        profit = _safe_float(row.get("profit_kzt"))
        cogs = _safe_float(row.get("cogs_kzt"))
        net = _safe_float(row.get("net_rev_kzt"))
        if profit is None:
            fallback_net, fallback_cogs, fallback_profit = _fallback_product_profit_for_row(row)
            net_total += fallback_net
            cogs_total += fallback_cogs
            profit_total += fallback_profit
            continue
        profit_total += profit
        cogs_total += cogs if cogs is not None else LINE61_COGS * qty
        net_total += net if net is not None else profit + (cogs if cogs is not None else LINE61_COGS * qty)
    return {
        "product_net_revenue_total": _r2(net_total),
        "product_cogs_total": _r2(cogs_total),
        "product_profit_total": _r2(profit_total),
    }


def _sales_truth_source_label(rows: Sequence[dict[str, Any]]) -> str:
    sources = sorted(
        {
            str(row.get("sales_truth_source") or "").strip()
            for row in rows
            if str(row.get("sales_truth_source") or "").strip()
        }
    )
    if not sources:
        return "unknown"
    if len(sources) == 1:
        return sources[0]
    return "mixed:" + ",".join(sources)


def _order_date_units(rows: Sequence[dict[str, Any]]) -> dict[str, int]:
    out: dict[str, int] = {}
    for row in rows:
        created_at = str(row.get("created_at") or "").strip()
        if not created_at:
            continue
        date = created_at[:10]
        out[date] = out.get(date, 0) + _safe_qty(row)
    return out


def is_out_of_stock_warehouse_values(values: Sequence[Any]) -> bool:
    """Return True when every warehouse value is effectively not sellable."""
    saw_value = False
    for value in values:
        text = str(value or "").strip().lower()
        if text in {"", "no", "нет", "none", "nan", "-"}:
            continue
        saw_value = True
        numeric = _safe_float(text.replace(",", "."))
        if numeric is None:
            return False
        if numeric > 0:
            return False
    return True if values else False if saw_value else True


def infer_line61_size_from_sku(sku: str, model: str | None = None) -> str | None:
    """Infer Line61 internal letter size from explicit size tokens only.

    Numeric Kaspi sizes such as 56/58/60 are intentionally not mapped here:
    the price/ads comparison must not treat numeric-only evidence as internal
    4XL without a canonical SKU token.
    """
    text = f"{sku or ''} {model or ''}".upper()
    if not any(token in text for token in ("SUIT-61", "LINE61", "CL_NEW-CLO2_MEN_SUIT-61_BLACK", "OF_SUIT-61_BLK")):
        return None
    compact = text.replace("-", "_")
    for size in ("4XL", "3XL", "2XL"):
        if re_search_size_token(compact, size):
            return size
    if re_search_size_token(compact, "XL"):
        return "XL"
    for size in ("L", "M", "S"):
        if re_search_size_token(compact, size):
            return size
    return None


def re_search_size_token(text: str, size: str) -> bool:
    import re

    return bool(re.search(rf"(^|[^A-Z0-9]){re.escape(size)}([^A-Z0-9]|$)", text))


def _size_units_from_order_rows(rows: Sequence[dict[str, Any]]) -> dict[str, int]:
    out: dict[str, int] = {}
    for row in rows:
        size = str(row.get("assigned_size") or row.get("my_size") or "").strip().upper()
        if not size:
            size = infer_line61_size_from_sku(
                str(row.get("sku_id") or row.get("sku") or ""),
                str(row.get("kaspi_offer_name") or row.get("model") or ""),
            ) or ""
        if not size:
            size = "UNKNOWN"
        out[size] = out.get(size, 0) + _safe_qty(row)
    return out


def apply_size_availability_to_scenarios(
    scenarios: Sequence[dict[str, Any]],
    audit_rows: Sequence[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Annotate scenarios with size-stock contamination metadata."""
    starved_sizes = sorted({
        str(row.get("internal_size") or row.get("size") or "").strip().upper()
        for row in audit_rows
        if str(row.get("stock_status") or "").strip().lower() in {"out_of_stock", "oos", "archive"}
    })
    starved_sizes = [s for s in starved_sizes if s]
    if not starved_sizes:
        return [dict(sc) for sc in scenarios]

    first_verified = min(
        (
            str(row.get("first_verified_oos_at") or row.get("snapshot_ts_local") or "").strip()
            for row in audit_rows
            if str(row.get("first_verified_oos_at") or row.get("snapshot_ts_local") or "").strip()
        ),
        default="",
    )
    evidence_paths = sorted({
        str(row.get("evidence_path") or row.get("source_file") or "").strip()
        for row in audit_rows
        if str(row.get("evidence_path") or row.get("source_file") or "").strip()
    })

    enriched: list[dict[str, Any]] = []
    for scenario in scenarios:
        sc = dict(scenario)
        size_units_raw = sc.get("size_units") or {}
        size_units = {
            str(k).strip().upper(): _safe_int(v)
            for k, v in size_units_raw.items()
        } if isinstance(size_units_raw, dict) else {}
        starved_units = sum(size_units.get(size, 0) for size in starved_sizes)
        total_units = _safe_int(sc.get("units_sold"))
        common_units = max(total_units - starved_units, 0) if total_units else None
        sc.update({
            "stock_starvation_flag": True,
            "stock_starvation_sizes": starved_sizes,
            "stock_starvation_first_verified_oos_at": first_verified,
            "stock_starvation_evidence_paths": evidence_paths,
            "stock_starved_units_sold": starved_units,
            "common_size_units_sold": common_units,
            "stock_starvation_note": (
                f"{', '.join(starved_sizes)} excluded from common-size demand comparisons; "
                "historical all-size demand may not be directly comparable when this size was sellable."
            ),
        })
        enriched.append(sc)
    return enriched


def _fetch_canonical_sales_truth_rows(
    *,
    ab_root: Path,
    store_code: str,
    sku_key: str,
    date_to: str,
) -> tuple[list[dict[str, Any]], list[str]]:
    warnings: list[str] = []
    dt_to = _parse_boundary(date_to, is_end=True)
    period_end = _dt_text(dt_to) if dt_to else str(date_to)
    db_uri = build_app_db_uri(ab_root)
    try:
        with sqlite3.connect(db_uri, uri=True) as conn:
            conn.row_factory = sqlite3.Row
            if not _sqlite_relation_exists(conn, "view_sales_line_truth"):
                return [], ["canonical_sales_truth_unavailable:view_sales_line_truth_missing"]
            if not _sqlite_relation_exists(conn, "sales_fact_v2"):
                return [], ["canonical_sales_truth_unavailable:sales_fact_v2_missing"]

            where = [
                "v.sku_key = ?",
                "datetime(CASE WHEN length(v.sale_date) = 10 THEN v.sale_date || ' 23:59:59' ELSE v.sale_date END) < datetime(?)",
            ]
            params: list[Any] = [sku_key, period_end]
            if store_code:
                where.append("v.store_code = ?")
                params.append(store_code)
            sql = f"""
                WITH price_lookup AS (
                    SELECT
                        order_id,
                        sku_key,
                        sku_id,
                        MAX(sell_price_kzt) AS sell_price_kzt,
                        MAX(kaspi_offer_name) AS kaspi_offer_name
                    FROM sales_fact_v2
                    WHERE sell_price_kzt IS NOT NULL
                      AND sell_price_kzt > 0
                    GROUP BY order_id, sku_key, sku_id
                )
                SELECT
                    v.order_id,
                    CASE
                        WHEN length(v.sale_date) = 10 THEN v.sale_date || ' 12:00:00'
                        ELSE v.sale_date
                    END AS created_at,
                    v.store_code,
                    COALESCE(pl.kaspi_offer_name, '') AS kaspi_offer_name,
                    v.sku_key,
                    v.sku_id,
                    CAST(ROUND(COALESCE(v.units, 1)) AS INTEGER) AS quantity,
                    pl.sell_price_kzt AS unit_price_kzt,
                    v.my_size AS assigned_size,
                    'CANONICAL_SALES_TRUTH' AS size_source,
                    'COMPLETED' AS internal_status,
                    CASE
                        WHEN length(v.sale_date) = 10 THEN v.sale_date || ' 12:00:00'
                        ELSE v.sale_date
                    END AS actual_shipment_date,
                    '' AS courier_transmission_date,
                    v.net_rev_kzt,
                    v.cogs_kzt,
                    v.profit_kzt,
                    'view_sales_line_truth' AS sales_truth_source
                FROM view_sales_line_truth v
                LEFT JOIN price_lookup pl
                  ON pl.order_id = v.order_id
                 AND pl.sku_key = v.source_sku_key
                 AND COALESCE(pl.sku_id, '') = COALESCE(v.source_sku_id, '')
                WHERE {' AND '.join(where)}
                  AND pl.sell_price_kzt IS NOT NULL
                ORDER BY v.sale_date, v.order_id
            """
            rows = [dict(row) for row in conn.execute(sql, params).fetchall()]
    except sqlite3.DatabaseError as exc:
        return [], [f"canonical_sales_truth_db_error:{exc}"]

    for row in rows:
        category = classify_marketing_category(row)
        row["category"] = category if category != "UNKNOWN" else "UNKNOWN"
        row["is_shipped"] = True
    if rows:
        warnings.append("sales_truth_source:view_sales_line_truth")
    return rows, warnings


def _fetch_fact_order_rows(
    *,
    ab_root: Path,
    store_code: str,
    sku_key: str,
    date_to: str,
) -> tuple[list[dict[str, Any]], list[str]]:
    warnings: list[str] = []
    dt_to = _parse_boundary(date_to, is_end=True)
    period_end = _dt_text(dt_to) if dt_to else str(date_to)
    where = ["datetime(created_at) < datetime(?)"]
    params: list[Any] = [period_end]
    if store_code:
        where.append("store_code = ?")
        params.append(store_code)
    if sku_key:
        where.append("sku_key = ?")
        params.append(sku_key)
    sql = f"""
        SELECT
            order_id,
            created_at,
            store_code,
            kaspi_offer_name,
            sku_key,
            sku_id,
            quantity,
            unit_price_kzt,
            assigned_size,
            size_source,
            internal_status,
            actual_shipment_date,
            courier_transmission_date
        FROM fact_orders_kaspi
        WHERE {' AND '.join(where)}
        ORDER BY created_at, order_id
    """
    try:
        with sqlite3.connect(build_app_db_uri(ab_root), uri=True) as conn:
            conn.row_factory = sqlite3.Row
            rows = [dict(row) for row in conn.execute(sql, params).fetchall()]
    except sqlite3.DatabaseError as exc:
        return [], [f"observed_order_baseline_db_error:{exc}"]
    for row in rows:
        category = classify_marketing_category(row)
        row["category"] = category if category != "UNKNOWN" else "UNKNOWN"
        row["is_shipped"] = (
            str(row.get("internal_status") or "").upper() in REALIZED_ORDER_STATUSES
            and str(row.get("actual_shipment_date") or row.get("courier_transmission_date") or "").strip() != ""
        )
        row["sales_truth_source"] = "fact_orders_kaspi"
    return rows, warnings


def _fetch_observed_order_rows(
    *,
    ab_root: Path,
    store_code: str,
    sku_key: str,
    date_to: str,
) -> tuple[list[dict[str, Any]], list[str]]:
    canonical_rows, canonical_warnings = _fetch_canonical_sales_truth_rows(
        ab_root=ab_root,
        store_code=store_code,
        sku_key=sku_key,
        date_to=date_to,
    )
    fact_rows, fact_warnings = _fetch_fact_order_rows(
        ab_root=ab_root,
        store_code=store_code,
        sku_key=sku_key,
        date_to=date_to,
    )
    if not canonical_rows:
        return fact_rows, [*canonical_warnings, *fact_warnings]

    max_canonical_dt = max(
        (_parse_dt(str(row.get("created_at") or "")) for row in canonical_rows),
        default=None,
    )
    canonical_order_ids = {str(row.get("order_id") or "").strip() for row in canonical_rows}
    tail_rows: list[dict[str, Any]] = []
    excluded_statuses = {"CANCELLED", "RETURNED"}
    for row in fact_rows:
        order_id = str(row.get("order_id") or "").strip()
        if order_id in canonical_order_ids:
            continue
        row_dt = _parse_dt(str(row.get("created_at") or ""))
        if max_canonical_dt and (row_dt is None or row_dt <= max_canonical_dt):
            continue
        if str(row.get("internal_status") or "").upper() in excluded_statuses:
            continue
        row = dict(row)
        row["sales_truth_source"] = "fact_orders_kaspi_tail"
        tail_rows.append(row)

    warnings = [*canonical_warnings, *fact_warnings]
    if tail_rows:
        warnings.append(f"sales_truth_tail:fact_orders_kaspi_tail_rows={len(tail_rows)}")
    return [*canonical_rows, *tail_rows], warnings


def _fetch_product_daily_metrics_by_date(
    *,
    marketing_db: Path,
    external_marketing_db: Path,
    campaign_ids: Sequence[str],
    dates: Sequence[str],
) -> tuple[dict[str, dict[str, dict[str, Any]]], str | None, list[str]]:
    warnings: list[str] = []
    normalized_dates = sorted({str(d).strip() for d in dates if str(d).strip()})
    normalized_campaigns = [str(c).strip() for c in campaign_ids if str(c).strip()]
    if not normalized_dates or not normalized_campaigns:
        return {}, None, ["observed_marketing_baseline_no_dates_or_campaigns"]

    out: dict[str, dict[str, dict[str, Any]]] = {}
    source_paths: list[str] = []
    for db_path in _marketing_candidate_paths(marketing_db, external_marketing_db):
        placeholders_c = ",".join("?" for _ in normalized_campaigns)
        placeholders_d = ",".join("?" for _ in normalized_dates)
        sql = f"""
            WITH ranked AS (
                SELECT
                    date,
                    campaign_id,
                    bid_cpc,
                    avg_cpc,
                    views,
                    clicks,
                    gmv,
                    orders_total,
                    cost,
                    ingested_at,
                    ROW_NUMBER() OVER (
                        PARTITION BY date, campaign_id
                        ORDER BY ingested_at DESC
                    ) AS rn
                FROM campaign_product_daily_history
                WHERE campaign_id IN ({placeholders_c})
                  AND date IN ({placeholders_d})
            )
            SELECT *
            FROM ranked
            WHERE rn = 1
        """
        try:
            with sqlite3.connect(db_path) as conn:
                conn.row_factory = sqlite3.Row
                rows = [dict(row) for row in conn.execute(sql, [*normalized_campaigns, *normalized_dates]).fetchall()]
        except sqlite3.DatabaseError as exc:
            warnings.append(f"observed_marketing_baseline_db_error:{db_path}:{exc}")
            continue
        if not rows:
            warnings.append(f"observed_marketing_baseline_empty:{db_path}")
            continue
        source_paths.append(str(db_path))
        for row in rows:
            date = str(row.get("date") or "").strip()
            campaign_id = str(row.get("campaign_id") or "").strip()
            if not date or not campaign_id:
                continue
            # Prefer the first source that has a date/campaign row. The default
            # order is external archive first, then the local fresh cache to fill
            # recent dates that the archive has not captured yet.
            out.setdefault(date, {}).setdefault(campaign_id, row)
    if out:
        return out, ",".join(source_paths), warnings
    return {}, None, warnings or ["observed_marketing_baseline_missing"]


def _daily_bid_policy(
    *,
    metric_rows_by_date: dict[str, dict[str, dict[str, Any]]],
    date: str,
    campaign_ids: Sequence[str],
) -> str:
    parts: list[str] = []
    per_campaign = metric_rows_by_date.get(date) or {}
    for cid in sorted(str(c).strip() for c in campaign_ids if str(c).strip()):
        row = per_campaign.get(cid) or {}
        bid = _safe_float(row.get("bid_cpc"))
        if bid is None:
            continue
        parts.append(f"{cid}:{int(round(bid))}")
    return "/".join(parts) if parts else "unknown"


def _nearest_prior_daily_bid_policy(
    *,
    metric_rows_by_date: dict[str, dict[str, dict[str, Any]]],
    date: str,
    campaign_ids: Sequence[str],
) -> str:
    prior_dates = sorted(d for d in metric_rows_by_date if d <= date)
    if not prior_dates:
        return "unknown"
    return _daily_bid_policy(
        metric_rows_by_date=metric_rows_by_date,
        date=prior_dates[-1],
        campaign_ids=campaign_ids,
    )


def _change_window_bid_policy(
    *,
    created_at: str,
    windows: Sequence[dict[str, Any]],
) -> str:
    created_dt = _parse_dt(created_at)
    if created_dt is None:
        return "unknown"
    for window in windows:
        start = _parse_dt(str(window.get("window_start_at") or ""))
        end = _parse_dt(str(window.get("window_end_at") or ""))
        if start and end and start <= created_dt < end:
            bid_policy = str(window.get("bid_policy") or "").strip()
            if bid_policy and bid_policy != "unknown":
                return bid_policy
    return "unknown"


def _summarize_marketing_dates(
    *,
    metric_rows_by_date: dict[str, dict[str, dict[str, Any]]],
    campaign_ids: Sequence[str],
    dates: Sequence[str],
    source_path: str | None,
) -> dict[str, Any]:
    normalized_dates = sorted({str(d).strip() for d in dates if str(d).strip()})
    normalized_campaigns = [str(c).strip() for c in campaign_ids if str(c).strip()]
    expected_rows = len(normalized_dates) * len(normalized_campaigns)
    row_count = 0
    total_cost = 0.0
    clicks = 0
    views = 0
    warnings: list[str] = []
    missing_rows: list[dict[str, str]] = []
    observed_rows: list[dict[str, Any]] = []
    for date in normalized_dates:
        per_campaign = metric_rows_by_date.get(date) or {}
        for cid in normalized_campaigns:
            row = per_campaign.get(cid)
            if not row:
                missing_rows.append({"date": date, "campaign_id": cid})
                continue
            row_count += 1
            cost = _safe_float(row.get("cost")) or 0.0
            row_clicks = _safe_int(row.get("clicks"))
            row_views = _safe_int(row.get("views"))
            total_cost += cost
            clicks += row_clicks
            views += row_views
            observed_rows.append({
                "date": date,
                "campaign_id": cid,
                "cost": _r2(cost),
                "clicks": row_clicks,
                "views": row_views,
            })
    if row_count == 0:
        status = "MISSING"
        warnings.append("observed_marketing_baseline_no_rows")
    elif row_count < expected_rows:
        status = "INCOMPLETE_COVERAGE"
        warnings.append(f"observed_marketing_baseline_coverage:rows={row_count}/expected={expected_rows}")
    else:
        status = "OK"
    if source_path:
        warnings.append(f"observed_marketing_source:{source_path}")
    return {
        "ad_spend": round(total_cost, 2) if row_count else None,
        "ad_spend_allocated": round(total_cost, 2) if row_count else 0.0,
        "ad_spend_status": status,
        "ad_coverage_pct": _r4(row_count / expected_rows) if expected_rows else None,
        "clicks": clicks,
        "views": views,
        "avg_cpc": round(total_cost / clicks, 2) if clicks else None,
        "rows": row_count,
        "expected_rows": expected_rows,
        "missing_rows": missing_rows,
        "observed_rows": observed_rows,
        "warnings": warnings,
    }


def build_marketing_gap_report(scenarios: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for sc in scenarios:
        for missing in sc.get("marketing_missing_rows") or []:
            date = str(missing.get("date") or "").strip()
            campaign_id = str(missing.get("campaign_id") or "").strip()
            scenario_id = str(sc.get("scenario_id") or "").strip()
            if not date or not campaign_id:
                continue
            key = (scenario_id, date, campaign_id)
            if key in seen:
                continue
            seen.add(key)
            rows.append({
                "scenario_id": scenario_id,
                "scenario_label": sc.get("scenario_label"),
                "price_policy": sc.get("price_policy"),
                "bid_policy": sc.get("bid_policy"),
                "window_start_at": sc.get("window_start_at"),
                "window_end_at": sc.get("window_end_at"),
                "date": date,
                "campaign_id": campaign_id,
                "campaign_label": CAMPAIGN_LABELS.get(campaign_id, campaign_id),
                "ad_spend_status": sc.get("ad_spend_status"),
                "ad_coverage_pct": sc.get("ad_coverage_pct"),
                "backfill_action": "kaspi-marketing period fetch",
            })
    return rows


# ---------------------------------------------------------------------------
# Gap report parsing and backfill planning
# ---------------------------------------------------------------------------


def parse_gap_report(path: Path) -> list[dict[str, Any]]:
    """Parse a marketing_gap_report CSV or JSON file.

    Returns all rows with normalised string fields.  Deduplication by
    ``(date, campaign_id)`` is intentionally deferred to
    :func:`plan_gap_backfill` so that price-policy filters work correctly
    when the same date/campaign appears under multiple price policies.
    """
    path = Path(path)
    suffix = path.suffix.lower()
    if suffix == ".json":
        with open(path) as f:
            raw_rows = json.load(f)
    elif suffix == ".csv":
        with open(path, newline="") as f:
            reader = csv.DictReader(f)
            raw_rows = list(reader)
    else:
        raise ValueError(f"Unsupported gap report format: {suffix}")
    parsed: list[dict[str, Any]] = []
    malformed: list[str] = []
    for idx, row in enumerate(raw_rows, start=1):
        if not isinstance(row, dict):
            malformed.append(f"row {idx}: expected object")
            continue
        date = str(row.get("date") or "").strip()
        campaign_id = str(row.get("campaign_id") or "").strip()
        missing = []
        if not date:
            missing.append("date")
        if not campaign_id:
            missing.append("campaign_id")
        if missing:
            malformed.append(f"row {idx}: missing {', '.join(missing)}")
            continue
        parsed.append({
            "date": date,
            "campaign_id": campaign_id,
            "scenario_id": str(row.get("scenario_id") or ""),
            "scenario_label": str(row.get("scenario_label") or ""),
            "price_policy": str(row.get("price_policy") or ""),
            "bid_policy": str(row.get("bid_policy") or ""),
            "campaign_label": str(row.get("campaign_label") or ""),
            "ad_spend_status": str(row.get("ad_spend_status") or ""),
            "backfill_action": str(row.get("backfill_action") or ""),
        })
    if malformed:
        preview = "; ".join(malformed[:5])
        more = f"; +{len(malformed) - 5} more" if len(malformed) > 5 else ""
        raise ValueError(f"Malformed gap report required fields: {preview}{more}")
    return parsed


def plan_gap_backfill(
    gap_rows: Sequence[dict[str, Any]],
    *,
    campaign_ids: Sequence[str] | None = None,
    price_policies: Sequence[str] | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    """Filter and deduplicate gap rows into an ordered fetch plan."""
    campaign_set = set(campaign_ids) if campaign_ids else None
    price_set = set(price_policies) if price_policies else None
    filtered: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for row in gap_rows:
        date = str(row.get("date") or "").strip()
        cid = str(row.get("campaign_id") or "").strip()
        if not date or not cid:
            continue
        if campaign_set and cid not in campaign_set:
            continue
        if price_set and str(row.get("price_policy") or "") not in price_set:
            continue
        if date_from and date < date_from:
            continue
        if date_to and date > date_to:
            continue
        key = (date, cid)
        if key in seen:
            continue
        seen.add(key)
        filtered.append(dict(row))
    filtered.sort(key=lambda r: (r["date"], r["campaign_id"]))
    if limit is not None and limit > 0:
        filtered = filtered[:limit]
    return filtered


def build_observed_price_baseline_scenarios(
    *,
    marketing_db: Path = DEFAULT_MARKETING_DB,
    external_marketing_db: Path = DEFAULT_EXTERNAL_MARKETING_DB,
    ab_root: Path = DEFAULT_AB_ROOT,
    date_to: str,
    store_code: str,
    sku_key: str,
    campaign_ids: Sequence[str],
    source_statuses: Sequence[dict[str, Any]],
    change_windows: Sequence[dict[str, Any]] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, list[dict[str, Any]]]]:
    rows, order_warnings = _fetch_observed_order_rows(
        ab_root=ab_root,
        store_code=store_code,
        sku_key=sku_key,
        date_to=date_to,
    )
    if not rows:
        return [], {}

    created_values_for_metrics = sorted(
        str(row.get("created_at") or "").strip()
        for row in rows
        if str(row.get("created_at") or "").strip()
    )
    if created_values_for_metrics:
        dates = _calendar_dates_touched(created_values_for_metrics[0], created_values_for_metrics[-1])
    else:
        dates = []
    metric_rows_by_date, marketing_source, marketing_source_warnings = _fetch_product_daily_metrics_by_date(
        marketing_db=marketing_db,
        external_marketing_db=external_marketing_db,
        campaign_ids=campaign_ids,
        dates=dates,
    )

    groups: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        price_policy = _price_policy_from_order(row)
        created_at = str(row.get("created_at") or "").strip()
        if not price_policy or not created_at:
            continue
        created_date = created_at[:10]
        # Prefer the timestamped operator change ledger for bid regimes.
        # Kaspi's historical product endpoint can return the current bid for a
        # past metric date, which is useful for cost but unsafe for period labels.
        bid_policy = "unknown"
        if change_windows:
            bid_policy = _change_window_bid_policy(
                created_at=created_at,
                windows=change_windows,
            )
        if bid_policy == "unknown":
            bid_policy = _daily_bid_policy(
                metric_rows_by_date=metric_rows_by_date,
                date=created_date,
                campaign_ids=campaign_ids,
            )
        if bid_policy == "unknown":
            bid_policy = _nearest_prior_daily_bid_policy(
                metric_rows_by_date=metric_rows_by_date,
                date=created_date,
                campaign_ids=campaign_ids,
            )
        key = (price_policy, bid_policy)
        group = groups.setdefault(key, {"rows": [], "dates": set()})
        group["rows"].append(row)
        group["dates"].add(created_date)

    scenarios: list[dict[str, Any]] = []
    order_rows_by_window: dict[str, list[dict[str, Any]]] = {}
    for (price_policy, bid_policy), group in sorted(groups.items(), key=lambda item: (int(item[0][0]), item[0][1])):
        group_rows = group["rows"]
        if not group_rows:
            continue
        created_values = sorted(str(r.get("created_at") or "").strip() for r in group_rows if str(r.get("created_at") or "").strip())
        if not created_values:
            continue
        dt_start = _parse_dt(created_values[0])
        dt_end_raw = _parse_dt(created_values[-1])
        window_start = _dt_text(dt_start) if dt_start else created_values[0]
        window_end = _dt_text(dt_end_raw + timedelta(seconds=1)) if dt_end_raw else created_values[-1]
        scenario_id = _scenario_key(f"observed:{price_policy}", bid_policy)

        summaries = summarize_orders(group_rows)
        total_orders = len(group_rows)
        total_units = sum(_safe_int(r.get("qty")) for r in summaries)
        gross_revenue = sum(_safe_float(r.get("gross_value")) or 0.0 for r in summaries)
        avg_sell_price = round(gross_revenue / total_units, 2) if total_units > 0 else None
        shipped_units, shipped_revenue = _summarize_shipped_orders(group_rows)
        realized_avg_sell_price = round(shipped_revenue / shipped_units, 2) if shipped_units > 0 else None
        ship_rate = shipped_units / total_units if total_units > 0 else 0.0
        # Financial status: detect low ship rate distortion
        financial_status = None
        if shipped_units > 0 and ship_rate < MIN_REALIZED_SHIP_RATE and total_units >= MIN_MAIN_UNITS:
            # Too few shipped to trust realized economics — suppress finance
            financial_status = "awaiting_shipment"
            financial_units = total_units
            financial_basis = "created_demand_awaiting_shipment"
        elif shipped_units > 0:
            financial_units = shipped_units
            financial_basis = "shipped_realized"
        else:
            financial_units = total_units
            financial_basis = "created_demand_no_shipped"
        categories = sorted(set(str(r.get("category") or "UNKNOWN") for r in summaries))
        product_totals = _product_profit_totals(group_rows)
        sales_truth_source = _sales_truth_source_label(group_rows)
        order_date_units = _order_date_units(group_rows)
        size_units = _size_units_from_order_rows(group_rows)

        group_dates = _calendar_dates_touched(window_start, window_end) or sorted(group["dates"])
        mkt = _summarize_marketing_dates(
            metric_rows_by_date=metric_rows_by_date,
            campaign_ids=campaign_ids,
            dates=group_dates,
            source_path=marketing_source,
        )
        days = max(len(group_dates), 0.01)
        econ_price = realized_avg_sell_price or avg_sell_price or _safe_float(price_policy)
        if financial_status == "awaiting_shipment":
            # Suppress economics for low ship rate
            econ = {
                "net_revenue": None, "unit_profit_before_ads": None,
                "unit_profit_after_ads": None, "ads_per_unit": None,
                "monthly_profit_estimate": None, "capital": None, "roic_estimate": None,
            }
            gate, confidence = "REVIEW", "low"
            note = (
                f"Awaiting shipment: {shipped_units}/{total_units} shipped; "
                f"realized finance locked. "
                f"{len(group_dates)} order date(s)."
            )
        else:
            if product_totals["product_profit_total"] is not None:
                econ = compute_economics_from_product_profit(
                    product_profit_total=product_totals["product_profit_total"],
                    product_cogs_total=product_totals["product_cogs_total"],
                    units_sold=total_units,
                    days_in_window=days,
                    ad_spend=mkt["ad_spend"],
                )
            else:
                econ = compute_economics(
                    gross_price=econ_price,
                    units_sold=financial_units,
                    days_in_window=days,
                    ad_spend=mkt["ad_spend"],
                )
            gate, confidence = apply_decision_gate(
                orders_count=total_orders,
                units_sold=total_units,
                unit_profit_after_ads=econ["unit_profit_after_ads"],
                roic_estimate=econ["roic_estimate"],
                source_statuses=source_statuses,
            )
            note = (
                f"Observed order-price baseline through {str(date_to)[:10]}; "
                f"{len(group_dates)} order date(s), ads matched by same order dates."
            )
        scenarios.append({
            "scenario_id": scenario_id,
            "scenario_label": f"P{price_policy}/B{bid_policy}",
            "scenario_source": "observed_order_price_baseline",
            "window_start_at": window_start,
            "window_end_at": window_end,
            "price_policy": price_policy,
            "bid_policy": bid_policy,
            "categories_included": categories,
            "campaign_ids": list(campaign_ids),
            "orders_count": total_orders,
            "units_sold": total_units,
            "shipped_units": shipped_units,
            "shipped_revenue": _r2(shipped_revenue),
            "realized_avg_sell_price": realized_avg_sell_price,
            "financial_units": financial_units,
            "financial_basis": financial_basis,
            "financial_status": financial_status,
            "ship_rate": _r4(ship_rate),
            "gross_revenue": _r2(gross_revenue),
            "avg_sell_price": avg_sell_price,
            "ad_spend": _r2(mkt["ad_spend"]),
            "ad_spend_allocated": _r2(mkt.get("ad_spend_allocated")),
            "ad_spend_status": mkt.get("ad_spend_status"),
            "ad_coverage_pct": mkt.get("ad_coverage_pct"),
            "avg_cpc": mkt["avg_cpc"],
            "clicks": mkt["clicks"],
            "days_in_window": _r2(days),
            "avg_daily_demand": _r2(total_units / days) if days > 0 else None,
            "avg_daily_ad_cost": _r2(mkt["ad_spend"] / days) if days > 0 and mkt["ad_spend"] is not None else None,
            "ads_per_unit": econ["ads_per_unit"],
            "sales_truth_source": sales_truth_source,
            **product_totals,
            "order_date_units": order_date_units,
            "size_units": size_units,
            "unit_profit_before_ads": econ["unit_profit_before_ads"],
            "unit_profit_after_ads": econ["unit_profit_after_ads"],
            "monthly_profit_estimate": econ["monthly_profit_estimate"],
            "capital": econ["capital"],
            "roic_estimate": econ["roic_estimate"],
            "decision_gate": gate,
            "confidence": confidence,
            "evidence_ids": [],
            "observation_note": note,
            "marketing_missing_rows": list(mkt.get("missing_rows") or []),
            "marketing_rows_used": list(mkt.get("observed_rows") or []),
            "marketing_warnings": [*marketing_source_warnings, *list(mkt.get("warnings") or [])],
            "order_warnings": list(order_warnings),
        })
        order_rows_by_window[_window_key(scenario_id, window_start)] = group_rows
    scenarios = allocate_shared_marketing_spend(scenarios)
    scenarios = [_recompute_scenario_economics(sc, source_statuses) for sc in scenarios]
    return scenarios, order_rows_by_window


# ---------------------------------------------------------------------------
# Scenario builder
# ---------------------------------------------------------------------------


def build_scenario_observations(
    *,
    marketing_db: Path = DEFAULT_MARKETING_DB,
    external_marketing_db: Path = DEFAULT_EXTERNAL_MARKETING_DB,
    change_log: Path = DEFAULT_CHANGE_LOG,
    ab_root: Path = DEFAULT_AB_ROOT,
    date_from: str,
    date_to: str,
    store_code: str,
    sku_key: str,
    campaign_ids: Sequence[str],
    source_statuses: Sequence[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, list[dict[str, Any]]]]:
    events = load_events(db_path=marketing_db, ledger_path=change_log)
    windows = _walk_change_timeline(events, date_from, date_to, campaign_ids)

    # If no windows (no events, no date range), create a single catch-all window
    if not windows:
        dt_from_obj = _parse_boundary(date_from, is_end=False)
        dt_to_obj = _parse_boundary(date_to, is_end=True)
        dt_from = _dt_text(dt_from_obj) if dt_from_obj else (date_from if " " in date_from else f"{date_from} 00:00:00")
        dt_to = _dt_text(dt_to_obj) if dt_to_obj else (date_to if " " in date_to else f"{date_to} 23:59:59")
        windows = [{
            "window_start_at": dt_from,
            "window_end_at": dt_to,
            "price_policy": "unknown",
            "bid_policy": "unknown",
            "state_snapshot": {},
        }]

    scenarios: list[dict[str, Any]] = []
    order_rows_by_window: dict[str, list[dict[str, Any]]] = {}

    observed_scenarios, observed_rows_by_window = build_observed_price_baseline_scenarios(
        marketing_db=marketing_db,
        external_marketing_db=external_marketing_db,
        ab_root=ab_root,
        date_to=date_to,
        store_code=store_code,
        sku_key=sku_key,
        campaign_ids=campaign_ids,
        source_statuses=source_statuses,
        change_windows=windows,
    )
    scenarios.extend(observed_scenarios)
    order_rows_by_window.update(observed_rows_by_window)

    for w in windows:
        if observed_scenarios and w["price_policy"] == "unknown":
            # The order-price baseline is more useful than a ledger-only row with no
            # price policy; keeping both would double-count the same current orders.
            continue
        w_start = w["window_start_at"]
        w_end = w["window_end_at"]
        sid = _scenario_key(w["price_policy"], w["bid_policy"])

        # Fetch orders
        synthetic_event = {"store_code": store_code, "sku_key": sku_key}
        order_rows, _max_created, order_warnings = fetch_order_period_rows(
            ab_root=ab_root,
            event=synthetic_event,
            period_start=w_start,
            period_end=w_end,
        )
        order_rows_by_window[_window_key(sid, w_start)] = order_rows

        # Fetch marketing
        mkt = _fetch_window_marketing_metrics(
            marketing_db=marketing_db,
            campaign_ids=list(campaign_ids),
            date_from=w_start,
            date_to=w_end,
        )

        # Aggregate orders
        order_summary = summarize_orders(order_rows)
        total_orders = len(order_rows)
        total_units = sum(int(r.get("qty") or 0) for r in order_summary)
        gross_revenue = sum(float(r.get("gross_value") or 0) for r in order_summary)
        avg_sell_price = round(gross_revenue / total_units, 2) if total_units > 0 else None
        shipped_units, shipped_revenue = _summarize_shipped_orders(order_rows)
        realized_avg_sell_price = round(shipped_revenue / shipped_units, 2) if shipped_units > 0 else None
        financial_units = shipped_units if shipped_units > 0 else total_units
        financial_basis = "shipped_realized" if shipped_units > 0 else "created_demand_no_shipped"
        categories = sorted(set(str(r.get("category") or "UNKNOWN") for r in order_summary))
        product_totals = _product_profit_totals(order_rows)
        sales_truth_source = _sales_truth_source_label(order_rows)
        order_date_units = _order_date_units(order_rows)
        size_units = _size_units_from_order_rows(order_rows)

        # Time window in days
        dt_start = _parse_dt(w_start)
        dt_end = _parse_dt(w_end)
        days = max((dt_end - dt_start).total_seconds() / 86400, 0.01) if dt_start and dt_end else 1

        econ_price = realized_avg_sell_price or avg_sell_price
        if w["price_policy"] != "unknown":
            try:
                econ_price = float(w["price_policy"].split("/")[0])
            except (ValueError, IndexError):
                pass

        if product_totals["product_profit_total"] is not None:
            econ = compute_economics_from_product_profit(
                product_profit_total=product_totals["product_profit_total"],
                product_cogs_total=product_totals["product_cogs_total"],
                units_sold=total_units,
                days_in_window=days,
                ad_spend=mkt["ad_spend"],
            )
        else:
            econ = compute_economics(
                gross_price=econ_price,
                units_sold=financial_units,
                days_in_window=days,
                ad_spend=mkt["ad_spend"],
            )

        gate, confidence = apply_decision_gate(
            orders_count=total_orders,
            units_sold=total_units,
            unit_profit_after_ads=econ["unit_profit_after_ads"],
            roic_estimate=econ["roic_estimate"],
            source_statuses=source_statuses,
        )

        label = f"P{w['price_policy']}/B{w['bid_policy']}"
        scenarios.append({
            "scenario_id": sid,
            "scenario_label": label,
            "scenario_source": "change_ledger_window",
            "window_start_at": w_start,
            "window_end_at": w_end,
            "price_policy": w["price_policy"],
            "bid_policy": w["bid_policy"],
            "categories_included": categories,
            "campaign_ids": list(campaign_ids),
            "orders_count": total_orders,
            "units_sold": total_units,
            "shipped_units": shipped_units,
            "shipped_revenue": _r2(shipped_revenue),
            "realized_avg_sell_price": realized_avg_sell_price,
            "financial_units": financial_units,
            "financial_basis": financial_basis,
            "gross_revenue": _r2(gross_revenue),
            "avg_sell_price": avg_sell_price,
            "ad_spend": _r2(mkt["ad_spend"]),
            "ad_spend_allocated": _r2(mkt.get("ad_spend_allocated")),
            "ad_spend_status": mkt.get("ad_spend_status"),
            "ad_coverage_pct": mkt.get("ad_coverage_pct"),
            "avg_cpc": mkt["avg_cpc"],
            "clicks": mkt["clicks"],
            "days_in_window": _r2(days),
            "avg_daily_demand": _r2(total_units / days) if days > 0 else None,
            "avg_daily_ad_cost": _r2(mkt["ad_spend"] / days) if days > 0 and mkt["ad_spend"] is not None else None,
            "ads_per_unit": econ["ads_per_unit"],
            "sales_truth_source": sales_truth_source,
            **product_totals,
            "order_date_units": order_date_units,
            "size_units": size_units,
            "unit_profit_before_ads": econ["unit_profit_before_ads"],
            "unit_profit_after_ads": econ["unit_profit_after_ads"],
            "monthly_profit_estimate": econ["monthly_profit_estimate"],
            "capital": econ["capital"],
            "roic_estimate": econ["roic_estimate"],
            "decision_gate": gate,
            "confidence": confidence,
            "evidence_ids": _evidence_ids_for_window(events, w, campaign_ids),
            "observation_note": "Direct change-log window.",
            "marketing_missing_rows": list(mkt.get("missing_rows") or []),
            "marketing_rows_used": list(mkt.get("observed_rows") or []),
            "marketing_warnings": list(mkt.get("warnings") or []),
            "order_warnings": order_warnings,
        })

    scenarios = allocate_shared_marketing_spend(scenarios)
    scenarios = [_recompute_scenario_economics(sc, source_statuses) for sc in scenarios]
    scenarios.sort(key=lambda sc: (str(sc.get("window_start_at") or ""), str(sc.get("price_policy") or "")))
    return scenarios, order_rows_by_window


def build_category_drilldown(
    order_rows_by_window: dict[str, list[dict[str, Any]]],
    scenarios: Sequence[dict[str, Any]],
) -> list[dict[str, Any]]:
    drilldown: list[dict[str, Any]] = []
    for sc in scenarios:
        key = _window_key(sc["scenario_id"], sc["window_start_at"])
        rows = order_rows_by_window.get(key, [])
        summaries = summarize_orders(rows)
        for s in summaries:
            drilldown.append({
                "scenario_id": sc["scenario_id"],
                "scenario_label": sc["scenario_label"],
                "category": s.get("category", "UNKNOWN"),
                "orders": s.get("orders", 0),
                "qty": s.get("qty", 0),
                "gross_value": _r2(s.get("gross_value", 0)),
                "shipped_qty": s.get("shipped_qty", 0),
                "avg_price": s.get("avg_price"),
            })
    return drilldown


# ---------------------------------------------------------------------------
# Daily orders & evidence
# ---------------------------------------------------------------------------


def resolve_daily_history_window(
    *,
    experiment_date_from: str,
    date_to: str,
    daily_history_days: int = 90,
) -> tuple[str, str]:
    """Return the chart window independently from the experiment window.

    The experiment window controls price/bid comparison. The daily demand chart
    is a context panel, so it should not collapse to a short A/B test window.
    """
    end_dt = _parse_dt(str(date_to)[:10]) or datetime.now()
    experiment_start_dt = _parse_dt(str(experiment_date_from)[:10]) or end_dt
    if daily_history_days and daily_history_days > 0:
        rolling_start_dt = end_dt - timedelta(days=max(int(daily_history_days) - 1, 0))
        start_dt = min(experiment_start_dt, rolling_start_dt)
    else:
        start_dt = experiment_start_dt
    return start_dt.date().isoformat(), end_dt.date().isoformat()


def _fetch_order_source_maxima(
    conn: sqlite3.Connection,
    *,
    sku_key: str,
    store_code: str | None = None,
) -> dict[str, Any]:
    store_clause = ""
    params: list[Any] = [sku_key]
    if store_code:
        store_clause = " AND store_code = ?"
        params.append(store_code)
    sql = f"""
        SELECT
            MAX(created_at) AS max_created_at,
            MAX(COALESCE(NULLIF(actual_shipment_date, ''), NULLIF(courier_transmission_date, ''))) AS max_shipped_at
        FROM fact_orders_kaspi
        WHERE sku_key = ?
          {store_clause}
    """
    row = conn.execute(sql, params).fetchone()
    if not row:
        return {"max_created_at": None, "max_shipped_at": None}
    return {
        "max_created_at": row["max_created_at"] if isinstance(row, sqlite3.Row) else row[0],
        "max_shipped_at": row["max_shipped_at"] if isinstance(row, sqlite3.Row) else row[1],
    }


def build_daily_order_series(
    *,
    ab_root: Path = DEFAULT_AB_ROOT,
    sku_key: str,
    date_from: str,
    date_to: str,
    daily_history_days: int = 90,
    store_code: str | None = None,
) -> dict[str, Any]:
    daily_date_from, daily_date_to = resolve_daily_history_window(
        experiment_date_from=date_from,
        date_to=date_to,
        daily_history_days=daily_history_days,
    )
    meta: dict[str, Any] = {
        "experiment_date_from": str(date_from)[:10],
        "date_to": str(date_to)[:10],
        "daily_date_from": daily_date_from,
        "daily_date_to": daily_date_to,
        "daily_history_days": daily_history_days,
        "basis": "created_orders_plus_shipped_overlay",
        "source_error": None,
        "source_max_created_at": None,
        "source_max_shipped_at": None,
    }
    try:
        conn = connect_app_db_ro(ab_root)
        with conn:
            conn.row_factory = sqlite3.Row
            created = fetch_daily_created_metrics(
                conn,
                sku_key=sku_key,
                start_date=daily_date_from,
                end_date=daily_date_to,
                store_codes=[store_code] if store_code else None,
            )
            shipped = fetch_daily_shipped_metrics(
                conn,
                sku_key=sku_key,
                start_date=daily_date_from,
                end_date=daily_date_to,
                store_codes=[store_code] if store_code else None,
            )
            maxima = _fetch_order_source_maxima(conn, sku_key=sku_key, store_code=store_code)
            meta["source_max_created_at"] = maxima.get("max_created_at")
            meta["source_max_shipped_at"] = maxima.get("max_shipped_at")
        conn.close()
        return {"created": created, "shipped": shipped, "meta": meta}
    except Exception as exc:
        meta["source_error"] = str(exc)
        return {"created": [], "shipped": [], "meta": meta}


def build_daily_orders(
    *,
    ab_root: Path = DEFAULT_AB_ROOT,
    sku_key: str,
    date_from: str,
    date_to: str,
    daily_history_days: int = 90,
    store_code: str | None = None,
) -> list[dict[str, Any]]:
    return build_daily_order_series(
        ab_root=ab_root,
        sku_key=sku_key,
        date_from=date_from,
        date_to=date_to,
        daily_history_days=daily_history_days,
        store_code=store_code,
    )["created"]


def build_evidence_manifest(
    *,
    experiment_root: Path = DEFAULT_EXPERIMENT_ROOT,
    watcher_root: Path = DEFAULT_WATCH_ROOT,
    cache_dir: Path = DEFAULT_DASHBOARD_CACHE_DIR,
) -> list[dict[str, Any]]:
    manifest: list[dict[str, Any]] = []
    for root_dir, source in [(experiment_root, "experiment"), (watcher_root, "watcher")]:
        if not root_dir.exists():
            continue
        for child in sorted(root_dir.iterdir()):
            if not child.is_dir():
                continue
            summary = child / "summary.json"
            if summary.exists():
                try:
                    data = json.loads(summary.read_text(encoding="utf-8"))
                    manifest.append({
                        "evidence_id": child.name,
                        "source": source,
                        "path": str(summary),
                        "generated_at": str(data.get("generated_at") or ""),
                    })
                except Exception:
                    pass
    return manifest


# ---------------------------------------------------------------------------
# v2 consolidation: anomaly tagging, price groups, bid labels
# ---------------------------------------------------------------------------


def format_bid_label(bid_policy: str, labels: dict[str, str] | None = None) -> str:
    """Convert '2545773:220/2629982:190' to 'ST:220 / TRM:190'."""
    if not bid_policy or bid_policy == "unknown":
        return bid_policy or "unknown"
    lmap = labels or CAMPAIGN_LABELS
    parts = []
    for segment in bid_policy.split("/"):
        segment = segment.strip()
        if ":" in segment:
            cid, val = segment.split(":", 1)
            label = lmap.get(cid.strip(), cid.strip())
            parts.append(f"{label}:{val.strip()}")
        else:
            parts.append(segment)
    return " / ".join(parts)


def tag_anomalies(
    scenarios: list[dict[str, Any]],
    *,
    min_orders: int = MIN_MAIN_ORDERS,
    min_units: int = MIN_MAIN_UNITS,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Split scenarios into main and anomaly lists.

    Returns (main_scenarios, anomaly_scenarios).
    """
    # First pass: compute total units per price point
    price_totals: dict[str, int] = {}
    for sc in scenarios:
        pp = str(sc.get("price_policy") or "unknown")
        price_totals[pp] = price_totals.get(pp, 0) + (sc.get("units_sold") or 0)

    main: list[dict[str, Any]] = []
    anomalies: list[dict[str, Any]] = []
    for sc in scenarios:
        pp = str(sc.get("price_policy") or "unknown")
        orders = sc.get("orders_count") or 0
        units = sc.get("units_sold") or 0
        reason = None

        if pp in ("unknown", "", "0"):
            reason = "missing_price"
        elif price_totals.get(pp, 0) < min_units:
            reason = "single_order_outlier_price"
        elif orders < min_orders or units < min_units:
            reason = "low_volume"

        sc_copy = dict(sc)
        if reason:
            sc_copy["is_anomaly"] = True
            sc_copy["anomaly_reason"] = reason
            anomalies.append(sc_copy)
        else:
            sc_copy["is_anomaly"] = False
            sc_copy["anomaly_reason"] = None
            main.append(sc_copy)
    return main, anomalies


def _compact_date_range(start: str, end: str) -> str:
    """'2026-02-07 01:24:45' + '2026-02-28 23:51:15' -> 'Feb 7-28'."""
    months = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
              "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
    try:
        s = _parse_dt(start)
        e = _parse_dt(end)
        if s is None or e is None:
            return f"{start[:10]}—{end[:10]}"
        # If end is start of next day (00:00:00), display previous day
        if e.hour == 0 and e.minute == 0 and e.second == 0:
            e = e - timedelta(seconds=1)
        sm = months[s.month - 1]
        em = months[e.month - 1]
        if s.date() == e.date():
            return f"{sm} {s.day}"
        if s.month == e.month:
            return f"{sm} {s.day}-{e.day}"
        return f"{sm} {s.day} – {em} {e.day}"
    except Exception:
        return f"{start[:10]}—{end[:10]}"


def _compact_number(v: float | None) -> str:
    """Format large numbers compactly: 4525930 -> '4.5M', 479561 -> '480K'."""
    if v is None:
        return "—"
    if abs(v) >= 1_000_000:
        return f"{v / 1_000_000:.1f}M"
    if abs(v) >= 10_000:
        return f"{v / 1_000:.0f}K"
    if abs(v) >= 1_000:
        return f"{v / 1_000:.1f}K"
    return f"{v:,.0f}"


def _signed_profit(v: float | None) -> str:
    if v is None:
        return "—"
    sign = "+" if v >= 0 else ""
    return f"{sign}{v:,.0f}"


def _scenario_ad_weight(sc: dict[str, Any], date: str) -> float:
    date_units = sc.get("order_date_units") or {}
    if isinstance(date_units, dict):
        weight = _safe_float(date_units.get(date))
        if weight is not None and weight > 0:
            return weight
        if date_units:
            return 0.0
    units = _safe_float(sc.get("financial_units")) or _safe_float(sc.get("units_sold"))
    return units if units is not None and units > 0 else 1.0


def allocate_shared_marketing_spend(
    scenarios: Sequence[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Allocate each campaign-date cost once across overlapping scenarios."""
    out = []
    owners: dict[tuple[str, str], list[tuple[int, int]]] = {}
    for idx, scenario in enumerate(scenarios):
        sc = dict(scenario)
        sc["marketing_rows_used"] = [dict(row) for row in scenario.get("marketing_rows_used") or []]
        out.append(sc)
        for row_idx, row in enumerate(sc["marketing_rows_used"]):
            date = str(row.get("date") or "").strip()
            campaign_id = str(row.get("campaign_id") or "").strip()
            if not date or not campaign_id:
                continue
            owners.setdefault((date, campaign_id), []).append((idx, row_idx))

    for (date, campaign_id), refs in owners.items():
        if not refs:
            continue
        base_row = out[refs[0][0]]["marketing_rows_used"][refs[0][1]]
        raw_cost = _safe_float(base_row.get("raw_cost")) or _safe_float(base_row.get("cost")) or 0.0
        raw_clicks = _safe_float(base_row.get("raw_clicks")) or _safe_float(base_row.get("clicks")) or 0.0
        raw_views = _safe_float(base_row.get("raw_views")) or _safe_float(base_row.get("views")) or 0.0
        weights = [_scenario_ad_weight(out[idx], date) for idx, _row_idx in refs]
        total_weight = sum(weights)
        if total_weight <= 0:
            weights = [1.0 for _ in refs]
            total_weight = float(len(refs))
        for (idx, row_idx), weight in zip(refs, weights):
            share = weight / total_weight if total_weight else 0.0
            row = out[idx]["marketing_rows_used"][row_idx]
            row["raw_cost"] = _r2(raw_cost)
            row["raw_clicks"] = _r2(raw_clicks)
            row["raw_views"] = _r2(raw_views)
            row["cost"] = _r2(raw_cost * share)
            row["clicks"] = int(round(raw_clicks * share))
            row["views"] = int(round(raw_views * share))
            row["allocation_share"] = _r4(share)
            row["allocation_basis"] = "same_campaign_date_order_units"

    for sc in out:
        rows = sc.get("marketing_rows_used") or []
        allocated_cost = sum(_safe_float(row.get("cost")) or 0.0 for row in rows)
        raw_cost = sum(_safe_float(row.get("raw_cost")) or _safe_float(row.get("cost")) or 0.0 for row in rows)
        clicks = sum(_safe_int(row.get("clicks")) for row in rows)
        sc["ad_spend_raw"] = _r2(raw_cost)
        sc["ad_spend"] = _r2(allocated_cost) if rows else sc.get("ad_spend")
        sc["ad_spend_allocated"] = _r2(allocated_cost) if rows else sc.get("ad_spend_allocated")
        sc["clicks"] = clicks if rows else sc.get("clicks")
        sc["ad_allocation_basis"] = (
            "same_campaign_date_order_units" if any(len(owners.get((str(row.get("date") or ""), str(row.get("campaign_id") or "")), [])) > 1 for row in rows)
            else "direct_campaign_date"
        )
    return out


def _recompute_scenario_economics(
    scenario: dict[str, Any],
    source_statuses: Sequence[dict[str, Any]],
) -> dict[str, Any]:
    sc = dict(scenario)
    if sc.get("financial_status") == "awaiting_shipment":
        return sc
    financial_units = _safe_int(sc.get("financial_units")) or _safe_int(sc.get("units_sold"))
    days = _safe_float(sc.get("days_in_window")) or _days_between(
        str(sc.get("window_start_at") or ""),
        str(sc.get("window_end_at") or ""),
    )
    ad_spend = _safe_float(sc.get("ad_spend"))
    product_profit_total = _safe_float(sc.get("product_profit_total"))
    if product_profit_total is not None:
        product_units = _safe_int(sc.get("units_sold")) or financial_units
        econ = compute_economics_from_product_profit(
            product_profit_total=product_profit_total,
            product_cogs_total=_safe_float(sc.get("product_cogs_total")),
            units_sold=product_units,
            days_in_window=max(days, 0.01),
            ad_spend=ad_spend,
        )
    else:
        econ_price = (
            _safe_float(sc.get("realized_avg_sell_price"))
            or _safe_float(sc.get("avg_sell_price"))
            or _safe_float(sc.get("price_policy"))
        )
        econ = compute_economics(
            gross_price=econ_price,
            units_sold=financial_units,
            days_in_window=max(days, 0.01),
            ad_spend=ad_spend,
        )
    sc.update({
        "ads_per_unit": econ["ads_per_unit"],
        "unit_profit_before_ads": econ["unit_profit_before_ads"],
        "unit_profit_after_ads": econ["unit_profit_after_ads"],
        "monthly_profit_estimate": econ["monthly_profit_estimate"],
        "capital": econ["capital"],
        "roic_estimate": econ["roic_estimate"],
    })
    gate, confidence = apply_decision_gate(
        orders_count=_safe_int(sc.get("orders_count")),
        units_sold=_safe_int(sc.get("units_sold")),
        unit_profit_after_ads=econ["unit_profit_after_ads"],
        roic_estimate=econ["roic_estimate"],
        source_statuses=source_statuses,
    )
    sc["decision_gate"] = gate
    sc["confidence"] = confidence
    return sc


def _marketing_baseline_cache_path(cache_dir: Path) -> Path:
    return Path(cache_dir) / MARKETING_BASELINE_CACHE_FILE


def _marketing_baseline_cache_key(scenario: dict[str, Any]) -> str:
    parts = [
        str(scenario.get("scenario_id") or "").strip(),
        str(scenario.get("price_policy") or "").strip(),
        str(scenario.get("bid_policy") or "").strip(),
        str(scenario.get("window_start_at") or "").strip(),
        str(scenario.get("window_end_at") or "").strip(),
    ]
    if any(not part for part in parts):
        return ""
    return "|".join(parts)


def read_marketing_baseline_cache(cache_dir: Path) -> dict[str, Any]:
    path = _marketing_baseline_cache_path(cache_dir)
    if not path.exists():
        return {"schema_version": 1, "entries": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {"schema_version": 1, "entries": {}}
    entries = data.get("entries")
    if not isinstance(entries, dict):
        entries = {}
    return {"schema_version": 1, "updated_at": data.get("updated_at"), "entries": entries}


def _scenario_has_complete_marketing(scenario: dict[str, Any]) -> bool:
    return (
        str(scenario.get("ad_spend_status") or "").strip() == "OK"
        and (_safe_float(scenario.get("ad_coverage_pct")) or 0.0) >= 1.0
        and _safe_float(scenario.get("ad_spend")) is not None
    )


def _marketing_baseline_entry(scenario: dict[str, Any], *, generated_at: str) -> dict[str, Any] | None:
    if not _scenario_has_complete_marketing(scenario):
        return None
    key = _marketing_baseline_cache_key(scenario)
    if not key:
        return None
    fields = [
        "scenario_id",
        "scenario_label",
        "scenario_source",
        "window_start_at",
        "window_end_at",
        "price_policy",
        "bid_policy",
        "campaign_ids",
        "ad_spend",
        "ad_spend_allocated",
        "ad_spend_raw",
        "ad_spend_status",
        "ad_coverage_pct",
        "avg_cpc",
        "clicks",
        "marketing_rows_used",
        "ad_allocation_basis",
    ]
    entry = {field: scenario.get(field) for field in fields}
    entry["cache_key"] = key
    entry["cached_at"] = generated_at or now_local_text()
    return entry


def update_marketing_baseline_cache(payload: dict[str, Any], cache_dir: Path) -> dict[str, Any]:
    cache = read_marketing_baseline_cache(cache_dir)
    entries = dict(cache.get("entries") or {})
    generated_at = str(payload.get("generated_at") or now_local_text())
    changed = False
    for scenario in payload.get("scenario_observations") or []:
        if not isinstance(scenario, dict):
            continue
        entry = _marketing_baseline_entry(scenario, generated_at=generated_at)
        if not entry:
            continue
        entries[str(entry["cache_key"])] = entry
        changed = True
    if changed:
        cache = {
            "schema_version": 1,
            "updated_at": now_local_text(),
            "entries": entries,
        }
        _write_json(_marketing_baseline_cache_path(cache_dir), cache)
    return {"schema_version": 1, "entries": entries, "updated_at": cache.get("updated_at")}


def apply_marketing_baseline_cache(
    scenarios: Sequence[dict[str, Any]],
    *,
    cache_dir: Path,
    source_statuses: Sequence[dict[str, Any]],
) -> list[dict[str, Any]]:
    cache = read_marketing_baseline_cache(cache_dir)
    entries = cache.get("entries") or {}
    out: list[dict[str, Any]] = []
    for scenario in scenarios:
        sc = dict(scenario)
        if _scenario_has_complete_marketing(sc):
            out.append(sc)
            continue
        key = _marketing_baseline_cache_key(sc)
        entry = entries.get(key) if key else None
        if not isinstance(entry, dict):
            out.append(sc)
            continue
        for field in (
            "ad_spend",
            "ad_spend_allocated",
            "ad_spend_raw",
            "ad_coverage_pct",
            "avg_cpc",
            "clicks",
            "marketing_rows_used",
            "ad_allocation_basis",
        ):
            if field in entry:
                sc[field] = entry.get(field)
        sc["ad_spend_status"] = "OK"
        sc["marketing_missing_rows"] = []
        warnings = list(sc.get("marketing_warnings") or [])
        if "marketing_baseline_cache_used" not in warnings:
            warnings.append("marketing_baseline_cache_used")
        cached_at = str(entry.get("cached_at") or "").strip()
        if cached_at:
            warnings.append(f"marketing_baseline_cache_cached_at:{cached_at}")
        sc["marketing_warnings"] = warnings
        out.append(_recompute_scenario_economics(sc, source_statuses))
    return out


def build_price_group_summaries(
    main_scenarios: Sequence[dict[str, Any]],
    source_statuses: Sequence[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Build parent price-group rows with child bid-regime rows."""
    allocated_scenarios = allocate_shared_marketing_spend(main_scenarios)
    main_scenarios = [
        _recompute_scenario_economics(sc, source_statuses)
        if sc.get("marketing_rows_used") or _safe_float(sc.get("product_profit_total")) is not None
        else sc
        for sc in allocated_scenarios
    ]
    # Group by price_policy
    by_price: dict[str, list[dict[str, Any]]] = {}
    for sc in main_scenarios:
        pp = str(sc.get("price_policy") or "unknown")
        by_price.setdefault(pp, []).append(sc)

    groups: list[dict[str, Any]] = []
    for price_policy in sorted(by_price.keys(), key=lambda p: int(p) if p.isdigit() else 0):
        children = by_price[price_policy]
        # Build child rows grouped by bid_regime
        child_rows = _build_child_bid_rows(children, source_statuses)

        # Aggregate parent metrics from children
        total_orders = sum(c.get("orders_count") or 0 for c in children)
        total_units = sum(c.get("units_sold") or 0 for c in children)
        total_shipped = sum(c.get("shipped_units") or 0 for c in children)
        total_revenue = sum(c.get("gross_revenue") or 0 for c in children)
        product_net_values = [_safe_float(c.get("product_net_revenue_total")) for c in children if _safe_float(c.get("product_net_revenue_total")) is not None]
        product_cogs_values = [_safe_float(c.get("product_cogs_total")) for c in children if _safe_float(c.get("product_cogs_total")) is not None]
        product_profit_values = [_safe_float(c.get("product_profit_total")) for c in children if _safe_float(c.get("product_profit_total")) is not None]
        common_size_values = [
            _safe_int(c.get("common_size_units_sold"))
            for c in children
            if c.get("common_size_units_sold") is not None
        ]
        stock_starvation_sizes = sorted({
            str(size)
            for c in children
            for size in (c.get("stock_starvation_sizes") or [])
            if str(size).strip()
        })
        product_net_total = _r2(sum(product_net_values)) if product_net_values else None
        product_cogs_total = _r2(sum(product_cogs_values)) if product_cogs_values else None
        product_profit_total = _r2(sum(product_profit_values)) if product_profit_values else None
        unique_marketing_rows: dict[tuple[str, str], dict[str, Any]] = {}
        for c in children:
            for mrow in c.get("marketing_rows_used") or []:
                date = str(mrow.get("date") or "").strip()
                campaign_id = str(mrow.get("campaign_id") or "").strip()
                if not date or not campaign_id:
                    continue
                unique_marketing_rows[(date, campaign_id)] = mrow
        has_allocated_marketing_rows = any(
            row.get("allocation_share") is not None
            for c in children
            for row in c.get("marketing_rows_used") or []
        )
        if has_allocated_marketing_rows:
            total_ad_spend = sum(_safe_float(c.get("ad_spend")) or _safe_float(c.get("ad_spend_allocated")) or 0.0 for c in children)
            total_clicks = sum(_safe_int(c.get("clicks")) for c in children)
            marketing_days_count = len({date for date, _campaign_id in unique_marketing_rows}) if unique_marketing_rows else 0
        elif unique_marketing_rows:
            total_ad_spend = sum(_safe_float(row.get("cost")) or 0.0 for row in unique_marketing_rows.values())
            total_clicks = sum(_safe_int(row.get("clicks")) for row in unique_marketing_rows.values())
            marketing_days_count = len({date for date, _campaign_id in unique_marketing_rows})
        else:
            total_ad_spend = sum(c.get("ad_spend") or c.get("ad_spend_allocated") or 0 for c in children)
            total_clicks = sum(c.get("clicks") or 0 for c in children)
            marketing_days_count = 0

        # Date range across all children
        starts = [str(c.get("window_start_at") or "") for c in children if c.get("window_start_at")]
        ends = [str(c.get("window_end_at") or "") for c in children if c.get("window_end_at")]
        period_start = min(starts) if starts else ""
        period_end = max(ends) if ends else ""

        avg_price = round(total_revenue / total_units, 2) if total_units > 0 else None
        ads_per_unit = round(total_ad_spend / total_units, 2) if total_units > 0 and total_ad_spend else None
        ship_rate = round(total_shipped / total_units, 4) if total_units > 0 else None
        any_awaiting = any(c.get("financial_status") == "awaiting_shipment" for c in children)
        if any_awaiting:
            financial_status = "awaiting_shipment"
        elif total_units >= MIN_MAIN_UNITS and total_shipped == 0:
            financial_status = "created_demand_no_shipped"
        elif total_shipped > 0:
            financial_status = "shipped_realized"
        else:
            financial_status = "created_demand"

        child_ads_per_unit = []
        for c in children:
            direct = _safe_float(c.get("ads_per_unit"))
            if direct is None:
                ad_spend = _safe_float(c.get("ad_spend")) or _safe_float(c.get("ad_spend_allocated"))
                units = _safe_int(c.get("units_sold"))
                direct = round(ad_spend / units, 2) if ad_spend and units else None
            if direct is not None:
                child_ads_per_unit.append(direct)
        child_cpcs = [_safe_float(c.get("avg_cpc")) for c in children if _safe_float(c.get("avg_cpc")) is not None]
        child_coverages = []
        for c in children:
            coverage = _safe_float(c.get("ad_coverage_pct"))
            if coverage is None:
                status = str(c.get("ad_spend_status") or "").upper()
                coverage = 1.0 if status == "OK" else 0.5 if status in {"INCOMPLETE_COVERAGE", "PARTIAL_DAY_UNALLOCATED"} else 0.0
            child_coverages.append(coverage)
        median_ads_per_unit = _median(child_ads_per_unit)
        median_cpc = _median(child_cpcs)
        ad_coverage_pct = _r4(mean(child_coverages)) if child_coverages else None
        incomplete_ad_coverage = (
            ad_coverage_pct is not None
            and ad_coverage_pct < MIN_CONFIDENT_AD_COVERAGE
        )
        if financial_status == "shipped_realized" and incomplete_ad_coverage:
            financial_status = "incomplete_ad_coverage"

        # Recompute economics from aggregated values. Awaiting-shipment parent
        # rows and incomplete-ad rows keep demand totals but suppress finance so
        # CSV/JSON cannot imply realized profitability from a distorted sample.
        days = _days_between(period_start, period_end)
        avg_daily_demand = _r2(total_units / days) if days > 0 else None
        ad_days = marketing_days_count or days
        avg_daily_ad_cost = _r2(total_ad_spend / ad_days) if ad_days > 0 and total_ad_spend is not None else None
        if financial_status in {"awaiting_shipment", "incomplete_ad_coverage"}:
            econ = {
                "unit_profit_before_ads": None,
                "unit_profit_after_ads": None,
                "monthly_profit_estimate": None,
                "capital": None,
                "roic_estimate": None,
            }
            ads_per_unit = None
            if financial_status == "incomplete_ad_coverage":
                avg_daily_ad_cost = None
        else:
            if product_profit_total is not None:
                econ = compute_economics_from_product_profit(
                    product_profit_total=product_profit_total,
                    product_cogs_total=product_cogs_total,
                    units_sold=total_units,
                    days_in_window=days,
                    ad_spend=total_ad_spend if total_ad_spend else None,
                )
            else:
                econ = compute_economics(
                    gross_price=avg_price or _safe_float(price_policy),
                    units_sold=total_units,
                    days_in_window=days,
                    ad_spend=total_ad_spend if total_ad_spend else None,
                )

        # Parent gate = worst meaningful child gate
        gate_order = {"FLAG": 0, "REVIEW": 1, "INSUFFICIENT_DATA": 2, "ORDER_FULL": 3}
        child_gates = [c.get("decision_gate", "REVIEW") for c in child_rows]
        worst_gate = min(child_gates, key=lambda g: gate_order.get(g, 99)) if child_gates else "REVIEW"
        child_confs = [c.get("confidence", "low") for c in child_rows]
        conf_order = {"low": 0, "medium": 1, "high": 2}
        worst_conf = min(child_confs, key=lambda c: conf_order.get(c, 0)) if child_confs else "low"

        if financial_status in {"awaiting_shipment", "created_demand_no_shipped", "incomplete_ad_coverage"}:
            worst_gate = "REVIEW"
            worst_conf = "low"

        groups.append({
            "price_policy": price_policy,
            "price_label": f"{int(price_policy):,} KZT" if price_policy.isdigit() else price_policy,
            "period": _compact_date_range(period_start, period_end),
            "period_start": period_start,
            "period_end": period_end,
            "total_orders": total_orders,
            "total_units": total_units,
            "common_size_units_sold": sum(common_size_values) if common_size_values else None,
            "stock_starvation_flag": bool(stock_starvation_sizes),
            "stock_starvation_sizes": stock_starvation_sizes,
            "total_shipped": total_shipped,
            "total_revenue": _r2(total_revenue),
            "total_ad_spend": _r2(total_ad_spend),
            "total_clicks": total_clicks,
            "avg_sell_price": avg_price,
            "days_in_window": _r2(days),
            "marketing_days_count": marketing_days_count,
            "avg_daily_demand": avg_daily_demand,
            "avg_daily_ad_cost": avg_daily_ad_cost,
            "ads_per_unit": ads_per_unit,
            "median_ads_per_unit": median_ads_per_unit,
            "ads_per_unit_p25": _r2(_percentile(child_ads_per_unit, 0.25)),
            "ads_per_unit_p75": _r2(_percentile(child_ads_per_unit, 0.75)),
            "median_cpc": median_cpc,
            "cpc_p25": _r2(_percentile(child_cpcs, 0.25)),
            "cpc_p75": _r2(_percentile(child_cpcs, 0.75)),
            "ad_coverage_pct": ad_coverage_pct,
            "product_net_revenue_total": product_net_total,
            "product_cogs_total": product_cogs_total,
            "product_profit_total": product_profit_total,
            "unit_profit_before_ads": econ["unit_profit_before_ads"],
            "unit_profit_after_ads": econ["unit_profit_after_ads"],
            "monthly_profit_estimate": econ["monthly_profit_estimate"],
            "capital": econ["capital"],
            "roic_estimate": econ["roic_estimate"],
            "decision_gate": worst_gate,
            "confidence": worst_conf,
            "any_awaiting_shipment": any_awaiting,
            "financial_status": financial_status,
            "ship_rate": ship_rate,
            "child_count": len(child_rows),
            "child_scenario_ids": [c.get("scenario_id") for c in children],
            "children": child_rows,
        })

    return groups


def _parse_bid_policy_per_campaign(
    bid_policy: str,
    labels: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    """Split '2545773:120/2629982:100' into per-campaign rows for decision-grade display.

    Honors the experiment YAML's bid_interpretation_guardrail: bid efficiency must
    be evaluated within the same campaign + offer surface, never collapsed into a
    single number. The renderer surfaces these rows so ST and TRM bids stay visible.
    Per-campaign ad spend is intentionally not split here — the marketing
    aggregation upstream collapses cost across campaigns; if that is needed later,
    extend `_fetch_window_marketing_metrics` to return a `by_campaign` map.
    """
    if not bid_policy or bid_policy == "unknown":
        return []
    label_map = labels if labels is not None else CAMPAIGN_LABELS
    rows: list[dict[str, Any]] = []
    for part in str(bid_policy).split("/"):
        part = part.strip()
        if not part or ":" not in part:
            continue
        cid, _, bid_text = part.partition(":")
        cid = cid.strip()
        bid_text = bid_text.strip()
        try:
            bid_value: int | None = int(bid_text)
        except (TypeError, ValueError):
            bid_value = None
        rows.append({
            "campaign_id": cid,
            "campaign_label": label_map.get(cid, cid),
            "bid": bid_value,
        })
    return rows


def _build_child_bid_rows(
    scenarios: Sequence[dict[str, Any]],
    source_statuses: Sequence[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Build child rows grouped by bid regime within a price group."""
    children: list[dict[str, Any]] = []
    for sc in sorted(scenarios, key=lambda s: str(s.get("window_start_at") or "")):
        bid_label = format_bid_label(str(sc.get("bid_policy") or "unknown"))
        window_start = str(sc.get("window_start_at") or "")
        window_end = str(sc.get("window_end_at") or "")
        days = _days_between(window_start, window_end)
        units = _safe_int(sc.get("units_sold"))
        ad_spend = sc.get("ad_spend") or sc.get("ad_spend_allocated") or 0
        capital = sc.get("capital")
        if capital is None and units:
            capital = compute_economics(
                gross_price=_safe_float(sc.get("price_policy")) or _safe_float(sc.get("avg_sell_price")),
                units_sold=units,
                days_in_window=days,
                ad_spend=ad_spend,
            ).get("capital")
        child = {
            "scenario_id": sc.get("scenario_id"),
            "bid_label": bid_label,
            "bid_policy": sc.get("bid_policy"),
            "period": _compact_date_range(
                window_start,
                window_end,
            ),
            "window_start_at": sc.get("window_start_at"),
            "window_end_at": sc.get("window_end_at"),
            "orders_count": sc.get("orders_count"),
            "units_sold": units,
            "shipped_units": sc.get("shipped_units"),
            "gross_revenue": sc.get("gross_revenue"),
            "ad_spend": ad_spend,
            "ad_spend_status": sc.get("ad_spend_status"),
            "ad_coverage_pct": sc.get("ad_coverage_pct"),
            "clicks": sc.get("clicks"),
            "avg_cpc": sc.get("avg_cpc"),
            "days_in_window": sc.get("days_in_window") or _r2(days),
            "avg_daily_demand": sc.get("avg_daily_demand") or (_r2(units / days) if days > 0 else None),
            "avg_daily_ad_cost": sc.get("avg_daily_ad_cost") or (_r2(ad_spend / days) if days > 0 and ad_spend is not None else None),
            "ads_per_unit": sc.get("ads_per_unit"),
            "sales_truth_source": sc.get("sales_truth_source"),
            "product_net_revenue_total": sc.get("product_net_revenue_total"),
            "product_cogs_total": sc.get("product_cogs_total"),
            "product_profit_total": sc.get("product_profit_total"),
            "size_units": sc.get("size_units"),
            "stock_starvation_flag": sc.get("stock_starvation_flag"),
            "stock_starvation_sizes": sc.get("stock_starvation_sizes"),
            "common_size_units_sold": sc.get("common_size_units_sold"),
            "stock_starved_units_sold": sc.get("stock_starved_units_sold"),
            "stock_starvation_first_verified_oos_at": sc.get("stock_starvation_first_verified_oos_at"),
            "stock_starvation_note": sc.get("stock_starvation_note"),
            "ad_spend_raw": sc.get("ad_spend_raw"),
            "ad_allocation_basis": sc.get("ad_allocation_basis"),
            "unit_profit_before_ads": sc.get("unit_profit_before_ads"),
            "unit_profit_after_ads": sc.get("unit_profit_after_ads"),
            "monthly_profit_estimate": sc.get("monthly_profit_estimate"),
            "capital": capital,
            "roic_estimate": sc.get("roic_estimate"),
            "decision_gate": sc.get("decision_gate"),
            "confidence": sc.get("confidence"),
            "financial_status": sc.get("financial_status"),
            "categories_included": sc.get("categories_included"),
            "per_campaign": _parse_bid_policy_per_campaign(
                str(sc.get("bid_policy") or "unknown"),
            ),
        }
        children.append(child)
    return children


def _days_between(start: str, end: str) -> float:
    s = _parse_dt(start)
    e = _parse_dt(end)
    if s and e and e > s:
        return max((e - s).total_seconds() / 86400, 0.01)
    return 1.0


# ---------------------------------------------------------------------------
# Experiment plan (config-backed) + decision-grade derivations
# ---------------------------------------------------------------------------

DEFAULT_EXPERIMENT_CONFIG_DIR = Path("config/experiments")

_SUCCESS_CRITERIA_PATTERNS = [
    # (regex, metric_key, op, unit)
    (
        r"demand[^0-9]*(>=|>|<=|<)\s*([0-9]+(?:\.[0-9]+)?)\s*(?:units?\s*)?/?\s*day",
        "demand_per_day",
        None,
        "/day",
    ),
    (
        r"ads?\s*/\s*u(?:nit)?\s*(>=|>|<=|<)\s*([0-9]+(?:\.[0-9]+)?)\s*kzt",
        "ads_per_unit",
        None,
        "KZT",
    ),
    (
        r"monthly\s*profit\s*(>=|>|<=|<)\s*([0-9]+(?:\.[0-9]+)?)\s*kzt",
        "monthly_profit_estimate",
        None,
        "KZT",
    ),
    (
        r"roic\s*(>=|>|<=|<)\s*([0-9]+(?:\.[0-9]+)?)\s*%",
        "roic_estimate",
        None,
        "%",
    ),
]


def _load_experiment_plan(
    sku_key: str,
    config_dir: Path = DEFAULT_EXPERIMENT_CONFIG_DIR,
) -> dict[str, Any] | None:
    """Load YAML experiment plan for the given sku_key, or None if absent."""
    if not sku_key:
        return None
    config_dir = Path(config_dir)
    if not config_dir.exists():
        return None
    try:
        import yaml  # local import keeps module load cheap when unused
    except ImportError:
        return None
    for path in sorted(config_dir.glob("*.yaml")):
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(data, dict):
            continue
        if str(data.get("sku_key") or "") == sku_key:
            data["_source_path"] = str(path)
            return data
    return None


def _resolve_current_experiment_step(plan: dict[str, Any] | None) -> dict[str, Any] | None:
    """Pick the active ladder step; fall back to most recent closed step."""
    if not plan:
        return None
    ladder = plan.get("ladder") or []
    if not isinstance(ladder, list) or not ladder:
        return None
    # Prefer any step whose status begins with 'active'.
    for step in ladder:
        status = str(step.get("status") or "")
        if status.startswith("active"):
            return dict(step)
    # Fall back to most recent closed step.
    closed = [s for s in ladder if str(s.get("status") or "").startswith("closed")]
    if closed:
        return dict(closed[-1])
    # Otherwise return the first step so the dashboard can still surface plan context.
    return dict(ladder[0])


_CALENDAR_DATE_RE = re.compile(r"(\d{4})-(\d{2})-(\d{2})")


def _extract_calendar_datetime(value: Any) -> datetime | None:
    """Extract a date from operator-facing timestamp strings without trusting timezone shape."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return datetime(value.year, value.month, value.day)
    match = _CALENDAR_DATE_RE.search(str(value))
    if not match:
        return None
    try:
        return datetime(int(match.group(1)), int(match.group(2)), int(match.group(3)))
    except ValueError:
        return None


def _duration_days(step: dict[str, Any]) -> int | None:
    try:
        raw = step.get("expected_duration_days")
        if raw is None:
            return None
        days = int(float(raw))
    except (TypeError, ValueError):
        return None
    return days if days > 0 else None


def _format_calendar_day(dt: datetime) -> str:
    return dt.strftime("%b ") + str(dt.day)


def _format_calendar_range(start: datetime | None, end: datetime | None) -> str:
    if start and end:
        if start.date() == end.date():
            return _format_calendar_day(start)
        if start.year == end.year and start.month == end.month:
            return f"{start.strftime('%b')} {start.day}-{end.day}"
        if start.year == end.year:
            return f"{_format_calendar_day(start)} - {_format_calendar_day(end)}"
        return f"{_format_calendar_day(start)}, {start.year} - {_format_calendar_day(end)}, {end.year}"
    if start:
        return f"from {_format_calendar_day(start)}"
    if end:
        return f"until {_format_calendar_day(end)}"
    return ""


def _format_exact_calendar_range(start: datetime | None, end: datetime | None) -> str:
    if start and end:
        if start.date() == end.date():
            return start.date().isoformat()
        return f"{start.date().isoformat()} -> {end.date().isoformat()}"
    if start:
        return f"from {start.date().isoformat()}"
    if end:
        return f"until {end.date().isoformat()}"
    return ""


def _enrich_experiment_plan_schedule(
    plan: dict[str, Any] | None,
    *,
    today: datetime | None = None,
) -> dict[str, Any] | None:
    """Add derived calendar windows to ladder steps from actual/planned timestamps.

    The YAML remains the source of truth. This enrichment only makes the
    dashboard calendar readable and avoids hand-maintaining duplicate dates.
    """
    if not plan:
        return None
    raw_ladder = plan.get("ladder") or []
    if not isinstance(raw_ladder, list):
        return dict(plan)

    enriched = dict(plan)
    ladder: list[dict[str, Any]] = []
    rolling_start: datetime | None = None
    conditional_path_seen = False
    today_date = (today or datetime.now()).date()

    for raw_step in raw_ladder:
        if not isinstance(raw_step, dict):
            continue
        step = dict(raw_step)
        status = str(step.get("status") or "").lower()
        duration = _duration_days(step)
        start = (
            _extract_calendar_datetime(step.get("actual_started_at_local"))
            or _extract_calendar_datetime(step.get("planned_start_at_local"))
            or _extract_calendar_datetime(step.get("calendar_start_date"))
            or rolling_start
        )
        end = (
            _extract_calendar_datetime(step.get("actual_ended_at_local"))
            or _extract_calendar_datetime(step.get("planned_end_at_local"))
            or _extract_calendar_datetime(step.get("calendar_end_date"))
        )

        if start and not end and duration:
            end = start + timedelta(days=duration)
        if end and not start and duration:
            start = end - timedelta(days=duration)

        label = _format_calendar_range(start, end)
        exact_label = _format_exact_calendar_range(start, end)
        if label:
            if status.startswith("conditional"):
                label = f"if run: {label}"
            elif conditional_path_seen and status.startswith("planned"):
                label = f"tentative: {label}"
            step["schedule_label"] = label
        if exact_label:
            step["schedule_exact_label"] = exact_label
        elif duration:
            step["schedule_label"] = f"plan {duration}d"

        if start:
            step["calendar_start_date"] = start.date().isoformat()
        if end:
            step["calendar_end_date"] = end.date().isoformat()
            rolling_start = end
        if start and end:
            step["is_today_window"] = start.date() <= today_date <= end.date()
        elif start:
            step["is_today_window"] = start.date() == today_date
        elif end:
            step["is_today_window"] = end.date() == today_date

        if duration:
            step["calendar_duration_days"] = duration
        if status.startswith("conditional"):
            step.setdefault("schedule_mode", "conditional_tentative")
            conditional_path_seen = True
        elif status.startswith("planned") and conditional_path_seen:
            step.setdefault("schedule_mode", "tentative_after_conditional")
        elif start or end:
            step.setdefault("schedule_mode", "actual_or_derived")

        ladder.append(step)

    enriched["ladder"] = ladder
    return enriched


def _parse_success_criteria(text: str) -> list[dict[str, Any]]:
    """Extract structured (metric, op, value) targets from a free-text criteria string."""
    if not text:
        return []
    import re

    out: list[dict[str, Any]] = []
    lower = text.lower()
    for pattern, metric_key, _op_unused, unit in _SUCCESS_CRITERIA_PATTERNS:
        for match in re.finditer(pattern, lower):
            try:
                value = float(match.group(2))
            except (TypeError, ValueError):
                continue
            out.append({
                "metric": metric_key,
                "op": match.group(1),
                "value": value,
                "unit": unit,
            })
    return out


def _matching_price_group(
    step: dict[str, Any],
    price_group_summaries: Sequence[dict[str, Any]],
) -> dict[str, Any] | None:
    """Find the price_group whose policy/label matches the ladder step price."""
    target = _safe_float(step.get("price_kzt"))
    if target is None:
        return None
    for pg in price_group_summaries:
        pg_price = _safe_float(pg.get("price_policy"))
        if pg_price is not None and abs(pg_price - target) < 1.0:
            return pg
        label = str(pg.get("price_label") or "")
        if label and str(int(target)) in label.replace(",", "").replace(" ", ""):
            return pg
    return None


def _evaluate_target_status(metric: str, op: str, target_val: float, actual_val: float | None) -> str:
    """Return tva_green/tva_amber/tva_red status by comparing actual vs target."""
    if actual_val is None:
        return "tva_missing"
    try:
        if op in (">=", ">"):
            ok = actual_val >= target_val
            soft = actual_val >= target_val * 0.9
        elif op in ("<=", "<"):
            ok = actual_val <= target_val
            soft = actual_val <= target_val * 1.1
        else:
            return "tva_missing"
    except TypeError:
        return "tva_missing"
    if ok:
        return "tva_green"
    if soft:
        return "tva_amber"
    return "tva_red"


def build_target_vs_actual(
    plan: dict[str, Any] | None,
    price_group_summaries: Sequence[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Build target-vs-actual rows from each ladder step's success_criteria.

    Each step contributes one summary row (free-text criteria + status) and
    zero or more parsed structured rows (one per recognized metric).
    """
    if not plan:
        return []
    ladder = plan.get("ladder") or []
    rows: list[dict[str, Any]] = []
    for step in ladder:
        step_id = str(step.get("step_id") or "")
        status = str(step.get("status") or "")
        price_kzt = step.get("price_kzt")
        schedule_label = str(step.get("schedule_label") or "")
        schedule_exact_label = str(step.get("schedule_exact_label") or "")
        criteria_text = str(step.get("success_criteria") or "")
        match = _matching_price_group(step, price_group_summaries)

        # Always emit a summary row so planned/closed steps are still visible.
        summary_row = {
            "step_id": step_id,
            "step_status": status,
            "price_kzt": price_kzt,
            "schedule_label": schedule_label,
            "schedule_exact_label": schedule_exact_label,
            "is_today_window": bool(step.get("is_today_window")),
            "kind": "summary",
            "target_label": criteria_text or "—",
            "target_metric": None,
            "target_op": None,
            "target_value": None,
            "target_unit": None,
            "actual_value": None,
            "status": "tva_missing" if not match else "tva_neutral",
            "note": "awaiting data" if not match else "",
        }
        rows.append(summary_row)

        # Parsed structured targets, only when criteria text is present.
        for parsed in _parse_success_criteria(criteria_text):
            metric = parsed["metric"]
            actual = None
            if match is not None:
                if metric == "demand_per_day":
                    actual = match.get("avg_daily_demand")
                elif metric == "ads_per_unit":
                    actual = match.get("ads_per_unit")
                elif metric == "monthly_profit_estimate":
                    actual = match.get("monthly_profit_estimate")
                elif metric == "roic_estimate":
                    raw = match.get("roic_estimate")
                    actual = raw * 100 if raw is not None else None
            target_val = parsed["value"]
            row_status = _evaluate_target_status(metric, parsed["op"], target_val, actual)
            rows.append({
                "step_id": step_id,
                "step_status": status,
                "price_kzt": price_kzt,
                "schedule_label": schedule_label,
                "schedule_exact_label": schedule_exact_label,
                "is_today_window": bool(step.get("is_today_window")),
                "kind": "metric",
                "target_label": f"{metric} {parsed['op']} {target_val}{parsed['unit']}",
                "target_metric": metric,
                "target_op": parsed["op"],
                "target_value": target_val,
                "target_unit": parsed["unit"],
                "actual_value": actual,
                "status": row_status,
                "note": "" if match else "no matching price group",
            })

    return rows


def build_observed_vs_projected(
    price_group_summaries: Sequence[dict[str, Any]],
    projections: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Build observed-vs-projected rows; renders 'projection missing' when absent.

    Projections are intentionally not invented. When a projection value is None,
    the renderer surfaces 'projection missing' so the operator never confuses an
    empty cell with a model output.
    """
    metrics = [
        ("units_sold", "Units", "total_units"),
        ("gross_revenue", "Revenue", "total_revenue"),
        ("ad_spend", "Ad spend", "total_ad_spend"),
        ("unit_profit_after_ads", "Profit/u", "unit_profit_after_ads"),
        ("roic_estimate", "ROIC", "roic_estimate"),
    ]
    rows: list[dict[str, Any]] = []
    for pg in price_group_summaries:
        price_label = str(pg.get("price_label") or pg.get("price_policy") or "—")
        for metric_key, label, pg_field in metrics:
            observed = pg.get(pg_field)
            projected = None
            if isinstance(projections, dict):
                bucket = projections.get(price_label) or projections.get(str(pg.get("price_policy") or ""))
                if isinstance(bucket, dict):
                    projected = bucket.get(metric_key)
            row = {
                "price_label": price_label,
                "metric": metric_key,
                "metric_label": label,
                "observed": observed,
                "projected": projected,
                "status": "projection_missing" if projected is None else "ok",
            }
            rows.append(row)
    return rows


def _read_delivery_promise_capture(
    delivery_watch_root: Path = DEFAULT_DELIVERY_PROMISE_ROOT,
) -> dict[str, Any] | None:
    """Read the rich latest_capture block from delivery_promise_watch heartbeat."""
    hb_path = Path(delivery_watch_root) / "latest_heartbeat.json"
    if not hb_path.exists():
        return None
    try:
        data = json.loads(hb_path.read_text(encoding="utf-8"))
    except Exception:
        return None
    capture = data.get("latest_capture")
    if isinstance(capture, dict):
        return normalize_delivery_capture_health(capture)
    return None


def build_deterministic_insights(
    *,
    plan: dict[str, Any] | None,
    current_step: dict[str, Any] | None,
    price_group_summaries: Sequence[dict[str, Any]],
    anomaly_observations: Sequence[dict[str, Any]],
    source_status: Sequence[dict[str, Any]],
    delivery_capture: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Build small severity-tagged decision cards from already-computed payload pieces."""
    cards: list[dict[str, Any]] = []

    # Best ROIC across active price groups.
    best = None
    for pg in price_group_summaries:
        roic = pg.get("roic_estimate")
        if roic is None or pg.get("any_awaiting_shipment"):
            continue
        if best is None or roic > (best.get("roic_estimate") or float("-inf")):
            best = pg
    if best is not None:
        roic_pct = (best.get("roic_estimate") or 0) * 100
        cards.append({
            "title": "Best ROIC",
            "value": f"{roic_pct:.1f}%",
            "severity": "ok",
            "note": str(best.get("price_label") or ""),
        })
    else:
        cards.append({
            "title": "Best ROIC",
            "value": "—",
            "severity": "warn",
            "note": "no realized price group yet",
        })

    # Minimum ad coverage across active price groups (fail < 50%, warn < 80%).
    coverage_values = [
        pg.get("ad_coverage_pct") for pg in price_group_summaries
        if pg.get("ad_coverage_pct") is not None
    ]
    if coverage_values:
        min_cov = min(coverage_values)
        sev = "fail" if min_cov < 0.5 else ("warn" if min_cov < 0.8 else "ok")
        cards.append({
            "title": "Min ad coverage",
            "value": f"{min_cov * 100:.1f}%",
            "severity": sev,
            "note": "across active price groups",
        })
    else:
        cards.append({
            "title": "Min ad coverage",
            "value": "—",
            "severity": "warn",
            "note": "no marketing data",
        })

    # Anomaly count.
    anomaly_count = len(list(anomaly_observations or []))
    cards.append({
        "title": "Anomalies",
        "value": str(anomaly_count),
        "severity": "warn" if anomaly_count > 0 else "ok",
        "note": "rows excluded from main averages" if anomaly_count else "all rows in main",
    })

    # Stock starvation (from plan stock_starvation_protocol).
    if plan:
        ss = plan.get("stock_starvation_protocol") or {}
        if isinstance(ss, dict) and str(ss.get("status") or "") == "out_of_stock":
            cards.append({
                "title": "Stock starvation",
                "value": str(ss.get("affected_internal_size") or "—") + " OOS",
                "severity": "warn",
                "note": str(ss.get("reorder_policy_status") or ""),
            })
    if not any(c.get("title") == "Stock starvation" for c in cards):
        cards.append({
            "title": "Stock starvation",
            "value": "none",
            "severity": "ok",
            "note": "all sizes available",
        })

    # Delivery promise (from delivery_capture or source_status).
    if delivery_capture is None:
        delivery_capture = _read_delivery_promise_capture()
    if delivery_capture:
        delta = _safe_int(delivery_capture.get("delivery_promise_delta_days"))
        status = str(delivery_capture.get("health_status") or "")
        contamination = str(delivery_capture.get("experiment_contamination") or "")
        cutoff_normal = (
            status == "CUTOFF_NORMAL_POST_15"
            or contamination == "none_cutoff_normal_post_15"
        )
        severity_label = str(delivery_capture.get("severity") or "").lower()
        if cutoff_normal:
            sev = "ok"
            value = f"+{delta}d cutoff normal" if delta else "on time"
        else:
            sev = "fail" if (delta and delta >= 2) or severity_label == "red" else ("warn" if delta else "ok")
            value = f"+{delta}d" if delta else "on time"
        cards.append({
            "title": "Delivery promise",
            "value": value,
            "severity": sev,
            "note": str(
                delivery_capture.get("experiment_contamination")
                or delivery_capture.get("health_status")
                or ""
            ),
        })
    else:
        # Fall back to source_status freshness.
        for src in source_status:
            if str(src.get("source_name") or "") == "delivery_promise_watch":
                fr = str(src.get("freshness_status") or "")
                cards.append({
                    "title": "Delivery promise",
                    "value": fr,
                    "severity": "warn" if fr != "OK" else "ok",
                    "note": "no capture available",
                })
                break

    # Current step contamination flags.
    if current_step:
        flags = current_step.get("contamination_flags") or []
        if flags:
            cards.append({
                "title": "Current step contamination",
                "value": str(len(flags)) + " flag" + ("s" if len(flags) != 1 else ""),
                "severity": "warn",
                "note": ", ".join(str(f) for f in flags[:3]),
            })

    return cards


# ---------------------------------------------------------------------------
# Build orchestrator
# ---------------------------------------------------------------------------


def build_experiment_dashboard_payload(
    *,
    sku_key: str,
    store: str = "ACMEWEAR",
    campaign_ids: Sequence[str],
    date_from: str,
    date_to: str,
    marketing_db: Path = DEFAULT_MARKETING_DB,
    external_marketing_db: Path = DEFAULT_EXTERNAL_MARKETING_DB,
    change_log: Path = DEFAULT_CHANGE_LOG,
    ab_root: Path = DEFAULT_AB_ROOT,
    watcher_root: Path = DEFAULT_WATCH_ROOT,
    experiment_root: Path = DEFAULT_EXPERIMENT_ROOT,
    cache_dir: Path = DEFAULT_DASHBOARD_CACHE_DIR,
    daily_history_days: int = 90,
    experiment_config_dir: Path = DEFAULT_EXPERIMENT_CONFIG_DIR,
    projections_root: Path | None = None,
) -> dict[str, Any]:
    source_status = build_source_status(
        marketing_db=marketing_db,
        external_marketing_db=external_marketing_db,
        change_log=change_log,
        ab_root=ab_root,
        watcher_root=watcher_root,
    )

    scenarios, order_rows_by_window = build_scenario_observations(
        marketing_db=marketing_db,
        external_marketing_db=external_marketing_db,
        change_log=change_log,
        ab_root=ab_root,
        date_from=date_from,
        date_to=date_to,
        store_code=store,
        sku_key=sku_key,
        campaign_ids=campaign_ids,
        source_statuses=source_status,
    )
    scenarios = apply_marketing_baseline_cache(
        scenarios,
        cache_dir=cache_dir,
        source_statuses=source_status,
    )
    if sku_key == LINE61_CANONICAL_SKU_KEY:
        scenarios = apply_size_availability_to_scenarios(
            scenarios,
            DEFAULT_LINE61_STOCK_AUDIT_ROWS,
        )

    # v2 consolidation: split into main vs anomaly, build price groups
    main_scenarios, anomaly_scenarios = tag_anomalies(scenarios)
    price_group_summaries = build_price_group_summaries(main_scenarios, source_status)

    category_drilldown = build_category_drilldown(order_rows_by_window, scenarios)

    daily_order_series = build_daily_order_series(
        ab_root=ab_root,
        sku_key=sku_key,
        date_from=date_from,
        date_to=date_to,
        daily_history_days=daily_history_days,
        store_code=store,
    )

    evidence = build_evidence_manifest(
        experiment_root=experiment_root,
        watcher_root=watcher_root,
        cache_dir=cache_dir,
    )

    experiment_plan = _enrich_experiment_plan_schedule(
        _load_experiment_plan(sku_key, experiment_config_dir)
    )
    current_step = _resolve_current_experiment_step(experiment_plan)
    target_vs_actual = build_target_vs_actual(experiment_plan, price_group_summaries)
    # Projection model is not yet wired in; reader hook for future projections_root.
    # TODO: load projections from projections_root when a projections format is defined.
    projections_payload: dict[str, Any] | None = None
    observed_vs_projected = build_observed_vs_projected(
        price_group_summaries,
        projections_payload,
    )
    delivery_capture = _read_delivery_promise_capture()
    deterministic_insights = build_deterministic_insights(
        plan=experiment_plan,
        current_step=current_step,
        price_group_summaries=price_group_summaries,
        anomaly_observations=anomaly_scenarios,
        source_status=source_status,
        delivery_capture=delivery_capture,
    )

    return {
        "generated_at": now_local_text(),
        "timezone": "Asia/Almaty",
        "target": {
            "store": store,
            "sku_key": sku_key,
            "campaign_ids": list(campaign_ids),
        },
        "source_status": source_status,
        "source_heartbeat": build_sync_heartbeat(source_status),
        "scenario_observations": scenarios,
        "price_group_summaries": price_group_summaries,
        "anomaly_observations": anomaly_scenarios,
        "marketing_gap_report": build_marketing_gap_report(scenarios),
        "category_drilldown": category_drilldown,
        "daily_orders": daily_order_series["created"],
        "daily_shipped_orders": daily_order_series["shipped"],
        "daily_series_meta": daily_order_series["meta"],
        "evidence": evidence,
        "experiment_plan": experiment_plan,
        "current_experiment_step": current_step,
        "target_vs_actual": target_vs_actual,
        "observed_vs_projected": observed_vs_projected,
        "deterministic_insights": deterministic_insights,
        "delivery_promise_capture": delivery_capture,
    }


# ---------------------------------------------------------------------------
# Sync orchestrator
# ---------------------------------------------------------------------------


def sync_experiment_dashboard(
    *,
    sku_key: str,
    store: str = "ACMEWEAR",
    campaign_ids: Sequence[str],
    date_from: str,
    date_to: str,
    marketing_db: Path = DEFAULT_MARKETING_DB,
    change_log: Path = DEFAULT_CHANGE_LOG,
    ab_root: Path = DEFAULT_AB_ROOT,
    cache_dir: Path = DEFAULT_DASHBOARD_CACHE_DIR,
    headless: bool = True,
    offline: bool = False,
    dry_run: bool = False,
    allow_stale: bool = False,
    creds: Any = None,
    credential_error: str | None = None,
) -> dict[str, Any]:
    warnings: list[str] = []
    fetch_status = "skipped"

    if dry_run:
        source_status = build_source_status(
            marketing_db=marketing_db,
            change_log=change_log,
            ab_root=ab_root,
        )
        return {
            "status": "dry_run",
            "generated_at": now_local_text(),
            "fetch_status": "dry_run",
            "planned_live_fetch_dates": _inclusive_date_strings(date_from, date_to) if not offline else [],
            "planned_cache_dir": str(cache_dir),
            "source_status": source_status,
            "warnings": warnings,
        }

    cache_dir.mkdir(parents=True, exist_ok=True)

    if credential_error:
        warnings.append(f"marketing_credentials_error:{credential_error}")
        if not offline and campaign_ids and not allow_stale:
            return {
                "status": "fetch_failed",
                "fetch_status": "failed",
                "warnings": warnings,
            }

    if not offline and creds and campaign_ids:
        fetch_status = "success"
        for target_date in _inclusive_date_strings(date_from, date_to):
            try:
                run_kaspi_marketing_fetch(
                    creds=creds,
                    campaign_ids=list(campaign_ids),
                    target_date=target_date,
                    run_dir=cache_dir / "sync_marketing" / target_date,
                    db_path=marketing_db,
                    headless=headless,
                )
            except Exception as exc:
                fetch_status = "failed"
                warnings.append(f"marketing_fetch_error:{target_date}:{exc}")
                if not allow_stale:
                    return {
                        "status": "fetch_failed",
                        "fetch_status": fetch_status,
                        "warnings": warnings,
                    }
                break
    elif not offline and campaign_ids and not creds:
        fetch_status = "failed"
        warnings.append("marketing_fetch_skipped:no_credentials")
        if not allow_stale:
            return {
                "status": "fetch_failed",
                "fetch_status": fetch_status,
                "warnings": warnings,
            }

    source_status = build_source_status(
        marketing_db=marketing_db,
        change_log=change_log,
        ab_root=ab_root,
    )

    summary = {
        "status": "success",
        "generated_at": now_local_text(),
        "fetch_status": fetch_status,
        "source_status": source_status,
        "cache_dir": str(cache_dir),
        "warnings": warnings,
    }
    _write_json(cache_dir / "sync_status.json", summary)
    return summary


def build_sync_heartbeat(
    source_statuses: Sequence[dict[str, Any]],
    latest_dashboard_dir: str | None = None,
) -> dict[str, Any]:
    """Build a heartbeat JSON summarizing source freshness without writing."""
    sources: dict[str, Any] = {}
    for s in source_statuses:
        name = s.get("source_name", "unknown")
        sources[name] = {
            "status": s.get("freshness_status", "MISSING"),
            "required": bool(s.get("required", True)),
            "max_event_at": s.get("max_event_at"),
            "failure_step": s.get("failure_step"),
            "failure_message": s.get("failure_message"),
        }

    critical = {"marketing_db", "ab_order_db"}
    has_critical_issue = any(
        sources.get(n, {}).get("required", True)
        and sources.get(n, {}).get("status") not in ("OK",)
        for n in critical
    )
    has_required_issue = any(
        v.get("required", True)
        and v.get("status") not in ("OK",)
        for v in sources.values()
    )
    has_optional_issue = any(
        not v.get("required", True)
        and v.get("status") not in ("OK",)
        for v in sources.values()
    )
    has_any_issue = any(
        v.get("status") not in ("OK",)
        for v in sources.values()
    )
    if has_critical_issue:
        overall = "red"
    elif has_required_issue:
        overall = "yellow"
    elif has_optional_issue:
        overall = "green"
    else:
        overall = "green"

    heartbeat = {
        "last_sync_at": now_local_text(),
        "overall_status": overall,
        "latest_dashboard_dir": latest_dashboard_dir,
        "sources": sources,
    }
    return heartbeat


def write_sync_heartbeat(
    cache_dir: Path,
    source_statuses: Sequence[dict[str, Any]],
    latest_dashboard_dir: str | None = None,
) -> dict[str, Any]:
    """Write a heartbeat JSON summarizing source freshness."""
    heartbeat = build_sync_heartbeat(source_statuses, latest_dashboard_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    _write_json(cache_dir / "sync_heartbeat.json", heartbeat)
    return heartbeat


def read_sync_heartbeat(cache_dir: Path) -> dict[str, Any] | None:
    """Read the sync heartbeat file, or None if missing."""
    hb_path = cache_dir / "sync_heartbeat.json"
    if not hb_path.exists():
        return None
    try:
        return json.loads(hb_path.read_text(encoding="utf-8"))
    except Exception:
        return None


def read_latest_run_metadata(cache_dir: Path) -> dict[str, Any] | None:
    """Read latest_run.json from a dashboard cache directory, or None if missing."""
    latest_path = cache_dir / "latest_run.json"
    if not latest_path.exists():
        return None
    try:
        return json.loads(latest_path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _dashboard_dir_has_index(path: Path) -> bool:
    return path.is_dir() and (path / "index.html").exists()


def _timestamped_dashboard_dirs(cache_dir: Path) -> list[Path]:
    if not cache_dir.exists():
        return []
    return sorted(
        [
            d
            for d in cache_dir.iterdir()
            if d.name != LIVE_DASHBOARD_DIR_NAME and _dashboard_dir_has_index(d)
        ],
        key=lambda d: d.name,
    )


def resolve_dashboard_serve_dir(
    cache_dir: Path,
    run_dir: Path | None = None,
) -> Path:
    """Resolve the dashboard directory to serve for this request."""
    if run_dir is not None:
        if not _dashboard_dir_has_index(run_dir):
            raise FileNotFoundError(f"Dashboard run not found or missing index.html: {run_dir}")
        return run_dir

    if not cache_dir.exists():
        raise FileNotFoundError(f"Cache dir not found: {cache_dir}")

    live_dir = cache_dir / LIVE_DASHBOARD_DIR_NAME
    if _dashboard_dir_has_index(live_dir):
        return live_dir

    candidates = _timestamped_dashboard_dirs(cache_dir)
    if not candidates:
        raise FileNotFoundError(f"No dashboard runs found in {cache_dir}")
    return candidates[-1]


def _inclusive_date_strings(date_from: str, date_to: str) -> list[str]:
    start = _parse_boundary(date_from, is_end=False)
    end_exclusive = _parse_boundary(date_to, is_end=True)
    if start is None or end_exclusive is None or end_exclusive <= start:
        return []
    cursor = start.date()
    final = (end_exclusive - timedelta(microseconds=1)).date()
    out: list[str] = []
    while cursor <= final:
        out.append(cursor.isoformat())
        cursor += timedelta(days=1)
    return out


# ---------------------------------------------------------------------------
# Artifact writer
# ---------------------------------------------------------------------------


def _write_dashboard_artifact_files(
    payload: dict[str, Any],
    output_dir: Path,
) -> dict[str, str]:
    output_dir.mkdir(parents=True, exist_ok=True)

    _write_json(output_dir / "dashboard.json", payload)
    _write_json(output_dir / "source_status.json", payload.get("source_status", []))
    _write_json(output_dir / "evidence_manifest.json", payload.get("evidence", []))
    _write_json(output_dir / "marketing_gap_report.json", payload.get("marketing_gap_report", []))
    _write_csv(output_dir / "scenario_metrics.csv", payload.get("scenario_observations", []))
    _write_csv(output_dir / "anomaly_observations.csv", payload.get("anomaly_observations", []))
    _write_csv(output_dir / "marketing_gap_report.csv", payload.get("marketing_gap_report", []))
    # Flatten price group summaries for CSV (exclude nested children)
    pg_rows = []
    for pg in payload.get("price_group_summaries", []):
        row = {k: v for k, v in pg.items() if k != "children"}
        pg_rows.append(row)
    _write_csv(output_dir / "price_group_summaries.csv", pg_rows)

    html = render_dashboard_html(payload)
    (output_dir / "index.html").write_text(html, encoding="utf-8")

    return {
        "dashboard_json": str(output_dir / "dashboard.json"),
        "index_html": str(output_dir / "index.html"),
        "scenario_metrics_csv": str(output_dir / "scenario_metrics.csv"),
        "anomaly_observations_csv": str(output_dir / "anomaly_observations.csv"),
        "marketing_gap_report_json": str(output_dir / "marketing_gap_report.json"),
        "marketing_gap_report_csv": str(output_dir / "marketing_gap_report.csv"),
        "price_group_summaries_csv": str(output_dir / "price_group_summaries.csv"),
        "source_status_json": str(output_dir / "source_status.json"),
        "evidence_manifest_json": str(output_dir / "evidence_manifest.json"),
    }


def _build_latest_run_metadata(
    payload: dict[str, Any],
    output_dir: Path,
    live_dir: Path,
) -> dict[str, Any]:
    return {
        "latest_dashboard_dir": str(output_dir),
        "immutable_dashboard_dir": str(output_dir),
        "generated_at": payload.get("generated_at"),
        "index_html": str(output_dir / "index.html"),
        "dashboard_json": str(output_dir / "dashboard.json"),
        "live_dashboard_dir": str(live_dir),
        "live_index_html": str(live_dir / "index.html"),
        "live_dashboard_json": str(live_dir / "dashboard.json"),
        "live_latest_run_json": str(live_dir / "latest_run.json"),
        "live_updated_at": now_local_text(),
    }


def write_dashboard_artifacts(
    payload: dict[str, Any],
    output_dir: Path,
) -> dict[str, str]:
    cache_dir = output_dir.parent
    live_dir = cache_dir / LIVE_DASHBOARD_DIR_NAME

    update_marketing_baseline_cache(payload, cache_dir)
    artifacts = _write_dashboard_artifact_files(payload, output_dir)
    live_artifacts = _write_dashboard_artifact_files(payload, live_dir)

    latest = _build_latest_run_metadata(payload, output_dir, live_dir)
    _write_json(cache_dir / "latest_run.json", latest)
    _write_json(live_dir / "latest_run.json", {
        **latest,
        "current_dashboard_dir": str(live_dir),
        "current_index_html": str(live_dir / "index.html"),
        "current_dashboard_json": str(live_dir / "dashboard.json"),
    })

    artifacts.update({
        "latest_run_json": str(cache_dir / "latest_run.json"),
        "live_dashboard_json": live_artifacts["dashboard_json"],
        "live_index_html": live_artifacts["index_html"],
        "live_latest_run_json": str(live_dir / "latest_run.json"),
    })
    return artifacts


# ---------------------------------------------------------------------------
# HTML renderer
# ---------------------------------------------------------------------------

_CSS = """
:root {
  --bg: #000; --bg-card: #0a0a0a; --bg-row: #0f0f0f; --bg-group: #1a1a1a;
  --border: #1e1e1e; --border-light: #2a2a2a;
  --text: #e0e0e0; --text-dim: #888; --text-muted: #555;
  --orange: #ff8c00; --green: #00c853; --red: #ff1744; --amber: #ffc107;
  --blue: #2979ff;
  --mono: 'JetBrains Mono','Fira Code','Cascadia Code',monospace;
  --ui: system-ui,-apple-system,sans-serif;
}
*{margin:0;padding:0;box-sizing:border-box}
body{background:var(--bg);color:var(--text);font:13px var(--ui);line-height:1.4;max-width:1320px;margin:0 auto;padding:10px 16px}
.ticker{display:flex;align-items:center;gap:12px;padding:6px 12px;background:var(--bg-card);border-bottom:1px solid var(--border);margin:-10px -16px 10px;padding:8px 16px;font:13px/1 var(--ui);font-weight:600}
.ticker .sku{color:var(--orange)}
.ticker .sep{color:var(--text-muted)}
.ticker .ts{margin-left:auto;color:var(--text-dim);font-weight:400;font-size:11px}
.section{font:12px var(--ui);text-transform:uppercase;letter-spacing:1px;color:var(--text-dim);margin:16px 0 6px;font-weight:500}
.ribbon{display:flex;gap:6px;flex-wrap:wrap;margin-bottom:12px}
.badge{padding:3px 8px;border-radius:10px;font:10px var(--mono);font-weight:500}
.b-ok{background:rgba(0,200,83,.12);color:var(--green);border:1px solid rgba(0,200,83,.25)}
.b-stale{background:rgba(255,193,7,.1);color:var(--amber);border:1px solid rgba(255,193,7,.2)}
.b-fail{background:rgba(255,23,68,.1);color:var(--red);border:1px solid rgba(255,23,68,.2)}
.b-miss{background:rgba(136,136,136,.1);color:var(--text-dim);border:1px solid rgba(136,136,136,.2)}
.banner{padding:6px 10px;border-radius:4px;font-size:11px;margin-bottom:10px;background:rgba(255,193,7,.08);border:1px solid rgba(255,193,7,.2);color:var(--amber)}
.kpi-grid{display:grid;grid-template-columns:repeat(6,1fr);gap:8px;margin-bottom:14px}
.kpi{background:var(--bg-card);border:1px solid var(--border);border-radius:6px;padding:10px 12px;text-align:center}
.kpi-val{font:28px/1.1 var(--mono);font-weight:700;color:var(--orange)}
.kpi-lbl{font:10px var(--ui);text-transform:uppercase;color:var(--text-dim);margin-top:4px}
.card{background:var(--bg-card);border:1px solid var(--border);border-radius:6px;padding:12px;margin-bottom:10px}
table{width:100%;border-collapse:collapse;font:12px var(--mono)}
th{text-align:left;padding:5px 8px;border-bottom:2px solid var(--border);color:var(--text-dim);font:11px var(--ui);text-transform:uppercase;letter-spacing:.5px;font-weight:500;white-space:nowrap}
td{padding:4px 8px;border-bottom:1px solid var(--border);white-space:nowrap}
tr.parent td{background:var(--bg-group);font-weight:600;font-size:13px;cursor:pointer}
tr.parent td:first-child{padding-left:8px}
tr.child td{font-size:11px;color:var(--text-dim)}
tr.child td:first-child{padding-left:24px}
tr.child:hover td{color:var(--text)}
.num{text-align:right;font-variant-numeric:tabular-nums}
.pos{color:var(--green)} .neg{color:var(--red)} .warn{color:var(--amber)}
.gate{font-weight:600;padding:1px 6px;border-radius:3px;font-size:10px;text-transform:uppercase}
.g-of{color:var(--green)} .g-fl{color:var(--red)} .g-rv{color:var(--amber)} .g-id{color:var(--text-dim)}
.border-flag{border-left:3px solid var(--red)} .border-full{border-left:3px solid var(--green)}
.border-review{border-left:3px solid var(--amber)} .border-insuf{border-left:3px solid var(--text-muted)}
.wf-bar{height:18px;display:inline-block;vertical-align:middle;border-radius:2px;margin-right:1px}
.wf-label{font:10px var(--mono);color:var(--text-dim);display:inline-block;min-width:60px}
.wf-val{font:10px var(--mono);display:inline-block;min-width:50px;text-align:right}
.chart-svg{width:100%;display:block}
.toggle-row{display:flex;gap:6px;margin-bottom:8px}
.btn{padding:3px 10px;border-radius:4px;font-size:11px;cursor:pointer;border:1px solid var(--border);background:var(--bg-card);color:var(--text)}
.btn:hover{border-color:var(--blue)} .btn-active{border-color:var(--blue);background:rgba(41,121,255,.1)}
details{margin:3px 0}
summary{cursor:pointer;padding:4px 8px;border-radius:3px;font-size:11px;color:var(--text-dim)}
summary:hover{background:var(--bg-row)}
.ev-path{font:10px var(--mono);color:var(--blue)}
.anom-row td{opacity:.5;font-style:italic}
.contam-banner{padding:8px 12px;border-radius:4px;font-size:11px;margin:-4px -16px 10px;background:rgba(255,23,68,.08);border-bottom:1px solid rgba(255,23,68,.4);color:var(--red);font-weight:600;display:flex;align-items:center;gap:8px}
.contam-banner .chip{background:rgba(255,23,68,.15);padding:2px 6px;border-radius:3px;font-size:10px;font-weight:500}
.cs-card{display:grid;grid-template-columns:160px 1fr 1fr;gap:12px;align-items:start}
.cs-price{font:30px/1.1 var(--mono);font-weight:700;color:var(--orange)}
.cs-meta{font:10px var(--ui);color:var(--text-dim);text-transform:uppercase;letter-spacing:.5px;margin-top:4px}
.cs-bids{font:14px var(--mono);color:var(--text)}
.cs-bid-chip{display:inline-block;background:rgba(41,121,255,.12);color:var(--blue);padding:2px 8px;border-radius:3px;margin-right:6px;font:11px var(--mono)}
.cs-criteria{font:11px var(--ui);color:var(--text);line-height:1.5}
.cs-flags{margin-top:6px}
.cs-flag{display:inline-block;background:rgba(255,23,68,.12);color:var(--red);padding:1px 6px;border-radius:3px;font:9px var(--mono);margin-right:4px;text-transform:uppercase}
.timeline{display:flex;gap:0;font:10px var(--mono);overflow-x:auto}
.tl-step{flex:1;min-width:132px;padding:8px;border-left:3px solid var(--border);background:var(--bg-row)}
.tl-step:first-child{border-left:1px solid var(--border)}
.tl-active{border-left-color:var(--green);background:rgba(0,200,83,.08)}
.tl-closed{opacity:.55;border-left-color:var(--text-muted)}
.tl-planned{border-left-color:var(--amber)}
.tl-conditional{border-left-color:var(--text-dim);font-style:italic}
.tl-step .tl-id{color:var(--text-dim);font-size:9px;text-transform:uppercase;letter-spacing:.5px}
.tl-step .tl-price{color:var(--orange);font-weight:700;font-size:14px;margin:2px 0}
.tl-step .tl-bids{color:var(--text-dim);font-size:9px}
.tl-step .tl-dates{color:var(--text);font-size:10px;margin-top:2px;font-weight:600}
.tl-step .tl-exact{color:var(--text-dim);font-size:8px;margin-top:1px}
.tl-step .tl-status{color:var(--text);font-size:9px;margin-top:4px;text-transform:uppercase}
.tl-today{box-shadow:inset 0 0 0 1px var(--amber),0 0 16px rgba(255,214,0,.18);background:linear-gradient(180deg,rgba(255,214,0,.18),rgba(255,214,0,.04))}
.tl-today .tl-dates,.tl-today .tl-exact{color:var(--amber)}
.tl-active .tl-status{color:var(--green)}
.tl-planned .tl-status{color:var(--amber)}
.insight-grid{display:grid;grid-template-columns:repeat(6,1fr);gap:6px;margin-bottom:14px}
.insight{background:var(--bg-card);border:1px solid var(--border);border-left:3px solid var(--border);border-radius:4px;padding:8px 10px}
.insight-ok{border-left-color:var(--green)}
.insight-warn{border-left-color:var(--amber)}
.insight-fail{border-left-color:var(--red)}
.insight-title{font:9px var(--ui);text-transform:uppercase;letter-spacing:.5px;color:var(--text-dim)}
.insight-val{font:18px/1.1 var(--mono);font-weight:700;color:var(--text);margin:3px 0}
.insight-ok .insight-val{color:var(--green)}
.insight-warn .insight-val{color:var(--amber)}
.insight-fail .insight-val{color:var(--red)}
.insight-note{font:9px var(--mono);color:var(--text-dim);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.tva-table{width:100%;border-collapse:collapse;font:11px var(--mono)}
.tva-table th{padding:4px 8px;border-bottom:2px solid var(--border);color:var(--text-dim);font:10px var(--ui);text-transform:uppercase;letter-spacing:.5px}
.tva-table td{padding:3px 8px;border-bottom:1px solid var(--border);white-space:nowrap}
.tva-schedule{color:var(--amber);font-weight:600}
.tva-schedule .sched-exact{display:block;color:var(--text-dim);font-size:9px;font-weight:500;margin-top:1px}
.tva-today td{background:rgba(255,214,0,.08)!important;border-top:1px solid rgba(255,214,0,.35);border-bottom:1px solid rgba(255,214,0,.35)}
.tva-today td:first-child{box-shadow:inset 3px 0 0 var(--amber)}
.tva-row.tva-summary td{background:var(--bg-row);font-weight:600}
.tva-row.tva-summary td:first-child{color:var(--orange)}
.tva-green{color:var(--green)} .tva-amber{color:var(--amber)} .tva-red{color:var(--red)}
.tva-missing{color:var(--text-muted);font-style:italic}
.tva-neutral{color:var(--text-dim)}
.proj-missing{color:var(--text-muted);font-style:italic}
.sticky-thead thead{position:sticky;top:0;background:var(--bg-card);z-index:1}
.per-camp{font:9px var(--mono);color:var(--text-dim);margin-top:2px}
.per-camp .pc-chip{display:inline-block;background:rgba(41,121,255,.08);color:var(--blue);padding:0 5px;border-radius:2px;margin-right:4px}
@media(max-width:900px){.kpi-grid{grid-template-columns:repeat(3,1fr)} .insight-grid{grid-template-columns:repeat(3,1fr)} .cs-card{grid-template-columns:1fr}}
"""

_JS = r"""
const D=JSON.parse(document.getElementById('dashboard-data').textContent);
const PG=D.price_group_summaries||[];
const AN=D.anomaly_observations||[];
function f(v){if(v==null)return'—';if(typeof v==='number'){if(Math.abs(v)>=1e6)return(v/1e6).toFixed(1)+'M';if(Math.abs(v)>=1e4)return Math.round(v/1e3)+'K';if(Math.abs(v)>=1e3)return(v/1e3).toFixed(1)+'K';return v.toLocaleString('en-US',{maximumFractionDigits:0})}return String(v)}
function fs(v){if(v==null)return'—';return(v>=0?'+':'')+v.toLocaleString('en-US',{maximumFractionDigits:0})}
function fp(v){if(v==null)return'—';return(v*100).toFixed(1)+'%'}
function pc(v){return v==null?'':(v>=0?'pos':'neg')}
function gc(g){return g==='ORDER_FULL'?'g-of':g==='FLAG'?'g-fl':g==='REVIEW'?'g-rv':'g-id'}
function bc(g){return g==='ORDER_FULL'?'border-full':g==='FLAG'?'border-flag':g==='REVIEW'?'border-review':'border-insuf'}

function renderRibbon(){
  const el=document.getElementById('src-ribbon');
  const m={OK:'b-ok',STALE:'b-stale',FETCH_FAILED:'b-fail',MISSING:'b-miss',SCHEMA_FAILED:'b-fail',PARTIAL:'b-stale'};
  el.innerHTML=D.source_status.map(s=>`<span class="badge ${m[s.freshness_status]||'b-miss'}">${s.source_name}: ${s.freshness_status}${s.max_event_at?' ('+s.max_event_at.substring(0,16)+')':''}</span>`).join('');
  if(D.source_status.some(s=>s.freshness_status!=='OK'))document.getElementById('stale-banner').style.display='block';
}

function renderKPIs(){
  const el=document.getElementById('kpi-grid');
  const sc=D.scenario_observations.filter(s=>!s.is_anomaly);
  const u=sc.reduce((a,s)=>a+(s.units_sold||0),0);
  const r=sc.reduce((a,s)=>a+(s.gross_revenue||0),0);
  const ad=sc.reduce((a,s)=>a+(s.ad_spend||s.ad_spend_allocated||0),0);
  const best=PG.reduce((b,g)=>(g.roic_estimate!=null&&(b==null||g.roic_estimate>b))?g.roic_estimate:b,null);
  const bestP=PG.reduce((b,g)=>(g.unit_profit_after_ads!=null&&(b==null||g.unit_profit_after_ads>b))?g.unit_profit_after_ads:b,null);
  const items=[['UNITS',u],['REVENUE',f(r)],['AD SPEND',f(ad)],['BEST PROFIT/U',bestP!=null?fs(bestP):'—'],['BEST ROIC',best!=null?fp(best):'—'],['PRICE GROUPS',PG.length]];
  el.innerHTML=items.map(([l,v])=>`<div class="kpi"><div class="kpi-val">${v}</div><div class="kpi-lbl">${l}</div></div>`).join('');
}

function renderWaterfall(){
  const el=document.getElementById('waterfall');
  if(!PG.length){el.innerHTML='';return}
  const g=PG.reduce((b,g)=>(g.roic_estimate!=null&&(b==null||g.roic_estimate>(b.roic_estimate||0)))?g:b,PG[0]);
  const p=parseFloat(g.price_policy)||0;if(!p){el.innerHTML='';return}
  const comm=p*0.125;const dlvr=1099.14;const vat_amt=(p*(1-0.125)-dlvr)*0.04;
  const net=(p*(1-0.125)-dlvr)*(1-0.04);const cogs=5919;const ads=g.ads_per_unit||0;
  const profit=net-cogs-(ads||0);
  const steps=[['Gross',p,'#2979ff'],['Comm',-comm,'#f44'],['Delivery',-dlvr,'#f44'],['VAT',-vat_amt,'#f44'],['Net Rev',net,'#2979ff'],['COGS',-cogs,'#ff9800'],['Ads/u',-(ads||0),'#ff9800'],['Profit',profit,profit>=0?'#00c853':'#ff1744']];
  const maxV=Math.max(...steps.map(s=>Math.abs(s[1])));
  let html=`<div style="font:10px var(--ui);color:var(--text-dim);margin-bottom:4px">UNIT ECONOMICS @ ${g.price_label||g.price_policy}</div>`;
  steps.forEach(([label,val,color])=>{
    const w=Math.max(Math.abs(val)/maxV*100,2);
    html+=`<div style="margin:2px 0;display:flex;align-items:center"><span class="wf-label">${label}</span><span class="wf-bar" style="width:${w}%;background:${color}"></span><span class="wf-val">${val>=0?'+':''}${Math.round(val)}</span></div>`;
  });
  el.innerHTML=html;
}

function renderPriceTable(){
  const el=document.getElementById('price-table');
  if(!PG.length){el.innerHTML='<p style="color:var(--text-dim)">No price groups.</p>';return}
  let h='<table><thead><tr><th></th><th>Price</th><th>Period</th><th class="num">Units</th><th class="num">Demand/d</th><th class="num">Revenue</th><th class="num">Ads</th><th class="num">Ads/d</th><th class="num">Ads/U</th><th class="num">Ad Cov</th><th class="num">Profit/U</th><th class="num">Mo.Profit</th><th class="num">Capital</th><th class="num">ROIC</th><th>Status</th><th>Gate</th></tr></thead><tbody>';
  PG.forEach((g,gi)=>{
    const cls=bc(g.decision_gate);
    h+=`<tr class="parent ${cls}" onclick="toggleChildren(${gi})">`;
    h+=`<td><span id="arrow-${gi}">&#9654;</span></td>`;
    h+=`<td>${g.price_label}</td><td>${g.period}</td>`;
    h+=`<td class="num">${g.total_units}</td><td class="num">${f(g.avg_daily_demand)}</td><td class="num">${f(g.total_revenue)}</td>`;
    h+=`<td class="num">${f(g.total_ad_spend)}</td><td class="num">${f(g.avg_daily_ad_cost)}</td><td class="num">${f(g.ads_per_unit)}</td>`;
    h+=`<td class="num">${fp(g.ad_coverage_pct)}</td>`;
    h+=`<td class="num ${pc(g.unit_profit_after_ads)}">${g.any_awaiting_shipment?'—':fs(g.unit_profit_after_ads)}</td>`;
    h+=`<td class="num ${pc(g.monthly_profit_estimate)}">${g.any_awaiting_shipment?'—':f(g.monthly_profit_estimate)}</td>`;
    h+=`<td class="num">${g.any_awaiting_shipment?'—':f(g.capital)}</td>`;
    h+=`<td class="num">${g.any_awaiting_shipment?'—':fp(g.roic_estimate)}</td>`;
    h+=`<td>${g.financial_status||'—'}</td><td><span class="gate ${gc(g.decision_gate)}">${g.decision_gate}</span></td></tr>`;
    (g.children||[]).forEach(c=>{
      h+=`<tr class="child child-${gi}" style="display:none">`;
      const pcRow=(c.per_campaign||[]).map(p=>`<span class="pc-chip">${esc(p.campaign_label)}:${p.bid==null?'—':p.bid}</span>`).join('');
      const bidCell=pcRow?`${c.bid_label}<div class="per-camp">${pcRow}</div>`:c.bid_label;
      h+=`<td></td><td style="padding-left:24px">${bidCell}</td><td>${c.period}</td>`;
      h+=`<td class="num">${c.units_sold||0}</td><td class="num">${f(c.avg_daily_demand)}</td><td class="num">${f(c.gross_revenue)}</td>`;
      h+=`<td class="num">${f(c.ad_spend)}</td><td class="num">${f(c.avg_daily_ad_cost)}</td><td class="num">${f(c.ads_per_unit)}</td>`;
      h+=`<td class="num">${fp(c.ad_coverage_pct)}</td>`;
      h+=`<td class="num ${pc(c.unit_profit_after_ads)}">${c.financial_status==='awaiting_shipment'?'—':fs(c.unit_profit_after_ads)}</td>`;
      h+=`<td class="num ${pc(c.monthly_profit_estimate)}">${c.financial_status==='awaiting_shipment'?'—':f(c.monthly_profit_estimate)}</td>`;
      h+=`<td class="num">${c.financial_status==='awaiting_shipment'?'—':f(c.capital)}</td>`;
      h+=`<td class="num">${c.financial_status==='awaiting_shipment'?'—':fp(c.roic_estimate)}</td>`;
      h+=`<td>${c.ad_spend_status||c.financial_status||'—'}</td><td><span class="gate ${gc(c.decision_gate)}">${c.decision_gate}</span></td></tr>`;
    });
  });
  h+='</tbody></table>';
  el.innerHTML=h;
}
function toggleChildren(gi){
  const rows=document.querySelectorAll('.child-'+gi);
  const arrow=document.getElementById('arrow-'+gi);
  const show=rows.length&&rows[0].style.display==='none';
  rows.forEach(r=>r.style.display=show?'':'none');
  arrow.innerHTML=show?'&#9660;':'&#9654;';
}

function renderAnomalies(){
  const el=document.getElementById('anomaly-drawer');
  if(!AN.length){el.innerHTML='<p style="color:var(--text-dim)">No anomalies.</p>';return}
  let h='<table><thead><tr><th>Price</th><th>Bid</th><th>Units</th><th>Revenue</th><th>Reason</th></tr></thead><tbody>';
  AN.forEach(a=>{
    h+=`<tr class="anom-row"><td>${a.price_policy}</td><td style="font-size:10px">${a.bid_policy}</td>`;
    h+=`<td class="num">${a.units_sold||0}</td><td class="num">${f(a.gross_revenue)}</td>`;
    h+=`<td>${a.anomaly_reason||'—'}</td></tr>`;
  });
  h+='</tbody></table>';
  el.innerHTML=h;
}

function renderDailyChart(){
  const el=document.getElementById('daily-chart');
  const created=D.daily_orders||[];
  const shipped=D.daily_shipped_orders||[];
  if(!created.length&&!shipped.length){el.innerHTML='';return}
  const byDate=new Map();
  created.forEach(r=>{const d=r.metric_date;if(!d)return;byDate.set(d,{date:d,created:r.qty||0,shipped:0,created_price:r.weighted_avg_price||null,shipped_price:null})});
  shipped.forEach(r=>{const d=r.metric_date;if(!d)return;const row=byDate.get(d)||{date:d,created:0,shipped:0,created_price:null,shipped_price:null};row.shipped=r.qty||0;row.shipped_price=r.weighted_avg_price||null;byDate.set(d,row)});
  const data=Array.from(byDate.values()).sort((a,b)=>a.date.localeCompare(b.date));
  const maxQ=Math.max(...data.map(r=>Math.max(r.created||0,r.shipped||0)),1);
  const W=Math.max(data.length*34,260);const H=132;const pad={t:26,b:26,l:8,r:8};
  const cw=W-pad.l-pad.r;const ch=H-pad.t-pad.b;const bw=Math.max(cw/data.length-4,6);
  let svg=`<svg class="chart-svg" viewBox="0 0 ${W} ${H}" xmlns="http://www.w3.org/2000/svg">`;
  svg+=`<text x="${pad.l}" y="10" fill="#888" font-size="8" font-family="var(--ui)">created demand</text>`;
  svg+=`<rect x="${pad.l+72}" y="4" width="8" height="8" fill="#2979ff" rx="1"/>`;
  svg+=`<text x="${pad.l+88}" y="10" fill="#888" font-size="8" font-family="var(--ui)">shipped</text>`;
  svg+=`<rect x="${pad.l+132}" y="4" width="8" height="8" fill="#00c853" rx="1"/>`;
  const labelEvery=Math.max(Math.ceil(data.length/28),1);
  data.forEach((r,i)=>{
    const x=pad.l+i*(cw/data.length)+(cw/data.length-bw)/2;
    const hc=Math.max((r.created||0)/maxQ*ch,(r.created||0)>0?2:0);const yc=pad.t+ch-hc;
    const hs=Math.max((r.shipped||0)/maxQ*ch,(r.shipped||0)>0?2:0);const ys=pad.t+ch-hs;
    if(r.created>0)svg+=`<rect x="${x}" y="${yc}" width="${bw}" height="${hc}" rx="2" fill="#2979ff" opacity=".88"/>`;
    if(r.shipped>0)svg+=`<rect x="${x+bw*.55}" y="${ys}" width="${Math.max(bw*.42,3)}" height="${hs}" rx="2" fill="#00c853" opacity=".9"/>`;
    const top=Math.min(yc||H,ys||H);
    const label=r.shipped&&r.shipped!==r.created?`${r.created}/${r.shipped}`:(r.created||r.shipped||0);
    svg+=`<text x="${x+bw/2}" y="${top-3}" text-anchor="middle" fill="#e0e0e0" font-size="8" font-family="var(--mono)">${label}</text>`;
    if(i%labelEvery===0||i===data.length-1)svg+=`<text x="${x+bw/2}" y="${H-5}" text-anchor="middle" fill="#888" font-size="7" font-family="var(--ui)">${(r.date||'').substring(5)}</text>`;
  });
  svg+='</svg>';
  const m=D.daily_series_meta||{};
  const note=`Daily context: ${m.daily_date_from||'?'} → ${m.daily_date_to||'?'}; experiment starts ${m.experiment_date_from||'?'}. Source max created: ${m.source_max_created_at||'—'}; max shipped: ${m.source_max_shipped_at||'—'}.`;
  el.innerHTML=`<div style="font:10px var(--mono);color:var(--text-dim);margin-bottom:4px">${note}</div>${svg}`;
}

function renderCategoryDrilldown(){
  const el=document.getElementById('cat-drill');
  if(!D.category_drilldown||!D.category_drilldown.length){el.innerHTML='<p style="color:var(--text-dim)">No data.</p>';return}
  let h='<table><thead><tr><th>Scenario</th><th>Cat</th><th class="num">Orders</th><th class="num">Qty</th><th class="num">Revenue</th><th class="num">Shipped</th><th class="num">Avg Price</th></tr></thead><tbody>';
  D.category_drilldown.forEach(r=>{
    h+=`<tr><td style="font-size:10px">${r.scenario_label}</td><td>${r.category}</td><td class="num">${r.orders}</td>`;
    h+=`<td class="num">${r.qty}</td><td class="num">${f(r.gross_value)}</td><td class="num">${r.shipped_qty}</td><td class="num">${f(r.avg_price)}</td></tr>`;
  });
  h+='</tbody></table>';el.innerHTML=h;
}

function renderEvidence(){
  const el=document.getElementById('ev-drawer');
  if(!D.evidence||!D.evidence.length){el.innerHTML='<p style="color:var(--text-dim)">No evidence.</p>';return}
  el.innerHTML=D.evidence.map(e=>`<details><summary>${e.source}: ${e.evidence_id}</summary><div class="ev-path">${e.path}</div></details>`).join('');
}

function renderMarketingGaps(){
  const el=document.getElementById('mkt-gap-drawer');
  const rows=D.marketing_gap_report||[];
  if(!rows.length){el.innerHTML='<p style="color:var(--text-dim)">No campaign/date gaps detected.</p>';return}
  let h='<div class="banner" style="display:block;margin-bottom:8px">Backfill these exact campaign/date rows from Kaspi Marketing before trusting daily ad-cost averages.</div>';
  h+='<table><thead><tr><th>Date</th><th>Campaign</th><th>Price</th><th>Bid</th><th>Status</th><th>Action</th></tr></thead><tbody>';
  rows.forEach(r=>{
    h+=`<tr><td>${r.date}</td><td>${r.campaign_label} ${r.campaign_id}</td><td>${r.price_policy}</td><td style="font-size:10px">${r.bid_policy}</td><td>${r.ad_spend_status}</td><td>${r.backfill_action}</td></tr>`;
  });
  h+='</tbody></table>';
  el.innerHTML=h;
}

function copyMD(){
  if(!PG.length)return;
  const hd=['Price','Period','Units','Demand/d','Revenue','Ads/d','Ads/U','Ad Cov','Profit/U','Mo.Profit','Capital','ROIC','Status','Gate'];
  let md='| '+hd.join(' | ')+' |\\n| '+hd.map(()=>'---').join(' | ')+' |\\n';
  PG.forEach(g=>{md+=`| ${g.price_label} | ${g.period} | ${g.total_units} | ${f(g.avg_daily_demand)} | ${f(g.total_revenue)} | ${f(g.avg_daily_ad_cost)} | ${f(g.ads_per_unit)} | ${fp(g.ad_coverage_pct)} | ${fs(g.unit_profit_after_ads)} | ${f(g.monthly_profit_estimate)} | ${f(g.capital)} | ${fp(g.roic_estimate)} | ${g.financial_status||'—'} | ${g.decision_gate} |\\n`});
  navigator.clipboard.writeText(md).then(()=>{const b=document.getElementById('copy-btn');b.textContent='Copied!';setTimeout(()=>b.textContent='Copy Markdown',1500)});
}

function startLiveRefresh(){
  if(!/^https?:$/.test(location.protocol))return;
  const el=document.getElementById('live-refresh-state');
  if(el)el.textContent='LIVE 60s';
  const currentGenerated=D.generated_at||'';
  setInterval(async()=>{
    try{
      const res=await fetch('/api/latest',{cache:'no-store'});
      if(!res.ok)return;
      const latest=await res.json();
      if(latest.generated_at&&latest.generated_at!==currentGenerated)location.reload();
    }catch(_err){}
  },60000);
}

function esc(s){return String(s==null?'':s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}

function renderContaminationBanner(){
  const el=document.getElementById('contam-banner');if(!el)return;
  const cs=D.current_experiment_step||{};
  const flags=cs.contamination_flags||[];
  const cap=D.delivery_promise_capture||{};
  const delta=cap.delivery_promise_delta_days||0;
  const status=String(cap.health_status||'');
  const contamination=String(cap.experiment_contamination||'');
  const cutoffNormal=status==='CUTOFF_NORMAL_POST_15'||contamination==='none_cutoff_normal_post_15';
  const reasons=[];
  flags.forEach(f=>reasons.push(`<span class="chip">${esc(f)}</span>`));
  if(delta>0&&!cutoffNormal)reasons.push(`<span class="chip">delivery_promise +${delta}d</span>`);
  if(!reasons.length){el.style.display='none';return}
  el.innerHTML='⚠ EXPERIMENT CONTAMINATED — '+reasons.join(' ');
  el.style.display='flex';
}

function renderCurrentStep(){
  const el=document.getElementById('current-step');if(!el)return;
  const cs=D.current_experiment_step;
  if(!cs){el.innerHTML='<p style="color:var(--text-dim)">No experiment plan loaded.</p>';return}
  const price=cs.price_kzt!=null?Number(cs.price_kzt).toLocaleString('en-US')+' KZT':'—';
  const stBid=cs.st_bid!=null?cs.st_bid:'—';
  const trmBid=cs.trm_bid!=null?cs.trm_bid:'—';
  const schedule=cs.schedule_label||cs.actual_started_at_local||cs.actual_ended_at_local||'—';
  const scheduleExact=cs.schedule_exact_label?` · ${cs.schedule_exact_label}`:'';
  const expDur=cs.expected_duration_days!=null?(cs.expected_duration_days+' d'):'—';
  const minU=cs.minimum_units_before_interpretation!=null?cs.minimum_units_before_interpretation:'—';
  const expType=cs.experiment_type||'—';
  const status=cs.status||'—';
  const sc=cs.success_criteria||'—';
  const stop=cs.stop_conditions||'—';
  const flags=cs.contamination_flags||[];
  const flagHtml=flags.length?`<div class="cs-flags">${flags.map(f=>`<span class="cs-flag">${esc(f)}</span>`).join('')}</div>`:'';
  el.innerHTML=`<div class="cs-card">
    <div>
      <div class="cs-price">${esc(price)}</div>
      <div class="cs-meta">${esc(cs.step_id||'')} · ${esc(status)}</div>
      <div class="cs-bids">
        <span class="cs-bid-chip">ST ${esc(stBid)}</span>
        <span class="cs-bid-chip">TRM ${esc(trmBid)}</span>
      </div>
      <div class="cs-meta" style="margin-top:6px">calendar ${esc(schedule)}${esc(scheduleExact)} · plan ${esc(expDur)} · ≥${esc(minU)} units</div>
    </div>
    <div>
      <div class="cs-meta">success criteria</div>
      <div class="cs-criteria">${esc(sc)}</div>
      <div class="cs-meta" style="margin-top:8px">experiment type</div>
      <div class="cs-criteria">${esc(expType)}</div>
    </div>
    <div>
      <div class="cs-meta">stop conditions</div>
      <div class="cs-criteria">${esc(stop)}</div>
      ${flagHtml}
    </div>
  </div>`;
}

function renderTimeline(){
  const el=document.getElementById('exp-timeline');if(!el)return;
  const plan=D.experiment_plan;
  const ladder=plan&&plan.ladder?plan.ladder:[];
  if(!ladder.length){el.innerHTML='<p style="color:var(--text-dim);padding:6px">No experiment ladder configured.</p>';return}
  function cls(s){s=String(s||'');if(s.startsWith('active'))return'tl-active';if(s.startsWith('closed'))return'tl-closed';if(s.startsWith('conditional'))return'tl-conditional';if(s.startsWith('planned'))return'tl-planned';return''}
  const html='<div class="timeline">'+ladder.map(s=>{
    const price=s.price_kzt!=null?Number(s.price_kzt).toLocaleString('en-US'):'—';
    const bids=`ST${s.st_bid==null?'—':s.st_bid}/TRM${s.trm_bid==null?'—':s.trm_bid}`;
    const schedule=s.schedule_label||'date pending';
    const exact=s.schedule_exact_label||'';
    const today=s.is_today_window?' tl-today':'';
    return `<div class="tl-step ${cls(s.status)}${today}"><div class="tl-id">${esc(s.step_id||'')}</div><div class="tl-price">${esc(price)}</div><div class="tl-bids">${esc(bids)}</div><div class="tl-dates">${esc(schedule)}</div><div class="tl-exact">${esc(exact)}</div><div class="tl-status">${esc(s.status||'')}</div></div>`;
  }).join('')+'</div>';
  el.innerHTML=html;
}

function renderInsightCards(){
  const el=document.getElementById('insight-cards');if(!el)return;
  const cards=D.deterministic_insights||[];
  if(!cards.length){el.innerHTML='';return}
  el.innerHTML=cards.map(c=>{
    const sev=c.severity==='ok'?'insight-ok':c.severity==='warn'?'insight-warn':c.severity==='fail'?'insight-fail':'';
    return `<div class="insight ${sev}"><div class="insight-title">${esc(c.title)}</div><div class="insight-val">${esc(c.value)}</div><div class="insight-note" title="${esc(c.note)}">${esc(c.note)}</div></div>`;
  }).join('');
}

function renderTargetVsActual(){
  const el=document.getElementById('tva-table');if(!el)return;
  const rows=D.target_vs_actual||[];
  if(!rows.length){el.innerHTML='<p style="color:var(--text-dim)">No experiment plan loaded; target/actual unavailable.</p>';return}
  let h='<table class="tva-table"><thead><tr><th>Step</th><th>Status</th><th>Price</th><th>Schedule</th><th class="num">Actual</th><th>Target</th><th>Note</th></tr></thead><tbody>';
  rows.forEach(r=>{
    const cls=r.status||'tva-missing';
    const isSummary=r.kind==='summary';
    const rowCls=(isSummary?'tva-row tva-summary':'tva-row')+(r.is_today_window?' tva-today':'');
    const scheduleCell=`<span class="sched-main">${esc(r.schedule_label||'')}</span><span class="sched-exact">${esc(r.schedule_exact_label||'')}</span>`;
    let actual='—';
    if(r.actual_value!=null){
      if(r.target_unit==='%')actual=Number(r.actual_value).toFixed(1)+'%';
      else if(r.target_unit==='/day')actual=Number(r.actual_value).toFixed(2);
      else actual=Number(r.actual_value).toLocaleString('en-US',{maximumFractionDigits:0});
    }else if(isSummary){
      actual='—';
    }else{
      actual='<span class="tva-missing">— missing</span>';
    }
    h+=`<tr class="${rowCls}"><td>${esc(r.step_id||'')}</td><td>${esc(r.step_status||'')}</td><td>${esc(r.price_kzt!=null?(Number(r.price_kzt).toLocaleString('en-US')+' KZT'):'')}</td><td class="tva-schedule">${scheduleCell}</td><td class="num ${esc(cls)}">${actual}</td><td>${esc(r.target_label||'')}</td><td>${esc(r.note||'')}</td></tr>`;
  });
  h+='</tbody></table>';
  el.innerHTML=h;
}

function renderObservedVsProjected(){
  const el=document.getElementById('ovp-table');if(!el)return;
  const rows=D.observed_vs_projected||[];
  if(!rows.length){el.innerHTML='<p style="color:var(--text-dim)">No price groups available.</p>';return}
  let h='<table><thead><tr><th>Price</th><th>Metric</th><th class="num">Observed</th><th class="num">Projected</th><th>Status</th></tr></thead><tbody>';
  rows.forEach(r=>{
    const obs=r.observed!=null?(typeof r.observed==='number'?f(r.observed):esc(r.observed)):'—';
    const proj=r.projected!=null?(typeof r.projected==='number'?f(r.projected):esc(r.projected)):'<span class="proj-missing">projection missing</span>';
    h+=`<tr><td>${esc(r.price_label)}</td><td>${esc(r.metric_label||r.metric)}</td><td class="num">${obs}</td><td class="num">${proj}</td><td>${esc(r.status||'')}</td></tr>`;
  });
  h+='</tbody></table>';
  el.innerHTML=h;
}

document.addEventListener('DOMContentLoaded',()=>{renderContaminationBanner();renderRibbon();renderCurrentStep();renderTimeline();renderInsightCards();renderKPIs();renderWaterfall();renderTargetVsActual();renderPriceTable();renderObservedVsProjected();renderAnomalies();renderDailyChart();renderMarketingGaps();renderCategoryDrilldown();renderEvidence();startLiveRefresh()});
"""


def render_dashboard_html(payload: dict[str, Any]) -> str:
    data_json = json.dumps(payload, ensure_ascii=False, default=str)
    target = payload.get("target", {})
    generated = payload.get("generated_at", "")
    cids = target.get("campaign_ids", [])
    cid_labels = " | ".join(
        f"{CAMPAIGN_LABELS.get(c, c)} {c}" for c in cids
    )
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Line61 Dashboard — {_esc(target.get('store', ''))}</title>
<style>{_CSS}</style>
</head>
<body>
<div id="contam-banner" class="contam-banner" style="display:none"></div>

<div class="ticker">
  <span class="sku">LINE61</span>
  <span class="sep">|</span>
  <span>{_esc(target.get('store', ''))}</span>
  <span class="sep">|</span>
  <span>{_esc(cid_labels)}</span>
  <span class="sep" id="live-refresh-state"></span>
  <span class="ts">{_esc(generated)}</span>
</div>

<div id="stale-banner" class="banner" style="display:none">
  Data sources stale or missing — metrics may be incomplete.
</div>

<div id="src-ribbon" class="ribbon"></div>

<div class="section">CURRENT STEP</div>
<div class="card" id="current-step"></div>

<div class="section">EXPERIMENT TIMELINE</div>
<div class="card" id="exp-timeline" style="padding:6px"></div>

<div class="section">DETERMINISTIC INSIGHTS</div>
<div id="insight-cards" class="insight-grid"></div>

<div class="section">KEY METRICS</div>
<div id="kpi-grid" class="kpi-grid"></div>

<div class="section">UNIT ECONOMICS</div>
<div class="card" id="waterfall"></div>

<div class="section">TARGET vs ACTUAL</div>
<div class="card"><div id="tva-table"></div></div>

<div class="section">PRICE GROUP COMPARISON</div>
<div class="toggle-row">
  <button id="copy-btn" class="btn" onclick="copyMD()">Copy Markdown</button>
</div>
<div class="card">
  <div id="price-table" class="sticky-thead"></div>
</div>

<details>
  <summary class="section" style="cursor:pointer">OBSERVED vs PROJECTED ({len(payload.get('observed_vs_projected', []))})</summary>
  <div class="card"><div id="ovp-table"></div></div>
</details>

<div class="section">DAILY DEMAND</div>
<div class="card" style="padding-bottom:4px">
  <div id="daily-chart"></div>
</div>

<details style="margin-top:12px">
  <summary class="section" style="cursor:pointer">ANOMALIES ({len(payload.get('anomaly_observations', []))})</summary>
  <div class="card"><div id="anomaly-drawer"></div></div>
</details>

<details>
  <summary class="section" style="cursor:pointer">MARKETING GAPS ({len(payload.get('marketing_gap_report', []))})</summary>
  <div class="card"><div id="mkt-gap-drawer"></div></div>
</details>

<details>
  <summary class="section" style="cursor:pointer">CATEGORY DRILLDOWN</summary>
  <div class="card"><div id="cat-drill"></div></div>
</details>

<details>
  <summary class="section" style="cursor:pointer">EVIDENCE ({len(payload.get('evidence', []))})</summary>
  <div class="card"><div id="ev-drawer"></div></div>
</details>

<script id="dashboard-data" type="application/json">{data_json}</script>
<script>{_JS}</script>
</body>
</html>"""


# ---------------------------------------------------------------------------
# Serve
# ---------------------------------------------------------------------------


def serve_dashboard(
    *,
    cache_dir: Path = DEFAULT_DASHBOARD_CACHE_DIR,
    run_dir: Path | None = None,
    port: int = DEFAULT_DASHBOARD_PORT,
    host: str = "127.0.0.1",
    open_browser: bool = False,
) -> None:
    initial_dir = resolve_dashboard_serve_dir(cache_dir, run_dir)
    if run_dir is None:
        logging.info(
            "Serving latest dashboard from %s on http://%s:%s/ (cache=%s)",
            initial_dir,
            host,
            port,
            cache_dir,
        )
    else:
        logging.info("Serving dashboard from %s on http://%s:%s/", initial_dir, host, port)

    class Handler(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, directory=str(initial_dir), **kwargs)

        def _current_serve_dir(self) -> Path:
            return resolve_dashboard_serve_dir(cache_dir, run_dir)

        def _send_json(self, payload: Any, status: int = 200) -> None:
            body = json.dumps(payload, ensure_ascii=False, indent=2, default=str).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _send_missing(self, exc: Exception) -> None:
            self._send_json({"status": "missing", "message": str(exc)}, status=404)

        def translate_path(self, path: str) -> str:
            base = self._current_serve_dir()
            parsed_path = urllib.parse.urlparse(path).path
            parsed_path = posixpath.normpath(urllib.parse.unquote(parsed_path))
            words = [w for w in parsed_path.split("/") if w]
            resolved = base
            for word in words:
                if word in (os.curdir, os.pardir):
                    continue
                resolved = resolved / word
            return str(resolved)

        def do_GET(self) -> None:
            parsed_path = urllib.parse.urlparse(self.path).path
            try:
                if parsed_path == "/api/latest":
                    current = self._current_serve_dir()
                    metadata = read_latest_run_metadata(cache_dir) or {}
                    self._send_json({
                        **metadata,
                        "served_dashboard_dir": str(current),
                        "served_from_live": current.name == LIVE_DASHBOARD_DIR_NAME,
                    })
                    return
                if parsed_path == "/api/heartbeat":
                    heartbeat = read_sync_heartbeat(cache_dir)
                    if heartbeat is None:
                        self._send_json(
                            {"status": "missing", "message": "No heartbeat file found"},
                            status=404,
                        )
                    else:
                        self._send_json(heartbeat)
                    return
                if parsed_path == "/api/dashboard.json":
                    dashboard_path = self._current_serve_dir() / "dashboard.json"
                    if not dashboard_path.exists():
                        raise FileNotFoundError(f"Dashboard JSON not found: {dashboard_path}")
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json; charset=utf-8")
                    self.send_header("Cache-Control", "no-store")
                    self.send_header("Content-Length", str(dashboard_path.stat().st_size))
                    self.end_headers()
                    with dashboard_path.open("rb") as fh:
                        self.copyfile(fh, self.wfile)
                    return
                self._current_serve_dir()
            except FileNotFoundError as exc:
                self._send_missing(exc)
                return
            super().do_GET()

        def do_HEAD(self) -> None:
            try:
                self._current_serve_dir()
            except FileNotFoundError as exc:
                self._send_missing(exc)
                return
            super().do_HEAD()

        def log_message(self, format: str, *args: Any) -> None:
            logging.debug(format, *args)

    if open_browser:
        threading.Timer(0.5, lambda: webbrowser.open(f"http://{host}:{port}/")).start()

    with http.server.HTTPServer((host, port), Handler) as httpd:
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            logging.info("Dashboard server stopped.")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _r2(v: float | None) -> float | None:
    return round(v, 2) if v is not None else None


def _r4(v: float | None) -> float | None:
    return round(v, 4) if v is not None else None


def _esc(text: str) -> str:
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


def _write_csv(path: Path, rows: Sequence[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields: list[str] = []
    for row in rows:
        for key in row:
            if key not in fields:
                fields.append(key)
    with path.open("w", newline="", encoding="utf-8") as fh:
        if not fields:
            fh.write("")
            return
        writer = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            clean = {}
            for k, v in row.items():
                if isinstance(v, (list, dict)):
                    clean[k] = json.dumps(v, ensure_ascii=False)
                else:
                    clean[k] = v
            writer.writerow(clean)
