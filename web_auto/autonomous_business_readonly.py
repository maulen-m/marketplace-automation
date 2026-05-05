from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path
from typing import Any, Iterable, Sequence

DEFAULT_AB_SOURCE_ROOT = Path("~/Docs/Autonomous_business")
SEND_RELATIVE_ROOT = Path("excel_ui/Kaspi_orders/Today/MERGED/SEND")
APP_DB_RELATIVE_PATH = Path("db/app.db")
REALIZED_ORDER_STATUSES = ("SHIPPED", "COMPLETED")


def resolve_ab_source_root(explicit_root: str | Path | None = None) -> Path:
    if explicit_root is not None:
        return Path(explicit_root).expanduser().resolve()
    env_value = os.environ.get("AB_SOURCE_ROOT", "").strip()
    if env_value:
        return Path(env_value).expanduser().resolve()
    return DEFAULT_AB_SOURCE_ROOT.resolve()


def build_app_db_uri(explicit_root: str | Path | None = None) -> str:
    root = resolve_ab_source_root(explicit_root)
    db_path = (root / APP_DB_RELATIVE_PATH).resolve()
    return f"file:{db_path}?mode=ro"


def connect_app_db_ro(explicit_root: str | Path | None = None) -> sqlite3.Connection:
    conn = sqlite3.connect(build_app_db_uri(explicit_root), uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _json_load(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def iter_send_batches(explicit_root: str | Path | None = None) -> list[Path]:
    root = resolve_ab_source_root(explicit_root)
    send_root = root / SEND_RELATIVE_ROOT
    if not send_root.exists():
        return []
    return sorted(
        [
            p
            for p in send_root.iterdir()
            if p.is_dir()
            and (p / "send_batch_manifest.json").exists()
            and (p / "send_ledger.json").exists()
        ],
        key=lambda p: (p / "send_batch_manifest.json").stat().st_mtime,
    )


def select_latest_send_batch(
    explicit_root: str | Path | None = None,
    *,
    target_date: str | None = None,
) -> Path | None:
    batches = iter_send_batches(explicit_root)
    if not batches:
        return None
    if not target_date:
        return batches[-1]

    matching: list[Path] = []
    for batch in batches:
        manifest = _json_load(batch / "send_batch_manifest.json")
        if str(manifest.get("target_date") or "").strip() == str(target_date).strip():
            matching.append(batch)
    return matching[-1] if matching else batches[-1]


def load_send_batch(batch_dir: str | Path) -> tuple[dict[str, Any], dict[str, Any]]:
    batch_path = Path(batch_dir)
    manifest = _json_load(batch_path / "send_batch_manifest.json")
    ledger = _json_load(batch_path / "send_ledger.json")
    return manifest, ledger


def confirmed_send_order_rows(manifest: dict[str, Any], ledger: dict[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    ledger_entries = ledger.get("entries") or {}
    if not isinstance(ledger_entries, dict):
        ledger_entries = {}
    for entry in manifest.get("entries") or []:
        if not isinstance(entry, dict):
            continue
        pdf_key = str(entry.get("pdf_key") or "").strip()
        if not pdf_key:
            continue
        ledger_entry = ledger_entries.get(pdf_key) or {}
        if str(ledger_entry.get("state") or "").strip() != "confirmed":
            continue
        for order_id in entry.get("order_ids") or []:
            order_id_text = str(order_id or "").strip()
            if not order_id_text:
                continue
            out.append(
                {
                    "order_id": order_id_text,
                    "pdf_key": pdf_key,
                    "filename": str(entry.get("filename") or ledger_entry.get("filename") or "").strip(),
                    "bundle_relative_output_path": str(
                        entry.get("relative_output_path") or ledger_entry.get("relative_output_path") or ""
                    ).strip(),
                    "whatsapp_ledger_state": "confirmed",
                    "batch_label": str(manifest.get("batch_label") or ledger.get("batch_label") or "").strip(),
                    "target_date": str(manifest.get("target_date") or "").strip(),
                }
            )
    return out


def _store_code_clause(store_codes: Sequence[str] | None) -> tuple[str, list[str]]:
    if not store_codes:
        return "", []
    normalized = [str(code).strip() for code in store_codes if str(code).strip()]
    if not normalized:
        return "", []
    placeholders = ",".join("?" for _ in normalized)
    return f" AND store_code IN ({placeholders})", normalized


def fetch_daily_created_metrics(
    conn: sqlite3.Connection,
    *,
    sku_key: str,
    start_date: str,
    end_date: str,
    store_codes: Sequence[str] | None = None,
) -> list[dict[str, Any]]:
    store_clause, store_params = _store_code_clause(store_codes)
    sql = f"""
        SELECT
            date(created_at) AS metric_date,
            store_code,
            SUM(COALESCE(quantity, 1)) AS qty,
            ROUND(
                SUM(COALESCE(unit_price_kzt, 0) * COALESCE(quantity, 1)) * 1.0
                / NULLIF(SUM(COALESCE(quantity, 1)), 0),
                2
            ) AS weighted_avg_price
        FROM fact_orders_kaspi
        WHERE sku_key = ?
          AND date(created_at) BETWEEN date(?) AND date(?)
          {store_clause}
        GROUP BY 1, 2
        ORDER BY 1, 2
    """
    params: list[Any] = [sku_key, start_date, end_date, *store_params]
    rows = conn.execute(sql, params).fetchall()
    return [dict(row) for row in rows]


def fetch_daily_shipped_metrics(
    conn: sqlite3.Connection,
    *,
    sku_key: str,
    start_date: str,
    end_date: str,
    store_codes: Sequence[str] | None = None,
) -> list[dict[str, Any]]:
    store_clause, store_params = _store_code_clause(store_codes)
    sql = f"""
        SELECT
            date(COALESCE(NULLIF(actual_shipment_date, ''), NULLIF(courier_transmission_date, ''))) AS metric_date,
            store_code,
            SUM(COALESCE(quantity, 1)) AS qty,
            ROUND(
                SUM(COALESCE(unit_price_kzt, 0) * COALESCE(quantity, 1)) * 1.0
                / NULLIF(SUM(COALESCE(quantity, 1)), 0),
                2
            ) AS weighted_avg_price
        FROM fact_orders_kaspi
        WHERE sku_key = ?
          AND internal_status IN ('SHIPPED', 'COMPLETED')
          AND COALESCE(NULLIF(actual_shipment_date, ''), NULLIF(courier_transmission_date, ''), '') <> ''
          AND date(COALESCE(NULLIF(actual_shipment_date, ''), NULLIF(courier_transmission_date, ''))) BETWEEN date(?) AND date(?)
          {store_clause}
        GROUP BY 1, 2
        ORDER BY 1, 2
    """
    params: list[Any] = [sku_key, start_date, end_date, *store_params]
    rows = conn.execute(sql, params).fetchall()
    return [dict(row) for row in rows]


def summarize_weighted_average(rows: Iterable[dict[str, Any]]) -> dict[str, Any]:
    total_qty = 0
    total_value = 0.0
    for row in rows:
        try:
            qty = int(row.get("qty") or 0)
        except Exception:
            qty = 0
        try:
            avg_price = float(row.get("weighted_avg_price") or 0)
        except Exception:
            avg_price = 0.0
        if qty <= 0:
            continue
        total_qty += qty
        total_value += avg_price * qty
    weighted_avg = round(total_value / total_qty, 2) if total_qty > 0 else None
    return {
        "qty": total_qty,
        "weighted_avg_price": weighted_avg,
    }
