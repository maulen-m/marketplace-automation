#!/usr/bin/env python3
"""Initialize the private Kaspi order-size system-of-record database."""

from __future__ import annotations

import argparse
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SCHEMA_PATH = REPO_ROOT / "config" / "schemas" / "kaspi_order_size_records.sql"
DEFAULT_DB_PATH = REPO_ROOT / "data" / "kaspi_order_size_records" / "kaspi_order_size_records.sqlite"

REQUIRED_TABLES = {
    "record_schema_meta",
    "order_size_records",
    "order_size_record_events",
    "order_size_record_runs",
}

REQUIRED_ORDER_COLUMNS = {
    "record_key",
    "store_code",
    "merchant_id",
    "order_id",
    "order_ref_hash",
    "order_placed_at",
    "order_placed_hour_local",
    "product_offer_name",
    "ordered_offer_size",
    "current_google_board_my_size",
    "google_board_size_recommendation",
    "actual_fit_size_by_table",
    "height_cm",
    "weight_kg",
    "fit_preference",
    "preference_weight_adjustment_kg",
    "effective_weight_kg",
    "manual_call_required",
    "delivery_address",
    "delivery_region",
    "marketing_region",
    "raw_customer_text_stored",
}

OPTIONAL_MIGRATION_COLUMNS = {
    "fit_preference": "TEXT",
    "preference_weight_adjustment_kg": "INTEGER NOT NULL DEFAULT 0",
    "effective_weight_kg": "INTEGER",
}


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def init_db(db_path: Path = DEFAULT_DB_PATH, schema_path: Path = DEFAULT_SCHEMA_PATH) -> dict[str, object]:
    db_path = Path(db_path)
    schema_path = Path(schema_path)
    if not schema_path.exists():
        raise FileNotFoundError(f"Schema not found: {schema_path}")

    db_path.parent.mkdir(parents=True, exist_ok=True)
    schema_sql = schema_path.read_text(encoding="utf-8")
    with sqlite3.connect(db_path) as conn:
        conn.executescript(schema_sql)
        existing_columns = {
            row[1]
            for row in conn.execute("PRAGMA table_info(order_size_records)").fetchall()
        }
        for column_name, column_sql in OPTIONAL_MIGRATION_COLUMNS.items():
            if column_name not in existing_columns:
                conn.execute(
                    f"ALTER TABLE order_size_records ADD COLUMN {column_name} {column_sql}"
                )
        now = utc_now()
        conn.execute(
            """
            INSERT INTO record_schema_meta (key, value, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
            """,
            ("schema_name", "kaspi_order_size_records_v1", now),
        )
        conn.execute(
            """
            INSERT INTO record_schema_meta (key, value, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
            """,
            ("schema_path", str(schema_path.relative_to(REPO_ROOT)), now),
        )
        conn.commit()

        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        order_columns = {
            row[1]
            for row in conn.execute("PRAGMA table_info(order_size_records)").fetchall()
        }

    missing_tables = sorted(REQUIRED_TABLES - tables)
    missing_order_columns = sorted(REQUIRED_ORDER_COLUMNS - order_columns)
    if missing_tables or missing_order_columns:
        raise RuntimeError(
            "Schema validation failed: "
            f"missing_tables={missing_tables}, "
            f"missing_order_columns={missing_order_columns}"
        )

    try:
        db_path.chmod(0o600)
    except OSError:
        pass

    return {
        "ok": True,
        "db_path": str(db_path),
        "schema_path": str(schema_path),
        "tables": sorted(REQUIRED_TABLES),
        "required_order_columns": sorted(REQUIRED_ORDER_COLUMNS),
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db-path", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--schema-path", type=Path, default=DEFAULT_SCHEMA_PATH)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    result = init_db(args.db_path, args.schema_path)
    print(f"ok={result['ok']}")
    print(f"db_path={result['db_path']}")
    print(f"schema_path={result['schema_path']}")
    print(f"tables={','.join(result['tables'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
