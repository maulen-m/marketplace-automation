from __future__ import annotations

import json
import logging
import time
import traceback
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from playwright.sync_api import TimeoutError as PWTimeout
from playwright.sync_api import sync_playwright

from .checkpoint import Progress, load_checkpoint, progress_key, save_checkpoint
from .config import DumpingConfig
from .repricer_competitors import (
    _api_headers,
    _capture_artifact,
    _close_login_modal,
    _extract_row_id,
    _form_headers,
    _get_bot_token,
    _get_datatable_params,
    _get_records_total,
    _wait_table_ready,
)
from .utils import ensure_env


def run_repricer_dumping_enable_api(
    *,
    config: DumpingConfig,
    dry_run: bool,
    confirm: bool,
    resume: bool,
    account_name: str | None,
    store_ids: list[int] | None,
    checkpoint_path: str | None,
    artifacts_dir: str | None,
    slowmo_ms: int | None,
    timeout_ms: int | None,
    headless: bool,
    headed: bool,
    profile_dir: str | None,
    storage_state: str | None,
    verify: bool,
) -> dict[str, Any]:
    run_cfg = config.run

    effective_dry_run = dry_run or run_cfg.dry_run
    if not effective_dry_run and run_cfg.require_confirm and not confirm:
        raise RuntimeError("Non-dry run requires --confirm")

    effective_checkpoint = checkpoint_path or run_cfg.checkpoint_path
    effective_artifacts = Path(artifacts_dir or run_cfg.artifacts_dir)
    effective_artifacts.mkdir(parents=True, exist_ok=True)
    run_id = datetime.now().strftime("%Y%m%d_%H%M%S")
    run_dir = effective_artifacts / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    effective_timeout_ms = int(timeout_ms or run_cfg.page_timeout_ms)
    effective_slowmo_ms = int(slowmo_ms or run_cfg.slowmo_ms)

    effective_headless = run_cfg.headless
    if headless:
        effective_headless = True
    if headed:
        effective_headless = False

    effective_profile_dir = profile_dir or run_cfg.browser_profile_dir
    effective_storage_state = storage_state or run_cfg.storage_state_path

    checkpoint = load_checkpoint(effective_checkpoint, f"{config.task_id}_api")

    summary: dict[str, Any] = {
        "task_id": config.task_id,
        "run_id": run_id,
        "dry_run": effective_dry_run,
        "api_mode": True,
        "products_visited": 0,
        "dumping_enable_needed": 0,
        "dumping_enabled": 0,
        "api_requests": 0,
        "api_sets_attempted": 0,
        "api_sets_succeeded": 0,
        "errors": 0,
        "per_store": {},
        "remaining_off_after_verify": {},
        "artifacts_dir": str(run_dir),
        "checkpoint_path": effective_checkpoint,
    }

    def log_error(label: str, exc: Exception | None = None, context: dict[str, Any] | None = None) -> None:
        payload: dict[str, Any] = {
            "timestamp": datetime.utcnow().isoformat() + "Z",
            "label": label,
        }
        if context:
            payload["context"] = context
        if exc:
            payload["exception_type"] = type(exc).__name__
            payload["exception"] = str(exc)
            payload["traceback"] = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
        error_log = run_dir / "errors.jsonl"
        with error_log.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(payload, ensure_ascii=False) + "\n")

    def record_error(label: str, exc: Exception | None = None, context: dict[str, Any] | None = None) -> None:
        summary["errors"] += 1
        logging.error("Error: %s", label)
        if exc:
            logging.debug("%s", exc)
        log_error(label, exc, context)

    with sync_playwright() as p:
        for account in config.accounts:
            if account_name and account.name != account_name:
                continue

            token = ensure_env(account.token_env)
            base_url = account.base_url

            if effective_profile_dir:
                context = p.chromium.launch_persistent_context(
                    user_data_dir=effective_profile_dir,
                    channel="chrome",
                    headless=effective_headless,
                    slow_mo=effective_slowmo_ms,
                )
                close_context = True
            else:
                browser = p.chromium.launch(headless=effective_headless, slow_mo=effective_slowmo_ms)
                context = browser.new_context(storage_state=effective_storage_state)
                close_context = True

            page = context.new_page()

            for store_id in account.stores:
                if store_ids and store_id not in store_ids:
                    continue

                store_key = progress_key(f"{account.name}_dumping", store_id)
                if not resume:
                    checkpoint.progress.pop(store_key, None)

                store_summary = summary["per_store"].setdefault(
                    str(store_id),
                    {
                        "products_visited": 0,
                        "dumping_enable_needed": 0,
                        "dumping_enabled": 0,
                        "api_requests": 0,
                        "api_sets_attempted": 0,
                        "api_sets_succeeded": 0,
                        "errors": 0,
                    },
                )

                try:
                    page.goto(f"{base_url}?token={token}", wait_until="domcontentloaded", timeout=effective_timeout_ms)
                    referer = page.url
                    _close_login_modal(page)
                    page.wait_for_selector("#mid_header", timeout=effective_timeout_ms)
                    page.locator(f"#mid_header input[id='{store_id}']").click(timeout=5000, force=True)
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
                    _close_login_modal(page)

                    try:
                        _wait_table_ready(page, effective_timeout_ms)
                    except PWTimeout as exc:
                        record_error(
                            f"Table load timeout for store {store_id}",
                            exc,
                            {"store_id": store_id},
                        )
                        _capture_artifact(page, run_dir, f"store_{store_id}_table_timeout")
                        continue

                    params = _get_datatable_params(page)
                    if not params:
                        record_error(
                            f"Missing DataTables params store {store_id}",
                            None,
                            {"store_id": store_id},
                        )
                        _capture_artifact(page, run_dir, f"store_{store_id}_missing_params")
                        continue

                    bot_token = _get_bot_token(page)
                    if not bot_token:
                        record_error(
                            f"Missing bot_token store {store_id}",
                            None,
                            {"store_id": store_id},
                        )
                        _capture_artifact(page, run_dir, f"store_{store_id}_missing_bot_token")
                        continue

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
                            headers=_api_headers(bot_token, referer),
                        )
                        summary["api_requests"] += 1
                        store_summary["api_requests"] += 1

                        if resp.status != 200:
                            record_error(
                                f"API fetch failed store {store_id} page {page_index}",
                                None,
                                {"store_id": store_id, "page_index": page_index, "status": resp.status},
                            )
                            break

                        payload = resp.json()
                        if records_total == 0:
                            records_total = int(payload.get("recordsTotal") or 0)
                        rows = payload.get("data") or []
                        if not rows:
                            break

                        for row_idx, row in enumerate(rows):
                            if resume and store_key in checkpoint.progress:
                                resume_point = checkpoint.progress[store_key]
                                if page_index < resume_point.page_index:
                                    continue
                                if page_index == resume_point.page_index and row_idx <= resume_point.row_index:
                                    continue

                            row_id = _extract_row_id(row.get("DT_RowId") or row.get("id") or row.get("row_id"))
                            if row_id is None:
                                record_error(
                                    f"Missing row id store {store_id} page {page_index} row {row_idx}",
                                    None,
                                    {"store_id": store_id, "page_index": page_index, "row_index": row_idx},
                                )
                                checkpoint.progress[store_key] = Progress(page_index=page_index, row_index=row_idx)
                                save_checkpoint(effective_checkpoint, checkpoint)
                                continue

                            summary["products_visited"] += 1
                            store_summary["products_visited"] += 1

                            if row.get("dumping"):
                                checkpoint.progress[store_key] = Progress(page_index=page_index, row_index=row_idx)
                                save_checkpoint(effective_checkpoint, checkpoint)
                                continue

                            summary["dumping_enable_needed"] += 1
                            store_summary["dumping_enable_needed"] += 1

                            if effective_dry_run:
                                summary["dumping_enabled"] += 1
                                store_summary["dumping_enabled"] += 1
                                checkpoint.progress[store_key] = Progress(page_index=page_index, row_index=row_idx)
                                save_checkpoint(effective_checkpoint, checkpoint)
                                continue

                            min_price = row.get("min_price")
                            if min_price is None:
                                min_price = row.get("price")
                            if min_price is None:
                                min_price = ""

                            summary["api_sets_attempted"] += 1
                            store_summary["api_sets_attempted"] += 1
                            form_data = urlencode({"id": str(row_id), "min_price": str(min_price)})
                            resp_set = context.request.post(
                                "https://repricer.kz/follow_item/",
                                data=form_data,
                                headers=_form_headers(bot_token, referer),
                            )
                            summary["api_requests"] += 1
                            store_summary["api_requests"] += 1
                            if resp_set.status == 200:
                                summary["api_sets_succeeded"] += 1
                                store_summary["api_sets_succeeded"] += 1
                                summary["dumping_enabled"] += 1
                                store_summary["dumping_enabled"] += 1
                            else:
                                record_error(
                                    f"API set failed store {store_id} row {row_id}",
                                    None,
                                    {"store_id": store_id, "row_id": row_id, "status": resp_set.status},
                                )

                            checkpoint.progress[store_key] = Progress(page_index=page_index, row_index=row_idx)
                            save_checkpoint(effective_checkpoint, checkpoint)

                        start += length
                        draw += 1
                        page_index += 1
                        if effective_slowmo_ms:
                            time.sleep(effective_slowmo_ms / 1000.0)

                    if verify and not effective_dry_run:
                        remaining_off = 0
                        start = 0
                        page_index = 1
                        verify_draw = 1
                        while start < max(records_total, 1):
                            params["start"] = start
                            params["length"] = length
                            params["draw"] = verify_draw
                            params["mid"] = store_id

                            resp = context.request.post(
                                "https://repricer.kz/price_strategy_data_mid/",
                                data=json.dumps(params),
                                headers=_api_headers(bot_token, referer),
                            )
                            summary["api_requests"] += 1
                            store_summary["api_requests"] += 1
                            if resp.status != 200:
                                record_error(
                                    f"API verify fetch failed store {store_id} page {page_index}",
                                    None,
                                    {"store_id": store_id, "page_index": page_index, "status": resp.status},
                                )
                                break

                            payload = resp.json()
                            rows = payload.get("data") or []
                            if not rows:
                                break

                            for row in rows:
                                if not row.get("dumping"):
                                    remaining_off += 1

                            start += length
                            page_index += 1
                            verify_draw += 1
                            if effective_slowmo_ms:
                                time.sleep(effective_slowmo_ms / 1000.0)

                        summary["remaining_off_after_verify"][str(store_id)] = remaining_off

                except Exception as exc:
                    record_error(f"Store processing failed store {store_id}", exc, {"store_id": store_id})
                    _capture_artifact(page, run_dir, f"store_{store_id}_fatal")
                    store_summary["errors"] += 1

            if close_context:
                context.close()

    summary_path = run_dir / "summary.json"
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    return summary
