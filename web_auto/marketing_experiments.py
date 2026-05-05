from __future__ import annotations

import csv
import hashlib
import json
import sqlite3
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Sequence

from .autonomous_business_readonly import (
    build_app_db_uri,
    confirmed_send_order_rows,
    iter_send_batches,
    load_send_batch,
)
from .kaspi_marketing import MarketingCredentials, ensure_marketing_tables, run_kaspi_marketing_fetch
from .kaspi_merchant_common import normalize_store_name


DEFAULT_MARKETING_DB = Path("data/kaspi_marketing.sqlite")
DEFAULT_CHANGE_LOG = Path("data/marketing_experiment_change_log.csv")
DEFAULT_EXPERIMENT_ROOT = Path("runs/kaspi_marketing_experiments")
DEFAULT_WATCH_ROOT = Path("runs/kaspi_marketing_watch")
DEFAULT_AB_ROOT = Path("~/Docs/Autonomous_business")
DEFAULT_STALE_MINUTES = 75

CHANGE_LOG_FIELDS = [
    "event_id",
    "created_at",
    "effective_at",
    "store_name",
    "store_code",
    "product_scope",
    "sku_key",
    "campaign_id",
    "campaign_name",
    "category",
    "change_type",
    "metric_name",
    "old_value",
    "new_value",
    "currency",
    "reason",
    "expected_duration_hours",
    "operator_note",
    "source_url",
    "evidence_run_dir",
    "status",
]

ALLOWED_CHANGE_TYPES = {"bid_cpc", "price", "budget", "campaign_state", "product_state"}
ALLOWED_STATUSES = {"planned", "active", "closed", "rolled_back"}


def now_local_text() -> str:
    return datetime.now().replace(microsecond=0).isoformat(sep=" ")


def normalize_local_timestamp(value: str | None) -> str:
    text = str(value or "").strip().strip("`")
    if not text:
        return now_local_text()
    text = text.replace("T", " ")
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    if len(text) == 10 and text[4] == "-" and text[7] == "-":
        text = f"{text} 00:00:00"
    for candidate, fmt in (
        (text[:19], "%Y-%m-%d %H:%M:%S"),
        (text[:16], "%Y-%m-%d %H:%M"),
        (text[:10], "%Y-%m-%d"),
    ):
        try:
            return datetime.strptime(candidate, fmt).strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            pass
    try:
        parsed = datetime.fromisoformat(text)
        return parsed.replace(tzinfo=None, microsecond=0).isoformat(sep=" ")
    except Exception:
        return text


def _parse_dt(value: str | None) -> datetime | None:
    text = normalize_local_timestamp(value)
    try:
        return datetime.fromisoformat(text)
    except Exception:
        return None


def _slug(value: str, fallback: str = "na") -> str:
    out = []
    for ch in str(value or "").strip().lower():
        if ch.isalnum():
            out.append(ch)
        elif ch in {"-", "_"}:
            out.append("_")
    text = "".join(out).strip("_")
    return text or fallback


def build_event_id(row: dict[str, Any]) -> str:
    effective_at = normalize_local_timestamp(str(row.get("effective_at") or ""))
    digits = "".join(ch for ch in effective_at if ch.isdigit())
    ts = f"{digits[:8]}_{digits[8:14]}" if len(digits) >= 14 else datetime.now().strftime("%Y%m%d_%H%M%S")
    canonical = "|".join(
        str(row.get(key) or "").strip()
        for key in (
            "effective_at",
            "store_name",
            "store_code",
            "product_scope",
            "sku_key",
            "campaign_id",
            "category",
            "change_type",
            "metric_name",
            "old_value",
            "new_value",
        )
    )
    digest = hashlib.sha1(canonical.encode("utf-8")).hexdigest()[:8]
    return "_".join(
        [
            ts,
            _slug(str(row.get("store_name") or row.get("store_code") or "store")),
            _slug(str(row.get("campaign_id") or "campaign")),
            _slug(str(row.get("change_type") or "change")),
            _slug(str(row.get("metric_name") or "metric")),
            digest,
        ]
    )


def ensure_experiment_tables(conn: sqlite3.Connection) -> None:
    ensure_marketing_tables(conn)
    columns_sql = ",\n            ".join(f"{field} TEXT" for field in CHANGE_LOG_FIELDS)
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS experiment_change_events (
            {columns_sql},
            PRIMARY KEY (event_id)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS experiment_period_metrics (
            event_id TEXT,
            generated_at TEXT,
            period_start TEXT,
            period_end TEXT,
            campaign_rows INTEGER,
            campaign_product_rows INTEGER,
            order_rows INTEGER,
            order_qty INTEGER,
            shipped_qty INTEGER,
            gross_value REAL,
            avg_price REAL,
            watcher_status TEXT,
            summary_json TEXT,
            PRIMARY KEY (event_id, generated_at)
        )
        """
    )
    conn.commit()


def read_change_ledger(path: Path = DEFAULT_CHANGE_LOG) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open("r", newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        return [dict(row) for row in reader]


def latest_event_rows(rows: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    latest: dict[str, dict[str, Any]] = {}
    for row in rows:
        event_id = str(row.get("event_id") or "").strip()
        if not event_id:
            continue
        current = latest.get(event_id)
        if current is None or str(row.get("created_at") or "") >= str(current.get("created_at") or ""):
            latest[event_id] = dict(row)
    return list(latest.values())


def _append_ledger_row(path: Path, row: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    exists = path.exists() and path.stat().st_size > 0
    with path.open("a", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=CHANGE_LOG_FIELDS)
        if not exists:
            writer.writeheader()
        writer.writerow({field: str(row.get(field, "") or "") for field in CHANGE_LOG_FIELDS})


def _upsert_event(conn: sqlite3.Connection, row: dict[str, Any]) -> None:
    ensure_experiment_tables(conn)
    placeholders = ", ".join(f":{field}" for field in CHANGE_LOG_FIELDS)
    columns = ", ".join(CHANGE_LOG_FIELDS)
    updates = ", ".join(f"{field}=excluded.{field}" for field in CHANGE_LOG_FIELDS if field != "event_id")
    conn.execute(
        f"""
        INSERT INTO experiment_change_events ({columns})
        VALUES ({placeholders})
        ON CONFLICT(event_id) DO UPDATE SET {updates}
        """,
        {field: str(row.get(field, "") or "") for field in CHANGE_LOG_FIELDS},
    )
    conn.commit()


def log_change_event(
    *,
    db_path: Path,
    ledger_path: Path,
    event: dict[str, Any],
    run_root: Path = DEFAULT_EXPERIMENT_ROOT,
    append_if_existing: bool = False,
) -> dict[str, Any]:
    row = {field: str(event.get(field, "") or "").strip() for field in CHANGE_LOG_FIELDS}
    row["created_at"] = normalize_local_timestamp(row.get("created_at") or now_local_text())
    row["effective_at"] = normalize_local_timestamp(row.get("effective_at") or row["created_at"])
    row["store_name"] = normalize_store_name(row.get("store_name") or "ACMEWEAR")
    row["change_type"] = row.get("change_type") or "bid_cpc"
    row["status"] = row.get("status") or "active"
    if row["change_type"] not in ALLOWED_CHANGE_TYPES:
        raise ValueError(f"invalid change_type: {row['change_type']}")
    if row["status"] not in ALLOWED_STATUSES:
        raise ValueError(f"invalid status: {row['status']}")
    if not row.get("event_id"):
        row["event_id"] = build_event_id(row)
    if not row.get("evidence_run_dir"):
        row["evidence_run_dir"] = str(run_root / row["event_id"])

    existing = {item.get("event_id") for item in read_change_ledger(ledger_path)}
    appended = False
    if append_if_existing or row["event_id"] not in existing:
        _append_ledger_row(ledger_path, row)
        appended = True

    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as conn:
        _upsert_event(conn, row)

    run_dir = Path(row["evidence_run_dir"])
    run_dir.mkdir(parents=True, exist_ok=True)
    _write_json(run_dir / "change_event.json", row)
    summary = {
        "status": "success",
        "action": "logged" if appended else "already_exists",
        "event_id": row["event_id"],
        "ledger_path": str(ledger_path),
        "db_path": str(db_path),
        "run_dir": str(run_dir),
        "change_event": row,
    }
    _write_json(run_dir / "summary.json", summary)
    return summary


def load_events(db_path: Path = DEFAULT_MARKETING_DB, ledger_path: Path = DEFAULT_CHANGE_LOG) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if db_path.exists():
        try:
            with sqlite3.connect(db_path) as conn:
                conn.row_factory = sqlite3.Row
                ensure_experiment_tables(conn)
                rows.extend(dict(row) for row in conn.execute("SELECT * FROM experiment_change_events").fetchall())
        except sqlite3.DatabaseError:
            rows = []
    if not rows:
        rows = read_change_ledger(ledger_path)
    return latest_event_rows(rows)


def get_event(event_id: str, *, db_path: Path = DEFAULT_MARKETING_DB, ledger_path: Path = DEFAULT_CHANGE_LOG) -> dict[str, Any]:
    for row in load_events(db_path=db_path, ledger_path=ledger_path):
        if str(row.get("event_id") or "") == str(event_id):
            return row
    raise ValueError(f"event not found: {event_id}")


def active_events(
    *,
    db_path: Path = DEFAULT_MARKETING_DB,
    ledger_path: Path = DEFAULT_CHANGE_LOG,
    active_only: bool = True,
) -> list[dict[str, Any]]:
    statuses = {"active"} if active_only else {"planned", "active"}
    return [row for row in load_events(db_path=db_path, ledger_path=ledger_path) if str(row.get("status") or "") in statuses]


def dedupe_campaign_ids(events: Sequence[dict[str, Any]]) -> list[str]:
    out: list[str] = []
    for row in events:
        campaign_id = str(row.get("campaign_id") or "").strip()
        if campaign_id and campaign_id not in out:
            out.append(campaign_id)
    return out


def classify_marketing_category(row: dict[str, Any]) -> str:
    campaign_id = str(row.get("campaign_id") or "")
    text = " ".join(
        str(row.get(key) or "")
        for key in ("campaign_name", "category", "kaspi_offer_name", "sku_id", "sku_key", "json_merchant_sku", "product_name")
    ).lower()
    if campaign_id == "2545773" or "спортивный костюм" in text:
        return "ST"
    if campaign_id == "2629982" or "комплект" in text or "trm" in text:
        return "TRM"
    return str(row.get("category") or "UNKNOWN").strip() or "UNKNOWN"


def _dict_rows(conn: sqlite3.Connection, sql: str, params: Sequence[Any]) -> list[dict[str, Any]]:
    conn.row_factory = sqlite3.Row
    return [dict(row) for row in conn.execute(sql, list(params)).fetchall()]


def _filter_rows_by_time(rows: list[dict[str, Any]], field: str, start_at: str, end_at: str) -> list[dict[str, Any]]:
    start_dt = _parse_dt(start_at)
    end_dt = _parse_dt(end_at)
    out = []
    for row in rows:
        row_dt = _parse_dt(str(row.get(field) or ""))
        if row_dt is None:
            continue
        if start_dt and row_dt < start_dt:
            continue
        if end_dt and row_dt >= end_dt:
            continue
        out.append(row)
    return out


def resolve_period(
    event: dict[str, Any],
    events: Sequence[dict[str, Any]],
    *,
    cutoff_at: str | None = None,
) -> dict[str, str]:
    start_at = normalize_local_timestamp(str(event.get("effective_at") or ""))
    start_dt = _parse_dt(start_at)
    matching_next: list[datetime] = []
    for row in events:
        if str(row.get("event_id") or "") == str(event.get("event_id") or ""):
            continue
        if str(row.get("campaign_id") or "") != str(event.get("campaign_id") or ""):
            continue
        if str(row.get("metric_name") or "") != str(event.get("metric_name") or ""):
            continue
        row_dt = _parse_dt(str(row.get("effective_at") or ""))
        if start_dt and row_dt and row_dt > start_dt:
            matching_next.append(row_dt)
    if matching_next:
        end_at = min(matching_next).isoformat(sep=" ")
        end_reason = "next_change"
    elif cutoff_at:
        end_at = normalize_local_timestamp(cutoff_at)
        end_reason = "explicit_cutoff"
    else:
        end_at = now_local_text()
        end_reason = "report_generated_at"
    return {"period_start": start_at, "period_end": end_at, "period_end_reason": end_reason}


def fetch_marketing_period_rows(
    *,
    marketing_db: Path,
    event: dict[str, Any],
    period_start: str,
    period_end: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]:
    warnings: list[str] = []
    if not marketing_db.exists():
        return [], [], [f"marketing_db_missing:{marketing_db}"]
    campaign_id = str(event.get("campaign_id") or "").strip()
    if not campaign_id:
        return [], [], ["event_campaign_id_missing"]
    try:
        with sqlite3.connect(marketing_db) as conn:
            ensure_experiment_tables(conn)
            campaign_rows = _dict_rows(
                conn,
                "SELECT * FROM campaign_daily_history WHERE campaign_id = ? ORDER BY ingested_at",
                [campaign_id],
            )
            product_rows = _dict_rows(
                conn,
                "SELECT * FROM campaign_product_daily_history WHERE campaign_id = ? ORDER BY ingested_at",
                [campaign_id],
            )
    except sqlite3.DatabaseError as exc:
        return [], [], [f"marketing_db_error:{exc}"]
    return (
        _filter_rows_by_time(campaign_rows, "ingested_at", period_start, period_end),
        _filter_rows_by_time(product_rows, "ingested_at", period_start, period_end),
        warnings,
    )


def fetch_order_period_rows(
    *,
    ab_root: Path,
    event: dict[str, Any],
    period_start: str,
    period_end: str,
) -> tuple[list[dict[str, Any]], str, list[str]]:
    warnings: list[str] = []
    db_uri = build_app_db_uri(ab_root)
    store_code = str(event.get("store_code") or "").strip()
    sku_key = str(event.get("sku_key") or "").strip()
    where = ["datetime(created_at) >= datetime(?)", "datetime(created_at) < datetime(?)"]
    params: list[Any] = [period_start, period_end]
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
        with sqlite3.connect(db_uri, uri=True) as conn:
            rows = _dict_rows(conn, sql, params)
            max_created_at = conn.execute("SELECT max(created_at) FROM fact_orders_kaspi").fetchone()[0] or ""
    except sqlite3.DatabaseError as exc:
        return [], "", [f"ab_db_read_error:{exc}"]
    for row in rows:
        category = classify_marketing_category(row)
        if category == "UNKNOWN":
            category = str(event.get("category") or "UNKNOWN").strip() or "UNKNOWN"
        row["category"] = category
        row["is_shipped"] = (
            str(row.get("internal_status") or "") == "SHIPPED"
            and str(row.get("actual_shipment_date") or row.get("courier_transmission_date") or "").strip() != ""
        )
    return rows, str(max_created_at or ""), warnings


def summarize_orders(rows: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    buckets: dict[str, dict[str, Any]] = defaultdict(
        lambda: {
            "category": "",
            "orders": 0,
            "qty": 0,
            "gross_value": 0.0,
            "shipped_qty": 0,
            "first_created": "",
            "last_created": "",
        }
    )
    for row in rows:
        category = str(row.get("category") or "UNKNOWN")
        bucket = buckets[category]
        bucket["category"] = category
        try:
            qty = int(row.get("quantity") or 1)
        except Exception:
            qty = 1
        try:
            price = float(row.get("unit_price_kzt") or 0)
        except Exception:
            price = 0.0
        bucket["orders"] += 1
        bucket["qty"] += qty
        bucket["gross_value"] += qty * price
        if row.get("is_shipped"):
            bucket["shipped_qty"] += qty
        created_at = str(row.get("created_at") or "")
        if created_at:
            if not bucket["first_created"] or created_at < bucket["first_created"]:
                bucket["first_created"] = created_at
            if not bucket["last_created"] or created_at > bucket["last_created"]:
                bucket["last_created"] = created_at
    out = []
    for key in sorted(buckets):
        row = buckets[key]
        row["avg_price"] = round(row["gross_value"] / row["qty"], 2) if row["qty"] else None
        out.append(row)
    return out


def fetch_send_confirmation_rows(
    *,
    ab_root: Path,
    order_rows: Sequence[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[str]]:
    wanted = {str(row.get("order_id") or "").strip() for row in order_rows if str(row.get("order_id") or "").strip()}
    if not wanted:
        return [], []
    warnings: list[str] = []
    out: list[dict[str, Any]] = []
    try:
        batches = iter_send_batches(ab_root)
    except Exception as exc:
        return [], [f"send_batch_read_error:{exc}"]
    for batch in batches:
        try:
            manifest, ledger = load_send_batch(batch)
        except Exception as exc:
            warnings.append(f"send_batch_load_error:{batch}:{exc}")
            continue
        for row in confirmed_send_order_rows(manifest, ledger):
            if str(row.get("order_id") or "") in wanted:
                out.append({**row, "batch_dir": str(batch)})
    return out, warnings


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


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
        writer = csv.DictWriter(fh, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def build_experiment_report(
    *,
    event_id: str,
    marketing_db: Path = DEFAULT_MARKETING_DB,
    ledger_path: Path = DEFAULT_CHANGE_LOG,
    ab_root: Path = DEFAULT_AB_ROOT,
    run_dir: Path | None = None,
    cutoff_at: str | None = None,
) -> dict[str, Any]:
    event = get_event(event_id, db_path=marketing_db, ledger_path=ledger_path)
    all_events = load_events(db_path=marketing_db, ledger_path=ledger_path)
    period = resolve_period(event, all_events, cutoff_at=cutoff_at)
    output_dir = run_dir or Path(event.get("evidence_run_dir") or (DEFAULT_EXPERIMENT_ROOT / event_id))
    output_dir.mkdir(parents=True, exist_ok=True)
    period_start = period["period_start"]
    period_end = period["period_end"]

    campaign_rows, product_rows, marketing_warnings = fetch_marketing_period_rows(
        marketing_db=marketing_db,
        event=event,
        period_start=period_start,
        period_end=period_end,
    )
    order_rows, ab_db_max_created_at, order_warnings = fetch_order_period_rows(
        ab_root=ab_root,
        event=event,
        period_start=period_start,
        period_end=period_end,
    )
    send_rows, send_warnings = fetch_send_confirmation_rows(ab_root=ab_root, order_rows=order_rows)
    order_summary = summarize_orders(order_rows)

    campaign_product_rows = []
    for row in product_rows:
        campaign_product_rows.append({**row, "category": classify_marketing_category({**event, **row})})

    _write_json(output_dir / "change_event.json", event)
    _write_csv(output_dir / "campaign_metrics.csv", campaign_rows)
    _write_csv(output_dir / "campaign_product_metrics.csv", campaign_product_rows)
    _write_csv(output_dir / "orders.csv", order_rows)
    _write_csv(output_dir / "orders_summary.csv", order_summary)
    _write_csv(output_dir / "send_confirmation.csv", send_rows)
    _write_csv(output_dir / "campaign_metrics_by_period.csv", campaign_rows)
    _write_csv(output_dir / "orders_by_period.csv", order_rows)
    _write_csv(output_dir / "orders_by_period_summary.csv", order_summary)
    _write_csv(output_dir / "send_confirmation_by_period.csv", send_rows)

    total_qty = sum(int(row.get("qty") or 0) for row in order_summary)
    shipped_qty = sum(int(row.get("shipped_qty") or 0) for row in order_summary)
    gross_value = sum(float(row.get("gross_value") or 0) for row in order_summary)
    avg_price = round(gross_value / total_qty, 2) if total_qty else None
    warnings = [*marketing_warnings, *order_warnings, *send_warnings]
    summary = {
        "status": "success" if not order_warnings else "partial",
        "generated_at": now_local_text(),
        "event_id": event_id,
        "run_dir": str(output_dir),
        "period_start": period_start,
        "period_end": period_end,
        "period_end_reason": period["period_end_reason"],
        "change_event": event,
        "campaign_rows": len(campaign_rows),
        "campaign_product_rows": len(campaign_product_rows),
        "order_rows": len(order_rows),
        "order_qty": total_qty,
        "shipped_qty": shipped_qty,
        "gross_value": round(gross_value, 2),
        "avg_price": avg_price,
        "send_confirmed_orders": len(send_rows),
        "ab_db_max_created_at": ab_db_max_created_at,
        "warnings": warnings,
        "outputs": {
            "change_event": str(output_dir / "change_event.json"),
            "campaign_metrics": str(output_dir / "campaign_metrics.csv"),
            "campaign_product_metrics": str(output_dir / "campaign_product_metrics.csv"),
            "orders": str(output_dir / "orders.csv"),
            "orders_summary": str(output_dir / "orders_summary.csv"),
            "send_confirmation": str(output_dir / "send_confirmation.csv"),
            "experiment_report": str(output_dir / "experiment_report.md"),
        },
    }
    _write_json(output_dir / "summary.json", summary)
    _write_experiment_markdown(output_dir / "experiment_report.md", summary, order_summary)

    with sqlite3.connect(marketing_db) as conn:
        ensure_experiment_tables(conn)
        conn.execute(
            """
            INSERT OR REPLACE INTO experiment_period_metrics (
                event_id, generated_at, period_start, period_end, campaign_rows,
                campaign_product_rows, order_rows, order_qty, shipped_qty, gross_value,
                avg_price, watcher_status, summary_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                event_id,
                summary["generated_at"],
                period_start,
                period_end,
                len(campaign_rows),
                len(campaign_product_rows),
                len(order_rows),
                total_qty,
                shipped_qty,
                round(gross_value, 2),
                avg_price,
                "",
                json.dumps(summary, ensure_ascii=False),
            ],
        )
        conn.commit()
    return summary


def _write_experiment_markdown(path: Path, summary: dict[str, Any], order_summary: Sequence[dict[str, Any]]) -> None:
    event = summary["change_event"]
    lines = [
        "# Kaspi Marketing Experiment Report",
        "",
        f"- Generated at: `{summary['generated_at']}`",
        f"- Event ID: `{summary['event_id']}`",
        f"- Store: `{event.get('store_name')}` / `{event.get('store_code')}`",
        f"- Campaign: `{event.get('campaign_id')}` `{event.get('campaign_name')}`",
        f"- SKU key: `{event.get('sku_key')}`",
        f"- Change: `{event.get('change_type')}` `{event.get('metric_name')}` from `{event.get('old_value')}` to `{event.get('new_value')}`",
        f"- Effective at: `{event.get('effective_at')}`",
        f"- Period: `{summary['period_start']}` to `{summary['period_end']}`",
        f"- Period end reason: `{summary['period_end_reason']}`",
        f"- AB DB max `created_at`: `{summary.get('ab_db_max_created_at')}`",
        "",
        "## Period Counts",
        "",
        "| Campaign Rows | Product Rows | Order Rows | Qty | Shipped Qty | Avg Price | Send Confirmed |",
        "|---:|---:|---:|---:|---:|---:|---:|",
        f"| {summary['campaign_rows']} | {summary['campaign_product_rows']} | {summary['order_rows']} | {summary['order_qty']} | {summary['shipped_qty']} | {summary['avg_price']} | {summary['send_confirmed_orders']} |",
        "",
        "## Orders Summary",
        "",
        "| Category | Orders | Qty | Shipped Qty | Gross Value | Avg Price | First Created | Last Created |",
        "|---|---:|---:|---:|---:|---:|---|---|",
    ]
    for row in order_summary:
        lines.append(
            f"| {row.get('category')} | {row.get('orders')} | {row.get('qty')} | {row.get('shipped_qty')} | {row.get('gross_value')} | {row.get('avg_price')} | {row.get('first_created')} | {row.get('last_created')} |"
        )
    lines.extend(
        [
            "",
            "## Warnings",
            "",
        ]
    )
    if summary.get("warnings"):
        for warning in summary["warnings"]:
            lines.append(f"- `{warning}`")
    else:
        lines.append("- none")
    lines.extend(
        [
            "",
            "## Source Contract",
            "",
            "- Marketing truth is local to `Web_automation`: `data/kaspi_marketing.sqlite`.",
            "- Order truth is read-only from Autonomous Business: `file:~/Docs/Autonomous_business/db/app.db?mode=ro`.",
            "- WhatsApp send confirmation is read from `send_ledger.json`; it is not assumed to be present in the order DB.",
            "- This report writes only inside `Web_automation`.",
        ]
    )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def heartbeat_status(last_success_at: str | None, *, now_at: str | None = None, stale_minutes: int = DEFAULT_STALE_MINUTES) -> str:
    if not last_success_at:
        return "red"
    last_dt = _parse_dt(last_success_at)
    now_dt = _parse_dt(now_at or now_local_text())
    if last_dt is None or now_dt is None:
        return "red"
    return "red" if now_dt - last_dt > timedelta(minutes=stale_minutes) else "green"


def _load_latest_heartbeat(watch_root: Path) -> dict[str, Any]:
    path = watch_root / "latest_heartbeat.json"
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def default_watch_run_dir(watch_root: Path = DEFAULT_WATCH_ROOT) -> Path:
    return watch_root / datetime.now().strftime("%Y%m%d_%H%M%S")


def run_marketing_watch(
    *,
    creds: MarketingCredentials | None,
    db_path: Path = DEFAULT_MARKETING_DB,
    ledger_path: Path = DEFAULT_CHANGE_LOG,
    watch_root: Path = DEFAULT_WATCH_ROOT,
    active_only: bool = True,
    target_date: str | None = None,
    headless: bool = True,
    stale_minutes: int = DEFAULT_STALE_MINUTES,
    fetch_runner: Callable[..., dict[str, Any]] = run_kaspi_marketing_fetch,
) -> dict[str, Any]:
    events = active_events(db_path=db_path, ledger_path=ledger_path, active_only=active_only)
    campaign_ids = dedupe_campaign_ids(events)
    run_dir = default_watch_run_dir(watch_root)
    run_dir.mkdir(parents=True, exist_ok=True)
    previous_heartbeat = _load_latest_heartbeat(watch_root)
    last_success_at = str(previous_heartbeat.get("last_success_at") or "")
    now_at = now_local_text()

    if not campaign_ids:
        summary = {
            "status": "no_active_events",
            "generated_at": now_at,
            "run_dir": str(run_dir),
            "active_event_count": len(events),
            "campaign_ids": [],
            "last_success_at": last_success_at,
            "heartbeat_status": "green",
        }
        _write_json(run_dir / "summary.json", summary)
        _write_json(watch_root / "latest_heartbeat.json", summary)
        return summary

    if creds is None:
        raise ValueError("marketing credentials are required when active campaigns need capture")

    try:
        fetch_summary = fetch_runner(
            creds=creds,
            campaign_ids=campaign_ids,
            target_date=target_date,
            run_dir=run_dir / "capture",
            db_path=db_path,
            headless=headless,
        )
        last_success_at = now_at
        status = "success"
        error = ""
    except Exception as exc:
        fetch_summary = {}
        status = "capture_failed"
        error = str(exc)

    hb_status = "green" if status == "success" else heartbeat_status(last_success_at, now_at=now_at, stale_minutes=stale_minutes)
    summary = {
        "status": status,
        "generated_at": now_at,
        "run_dir": str(run_dir),
        "active_event_count": len(events),
        "campaign_ids": campaign_ids,
        "event_ids": [row.get("event_id") for row in events],
        "last_success_at": last_success_at,
        "heartbeat_status": hb_status,
        "stale_after_minutes": stale_minutes,
        "error": error,
        "fetch_summary": fetch_summary,
    }
    _write_json(run_dir / "summary.json", summary)
    _write_json(watch_root / "latest_heartbeat.json", summary)
    return summary


def close_experiment(
    *,
    event_id: str,
    db_path: Path = DEFAULT_MARKETING_DB,
    ledger_path: Path = DEFAULT_CHANGE_LOG,
    close_at: str | None = None,
    ab_root: Path = DEFAULT_AB_ROOT,
    run_dir: Path | None = None,
) -> dict[str, Any]:
    event = get_event(event_id, db_path=db_path, ledger_path=ledger_path)
    closed_at = normalize_local_timestamp(close_at or now_local_text())
    closed_event = {**event, "created_at": closed_at, "status": "closed"}
    log_summary = log_change_event(
        db_path=db_path,
        ledger_path=ledger_path,
        event=closed_event,
        run_root=DEFAULT_EXPERIMENT_ROOT,
        append_if_existing=True,
    )
    report_summary = build_experiment_report(
        event_id=event_id,
        marketing_db=db_path,
        ledger_path=ledger_path,
        ab_root=ab_root,
        run_dir=run_dir,
        cutoff_at=closed_at,
    )
    session_doc = Path("Docs/sessions") / f"{closed_at[:10]}_{event_id}_closeout.md"
    session_doc.parent.mkdir(parents=True, exist_ok=True)
    session_doc.write_text(
        "\n".join(
            [
                "# Kaspi Marketing Experiment Closeout",
                "",
                f"- Event ID: `{event_id}`",
                f"- Closed at: `{closed_at}`",
                f"- Report: `{report_summary.get('outputs', {}).get('experiment_report')}`",
                f"- Run dir: `{report_summary.get('run_dir')}`",
                f"- Status: `{report_summary.get('status')}`",
                "",
            ]
        ),
        encoding="utf-8",
    )
    return {
        "status": report_summary.get("status", "success"),
        "event_id": event_id,
        "closed_at": closed_at,
        "log": log_summary,
        "report": report_summary,
        "session_doc": str(session_doc),
    }
