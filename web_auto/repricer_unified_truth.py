from __future__ import annotations

import csv
import json
import re
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit
from zoneinfo import ZoneInfo

import yaml
from openpyxl import Workbook

from .repricer_items_export import export_repricer_items_to_sqlite


ALMATY_TZ = ZoneInfo("Asia/Almaty")
EXPECTED_STORE_CODES = {"UNIVERSAL", "ACMEWEAR", "MELVIS", "STOREB", "11KZ"}
DISPLAY_BY_CODE = {
    "UNIVERSAL": "UNIVERSAL",
    "ACMEWEAR": "ACMEWEAR",
    "MELVIS": "MELVIS",
    "STOREB": "STORE-B",
    "11KZ": "11KZ",
}


def _now_pair() -> tuple[str, str]:
    now = datetime.now(ALMATY_TZ)
    return now.isoformat(), now.astimezone(UTC).isoformat()


def _quote_ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _sanitize_table_name(stem: str) -> str:
    normalized = re.sub(r"[^0-9a-zA-Z_]+", "_", stem).strip("_")
    if not normalized:
        normalized = "table"
    return f"raw_external_{normalized.lower()}"


def _normalize_store_token(value: Any) -> str | None:
    if value is None:
        return None
    token = re.sub(r"[^0-9A-Z]+", "", str(value).upper())
    if not token:
        return None
    if token in {"STOREB", "MGROU"}:
        return "STOREB"
    if token == "MELVIS":
        return "MELVIS"
    if token == "ACMEWEAR":
        return "ACMEWEAR"
    if token == "UNIVERSAL":
        return "UNIVERSAL"
    if token == "11KZ":
        return "11KZ"
    return None


def _normalize_store_code(
    *,
    store_code: Any,
    store_name: Any,
    store_id: Any,
    store_id_to_code: dict[int, str],
) -> str | None:
    code = _normalize_store_token(store_code)
    if code:
        return code
    code = _normalize_store_token(store_name)
    if code:
        return code
    try:
        sid = int(store_id) if store_id is not None and str(store_id).strip() != "" else None
    except ValueError:
        sid = None
    if sid is None:
        return None
    return store_id_to_code.get(sid)


def _normalize_link(value: Any) -> str | None:
    if value is None:
        return None
    raw = str(value).strip()
    if not raw:
        return None
    try:
        parsed = urlsplit(raw)
    except Exception:
        return raw
    if not parsed.scheme or not parsed.netloc:
        return raw
    normalized_path = parsed.path.rstrip("/") or parsed.path
    normalized = urlunsplit(
        (
            parsed.scheme.lower(),
            parsed.netloc.lower(),
            normalized_path,
            "",
            "",
        )
    )
    return normalized


def _normalize_identity_token(value: Any) -> str | None:
    if value is None:
        return None
    token = str(value).strip()
    if not token:
        return None
    return token.upper()


def _as_float(value: Any) -> float | None:
    if value is None:
        return None
    raw = str(value).strip()
    if raw == "":
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def _as_int(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return int(value)
    raw = str(value).strip()
    if raw == "":
        return None
    try:
        return int(float(raw))
    except ValueError:
        return None


def _iso_pair_from_value(value: Any, fallback_almaty: str, fallback_utc: str) -> tuple[str, str]:
    if value is None:
        return fallback_almaty, fallback_utc
    raw = str(value).strip()
    if not raw:
        return fallback_almaty, fallback_utc
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        return raw, fallback_utc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=ALMATY_TZ)
    return dt.astimezone(ALMATY_TZ).isoformat(), dt.astimezone(UTC).isoformat()


def _compute_identity(link: Any, merchant_sku: Any, kaspi_sku: Any) -> tuple[str | None, str | None, str | None]:
    normalized_link = _normalize_link(link)
    if normalized_link:
        return "link", normalized_link, f"link:{normalized_link}"
    normalized_merchant = _normalize_identity_token(merchant_sku)
    if normalized_merchant:
        return "merchant_sku", normalized_merchant, f"merchant_sku:{normalized_merchant}"
    normalized_kaspi = _normalize_identity_token(kaspi_sku)
    if normalized_kaspi:
        return "kaspi_sku", normalized_kaspi, f"kaspi_sku:{normalized_kaspi}"
    return None, None, None


def _find_table_name(table_map: dict[str, str], prefix: str) -> str | None:
    matches: list[tuple[str, str]] = []
    for file_name, table_name in table_map.items():
        if file_name.startswith(prefix):
            matches.append((file_name, table_name))
    if not matches:
        return None
    matches.sort(key=lambda item: item[0])
    return matches[-1][1]


def _load_store_maps(
    *,
    kaspi_accounts_path: str | Path,
    repricer_config_path: str | Path,
) -> tuple[dict[int, str], dict[str, int], dict[str, str]]:
    store_id_to_code: dict[int, str] = {}
    store_code_to_id: dict[str, int] = {}
    store_code_to_name: dict[str, str] = dict(DISPLAY_BY_CODE)

    kaspi_raw = yaml.safe_load(Path(kaspi_accounts_path).read_text(encoding="utf-8")) or {}
    for account in (kaspi_raw.get("kaspi", {}) or {}).get("accounts", []) or []:
        account_id = account.get("account_id")
        name = account.get("name")
        code = _normalize_store_token(name)
        if account_id is None or code is None:
            continue
        sid = int(str(account_id))
        store_id_to_code[sid] = code
        store_code_to_id[code] = sid
        store_code_to_name[code] = DISPLAY_BY_CODE.get(code, str(name))

    repricer_raw = yaml.safe_load(Path(repricer_config_path).read_text(encoding="utf-8")) or {}
    store_name_map = repricer_raw.get("store_name_map", {}) or {}
    for raw_name, raw_id in store_name_map.items():
        code = _normalize_store_token(raw_name)
        if code is None:
            continue
        sid = int(raw_id)
        store_id_to_code[sid] = code
        store_code_to_id.setdefault(code, sid)
        store_code_to_name.setdefault(code, DISPLAY_BY_CODE.get(code, str(raw_name)))

    return store_id_to_code, store_code_to_id, store_code_to_name


def _create_unified_tables(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS raw_repricer_items_snapshot (
            source_snapshot TEXT NOT NULL,
            source_priority INTEGER NOT NULL,
            fetched_at TEXT,
            fetched_at_almaty TEXT,
            fetched_at_utc TEXT,
            store_id INTEGER,
            store_name TEXT,
            store_code TEXT,
            row_id INTEGER,
            merchant_sku TEXT,
            kaspi_sku TEXT,
            merchant_title TEXT,
            link TEXT,
            price REAL,
            min_price REAL,
            max_price REAL,
            strategy TEXT,
            time_to_react TEXT,
            step TEXT,
            dumping INTEGER,
            active INTEGER,
            is_available INTEGER,
            preorder TEXT,
            position TEXT,
            brand TEXT,
            city_name TEXT,
            is_on_sale_flag INTEGER,
            raw_json TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS item_store_truth_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_name TEXT NOT NULL,
            source_priority INTEGER NOT NULL,
            source_snapshot TEXT,
            source_updated_at TEXT,
            fetched_at_almaty TEXT,
            fetched_at_utc TEXT,
            store_code TEXT,
            store_id INTEGER,
            store_name TEXT,
            row_id INTEGER,
            merchant_sku TEXT,
            kaspi_sku TEXT,
            merchant_title TEXT,
            kaspi_offer_name TEXT,
            link TEXT,
            price REAL,
            min_price REAL,
            max_price REAL,
            sku_key TEXT,
            sku_id TEXT,
            my_size TEXT,
            active INTEGER,
            is_available INTEGER,
            dumping INTEGER,
            is_on_sale_flag INTEGER,
            identity_method TEXT,
            identity_value TEXT,
            canonical_item_key TEXT,
            raw_source TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS item_store_truth_latest (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            history_id INTEGER NOT NULL,
            source_name TEXT NOT NULL,
            source_priority INTEGER NOT NULL,
            source_snapshot TEXT,
            source_updated_at TEXT,
            fetched_at_almaty TEXT,
            fetched_at_utc TEXT,
            store_code TEXT,
            store_id INTEGER,
            store_name TEXT,
            row_id INTEGER,
            merchant_sku TEXT,
            kaspi_sku TEXT,
            merchant_title TEXT,
            kaspi_offer_name TEXT,
            link TEXT,
            price REAL,
            min_price REAL,
            max_price REAL,
            sku_key TEXT,
            sku_id TEXT,
            my_size TEXT,
            active INTEGER,
            is_available INTEGER,
            dumping INTEGER,
            is_on_sale_flag INTEGER,
            identity_method TEXT,
            identity_value TEXT,
            canonical_item_key TEXT,
            raw_source TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS item_identity_bridge (
            canonical_item_key TEXT PRIMARY KEY,
            identity_method TEXT,
            identity_value TEXT,
            first_seen_at TEXT,
            last_seen_at TEXT,
            rows_count INTEGER,
            stores_count INTEGER,
            best_source_priority INTEGER,
            freshness_rank_source TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS export_meta (
            key TEXT PRIMARY KEY,
            value TEXT
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_history_store ON item_store_truth_history(store_code, canonical_item_key)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_history_identity ON item_store_truth_history(identity_method, identity_value)"
    )


def _reset_unified_tables(conn: sqlite3.Connection) -> None:
    conn.execute("DELETE FROM raw_repricer_items_snapshot")
    conn.execute("DELETE FROM item_store_truth_history")
    conn.execute("DELETE FROM item_store_truth_latest")
    conn.execute("DELETE FROM item_identity_bridge")
    conn.execute("DELETE FROM export_meta")

    external_tables = conn.execute(
        """
        SELECT name
        FROM sqlite_master
        WHERE type = 'table'
          AND name LIKE 'raw_external_%'
        """
    ).fetchall()
    for (table_name,) in external_tables:
        conn.execute(f"DROP TABLE IF EXISTS {_quote_ident(table_name)}")


def _import_external_csvs(conn: sqlite3.Connection, external_truth_dir: str | Path) -> dict[str, dict[str, Any]]:
    base_dir = Path(external_truth_dir)
    if not base_dir.exists():
        raise RuntimeError(f"External truth directory not found: {base_dir}")

    imported: dict[str, dict[str, Any]] = {}
    for csv_path in sorted(base_dir.glob("*.csv")):
        table_name = _sanitize_table_name(csv_path.stem)
        with csv_path.open("r", encoding="utf-8", newline="") as fh:
            reader = csv.DictReader(fh)
            columns = reader.fieldnames or []
            if not columns:
                continue
            column_defs = ", ".join(f"{_quote_ident(col)} TEXT" for col in columns)
            conn.execute(f"CREATE TABLE {_quote_ident(table_name)} ({column_defs})")
            placeholders = ", ".join("?" for _ in columns)
            quoted_columns = ", ".join(_quote_ident(col) for col in columns)
            insert_sql = f"INSERT INTO {_quote_ident(table_name)} ({quoted_columns}) VALUES ({placeholders})"
            rows_inserted = 0
            batch: list[tuple[Any, ...]] = []
            for row in reader:
                batch.append(tuple(row.get(col) for col in columns))
                if len(batch) >= 1000:
                    conn.executemany(insert_sql, batch)
                    rows_inserted += len(batch)
                    batch.clear()
            if batch:
                conn.executemany(insert_sql, batch)
                rows_inserted += len(batch)
            imported[csv_path.name] = {"table_name": table_name, "rows": rows_inserted}
    return imported


def _ingest_repricer_snapshot(
    *,
    conn: sqlite3.Connection,
    snapshot_path: Path,
    source_snapshot: str,
    source_priority: int,
    store_id_to_code: dict[int, str],
    store_code_to_name: dict[str, str],
    fallback_almaty: str,
    fallback_utc: str,
) -> int:
    if not snapshot_path.exists():
        return 0
    source_conn = sqlite3.connect(snapshot_path)
    source_conn.row_factory = sqlite3.Row
    try:
        tables = {
            row[0]
            for row in source_conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='repricer_items'"
            ).fetchall()
        }
        if "repricer_items" not in tables:
            return 0
        rows_inserted = 0
        cursor = source_conn.execute("SELECT * FROM repricer_items")
        for row in cursor:
            store_id = _as_int(row["store_id"])
            store_code = _normalize_store_code(
                store_code=None,
                store_name=row["store_name"],
                store_id=store_id,
                store_id_to_code=store_id_to_code,
            )
            if store_code is None and store_id is not None:
                store_code = store_id_to_code.get(store_id)
            store_name = store_code_to_name.get(store_code, row["store_name"])
            fetched_at_almaty, fetched_at_utc = _iso_pair_from_value(
                row["fetched_at"], fallback_almaty, fallback_utc
            )
            active = _as_int(row["active"])
            is_available = _as_int(row["is_available"])
            is_on_sale_flag = int(bool((active or 0) or (is_available or 0)))
            conn.execute(
                """
                INSERT INTO raw_repricer_items_snapshot (
                    source_snapshot, source_priority, fetched_at, fetched_at_almaty, fetched_at_utc,
                    store_id, store_name, store_code, row_id, merchant_sku, kaspi_sku,
                    merchant_title, link, price, min_price, max_price, strategy,
                    time_to_react, step, dumping, active, is_available, preorder, position,
                    brand, city_name, is_on_sale_flag, raw_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    source_snapshot,
                    source_priority,
                    row["fetched_at"],
                    fetched_at_almaty,
                    fetched_at_utc,
                    store_id,
                    store_name,
                    store_code,
                    _as_int(row["row_id"]),
                    row["merchant_sku"],
                    row["kaspi_sku"],
                    row["merchant_title"],
                    row["link"],
                    _as_float(row["price"]),
                    _as_float(row["min_price"]),
                    _as_float(row["max_price"]),
                    row["strategy"],
                    row["time_to_react"],
                    row["step"],
                    _as_int(row["dumping"]),
                    active,
                    is_available,
                    row["preorder"],
                    row["position"],
                    row["brand"],
                    row["city_name"],
                    is_on_sale_flag,
                    row["raw_json"],
                ),
            )
            rows_inserted += 1
    finally:
        source_conn.close()
    return rows_inserted


def _build_article_lookup(
    *,
    conn: sqlite3.Connection,
    table_name: str | None,
) -> tuple[dict[tuple[str, str], str], dict[tuple[str, str], str]]:
    by_store_sku_id: dict[tuple[str, str], str] = {}
    by_store_sku_key: dict[tuple[str, str], str] = {}
    if not table_name:
        return by_store_sku_id, by_store_sku_key

    query = f"""
        SELECT store_code, sku_id, sku_key, kaspi_article
        FROM {_quote_ident(table_name)}
        WHERE COALESCE(kaspi_article, '') <> ''
    """
    for store_code, sku_id, sku_key, kaspi_article in conn.execute(query):
        canonical_store = _normalize_store_token(store_code)
        article = str(kaspi_article).strip()
        if not canonical_store or not article:
            continue
        if sku_id and str(sku_id).strip():
            key = (canonical_store, str(sku_id).strip())
            by_store_sku_id.setdefault(key, article)
        if sku_key and str(sku_key).strip():
            key = (canonical_store, str(sku_key).strip())
            by_store_sku_key.setdefault(key, article)
    return by_store_sku_id, by_store_sku_key


def _build_size_lookup(conn: sqlite3.Connection, table_name: str | None) -> dict[str, str]:
    mapping: dict[str, str] = {}
    if not table_name:
        return mapping
    query = f"""
        SELECT sku_id, my_size
        FROM {_quote_ident(table_name)}
        WHERE COALESCE(sku_id, '') <> ''
    """
    for sku_id, my_size in conn.execute(query):
        sku = str(sku_id).strip()
        size = str(my_size).strip() if my_size is not None else ""
        if sku and size:
            mapping.setdefault(sku, size)
    return mapping


def _insert_history_row(
    *,
    conn: sqlite3.Connection,
    record: dict[str, Any],
    store_id_to_code: dict[int, str],
    store_code_to_id: dict[str, int],
    store_code_to_name: dict[str, str],
    default_almaty: str,
    default_utc: str,
) -> None:
    store_id = _as_int(record.get("store_id"))
    store_code = _normalize_store_code(
        store_code=record.get("store_code"),
        store_name=record.get("store_name"),
        store_id=store_id,
        store_id_to_code=store_id_to_code,
    )
    if store_code is None and store_id is not None:
        store_code = store_id_to_code.get(store_id)
    if store_id is None and store_code is not None:
        store_id = store_code_to_id.get(store_code)

    store_name = record.get("store_name")
    if store_code and (store_name is None or str(store_name).strip() == ""):
        store_name = store_code_to_name.get(store_code, DISPLAY_BY_CODE.get(store_code, store_code))

    source_updated_at = record.get("source_updated_at")
    fetched_at_almaty, fetched_at_utc = _iso_pair_from_value(
        record.get("fetched_at_almaty") or source_updated_at,
        default_almaty,
        default_utc,
    )

    active = _as_int(record.get("active"))
    is_available = _as_int(record.get("is_available"))
    is_on_sale_flag = record.get("is_on_sale_flag")
    if is_on_sale_flag is None:
        if active is None and is_available is None:
            is_on_sale_flag = None
        else:
            is_on_sale_flag = int(bool((active or 0) or (is_available or 0)))
    else:
        is_on_sale_flag = _as_int(is_on_sale_flag)

    identity_method, identity_value, canonical_item_key = _compute_identity(
        record.get("link"),
        record.get("merchant_sku"),
        record.get("kaspi_sku"),
    )

    raw_source = record.get("raw_source")
    if raw_source is not None and not isinstance(raw_source, str):
        raw_source = json.dumps(raw_source, ensure_ascii=False)

    conn.execute(
        """
        INSERT INTO item_store_truth_history (
            source_name, source_priority, source_snapshot, source_updated_at,
            fetched_at_almaty, fetched_at_utc, store_code, store_id, store_name,
            row_id, merchant_sku, kaspi_sku, merchant_title, kaspi_offer_name, link,
            price, min_price, max_price, sku_key, sku_id, my_size, active, is_available,
            dumping, is_on_sale_flag, identity_method, identity_value, canonical_item_key, raw_source
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            record.get("source_name"),
            _as_int(record.get("source_priority")) or 99,
            record.get("source_snapshot"),
            source_updated_at,
            fetched_at_almaty,
            fetched_at_utc,
            store_code,
            store_id,
            store_name,
            _as_int(record.get("row_id")),
            record.get("merchant_sku"),
            record.get("kaspi_sku"),
            record.get("merchant_title"),
            record.get("kaspi_offer_name"),
            record.get("link"),
            _as_float(record.get("price")),
            _as_float(record.get("min_price")),
            _as_float(record.get("max_price")),
            record.get("sku_key"),
            record.get("sku_id"),
            record.get("my_size"),
            active,
            is_available,
            _as_int(record.get("dumping")),
            is_on_sale_flag,
            identity_method,
            identity_value,
            canonical_item_key,
            raw_source,
        ),
    )


def _build_history_and_latest(
    *,
    conn: sqlite3.Connection,
    external_tables: dict[str, dict[str, Any]],
    store_id_to_code: dict[int, str],
    store_code_to_id: dict[str, int],
    store_code_to_name: dict[str, str],
    default_almaty: str,
    default_utc: str,
) -> dict[str, int]:
    table_map = {name: data["table_name"] for name, data in external_tables.items()}
    article_table = _find_table_name(table_map, "dim_kaspi_article_map_5stores_active")
    sku_size_table = _find_table_name(table_map, "dim_sku_size_active")
    store_offer_table = _find_table_name(table_map, "store_offer_sku_identity_current")
    sales_7m_table = _find_table_name(table_map, "sales_last_7_months_db_")
    sales_fact_table = _find_table_name(table_map, "sales_fact_v2_5stores_key_fields")
    orders_table = _find_table_name(table_map, "fact_orders_kaspi_5stores_key_fields")

    article_by_sku_id, article_by_sku_key = _build_article_lookup(conn=conn, table_name=article_table)
    size_by_sku_id = _build_size_lookup(conn, sku_size_table)

    inserted = {
        "history_from_repricer": 0,
        "history_from_dim_article": 0,
        "history_from_store_offer_identity": 0,
        "history_from_sales_7m": 0,
        "history_from_sales_fact": 0,
        "history_from_orders": 0,
    }

    repricer_rows = conn.execute(
        "SELECT * FROM raw_repricer_items_snapshot ORDER BY source_priority ASC, fetched_at_almaty DESC"
    ).fetchall()
    for row in repricer_rows:
        _insert_history_row(
            conn=conn,
            record={
                "source_name": "repricer_snapshot",
                "source_priority": row["source_priority"],
                "source_snapshot": row["source_snapshot"],
                "source_updated_at": row["fetched_at_almaty"],
                "fetched_at_almaty": row["fetched_at_almaty"],
                "fetched_at_utc": row["fetched_at_utc"],
                "store_code": row["store_code"],
                "store_id": row["store_id"],
                "store_name": row["store_name"],
                "row_id": row["row_id"],
                "merchant_sku": row["merchant_sku"],
                "kaspi_sku": row["kaspi_sku"],
                "merchant_title": row["merchant_title"],
                "kaspi_offer_name": row["merchant_title"],
                "link": row["link"],
                "price": row["price"],
                "min_price": row["min_price"],
                "max_price": row["max_price"],
                "active": row["active"],
                "is_available": row["is_available"],
                "dumping": row["dumping"],
                "is_on_sale_flag": row["is_on_sale_flag"],
                "raw_source": {
                    "source_snapshot": row["source_snapshot"],
                    "source": "repricer_items",
                },
            },
            store_id_to_code=store_id_to_code,
            store_code_to_id=store_code_to_id,
            store_code_to_name=store_code_to_name,
            default_almaty=default_almaty,
            default_utc=default_utc,
        )
        inserted["history_from_repricer"] += 1

    if article_table:
        query = f"""
            SELECT store_code, kaspi_article, kaspi_offer_name, sku_key, sku_id, updated_at, id
            FROM {_quote_ident(article_table)}
        """
        for store_code, kaspi_article, kaspi_offer_name, sku_key, sku_id, updated_at, row_id in conn.execute(query):
            _insert_history_row(
                conn=conn,
                record={
                    "source_name": "external_dim_kaspi_article_map",
                    "source_priority": 4,
                    "source_snapshot": "external_db_truth",
                    "source_updated_at": updated_at,
                    "store_code": store_code,
                    "merchant_sku": kaspi_article,
                    "merchant_title": kaspi_offer_name,
                    "kaspi_offer_name": kaspi_offer_name,
                    "sku_key": sku_key,
                    "sku_id": sku_id,
                    "my_size": size_by_sku_id.get(str(sku_id).strip(), None) if sku_id else None,
                    "row_id": row_id,
                    "raw_source": {"table": article_table, "id": row_id},
                },
                store_id_to_code=store_id_to_code,
                store_code_to_id=store_code_to_id,
                store_code_to_name=store_code_to_name,
                default_almaty=default_almaty,
                default_utc=default_utc,
            )
            inserted["history_from_dim_article"] += 1

    if store_offer_table:
        query = f"""
            SELECT store_code, kaspi_offer_name, sku_key, sku_id, my_size, avg_sell_price_kzt, first_seen, last_seen
            FROM {_quote_ident(store_offer_table)}
        """
        for store_code, offer_name, sku_key, sku_id, my_size, avg_price, first_seen, last_seen in conn.execute(query):
            canonical_store = _normalize_store_token(store_code)
            sku_id_norm = str(sku_id).strip() if sku_id else None
            sku_key_norm = str(sku_key).strip() if sku_key else None
            merchant_sku = None
            if canonical_store and sku_id_norm:
                merchant_sku = article_by_sku_id.get((canonical_store, sku_id_norm))
            if merchant_sku is None and canonical_store and sku_key_norm:
                merchant_sku = article_by_sku_key.get((canonical_store, sku_key_norm))
            _insert_history_row(
                conn=conn,
                record={
                    "source_name": "external_store_offer_sku_identity_current",
                    "source_priority": 3,
                    "source_snapshot": "external_db_truth",
                    "source_updated_at": last_seen or first_seen,
                    "store_code": store_code,
                    "merchant_sku": merchant_sku,
                    "merchant_title": offer_name,
                    "kaspi_offer_name": offer_name,
                    "sku_key": sku_key,
                    "sku_id": sku_id,
                    "my_size": my_size or (size_by_sku_id.get(sku_id_norm) if sku_id_norm else None),
                    "price": avg_price,
                    "raw_source": {"table": store_offer_table},
                },
                store_id_to_code=store_id_to_code,
                store_code_to_id=store_code_to_id,
                store_code_to_name=store_code_to_name,
                default_almaty=default_almaty,
                default_utc=default_utc,
            )
            inserted["history_from_store_offer_identity"] += 1

    if sales_7m_table:
        query = f"""
            SELECT store_code, sku_key, sku_id, my_size, units, net_rev_kzt, sale_date, order_id
            FROM {_quote_ident(sales_7m_table)}
        """
        for store_code, sku_key, sku_id, my_size, units, net_rev_kzt, sale_date, order_id in conn.execute(query):
            canonical_store = _normalize_store_token(store_code)
            sku_id_norm = str(sku_id).strip() if sku_id else None
            sku_key_norm = str(sku_key).strip() if sku_key else None
            merchant_sku = None
            if canonical_store and sku_id_norm:
                merchant_sku = article_by_sku_id.get((canonical_store, sku_id_norm))
            if merchant_sku is None and canonical_store and sku_key_norm:
                merchant_sku = article_by_sku_key.get((canonical_store, sku_key_norm))
            units_val = _as_float(units)
            net_rev_val = _as_float(net_rev_kzt)
            price = None
            if units_val and units_val > 0 and net_rev_val is not None:
                price = net_rev_val / units_val
            _insert_history_row(
                conn=conn,
                record={
                    "source_name": "external_sales_last_7_months",
                    "source_priority": 2,
                    "source_snapshot": "external_db_truth",
                    "source_updated_at": sale_date,
                    "store_code": store_code,
                    "merchant_sku": merchant_sku,
                    "sku_key": sku_key,
                    "sku_id": sku_id,
                    "my_size": my_size or (size_by_sku_id.get(sku_id_norm) if sku_id_norm else None),
                    "price": price,
                    "raw_source": {"table": sales_7m_table, "order_id": order_id},
                },
                store_id_to_code=store_id_to_code,
                store_code_to_id=store_code_to_id,
                store_code_to_name=store_code_to_name,
                default_almaty=default_almaty,
                default_utc=default_utc,
            )
            inserted["history_from_sales_7m"] += 1

    if sales_fact_table:
        query = f"""
            SELECT store_code, kaspi_offer_name, sku_key, sku_id, my_size, sell_price_kzt, order_date, order_id
            FROM {_quote_ident(sales_fact_table)}
        """
        for store_code, offer_name, sku_key, sku_id, my_size, sell_price, order_date, order_id in conn.execute(query):
            canonical_store = _normalize_store_token(store_code)
            sku_id_norm = str(sku_id).strip() if sku_id else None
            sku_key_norm = str(sku_key).strip() if sku_key else None
            merchant_sku = None
            if canonical_store and sku_id_norm:
                merchant_sku = article_by_sku_id.get((canonical_store, sku_id_norm))
            if merchant_sku is None and canonical_store and sku_key_norm:
                merchant_sku = article_by_sku_key.get((canonical_store, sku_key_norm))
            _insert_history_row(
                conn=conn,
                record={
                    "source_name": "external_sales_fact_v2",
                    "source_priority": 5,
                    "source_snapshot": "external_db_truth",
                    "source_updated_at": order_date,
                    "store_code": store_code,
                    "merchant_sku": merchant_sku,
                    "merchant_title": offer_name,
                    "kaspi_offer_name": offer_name,
                    "sku_key": sku_key,
                    "sku_id": sku_id,
                    "my_size": my_size or (size_by_sku_id.get(sku_id_norm) if sku_id_norm else None),
                    "price": sell_price,
                    "raw_source": {"table": sales_fact_table, "order_id": order_id},
                },
                store_id_to_code=store_id_to_code,
                store_code_to_id=store_code_to_id,
                store_code_to_name=store_code_to_name,
                default_almaty=default_almaty,
                default_utc=default_utc,
            )
            inserted["history_from_sales_fact"] += 1

    if orders_table:
        query = f"""
            SELECT store_code, kaspi_offer_name, sku_key, sku_id, my_size, assigned_size, unit_price_kzt,
                   created_at, updated_at, order_id
            FROM {_quote_ident(orders_table)}
        """
        for (
            store_code,
            offer_name,
            sku_key,
            sku_id,
            my_size,
            assigned_size,
            unit_price,
            created_at,
            updated_at,
            order_id,
        ) in conn.execute(query):
            canonical_store = _normalize_store_token(store_code)
            sku_id_norm = str(sku_id).strip() if sku_id else None
            sku_key_norm = str(sku_key).strip() if sku_key else None
            merchant_sku = None
            if canonical_store and sku_id_norm:
                merchant_sku = article_by_sku_id.get((canonical_store, sku_id_norm))
            if merchant_sku is None and canonical_store and sku_key_norm:
                merchant_sku = article_by_sku_key.get((canonical_store, sku_key_norm))
            effective_size = assigned_size or my_size or (size_by_sku_id.get(sku_id_norm) if sku_id_norm else None)
            _insert_history_row(
                conn=conn,
                record={
                    "source_name": "external_fact_orders_kaspi",
                    "source_priority": 6,
                    "source_snapshot": "external_db_truth",
                    "source_updated_at": created_at or updated_at,
                    "store_code": store_code,
                    "merchant_sku": merchant_sku,
                    "merchant_title": offer_name,
                    "kaspi_offer_name": offer_name,
                    "sku_key": sku_key,
                    "sku_id": sku_id,
                    "my_size": effective_size,
                    "price": unit_price,
                    "raw_source": {"table": orders_table, "order_id": order_id},
                },
                store_id_to_code=store_id_to_code,
                store_code_to_id=store_code_to_id,
                store_code_to_name=store_code_to_name,
                default_almaty=default_almaty,
                default_utc=default_utc,
            )
            inserted["history_from_orders"] += 1

    conn.execute("DELETE FROM item_store_truth_latest")
    conn.execute(
        """
        INSERT INTO item_store_truth_latest (
            history_id, source_name, source_priority, source_snapshot, source_updated_at,
            fetched_at_almaty, fetched_at_utc, store_code, store_id, store_name, row_id,
            merchant_sku, kaspi_sku, merchant_title, kaspi_offer_name, link, price,
            min_price, max_price, sku_key, sku_id, my_size, active, is_available,
            dumping, is_on_sale_flag, identity_method, identity_value, canonical_item_key, raw_source
        )
        WITH ranked AS (
            SELECT
                h.*,
                ROW_NUMBER() OVER (
                    PARTITION BY COALESCE(h.canonical_item_key, '__row__' || h.id), COALESCE(h.store_code, '')
                    ORDER BY
                        h.source_priority ASC,
                        COALESCE(h.source_updated_at, '') DESC,
                        COALESCE(h.fetched_at_almaty, '') DESC,
                        h.id DESC
                ) AS rn
            FROM item_store_truth_history h
        )
        SELECT
            id, source_name, source_priority, source_snapshot, source_updated_at,
            fetched_at_almaty, fetched_at_utc, store_code, store_id, store_name, row_id,
            merchant_sku, kaspi_sku, merchant_title, kaspi_offer_name, link, price,
            min_price, max_price, sku_key, sku_id, my_size, active, is_available,
            dumping, is_on_sale_flag, identity_method, identity_value, canonical_item_key, raw_source
        FROM ranked
        WHERE rn = 1
        """
    )

    conn.execute("DELETE FROM item_identity_bridge")
    conn.execute(
        """
        INSERT INTO item_identity_bridge (
            canonical_item_key, identity_method, identity_value, first_seen_at, last_seen_at,
            rows_count, stores_count, best_source_priority, freshness_rank_source
        )
        SELECT
            h.canonical_item_key,
            MAX(h.identity_method),
            MAX(h.identity_value),
            MIN(COALESCE(h.source_updated_at, h.fetched_at_almaty)),
            MAX(COALESCE(h.source_updated_at, h.fetched_at_almaty)),
            COUNT(*) AS rows_count,
            COUNT(DISTINCT h.store_code) AS stores_count,
            MIN(h.source_priority) AS best_source_priority,
            (
                SELECT h2.source_name
                FROM item_store_truth_history h2
                WHERE h2.canonical_item_key = h.canonical_item_key
                ORDER BY h2.source_priority ASC, COALESCE(h2.source_updated_at, '') DESC, h2.id DESC
                LIMIT 1
            ) AS freshness_rank_source
        FROM item_store_truth_history h
        WHERE h.canonical_item_key IS NOT NULL
        GROUP BY h.canonical_item_key
        """
    )

    return inserted


def _table_to_markdown(headers: list[str], rows: list[tuple[Any, ...]]) -> str:
    if not rows:
        rows = []
    header_line = "| " + " | ".join(headers) + " |"
    divider_line = "| " + " | ".join("---" for _ in headers) + " |"
    body_lines: list[str] = []
    for row in rows:
        body_lines.append("| " + " | ".join("" if v is None else str(v) for v in row) + " |")
    return "\n".join([header_line, divider_line, *body_lines])


def _write_rows_sheet(
    *,
    workbook: Workbook,
    sheet_name: str,
    headers: list[str],
    rows: list[tuple[Any, ...]],
) -> None:
    ws = workbook.create_sheet(title=sheet_name)
    ws.append(headers)
    for row in rows:
        ws.append(list(row))


def _collect_unified_report(conn: sqlite3.Connection, db_path: Path) -> dict[str, Any]:
    history_rows = conn.execute("SELECT COUNT(*) AS c FROM item_store_truth_history").fetchone()["c"]
    latest_rows = conn.execute("SELECT COUNT(*) AS c FROM item_store_truth_latest").fetchone()["c"]
    bridge_rows = conn.execute("SELECT COUNT(*) AS c FROM item_identity_bridge").fetchone()["c"]
    unresolved_latest_rows = conn.execute(
        "SELECT COUNT(*) AS c FROM item_store_truth_latest WHERE canonical_item_key IS NULL"
    ).fetchone()["c"]

    store_coverage_rows = conn.execute(
        """
        SELECT
            COALESCE(store_code, 'UNKNOWN') AS store_code,
            MIN(store_id) AS store_id,
            COUNT(*) AS latest_rows,
            SUM(CASE WHEN COALESCE(is_on_sale_flag, 0) = 1 THEN 1 ELSE 0 END) AS on_sale_rows,
            SUM(CASE WHEN COALESCE(is_on_sale_flag, 0) = 0 THEN 1 ELSE 0 END) AS off_sale_rows,
            SUM(CASE WHEN canonical_item_key IS NULL THEN 1 ELSE 0 END) AS unresolved_identity_rows
        FROM item_store_truth_latest
        GROUP BY COALESCE(store_code, 'UNKNOWN')
        ORDER BY COALESCE(store_code, 'UNKNOWN')
        """
    ).fetchall()

    identity_method_rows = conn.execute(
        """
        SELECT
            COALESCE(identity_method, 'UNRESOLVED') AS identity_method,
            COUNT(*) AS rows_count
        FROM item_store_truth_latest
        GROUP BY COALESCE(identity_method, 'UNRESOLVED')
        ORDER BY rows_count DESC
        """
    ).fetchall()

    source_rows = conn.execute(
        """
        SELECT source_name, COUNT(*) AS rows_count
        FROM item_store_truth_latest
        GROUP BY source_name
        ORDER BY rows_count DESC
        """
    ).fetchall()

    return {
        "db_path": str(db_path),
        "history_rows": history_rows,
        "latest_rows": latest_rows,
        "bridge_rows": bridge_rows,
        "unresolved_latest_rows": unresolved_latest_rows,
        "store_coverage": [dict(row) for row in store_coverage_rows],
        "identity_methods": [dict(row) for row in identity_method_rows],
        "latest_source_usage": [dict(row) for row in source_rows],
    }


def export_repricer_unified_report(*, db_path: str | Path) -> dict[str, Any]:
    path = Path(db_path)
    if not path.exists():
        raise RuntimeError(f"Unified truth DB not found: {path}")
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        return _collect_unified_report(conn, path)
    finally:
        conn.close()


def export_repricer_unified_truth(
    *,
    config_path: str | Path,
    db_out_path: str | Path,
    xlsx_out_path: str | Path,
    markdown_out_path: str | Path,
    external_truth_dir: str | Path,
    kaspi_accounts_path: str | Path,
    legacy_snapshot_path: str | Path,
    refresh_repricer: bool,
    headless: bool,
) -> dict[str, Any]:
    db_path = Path(db_out_path)
    xlsx_path = Path(xlsx_out_path)
    markdown_path = Path(markdown_out_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    xlsx_path.parent.mkdir(parents=True, exist_ok=True)
    markdown_path.parent.mkdir(parents=True, exist_ok=True)

    run_almaty, run_utc = _now_pair()
    store_id_to_code, store_code_to_id, store_code_to_name = _load_store_maps(
        kaspi_accounts_path=kaspi_accounts_path,
        repricer_config_path=config_path,
    )

    refresh_snapshot_path = db_path.parent / "repricer_items_refresh.sqlite"
    refresh_summary: dict[str, Any] | None = None
    if refresh_repricer:
        refresh_summary = export_repricer_items_to_sqlite(
            config_path=config_path,
            output_path=refresh_snapshot_path,
            headless=headless,
            include_all_rows=True,
        )

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        _create_unified_tables(conn)
        _reset_unified_tables(conn)

        external_tables = _import_external_csvs(conn, external_truth_dir)

        legacy_rows = _ingest_repricer_snapshot(
            conn=conn,
            snapshot_path=Path(legacy_snapshot_path),
            source_snapshot="legacy_repricer_snapshot",
            source_priority=2,
            store_id_to_code=store_id_to_code,
            store_code_to_name=store_code_to_name,
            fallback_almaty=run_almaty,
            fallback_utc=run_utc,
        )
        refresh_rows = 0
        if refresh_repricer:
            refresh_rows = _ingest_repricer_snapshot(
                conn=conn,
                snapshot_path=refresh_snapshot_path,
                source_snapshot="refresh_repricer_snapshot",
                source_priority=1,
                store_id_to_code=store_id_to_code,
                store_code_to_name=store_code_to_name,
                fallback_almaty=run_almaty,
                fallback_utc=run_utc,
            )

        history_counters = _build_history_and_latest(
            conn=conn,
            external_tables=external_tables,
            store_id_to_code=store_id_to_code,
            store_code_to_id=store_code_to_id,
            store_code_to_name=store_code_to_name,
            default_almaty=run_almaty,
            default_utc=run_utc,
        )

        report = _collect_unified_report(conn, db_path)

        latest_headers = [
            "fetched_at",
            "store_id",
            "store_name",
            "row_id",
            "merchant_sku",
            "kaspi_sku",
            "merchant_title",
            "link",
            "price",
            "min_price",
            "max_price",
            "link_count_store-c",
            "link_count_acmewear",
            "store_code",
            "sku_key",
            "sku_id",
            "my_size",
            "kaspi_offer_name",
            "is_on_sale_flag",
            "active",
            "is_available",
            "dumping",
            "identity_method",
            "canonical_item_key",
            "source_name",
            "source_priority",
            "source_updated_at",
        ]
        latest_rows = conn.execute(
            """
            WITH link_counts AS (
                SELECT
                    link,
                    SUM(CASE WHEN store_code = 'MELVIS' THEN 1 ELSE 0 END) AS link_count_store-c,
                    SUM(CASE WHEN store_code = 'ACMEWEAR' THEN 1 ELSE 0 END) AS link_count_acmewear
                FROM item_store_truth_latest
                WHERE COALESCE(link, '') <> ''
                GROUP BY link
            )
            SELECT
                l.fetched_at_almaty AS fetched_at,
                l.store_id,
                l.store_name,
                l.row_id,
                l.merchant_sku,
                l.kaspi_sku,
                l.merchant_title,
                l.link,
                l.price,
                l.min_price,
                l.max_price,
                COALESCE(c.link_count_store-c, 0) AS link_count_store-c,
                COALESCE(c.link_count_acmewear, 0) AS link_count_acmewear,
                l.store_code,
                l.sku_key,
                l.sku_id,
                l.my_size,
                l.kaspi_offer_name,
                l.is_on_sale_flag,
                l.active,
                l.is_available,
                l.dumping,
                l.identity_method,
                l.canonical_item_key,
                l.source_name,
                l.source_priority,
                l.source_updated_at
            FROM item_store_truth_latest l
            LEFT JOIN link_counts c ON c.link = l.link
            ORDER BY COALESCE(l.store_code, ''), COALESCE(l.canonical_item_key, ''), l.id
            """
        ).fetchall()

        history_headers = [
            "id",
            "source_name",
            "source_priority",
            "source_snapshot",
            "source_updated_at",
            "fetched_at_almaty",
            "store_code",
            "store_id",
            "store_name",
            "merchant_sku",
            "kaspi_sku",
            "merchant_title",
            "kaspi_offer_name",
            "link",
            "price",
            "min_price",
            "max_price",
            "sku_key",
            "sku_id",
            "my_size",
            "identity_method",
            "canonical_item_key",
        ]
        history_rows = conn.execute(
            """
            SELECT
                id,
                source_name,
                source_priority,
                source_snapshot,
                source_updated_at,
                fetched_at_almaty,
                store_code,
                store_id,
                store_name,
                merchant_sku,
                kaspi_sku,
                merchant_title,
                kaspi_offer_name,
                link,
                price,
                min_price,
                max_price,
                sku_key,
                sku_id,
                my_size,
                identity_method,
                canonical_item_key
            FROM item_store_truth_history
            ORDER BY id
            """
        ).fetchall()

        bridge_headers = [
            "canonical_item_key",
            "identity_method",
            "identity_value",
            "first_seen_at",
            "last_seen_at",
            "rows_count",
            "stores_count",
            "best_source_priority",
            "freshness_rank_source",
        ]
        bridge_rows = conn.execute(
            """
            SELECT
                canonical_item_key,
                identity_method,
                identity_value,
                first_seen_at,
                last_seen_at,
                rows_count,
                stores_count,
                best_source_priority,
                freshness_rank_source
            FROM item_identity_bridge
            ORDER BY canonical_item_key
            """
        ).fetchall()

        coverage_headers = [
            "store_code",
            "store_id",
            "latest_rows",
            "on_sale_rows",
            "off_sale_rows",
            "unresolved_identity_rows",
        ]
        coverage_rows = conn.execute(
            """
            SELECT
                COALESCE(store_code, 'UNKNOWN') AS store_code,
                MIN(store_id) AS store_id,
                COUNT(*) AS latest_rows,
                SUM(CASE WHEN COALESCE(is_on_sale_flag, 0) = 1 THEN 1 ELSE 0 END) AS on_sale_rows,
                SUM(CASE WHEN COALESCE(is_on_sale_flag, 0) = 0 THEN 1 ELSE 0 END) AS off_sale_rows,
                SUM(CASE WHEN canonical_item_key IS NULL THEN 1 ELSE 0 END) AS unresolved_identity_rows
            FROM item_store_truth_latest
            GROUP BY COALESCE(store_code, 'UNKNOWN')
            ORDER BY COALESCE(store_code, 'UNKNOWN')
            """
        ).fetchall()

        identity_headers = ["identity_method", "rows_count"]
        identity_rows = conn.execute(
            """
            SELECT
                COALESCE(identity_method, 'UNRESOLVED') AS identity_method,
                COUNT(*) AS rows_count
            FROM item_store_truth_latest
            GROUP BY COALESCE(identity_method, 'UNRESOLVED')
            ORDER BY rows_count DESC
            """
        ).fetchall()

        source_usage_headers = ["source_name", "rows_count"]
        source_usage_rows = conn.execute(
            """
            SELECT source_name, COUNT(*) AS rows_count
            FROM item_store_truth_latest
            GROUP BY source_name
            ORDER BY rows_count DESC
            """
        ).fetchall()

        covered_store_codes = {str(row["store_code"]) for row in coverage_rows}
        quality_rows = [
            (
                "history_rows_nonzero",
                "PASS" if report["history_rows"] > 0 else "FAIL",
                f"history_rows={report['history_rows']}",
            ),
            (
                "latest_rows_nonzero",
                "PASS" if report["latest_rows"] > 0 else "FAIL",
                f"latest_rows={report['latest_rows']}",
            ),
            (
                "bridge_rows_nonzero",
                "PASS" if report["bridge_rows"] > 0 else "FAIL",
                f"bridge_rows={report['bridge_rows']}",
            ),
            (
                "expected_store_coverage",
                "PASS" if EXPECTED_STORE_CODES.issubset(covered_store_codes) else "WARN",
                f"covered={','.join(sorted(covered_store_codes))}",
            ),
            (
                "identity_chain_present",
                "PASS"
                if {"link", "merchant_sku", "kaspi_sku"} & {str(r['identity_method']) for r in identity_rows}
                else "WARN",
                "methods=" + ",".join(str(r["identity_method"]) for r in identity_rows),
            ),
            (
                "unresolved_rows_visible",
                "PASS",
                f"unresolved_latest_rows={report['unresolved_latest_rows']}",
            ),
        ]

        workbook = Workbook()
        workbook.remove(workbook.active)
        _write_rows_sheet(
            workbook=workbook,
            sheet_name="items_latest_all_stores",
            headers=latest_headers,
            rows=[tuple(r) for r in latest_rows],
        )
        _write_rows_sheet(
            workbook=workbook,
            sheet_name="items_history",
            headers=history_headers,
            rows=[tuple(r) for r in history_rows],
        )
        _write_rows_sheet(
            workbook=workbook,
            sheet_name="identity_bridge",
            headers=bridge_headers,
            rows=[tuple(r) for r in bridge_rows],
        )
        _write_rows_sheet(
            workbook=workbook,
            sheet_name="store_coverage_summary",
            headers=coverage_headers,
            rows=[tuple(r) for r in coverage_rows],
        )
        _write_rows_sheet(
            workbook=workbook,
            sheet_name="quality_checks",
            headers=["check_name", "status", "details"],
            rows=quality_rows,
        )
        workbook.save(xlsx_path)

        markdown_parts = [
            "# Repricer Unified Truth Export",
            "",
            f"- Generated at (Asia/Almaty): `{run_almaty}`",
            f"- Generated at (UTC): `{run_utc}`",
            f"- DB: `{db_path}`",
            f"- Workbook: `{xlsx_path}`",
            "",
            "## Store Coverage",
            _table_to_markdown(coverage_headers, [tuple(r) for r in coverage_rows]),
            "",
            "## Identity Method Coverage",
            _table_to_markdown(identity_headers, [tuple(r) for r in identity_rows]),
            "",
            "## Latest Source Usage",
            _table_to_markdown(source_usage_headers, [tuple(r) for r in source_usage_rows]),
            "",
            "## Quality Checks",
            _table_to_markdown(["check_name", "status", "details"], quality_rows),
        ]
        markdown_path.write_text("\n".join(markdown_parts) + "\n", encoding="utf-8")

        conn.execute(
            "INSERT OR REPLACE INTO export_meta(key, value) VALUES(?, ?)",
            ("generated_at_almaty", run_almaty),
        )
        conn.execute(
            "INSERT OR REPLACE INTO export_meta(key, value) VALUES(?, ?)",
            ("generated_at_utc", run_utc),
        )
        conn.execute(
            "INSERT OR REPLACE INTO export_meta(key, value) VALUES(?, ?)",
            ("external_truth_dir", str(Path(external_truth_dir))),
        )
        conn.commit()

        return {
            **report,
            **history_counters,
            "db_path": str(db_path),
            "xlsx_path": str(xlsx_path),
            "markdown_path": str(markdown_path),
            "generated_at_almaty": run_almaty,
            "generated_at_utc": run_utc,
            "imported_external_tables": external_tables,
            "legacy_snapshot_rows": legacy_rows,
            "refresh_snapshot_rows": refresh_rows,
            "refresh_summary": refresh_summary,
        }
    finally:
        conn.close()
