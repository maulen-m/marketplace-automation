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
from .config import MinPriceSyncConfig
from .repricer_competitors import (
    _api_headers,
    _capture_artifact,
    _close_login_modal,
    _extract_row_id,
    _form_headers,
    _get_bot_token,
    _get_datatable_params,
    _get_records_total,
    _resolve_delivery_days_by_mid_for_offer,
    _wait_table_ready,
)
from .delivery_days_cache import (
    DEFAULT_DELIVERY_DAYS_CACHE_PATH,
    load_delivery_days_cache,
    save_delivery_days_cache,
)
from .repricer_min_price_logic import (
    compute_external_anchor_target,
    extract_external_competitor_floor,
    needs_external_live_price_fix,
    needs_external_max_fix,
    parse_price_int,
    should_skip_line52_locked_9990,
    should_apply_external_anchor,
)
from .repricer_protection import is_protected_merchant_sku
from .price_write_vintage import (
    PRICE_WRITE_VINTAGE_VERSION,
    build_price_write_vintage_record,
    ensure_price_write_vintage_ok,
    live_source_times,
    write_price_write_vintage_log,
)
from .utils import ensure_env


def _find_account(accounts, name: str):
    for account in accounts:
        if account.name == name:
            return account
    raise RuntimeError(f"Account not found: {name}")


def _normalize_link(value: Any) -> str | None:
    if not value:
        return None
    link = str(value).strip()
    return link or None


def _normalize_sku(value: Any) -> str | None:
    if not value:
        return None
    sku = str(value).strip()
    return sku or None


def run_repricer_min_price_sync_api(
    *,
    config: MinPriceSyncConfig,
    dry_run: bool,
    confirm: bool,
    resume: bool,
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
    run_checked_at = datetime.now().astimezone()
    run_dir = effective_artifacts / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    price_write_source_times = live_source_times(run_checked_at)
    price_write_source_basis = "live_repricer_reference"

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
    delivery_days_cache_payload = load_delivery_days_cache(DEFAULT_DELIVERY_DAYS_CACHE_PATH)
    delivery_days_cache_dirty = False
    delivery_days_cache_by_link: dict[str, dict[str, int]] = {}

    summary: dict[str, Any] = {
        "task_id": config.task_id,
        "run_id": run_id,
        "dry_run": effective_dry_run,
        "api_mode": True,
        "pricing_mode": config.pricing_mode,
        "source_store_id": config.source_store_id,
        "fallback_source_store_id": config.fallback_source_store_id,
        "target_store_ids": config.target_store_ids,
        "source_rows": 0,
        "source_link_keys": 0,
        "source_sku_keys": 0,
        "source_kaspi_keys": 0,
        "source_link_duplicates": 0,
        "source_sku_duplicates": 0,
        "source_kaspi_duplicates": 0,
        "fallback_link_added": 0,
        "fallback_sku_added": 0,
        "fallback_kaspi_added": 0,
        "products_visited": 0,
        "min_price_updates": 0,
        "max_price_updates": 0,
        "current_price_updates": 0,
        "api_requests": 0,
        "api_sets_attempted": 0,
        "api_sets_succeeded": 0,
        "missing_matches": 0,
        "external_floor_found": 0,
        "external_long_delivery_ignored_rows": 0,
        "kaspi_delivery_requests": 0,
        "external_line52_locked_skipped": 0,
        "protected_rows_skipped": 0,
        "errors": 0,
        "per_store": {},
        "remaining_mismatches": {},
        "price_write_vintage_version": PRICE_WRITE_VINTAGE_VERSION,
        "price_write_vintage_source_basis": price_write_source_basis,
        "price_write_vintage_logged_rows": 0,
        "price_write_vintage_log_csv": "",
        "artifacts_dir": str(run_dir),
        "checkpoint_path": effective_checkpoint,
    }
    price_write_vintage_rows: list[dict[str, Any]] = []

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

    def build_vintage_record(
        *,
        store_id: int,
        row_id: Any,
        merchant_sku: Any,
        operation: str,
        target_field: str,
        target_value: int,
    ) -> dict[str, Any]:
        return build_price_write_vintage_record(
            writer_id="web_auto.repricer_min_price_sync",
            run_id=run_id,
            store_id=store_id,
            store_name=str(store_id),
            row_id=row_id,
            merchant_sku=str(merchant_sku or ""),
            operation=operation,
            target_field=target_field,
            target_value=target_value,
            source_times=price_write_source_times,
            source_basis=price_write_source_basis,
            as_of=run_checked_at,
        )

    def open_context(playwright):
        if effective_profile_dir:
            context = playwright.chromium.launch_persistent_context(
                user_data_dir=effective_profile_dir,
                channel="chrome",
                headless=effective_headless,
                slow_mo=effective_slowmo_ms,
            )
            return context, True
        browser = playwright.chromium.launch(headless=effective_headless, slow_mo=effective_slowmo_ms)
        context = browser.new_context(storage_state=effective_storage_state)
        return context, True

    def load_store(page, base_url: str, store_id: int, token: str) -> None:
        page.goto(f"{base_url}?token={token}", wait_until="domcontentloaded", timeout=effective_timeout_ms)
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
        _wait_table_ready(page, effective_timeout_ms)

    with sync_playwright() as p:
        source_account = _find_account(config.accounts, config.source_account)
        target_account = _find_account(config.accounts, config.target_account)
        line52_skip_store_ids = set(config.external_skip_line52_locked_9990_store_ids)

        # Build reference map from source store.
        source_token = ensure_env(source_account.token_env)
        source_context, close_context = open_context(p)
        source_page = source_context.new_page()

        source_by_link: dict[str, int] = {}
        source_by_sku: dict[str, int] = {}
        source_by_kaspi: dict[str, int] = {}

        try:
            try:
                load_store(source_page, source_account.base_url, config.source_store_id, source_token)
            except PWTimeout as exc:
                record_error(
                    f"Table load timeout for source store {config.source_store_id}",
                    exc,
                    {"store_id": config.source_store_id},
                )
                _capture_artifact(source_page, run_dir, f"source_{config.source_store_id}_table_timeout")
                return summary

            params = _get_datatable_params(source_page)
            if not params:
                record_error(
                    f"Missing DataTables params source store {config.source_store_id}",
                    None,
                    {"store_id": config.source_store_id},
                )
                _capture_artifact(source_page, run_dir, f"source_{config.source_store_id}_missing_params")
                return summary

            bot_token = _get_bot_token(source_page)
            if not bot_token:
                record_error(
                    f"Missing bot_token source store {config.source_store_id}",
                    None,
                    {"store_id": config.source_store_id},
                )
                _capture_artifact(source_page, run_dir, f"source_{config.source_store_id}_missing_bot_token")
                return summary

            records_total = _get_records_total(source_page) or 0
            length = int(params.get("length", 100))
            draw = int(params.get("draw", 1))
            start = 0
            page_index = 1

            while start < max(records_total, 1):
                params["start"] = start
                params["length"] = length
                params["draw"] = draw
                params["mid"] = config.source_store_id

                resp = source_context.request.post(
                    "https://repricer.kz/price_strategy_data_mid/",
                    data=json.dumps(params),
                    headers=_api_headers(bot_token, source_page.url),
                )
                summary["api_requests"] += 1

                if resp.status != 200:
                    record_error(
                        f"API fetch failed source store {config.source_store_id} page {page_index}",
                        None,
                        {"store_id": config.source_store_id, "page_index": page_index, "status": resp.status},
                    )
                    break

                payload = resp.json()
                if records_total == 0:
                    records_total = int(payload.get("recordsTotal") or 0)
                rows = payload.get("data") or []
                if not rows:
                    break

                for row in rows:
                    summary["source_rows"] += 1
                    min_price = parse_price_int(row.get("min_price"))
                    if min_price is None:
                        continue

                    link = _normalize_link(row.get("link"))
                    sku = _normalize_sku(row.get("merchant_sku"))
                    kaspi_sku = _normalize_sku(row.get("kaspi_sku"))

                    if link:
                        if link in source_by_link:
                            summary["source_link_duplicates"] += 1
                        else:
                            source_by_link[link] = min_price
                    if sku:
                        if sku in source_by_sku:
                            summary["source_sku_duplicates"] += 1
                        else:
                            source_by_sku[sku] = min_price
                    if kaspi_sku:
                        if kaspi_sku in source_by_kaspi:
                            summary["source_kaspi_duplicates"] += 1
                        else:
                            source_by_kaspi[kaspi_sku] = min_price

                start += length
                draw += 1
                page_index += 1
                if effective_slowmo_ms:
                    time.sleep(effective_slowmo_ms / 1000.0)

            summary["source_link_keys"] = len(source_by_link)
            summary["source_sku_keys"] = len(source_by_sku)
            summary["source_kaspi_keys"] = len(source_by_kaspi)

        finally:
            if close_context:
                source_context.close()

        # Optional fallback reference from another store (e.g., STORE-B).
        if config.fallback_source_account and config.fallback_source_store_id:
            fallback_account = _find_account(config.accounts, config.fallback_source_account)
            fallback_token = ensure_env(fallback_account.token_env)
            fallback_context, close_context = open_context(p)
            fallback_page = fallback_context.new_page()
            try:
                try:
                    load_store(fallback_page, fallback_account.base_url, config.fallback_source_store_id, fallback_token)
                except PWTimeout as exc:
                    record_error(
                        f"Table load timeout for fallback store {config.fallback_source_store_id}",
                        exc,
                        {"store_id": config.fallback_source_store_id},
                    )
                    _capture_artifact(fallback_page, run_dir, f"fallback_{config.fallback_source_store_id}_table_timeout")
                else:
                    params = _get_datatable_params(fallback_page)
                    if not params:
                        record_error(
                            f"Missing DataTables params fallback store {config.fallback_source_store_id}",
                            None,
                            {"store_id": config.fallback_source_store_id},
                        )
                        _capture_artifact(fallback_page, run_dir, f"fallback_{config.fallback_source_store_id}_missing_params")
                    else:
                        bot_token = _get_bot_token(fallback_page)
                        if not bot_token:
                            record_error(
                                f"Missing bot_token fallback store {config.fallback_source_store_id}",
                                None,
                                {"store_id": config.fallback_source_store_id},
                            )
                            _capture_artifact(
                                fallback_page,
                                run_dir,
                                f"fallback_{config.fallback_source_store_id}_missing_bot_token",
                            )
                        else:
                            records_total = _get_records_total(fallback_page) or 0
                            length = int(params.get("length", 100))
                            draw = int(params.get("draw", 1))
                            start = 0
                            page_index = 1
                            while start < max(records_total, 1):
                                params["start"] = start
                                params["length"] = length
                                params["draw"] = draw
                                params["mid"] = config.fallback_source_store_id
                                resp = fallback_context.request.post(
                                    "https://repricer.kz/price_strategy_data_mid/",
                                    data=json.dumps(params),
                                    headers=_api_headers(bot_token, fallback_page.url),
                                )
                                summary["api_requests"] += 1
                                if resp.status != 200:
                                    record_error(
                                        f"API fetch failed fallback store {config.fallback_source_store_id} page {page_index}",
                                        None,
                                        {
                                            "store_id": config.fallback_source_store_id,
                                            "page_index": page_index,
                                            "status": resp.status,
                                        },
                                    )
                                    break
                                payload = resp.json()
                                if records_total == 0:
                                    records_total = int(payload.get("recordsTotal") or 0)
                                rows = payload.get("data") or []
                                if not rows:
                                    break
                                for row in rows:
                                    min_price = parse_price_int(row.get("min_price"))
                                    if min_price is None:
                                        continue
                                    link = _normalize_link(row.get("link"))
                                    sku = _normalize_sku(row.get("merchant_sku"))
                                    kaspi_sku = _normalize_sku(row.get("kaspi_sku"))
                                    if link and link not in source_by_link:
                                        source_by_link[link] = min_price
                                        summary["fallback_link_added"] += 1
                                    if sku and sku not in source_by_sku:
                                        source_by_sku[sku] = min_price
                                        summary["fallback_sku_added"] += 1
                                    if kaspi_sku and kaspi_sku not in source_by_kaspi:
                                        source_by_kaspi[kaspi_sku] = min_price
                                        summary["fallback_kaspi_added"] += 1
                                start += length
                                draw += 1
                                page_index += 1
                                if effective_slowmo_ms:
                                    time.sleep(effective_slowmo_ms / 1000.0)
            finally:
                if close_context:
                    fallback_context.close()

        # Apply to target stores.
        target_token = ensure_env(target_account.token_env)
        target_context, close_context = open_context(p)
        target_page = target_context.new_page()

        try:
            for store_id in config.target_store_ids:
                store_key = progress_key(f"{target_account.name}_min_price", store_id)
                if not resume:
                    checkpoint.progress.pop(store_key, None)

                store_summary = summary["per_store"].setdefault(
                    str(store_id),
                    {
                        "products_visited": 0,
                        "min_price_updates": 0,
                        "max_price_updates": 0,
                        "current_price_updates": 0,
                        "api_requests": 0,
                        "api_sets_attempted": 0,
                        "api_sets_succeeded": 0,
                        "missing_matches": 0,
                        "external_floor_found": 0,
                        "external_long_delivery_ignored_rows": 0,
                        "kaspi_delivery_requests": 0,
                        "external_line52_locked_skipped": 0,
                        "protected_rows_skipped": 0,
                        "errors": 0,
                    },
                )

                try:
                    try:
                        load_store(target_page, target_account.base_url, store_id, target_token)
                    except PWTimeout as exc:
                        record_error(
                            f"Table load timeout for target store {store_id}",
                            exc,
                            {"store_id": store_id},
                        )
                        _capture_artifact(target_page, run_dir, f"target_{store_id}_table_timeout")
                        continue

                    params = _get_datatable_params(target_page)
                    if not params:
                        record_error(
                            f"Missing DataTables params target store {store_id}",
                            None,
                            {"store_id": store_id},
                        )
                        _capture_artifact(target_page, run_dir, f"target_{store_id}_missing_params")
                        continue

                    bot_token = _get_bot_token(target_page)
                    if not bot_token:
                        record_error(
                            f"Missing bot_token target store {store_id}",
                            None,
                            {"store_id": store_id},
                        )
                        _capture_artifact(target_page, run_dir, f"target_{store_id}_missing_bot_token")
                        continue

                    records_total = _get_records_total(target_page) or 0
                    length = int(params.get("length", 100))
                    draw = int(params.get("draw", 1))
                    start = 0
                    page_index = 1

                    while start < max(records_total, 1):
                        params["start"] = start
                        params["length"] = length
                        params["draw"] = draw
                        params["mid"] = store_id

                        resp = target_context.request.post(
                            "https://repricer.kz/price_strategy_data_mid/",
                            data=json.dumps(params),
                            headers=_api_headers(bot_token, target_page.url),
                        )
                        summary["api_requests"] += 1
                        store_summary["api_requests"] += 1

                        if resp.status != 200:
                            record_error(
                                f"API fetch failed target store {store_id} page {page_index}",
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
                                    f"Missing row id target store {store_id} page {page_index} row {row_idx}",
                                    None,
                                    {"store_id": store_id, "page_index": page_index, "row_index": row_idx},
                                )
                                checkpoint.progress[store_key] = Progress(page_index=page_index, row_index=row_idx)
                                save_checkpoint(effective_checkpoint, checkpoint)
                                continue

                            summary["products_visited"] += 1
                            store_summary["products_visited"] += 1

                            link = _normalize_link(row.get("link"))
                            sku = _normalize_sku(row.get("merchant_sku"))
                            kaspi_sku = _normalize_sku(row.get("kaspi_sku"))

                            if is_protected_merchant_sku(sku, config.protected_merchant_sku_prefixes) or (
                                kaspi_sku and is_protected_merchant_sku(kaspi_sku, config.protected_merchant_sku_prefixes)
                            ):
                                summary["protected_rows_skipped"] += 1
                                store_summary["protected_rows_skipped"] += 1
                                checkpoint.progress[store_key] = Progress(page_index=page_index, row_index=row_idx)
                                save_checkpoint(effective_checkpoint, checkpoint)
                                continue

                            current_min = parse_price_int(row.get("min_price"))
                            if current_min is None:
                                current_min = parse_price_int(row.get("price"))
                            if current_min is None:
                                checkpoint.progress[store_key] = Progress(page_index=page_index, row_index=row_idx)
                                save_checkpoint(effective_checkpoint, checkpoint)
                                continue
                            current_price = parse_price_int(row.get("price"))
                            current_max = parse_price_int(row.get("max_price"))

                            source_min: int | None = None
                            need_min_update = False
                            need_max_update = False
                            need_live_price_update = False
                            if config.pricing_mode == "external_competitor_anchor":
                                delivery_days_by_mid: dict[str, int] = {}
                                if link and row.get("competitors"):
                                    delivery_days_by_mid, req_count = _resolve_delivery_days_by_mid_for_offer(
                                        request_context=target_context.request,
                                        offer_link=str(row.get("link") or ""),
                                        in_run_cache=delivery_days_cache_by_link,
                                        persistent_cache=delivery_days_cache_payload,
                                    )
                                    if req_count > 0:
                                        delivery_days_cache_dirty = True
                                        summary["kaspi_delivery_requests"] += int(req_count)
                                        store_summary["kaspi_delivery_requests"] += int(req_count)
                                before_floor = extract_external_competitor_floor(
                                    row,
                                    store_id=store_id,
                                    exclude_not_competitors=config.external_exclude_not_competitors,
                                )
                                external_floor = extract_external_competitor_floor(
                                    row,
                                    store_id=store_id,
                                    exclude_not_competitors=config.external_exclude_not_competitors,
                                    delivery_days_by_mid=delivery_days_by_mid,
                                )
                                if before_floor is not None and external_floor != before_floor:
                                    summary["external_long_delivery_ignored_rows"] += 1
                                    store_summary["external_long_delivery_ignored_rows"] += 1
                                if external_floor is None:
                                    summary["missing_matches"] += 1
                                    store_summary["missing_matches"] += 1
                                    checkpoint.progress[store_key] = Progress(page_index=page_index, row_index=row_idx)
                                    save_checkpoint(effective_checkpoint, checkpoint)
                                    continue

                                summary["external_floor_found"] += 1
                                store_summary["external_floor_found"] += 1
                                source_min = compute_external_anchor_target(
                                    external_floor,
                                    minus_kzt=config.external_competitor_minus_kzt,
                                )

                                if config.external_skip_line52_locked_9990 and should_skip_line52_locked_9990(
                                    row=row,
                                    current_min_price=current_min,
                                    store_id=store_id,
                                    allowed_store_ids=line52_skip_store_ids,
                                ):
                                    summary["external_line52_locked_skipped"] += 1
                                    store_summary["external_line52_locked_skipped"] += 1
                                    checkpoint.progress[store_key] = Progress(page_index=page_index, row_index=row_idx)
                                    save_checkpoint(effective_checkpoint, checkpoint)
                                    continue

                                need_min_update = should_apply_external_anchor(
                                    current_min_price=current_min,
                                    target_min_price=source_min,
                                    only_if_below=config.external_only_if_below,
                                )
                                if config.external_fix_max_below_target:
                                    need_max_update = needs_external_max_fix(
                                        current_max_price=current_max,
                                        target_min_price=source_min,
                                    )
                                if config.external_fix_live_price_below_target:
                                    need_live_price_update = needs_external_live_price_fix(
                                        current_price=current_price,
                                        target_min_price=source_min,
                                    )
                                if not need_min_update and not need_max_update and not need_live_price_update:
                                    checkpoint.progress[store_key] = Progress(page_index=page_index, row_index=row_idx)
                                    save_checkpoint(effective_checkpoint, checkpoint)
                                    continue
                            else:
                                if link and link in source_by_link:
                                    source_min = source_by_link.get(link)
                                elif sku and sku in source_by_sku:
                                    source_min = source_by_sku.get(sku)
                                elif kaspi_sku and kaspi_sku in source_by_kaspi:
                                    source_min = source_by_kaspi.get(kaspi_sku)

                                if source_min is None:
                                    summary["missing_matches"] += 1
                                    store_summary["missing_matches"] += 1
                                    checkpoint.progress[store_key] = Progress(page_index=page_index, row_index=row_idx)
                                    save_checkpoint(effective_checkpoint, checkpoint)
                                    continue

                                need_min_update = current_min != source_min
                                if not need_min_update:
                                    checkpoint.progress[store_key] = Progress(page_index=page_index, row_index=row_idx)
                                    save_checkpoint(effective_checkpoint, checkpoint)
                                    continue

                            if effective_dry_run:
                                if need_min_update:
                                    price_write_vintage_rows.append(
                                        build_vintage_record(
                                            store_id=store_id,
                                            row_id=row_id,
                                            merchant_sku=sku or kaspi_sku or "",
                                            operation="planned_price_write",
                                            target_field="min_price",
                                            target_value=int(source_min),
                                        )
                                    )
                                    summary["min_price_updates"] += 1
                                    store_summary["min_price_updates"] += 1
                                if need_max_update:
                                    price_write_vintage_rows.append(
                                        build_vintage_record(
                                            store_id=store_id,
                                            row_id=row_id,
                                            merchant_sku=sku or kaspi_sku or "",
                                            operation="planned_price_write",
                                            target_field="max_price",
                                            target_value=int(source_min),
                                        )
                                    )
                                    summary["max_price_updates"] += 1
                                    store_summary["max_price_updates"] += 1
                                if need_live_price_update:
                                    price_write_vintage_rows.append(
                                        build_vintage_record(
                                            store_id=store_id,
                                            row_id=row_id,
                                            merchant_sku=sku or kaspi_sku or "",
                                            operation="planned_price_write",
                                            target_field="price",
                                            target_value=int(source_min),
                                        )
                                    )
                                    summary["current_price_updates"] += 1
                                    store_summary["current_price_updates"] += 1
                                checkpoint.progress[store_key] = Progress(page_index=page_index, row_index=row_idx)
                                save_checkpoint(effective_checkpoint, checkpoint)
                                continue

                            if need_min_update:
                                vintage_record = build_vintage_record(
                                    store_id=store_id,
                                    row_id=row_id,
                                    merchant_sku=sku or kaspi_sku or "",
                                    operation="set_new_price",
                                    target_field="min_price",
                                    target_value=int(source_min),
                                )
                                price_write_vintage_rows.append(vintage_record)
                                ensure_price_write_vintage_ok(vintage_record)
                                summary["api_sets_attempted"] += 1
                                store_summary["api_sets_attempted"] += 1
                                form_data = urlencode({"id": str(row_id), "min_price": str(source_min)})
                                resp_set = target_context.request.post(
                                    "https://repricer.kz/set_new_price/",
                                    data=form_data,
                                    headers=_form_headers(bot_token, target_page.url),
                                )
                                summary["api_requests"] += 1
                                store_summary["api_requests"] += 1
                                if resp_set.status == 200:
                                    summary["api_sets_succeeded"] += 1
                                    store_summary["api_sets_succeeded"] += 1
                                    summary["min_price_updates"] += 1
                                    store_summary["min_price_updates"] += 1
                                else:
                                    record_error(
                                        f"API set_new_price failed store {store_id} row {row_id}",
                                        None,
                                        {"store_id": store_id, "row_id": row_id, "status": resp_set.status},
                                    )

                            if need_max_update:
                                vintage_record = build_vintage_record(
                                    store_id=store_id,
                                    row_id=row_id,
                                    merchant_sku=sku or kaspi_sku or "",
                                    operation="set_max_price",
                                    target_field="max_price",
                                    target_value=int(source_min),
                                )
                                price_write_vintage_rows.append(vintage_record)
                                ensure_price_write_vintage_ok(vintage_record)
                                summary["api_sets_attempted"] += 1
                                store_summary["api_sets_attempted"] += 1
                                form_data = urlencode({"id": str(row_id), "max_price": str(source_min)})
                                resp_set = target_context.request.post(
                                    "https://repricer.kz/set_max_price/",
                                    data=form_data,
                                    headers=_form_headers(bot_token, target_page.url),
                                )
                                summary["api_requests"] += 1
                                store_summary["api_requests"] += 1
                                if resp_set.status == 200:
                                    summary["api_sets_succeeded"] += 1
                                    store_summary["api_sets_succeeded"] += 1
                                    summary["max_price_updates"] += 1
                                    store_summary["max_price_updates"] += 1
                                else:
                                    record_error(
                                        f"API set_max_price failed store {store_id} row {row_id}",
                                        None,
                                        {"store_id": store_id, "row_id": row_id, "status": resp_set.status},
                                    )

                            if need_live_price_update:
                                vintage_record = build_vintage_record(
                                    store_id=store_id,
                                    row_id=row_id,
                                    merchant_sku=sku or kaspi_sku or "",
                                    operation="set_item_price",
                                    target_field="price",
                                    target_value=int(source_min),
                                )
                                price_write_vintage_rows.append(vintage_record)
                                ensure_price_write_vintage_ok(vintage_record)
                                summary["api_sets_attempted"] += 1
                                store_summary["api_sets_attempted"] += 1
                                form_data = urlencode({"id": str(row_id), "price": str(source_min)})
                                resp_set = target_context.request.post(
                                    "https://repricer.kz/set_item_price/",
                                    data=form_data,
                                    headers=_form_headers(bot_token, target_page.url),
                                )
                                summary["api_requests"] += 1
                                store_summary["api_requests"] += 1
                                if resp_set.status == 200:
                                    summary["api_sets_succeeded"] += 1
                                    store_summary["api_sets_succeeded"] += 1
                                    summary["current_price_updates"] += 1
                                    store_summary["current_price_updates"] += 1
                                else:
                                    record_error(
                                        f"API set_item_price failed store {store_id} row {row_id}",
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
                        remaining = 0
                        start = 0
                        page_index = 1
                        verify_draw = 1
                        while start < max(records_total, 1):
                            params["start"] = start
                            params["length"] = length
                            params["draw"] = verify_draw
                            params["mid"] = store_id

                            resp = target_context.request.post(
                                "https://repricer.kz/price_strategy_data_mid/",
                                data=json.dumps(params),
                                headers=_api_headers(bot_token, target_page.url),
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
                                link = _normalize_link(row.get("link"))
                                sku = _normalize_sku(row.get("merchant_sku"))
                                kaspi_sku = _normalize_sku(row.get("kaspi_sku"))
                                if is_protected_merchant_sku(sku, config.protected_merchant_sku_prefixes) or (
                                    kaspi_sku
                                    and is_protected_merchant_sku(
                                        kaspi_sku,
                                        config.protected_merchant_sku_prefixes,
                                    )
                                ):
                                    continue
                                current_min = parse_price_int(row.get("min_price"))
                                if current_min is None:
                                    current_min = parse_price_int(row.get("price"))
                                if current_min is None:
                                    continue
                                current_price = parse_price_int(row.get("price"))
                                current_max = parse_price_int(row.get("max_price"))

                                source_min: int | None = None
                                if config.pricing_mode == "external_competitor_anchor":
                                    delivery_days_by_mid: dict[str, int] = {}
                                    if link and row.get("competitors"):
                                        delivery_days_by_mid, req_count = _resolve_delivery_days_by_mid_for_offer(
                                            request_context=target_context.request,
                                            offer_link=str(row.get("link") or ""),
                                            in_run_cache=delivery_days_cache_by_link,
                                            persistent_cache=delivery_days_cache_payload,
                                        )
                                        if req_count > 0:
                                            delivery_days_cache_dirty = True
                                            summary["kaspi_delivery_requests"] += int(req_count)
                                            store_summary["kaspi_delivery_requests"] += int(req_count)
                                    external_floor = extract_external_competitor_floor(
                                        row,
                                        store_id=store_id,
                                        exclude_not_competitors=config.external_exclude_not_competitors,
                                        delivery_days_by_mid=delivery_days_by_mid,
                                    )
                                    if external_floor is None:
                                        continue
                                    source_min = compute_external_anchor_target(
                                        external_floor,
                                        minus_kzt=config.external_competitor_minus_kzt,
                                    )
                                    if config.external_skip_line52_locked_9990 and should_skip_line52_locked_9990(
                                        row=row,
                                        current_min_price=current_min,
                                        store_id=store_id,
                                        allowed_store_ids=line52_skip_store_ids,
                                    ):
                                        continue
                                    need_min_update = should_apply_external_anchor(
                                        current_min_price=current_min,
                                        target_min_price=source_min,
                                        only_if_below=config.external_only_if_below,
                                    )
                                    need_max_update = False
                                    if config.external_fix_max_below_target:
                                        need_max_update = needs_external_max_fix(
                                            current_max_price=current_max,
                                            target_min_price=source_min,
                                        )
                                    need_live_price_update = False
                                    if config.external_fix_live_price_below_target:
                                        need_live_price_update = needs_external_live_price_fix(
                                            current_price=current_price,
                                            target_min_price=source_min,
                                        )
                                    if need_min_update or need_max_update or need_live_price_update:
                                        remaining += 1
                                    continue

                                if link and link in source_by_link:
                                    source_min = source_by_link.get(link)
                                elif sku and sku in source_by_sku:
                                    source_min = source_by_sku.get(sku)
                                elif kaspi_sku and kaspi_sku in source_by_kaspi:
                                    source_min = source_by_kaspi.get(kaspi_sku)
                                if source_min is None:
                                    continue
                                if current_min != source_min:
                                    remaining += 1

                            start += length
                            page_index += 1
                            verify_draw += 1
                            if effective_slowmo_ms:
                                time.sleep(effective_slowmo_ms / 1000.0)

                        summary["remaining_mismatches"][str(store_id)] = remaining

                except Exception as exc:
                    record_error(f"Store processing failed store {store_id}", exc, {"store_id": store_id})
                    _capture_artifact(target_page, run_dir, f"target_{store_id}_fatal")
                    store_summary["errors"] += 1

        finally:
            if close_context:
                target_context.close()

    if price_write_vintage_rows:
        vintage_path = run_dir / "price_write_vintage_log.csv"
        write_price_write_vintage_log(vintage_path, price_write_vintage_rows)
        summary["price_write_vintage_logged_rows"] = len(price_write_vintage_rows)
        summary["price_write_vintage_log_csv"] = str(vintage_path)

    if delivery_days_cache_dirty:
        save_delivery_days_cache(DEFAULT_DELIVERY_DAYS_CACHE_PATH, delivery_days_cache_payload)

    summary_path = run_dir / "summary.json"
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    return summary
