from __future__ import annotations

import argparse
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from playwright.sync_api import Download, Page, TimeoutError as PlaywrightTimeoutError, sync_playwright

from .kaspi_merchant_common import (
    login_kaspi_merchant,
    merchant_browser_launch_kwargs,
    normalize_store_name,
    resolve_store_credentials,
)


PRODUCTS_URL = "https://kaspi.kz/mc/#/products/active/1"


def download_target_filename(store_name: str, sale_state: str) -> str:
    store = normalize_store_name(store_name).replace("-", "").lower()
    state = str(sale_state or "").strip().upper()
    if state not in {"ACTIVE", "ARCHIVE"}:
        raise ValueError(f"unsupported sale_state: {sale_state}")
    return f"{store}_{state}.xlsx"


def _products_page_ready(page: Page) -> bool:
    body_text = page.locator("body").inner_text()
    normalized = " ".join(str(body_text or "").split())
    return "Прайс-лист" in normalized and ("В продаже" in normalized or "Сняты с продажи" in normalized)


def _wait_for_products_page(page: Page) -> None:
    page.get_by_role("link", name="Управление товарами").first.wait_for(timeout=20000)
    for _ in range(3):
        page.goto(PRODUCTS_URL, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(3000)
        if _products_page_ready(page):
            return
        try:
            page.get_by_role("link", name="Управление товарами").first.click()
            page.wait_for_timeout(3000)
            if _products_page_ready(page):
                return
        except Exception:
            pass
    raise PlaywrightTimeoutError("products_page_not_ready")


def _click_filter_button(page: Page) -> None:
    filter_button = page.get_by_text(re.compile(r"(В продаже|Сняты с продажи)\s*\(\d+\)"), exact=False).first
    filter_button.wait_for(timeout=15000)
    filter_button.click()
    page.wait_for_timeout(1000)


def _find_sale_state_select(page: Page):
    selects = page.locator("select")
    for index in range(selects.count()):
        select = selects.nth(index)
        try:
            text = " ".join(str(select.inner_text() or "").split())
        except Exception:
            continue
        if "В продаже" in text and "Сняты с продажи" in text:
            return select
    raise PlaywrightTimeoutError("sale_state_select_not_found")


def _select_sale_state(page: Page, sale_state: str, sale_state_select=None) -> str:
    target_text = "В продаже" if sale_state == "ACTIVE" else "Сняты с продажи"
    target_value = "active" if sale_state == "ACTIVE" else "archive"
    select = sale_state_select or _find_sale_state_select(page)
    select.select_option(value=target_value)
    page.wait_for_timeout(2500)
    return target_text


def _open_pricelist_dropdown(page: Page):
    button = page.locator("button").filter(has_text=re.compile(r"Прайс-лист")).first
    button.click()
    page.wait_for_timeout(500)
    dropdown_item = page.locator("a.dropdown-item").filter(has_text="Скачать в Excel").first
    return dropdown_item


def _click_hidden_dropdown_item(dropdown_item) -> None:
    dropdown_item.evaluate("(el) => el.click()")


def _download_excel(page: Page, *, store_name: str, sale_state: str, downloads_dir: Path) -> dict[str, Any]:
    state_label = _select_sale_state(page, sale_state)
    page.screenshot(path=str(downloads_dir / f"before_download_{sale_state}.png"), full_page=True)
    excel_item = _open_pricelist_dropdown(page)
    with page.expect_download(timeout=30000) as download_info:
        _click_hidden_dropdown_item(excel_item)
    download = download_info.value
    target_path = downloads_dir / download_target_filename(store_name, sale_state)
    _save_download(download, target_path)
    page.wait_for_timeout(1000)
    page.screenshot(path=str(downloads_dir / f"after_download_{sale_state}.png"), full_page=True)
    return {
        "sale_state": sale_state,
        "filter_label": state_label,
        "downloaded_filename": download.suggested_filename,
        "saved_path": str(target_path),
    }


def _save_download(download: Download, target_path: Path) -> None:
    target_path.parent.mkdir(parents=True, exist_ok=True)
    download.save_as(str(target_path))


def run_kaspi_pricelist_download(
    *,
    store_name: str,
    email: str,
    password: str,
    run_dir: Path,
    headless: bool,
) -> dict[str, Any]:
    run_dir.mkdir(parents=True, exist_ok=True)
    downloads_dir = run_dir / "downloads"
    summary: dict[str, Any] = {
        "run_dir": str(run_dir),
        "downloads_dir": str(downloads_dir),
        "store_name": normalize_store_name(store_name),
        "downloads": [],
        "status": "unknown",
    }
    with sync_playwright() as p:
        browser = p.chromium.launch(**merchant_browser_launch_kwargs(headless=headless))
        context = browser.new_context(accept_downloads=True, viewport={"width": 1440, "height": 1400})
        page = context.new_page()
        login_kaspi_merchant(page, email=email, password=password)
        page.screenshot(path=str(run_dir / "logged_in_home.png"), full_page=True)
        _wait_for_products_page(page)
        try:
            summary["downloads"].append(_download_excel(page, store_name=store_name, sale_state="ACTIVE", downloads_dir=downloads_dir))
            summary["downloads"].append(_download_excel(page, store_name=store_name, sale_state="ARCHIVE", downloads_dir=downloads_dir))
        except PlaywrightTimeoutError as exc:
            page.screenshot(path=str(run_dir / "download_error.png"), full_page=True)
            summary["status"] = "failed"
            summary["error"] = f"playwright_timeout:{exc}"
            context.close()
            browser.close()
            return summary
        context.close()
        browser.close()
    summary["status"] = "success"
    return summary


def default_run_dir(store_name: str) -> Path:
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    return Path("runs/kaspi_pricelist_ops") / f"{ts}_{normalize_store_name(store_name).replace('-', '').lower()}"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--env-file", type=Path, default=Path("~/Docs/Autonomous_business/.env"))
    parser.add_argument("--store", default="STORE-B")
    parser.add_argument("--run-dir", type=Path)
    parser.add_argument("--headless", action="store_true")
    args = parser.parse_args()

    creds = resolve_store_credentials(args.store, args.env_file)
    run_dir = args.run_dir or default_run_dir(creds["store_name"])
    summary = run_kaspi_pricelist_download(
        store_name=creds["store_name"],
        email=creds["email"],
        password=creds["password"],
        run_dir=run_dir,
        headless=args.headless,
    )
    summary_path = run_dir / "download_summary.json"
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if summary["status"] == "success" else 4


if __name__ == "__main__":
    raise SystemExit(main())
