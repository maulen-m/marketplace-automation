from __future__ import annotations

import json
import os
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import yaml
from dotenv import load_dotenv
from playwright.sync_api import Error as PWError
from playwright.sync_api import TimeoutError as PWTimeout
from playwright.sync_api import sync_playwright

from .repricer_competitors import (
    _api_headers,
    _capture_artifact,
    _close_login_modal,
    _extract_row_id,
    _get_bot_token,
    _get_datatable_params,
    _get_records_total,
    _wait_table_ready,
)


@dataclass
class ExportAccount:
    name: str
    base_url: str
    token_env: str
    stores: list[int]


@dataclass
class ExportConfig:
    accounts: list[ExportAccount]
    store_name_map: dict[int, str]
    storage_state_path: str
    timeout_ms: int
    slowmo_ms: int


def _load_config(path: str | Path) -> ExportConfig:
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    accounts: list[ExportAccount] = []
    for item in raw.get("accounts", []):
        accounts.append(
            ExportAccount(
                name=item["name"],
                base_url=item["base_url"],
                token_env=item["token_env"],
                stores=[int(s) for s in item.get("stores", [])],
            )
        )
    store_name_map_raw = raw.get("store_name_map", {}) or {}
    store_name_map = {int(v): str(k) for k, v in store_name_map_raw.items()}
    run = raw.get("run", {}) or {}
    return ExportConfig(
        accounts=accounts,
        store_name_map=store_name_map,
        storage_state_path=str(run.get("storage_state_path", "data/storage_state.json")),
        timeout_ms=int(run.get("page_timeout_ms", 240000)),
        slowmo_ms=int(run.get("slowmo_ms", 150)),
    )


def _create_tables(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS repricer_items (
            fetched_at TEXT NOT NULL,
            store_id INTEGER NOT NULL,
            store_name TEXT,
            row_id INTEGER,
            merchant_sku TEXT,
            kaspi_sku TEXT,
            merchant_title TEXT,
            link TEXT,
            price INTEGER,
            min_price INTEGER,
            max_price INTEGER,
            strategy INTEGER,
            time_to_react INTEGER,
            step INTEGER,
            dumping INTEGER,
            active INTEGER,
            is_available INTEGER,
            preorder INTEGER,
            position INTEGER,
            brand TEXT,
            city_name TEXT,
            item_points INTEGER,
            sold_by_rating REAL,
            raw_json TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS repricer_item_links (
            fetched_at TEXT NOT NULL,
            store_id INTEGER NOT NULL,
            store_name TEXT,
            link TEXT,
            merchant_sku TEXT,
            kaspi_sku TEXT,
            merchant_title TEXT,
            price INTEGER,
            min_price INTEGER,
            max_price INTEGER,
            row_id INTEGER
        )
        """
    )


def _is_on_sale(row: dict[str, Any]) -> bool:
    return bool(row.get("is_available") or row.get("active"))


def _as_scalar(value: Any) -> Any:
    if isinstance(value, (list, dict)):
        return json.dumps(value, ensure_ascii=False)
    return value


def _open_store(page, *, base_url: str, token: str, store_id: int, timeout_ms: int) -> None:
    last_error: Exception | None = None
    for attempt in range(2):
        page.goto(f"{base_url}?token={token}", wait_until="domcontentloaded", timeout=timeout_ms)
        try:
            _close_login_modal(page)
            page.wait_for_selector("#mid_header", timeout=timeout_ms)
            page.locator(f"#mid_header input[id='{store_id}']").click(timeout=5000, force=True)
            try:
                page.evaluate(
                    """
                    (sid) => {
                      const el = document.getElementById(String(sid));
                      if (el && typeof window.get_mid_data === 'function') {
                        window.get_mid_data(el);
                      }
                    }
                    """,
                    store_id,
                )
            except PWError as exc:
                # The radio click can already start get_mid_data; a duplicate call can
                # collide with DataTables teardown. Proceed only if params settle.
                last_error = exc
                page.wait_for_timeout(1000)
            _close_login_modal(page)
            _wait_table_ready(page, timeout_ms)
            if not _get_datatable_params(page):
                raise RuntimeError(f"missing_datatables_params_store_{store_id}")
            return
        except (PWTimeout, PWError, RuntimeError) as exc:
            last_error = exc
            if attempt == 0:
                page.wait_for_timeout(1500)
                continue
            raise RuntimeError(f"Table load failed for store {store_id}") from None
    raise RuntimeError(f"Table load failed for store {store_id}") from None


def export_repricer_items_to_sqlite(
    *,
    config_path: str | Path,
    output_path: str | Path,
    headless: bool = True,
    include_all_rows: bool = False,
) -> dict[str, Any]:
    config = _load_config(config_path)
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    fetched_at = datetime.now(ZoneInfo("Asia/Almaty")).isoformat()

    conn = sqlite3.connect(output_path)
    conn.execute("PRAGMA journal_mode=WAL")
    _create_tables(conn)
    conn.execute("DELETE FROM repricer_items")
    conn.execute("DELETE FROM repricer_item_links")

    summary = {
        "fetched_at": fetched_at,
        "include_all_rows": include_all_rows,
        "stores": {},
        "total_rows_scanned": 0,
        "total_on_sale_rows": 0,
        "total_off_sale_rows": 0,
        "total_items": 0,
        "total_links": 0,
    }

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless, slow_mo=config.slowmo_ms)
        context = browser.new_context(storage_state=config.storage_state_path)
        page = context.new_page()

        for account in config.accounts:
            token = os.environ.get(account.token_env)
            if not token:
                raise RuntimeError(f"Missing env var: {account.token_env}")

            for store_id in account.stores:
                store_name = config.store_name_map.get(store_id)
                store_summary = {"rows": 0, "on_sale_rows": 0, "off_sale_rows": 0, "inserted": 0, "links": 0}
                summary["stores"][str(store_id)] = store_summary

                try:
                    _open_store(page, base_url=account.base_url, token=token, store_id=store_id, timeout_ms=config.timeout_ms)
                except RuntimeError as exc:
                    _capture_artifact(page, Path("runs/repricer_items_export"), f"store_{store_id}_load_failed")
                    raise RuntimeError(f"Table load failed for store {store_id}") from None

                params = _get_datatable_params(page)
                if not params:
                    raise RuntimeError(f"Missing DataTables params for store {store_id}")
                bot_token = _get_bot_token(page)
                if not bot_token:
                    raise RuntimeError(f"Missing bot_token for store {store_id}")

                records_total = _get_records_total(page) or 0
                length = int(params.get("length", 100))
                draw = int(params.get("draw", 1))
                start = 0
                page_index = 1

                while start < max(records_total, 1):
                    params["start"] = start
                    params["length"] = length
                    params["draw"] = draw
                    params["mid"] = store_id

                    resp = context.request.post(
                        "https://repricer.kz/price_strategy_data_mid/",
                        data=json.dumps(params),
                        headers=_api_headers(bot_token, page.url),
                    )
                    payload = resp.json()
                    if records_total == 0:
                        records_total = int(payload.get("recordsTotal") or 0)
                    rows = payload.get("data") or []
                    if not rows:
                        break

                    for row in rows:
                        store_summary["rows"] += 1
                        summary["total_rows_scanned"] += 1
                        on_sale = _is_on_sale(row)
                        if on_sale:
                            store_summary["on_sale_rows"] += 1
                            summary["total_on_sale_rows"] += 1
                        else:
                            store_summary["off_sale_rows"] += 1
                            summary["total_off_sale_rows"] += 1

                        if not include_all_rows and not on_sale:
                            continue

                        row_id = _extract_row_id(row.get("DT_RowId") or row.get("id") or row.get("row_id"))
                        price = row.get("price")
                        min_price = row.get("min_price")
                        max_price = row.get("max_price")

                        conn.execute(
                            """
                            INSERT INTO repricer_items (
                                fetched_at, store_id, store_name, row_id, merchant_sku, kaspi_sku,
                                merchant_title, link, price, min_price, max_price, strategy,
                                time_to_react, step, dumping, active, is_available, preorder,
                                position, brand, city_name, item_points, sold_by_rating, raw_json
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                            """,
                            (
                                fetched_at,
                                store_id,
                                store_name,
                                row_id,
                                row.get("merchant_sku"),
                                row.get("kaspi_sku"),
                                row.get("merchant_title"),
                                row.get("link"),
                                int(price) if price is not None else None,
                                int(min_price) if min_price is not None else None,
                                int(max_price) if max_price is not None else None,
                                row.get("strategy"),
                                row.get("time_to_react"),
                                row.get("step"),
                                int(bool(row.get("dumping"))) if row.get("dumping") is not None else None,
                                int(bool(row.get("active"))) if row.get("active") is not None else None,
                                int(bool(row.get("is_available"))) if row.get("is_available") is not None else None,
                                row.get("preorder"),
                                row.get("position"),
                                row.get("brand"),
                                row.get("city_name"),
                                _as_scalar(row.get("item_points")),
                                _as_scalar(row.get("sold_by_rating")),
                                json.dumps(row, ensure_ascii=False),
                            ),
                        )
                        store_summary["inserted"] += 1
                        summary["total_items"] += 1

                        if row.get("link"):
                            conn.execute(
                                """
                                INSERT INTO repricer_item_links (
                                    fetched_at, store_id, store_name, link, merchant_sku, kaspi_sku,
                                    merchant_title, price, min_price, max_price, row_id
                                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                                """,
                                (
                                    fetched_at,
                                    store_id,
                                    store_name,
                                    row.get("link"),
                                    row.get("merchant_sku"),
                                    row.get("kaspi_sku"),
                                    row.get("merchant_title"),
                                    int(price) if price is not None else None,
                                    int(min_price) if min_price is not None else None,
                                    int(max_price) if max_price is not None else None,
                                    row_id,
                                ),
                            )
                            store_summary["links"] += 1
                            summary["total_links"] += 1

                    start += length
                    draw += 1
                    page_index += 1
                    if config.slowmo_ms:
                        time.sleep(config.slowmo_ms / 1000.0)

        context.close()
        browser.close()

    conn.commit()
    conn.close()
    return summary


def main() -> None:
    load_dotenv(".env")
    config_path = "config/tasks/repricer_competitors.yaml"
    output_path = "data/repricer_items.sqlite"
    summary = export_repricer_items_to_sqlite(
        config_path=config_path,
        output_path=output_path,
        headless=True,
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
