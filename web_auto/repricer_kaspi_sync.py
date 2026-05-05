from __future__ import annotations

import argparse
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from playwright.sync_api import sync_playwright

from .checkpoint import progress_key
from .config import load_dumping_config
from .repricer_competitors import _close_login_modal, _wait_table_ready
from .utils import ensure_env


def classify_kaspi_update_modal_state(text: str) -> str:
    normalized = " ".join(str(text or "").split()).lower()
    if not normalized:
        return "unknown"
    if "для обновления товаров нажмите" in normalized:
        return "ready_to_sync"
    if "идет обновление" in normalized or "идёт обновление" in normalized:
        return "in_progress"
    return "unknown"


def extract_updated_count(text: str) -> int | None:
    match = re.search(r"Обновлено\s+(\d+)\s+товара", str(text or ""))
    if not match:
        return None
    return int(match.group(1))


def run_repricer_kaspi_sync(
    *,
    config_path: str | Path,
    account_name: str,
    store_ids: list[int],
    confirm: bool,
    headless: bool,
    slowmo_ms: int | None = None,
    timeout_ms: int | None = None,
) -> dict[str, Any]:
    cfg = load_dumping_config(config_path)
    account = next((a for a in cfg.accounts if a.name == account_name), None)
    if account is None:
        raise ValueError(f"account not found: {account_name}")
    if not confirm:
        raise RuntimeError("live Repricer Kaspi sync requires --confirm")

    run_cfg = cfg.run
    eff_headless = bool(headless) if headless else bool(run_cfg.headless)
    eff_slowmo = int(slowmo_ms or run_cfg.slowmo_ms)
    eff_timeout = int(timeout_ms or run_cfg.page_timeout_ms)
    token = ensure_env(account.token_env)
    run_id = datetime.now().strftime("%Y%m%d_%H%M%S")
    run_dir = Path(run_cfg.artifacts_dir).parent / "repricer_kaspi_sync" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    summary: dict[str, Any] = {
        "run_id": run_id,
        "config_path": str(config_path),
        "account_name": account_name,
        "stores": {},
        "run_dir": str(run_dir),
    }

    with sync_playwright() as p:
        browser = p.chromium.launch(channel="chrome", headless=eff_headless, slow_mo=eff_slowmo)
        context = browser.new_context(storage_state=run_cfg.storage_state_path)
        page = context.new_page()
        page.goto(f"{account.base_url}?token={token}", wait_until="domcontentloaded", timeout=eff_timeout)
        _close_login_modal(page)
        page.wait_for_selector("#mid_header", timeout=eff_timeout)

        for store_id in store_ids:
            store_key = progress_key("repricer_kaspi_sync", store_id)
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
            _wait_table_ready(page, eff_timeout)
            page.screenshot(path=str(run_dir / f"{store_key}_before_sync.png"), full_page=True)

            page.locator("#sync_data").click(timeout=5000)
            page.wait_for_timeout(1500)
            modal = page.locator("#kaspi_update_modal")
            modal.wait_for(state="visible", timeout=eff_timeout)
            text = page.locator("#kaspi_update_modal_text").inner_text(timeout=5000)
            state = classify_kaspi_update_modal_state(text)
            updated_count = extract_updated_count(text)

            status = "unknown"
            if state == "ready_to_sync":
                page.locator("#kaspi_update_modal_sync").click(timeout=5000)
                page.wait_for_timeout(2000)
                text = page.locator("#kaspi_update_modal_text").inner_text(timeout=5000)
                state = classify_kaspi_update_modal_state(text)
                status = "started" if state == "in_progress" else "unexpected_post_click_state"
            elif state == "in_progress":
                status = "already_running"
            else:
                status = "unexpected_modal_state"

            page.screenshot(path=str(run_dir / f"{store_key}_after_sync.png"), full_page=True)
            summary["stores"][str(store_id)] = {
                "status": status,
                "modal_state": state,
                "modal_text": text,
                "updated_count": updated_count,
            }
            try:
                page.locator("#kaspi_update_modal_close").click(timeout=3000)
            except Exception:
                pass

        context.close()
        browser.close()

    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    parser.add_argument("--account", default="app_1")
    parser.add_argument("--stores", required=True, help="Comma-separated store IDs")
    parser.add_argument("--confirm", action="store_true")
    parser.add_argument("--headless", action="store_true")
    parser.add_argument("--slowmo-ms", type=int)
    parser.add_argument("--timeout-ms", type=int)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    store_ids = [int(chunk.strip()) for chunk in str(args.stores).split(",") if chunk.strip()]
    summary = run_repricer_kaspi_sync(
        config_path=args.config,
        account_name=args.account,
        store_ids=store_ids,
        confirm=args.confirm,
        headless=args.headless,
        slowmo_ms=args.slowmo_ms,
        timeout_ms=args.timeout_ms,
    )
    out = json.dumps(summary, ensure_ascii=False, indent=2)
    print(out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
