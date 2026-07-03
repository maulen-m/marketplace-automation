from __future__ import annotations

import argparse
import json
import re
import shutil
import time
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
DOWNLOAD_EVENT_TIMEOUT_MS = 30000
DOWNLOAD_FALLBACK_TIMEOUT_SECONDS = 45
DOWNLOAD_FALLBACK_POLL_SECONDS = 0.5
TEMP_DOWNLOAD_SUFFIXES = (".crdownload", ".download", ".part", ".tmp")


class PricelistDownloadTransportError(RuntimeError):
    def __init__(self, message: str, diagnostics: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.diagnostics = diagnostics or {}


def download_target_filename(store_name: str, sale_state: str) -> str:
    store = normalize_store_name(store_name).replace("-", "").lower()
    state = str(sale_state or "").strip().upper()
    if state not in {"ACTIVE", "ARCHIVE"}:
        raise ValueError(f"unsupported sale_state: {sale_state}")
    return f"{store}_{state}.xlsx"


def _products_page_ready(page: Page) -> bool:
    body_text = page.locator("body").inner_text()
    normalized = " ".join(str(body_text or "").split())
    has_export_control = "Прайс-лист" in normalized or "Действия с файлами" in normalized
    return has_export_control and ("В продаже" in normalized or "Сняты с продажи" in normalized)


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
    button = page.locator("button").filter(has_text=re.compile(r"(Прайс-лист|Действия с файлами)")).first
    button.click()
    dropdown_item = page.locator("a.dropdown-item").filter(has_text=re.compile(r"Скачать.*Excel")).first
    dropdown_item.wait_for(timeout=15000, state="visible")
    # Kaspi can render the Excel export item while it is still disabled/loading.
    # Clicking during that state silently produces no download, especially on
    # larger STORE-B active lists, so wait for the real enabled export control.
    page.wait_for_function(
        """(el) => {
            const cls = String(el.className || "");
            const ariaDisabled = el.getAttribute("aria-disabled");
            return !cls.includes("is-disabled")
                && !cls.includes("loading")
                && ariaDisabled !== "true";
        }""",
        arg=dropdown_item.element_handle(timeout=15000),
        timeout=60000,
    )
    return dropdown_item


def _click_hidden_dropdown_item(dropdown_item) -> None:
    dropdown_item.evaluate("(el) => el.click()")


def _is_temporary_download(path: Path) -> bool:
    name = path.name.lower()
    return any(name.endswith(suffix) for suffix in TEMP_DOWNLOAD_SUFFIXES)


def _iter_download_files(download_dirs: list[Path]) -> list[Path]:
    files: list[Path] = []
    for directory in download_dirs:
        try:
            entries = list(directory.iterdir()) if directory.exists() else []
        except OSError:
            continue
        for path in entries:
            if path.is_file():
                files.append(path)
    return files


def _capture_download_snapshot(download_dirs: list[Path]) -> set[Path]:
    snapshot: set[Path] = set()
    for path in _iter_download_files(download_dirs):
        try:
            snapshot.add(path.resolve())
        except OSError:
            continue
    return snapshot


def _describe_download_dirs(download_dirs: list[Path], known_paths: set[Path]) -> list[dict[str, Any]]:
    known = {p.resolve() for p in known_paths}
    diagnostics: list[dict[str, Any]] = []
    for directory in download_dirs:
        entry: dict[str, Any] = {
            "dir": str(directory),
            "exists": directory.exists(),
            "files": [],
        }
        for path in _iter_download_files([directory]):
            try:
                resolved = path.resolve()
                stat = path.stat()
            except OSError as exc:
                entry["files"].append({"name": path.name, "error": str(exc)})
                continue
            entry["files"].append(
                {
                    "name": path.name,
                    "size": stat.st_size,
                    "mtime": stat.st_mtime,
                    "is_known": resolved in known,
                    "is_temporary": _is_temporary_download(path),
                }
            )
        diagnostics.append(entry)
    return diagnostics


def _detect_new_completed_xlsx(download_dirs: list[Path], known_paths: set[Path], *, started_at: float) -> Path | None:
    known = {p.resolve() for p in known_paths}
    candidates: list[Path] = []
    for path in _iter_download_files(download_dirs):
        if _is_temporary_download(path):
            continue
        if path.suffix.lower() != ".xlsx":
            continue
        if path.name.startswith("~$"):
            continue
        try:
            resolved = path.resolve()
            stat = resolved.stat()
        except OSError:
            continue
        if resolved in known:
            continue
        if stat.st_size <= 0:
            continue
        if stat.st_mtime < started_at - 2:
            continue
        candidates.append(resolved)
    if not candidates:
        return None
    candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return candidates[0]


def _wait_for_new_completed_xlsx(
    download_dirs: list[Path],
    known_paths: set[Path],
    *,
    started_at: float,
    timeout_seconds: int = DOWNLOAD_FALLBACK_TIMEOUT_SECONDS,
    poll_seconds: float = DOWNLOAD_FALLBACK_POLL_SECONDS,
) -> Path:
    deadline = time.time() + max(timeout_seconds, 1)
    while time.time() <= deadline:
        candidate = _detect_new_completed_xlsx(download_dirs, known_paths, started_at=started_at)
        if candidate is not None:
            size1 = candidate.stat().st_size
            time.sleep(0.75)
            if candidate.exists():
                size2 = candidate.stat().st_size
                if size1 > 0 and size1 == size2:
                    return candidate
        time.sleep(max(poll_seconds, 0.1))
    dirs = ", ".join(str(path) for path in download_dirs)
    raise TimeoutError(f"Timed out waiting for new completed .xlsx in: {dirs}")


def _copy_downloaded_file(source_path: Path, target_path: Path) -> None:
    target_path.parent.mkdir(parents=True, exist_ok=True)
    source_resolved = source_path.resolve()
    target_resolved = target_path.resolve()
    if source_resolved == target_resolved:
        return
    temp_path = target_path.with_name(f"{target_path.name}.tmp")
    if temp_path.exists():
        temp_path.unlink()
    shutil.copy2(source_resolved, temp_path)
    temp_path.replace(target_path)


def _download_excel(
    page: Page,
    *,
    store_name: str,
    sale_state: str,
    downloads_dir: Path,
    poll_dirs: list[Path] | None = None,
    event_timeout_ms: int = DOWNLOAD_EVENT_TIMEOUT_MS,
    fallback_timeout_seconds: int = DOWNLOAD_FALLBACK_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    state_label = _select_sale_state(page, sale_state)
    page.screenshot(path=str(downloads_dir / f"before_download_{sale_state}.png"), full_page=True)
    excel_item = _open_pricelist_dropdown(page)
    target_path = downloads_dir / download_target_filename(store_name, sale_state)
    effective_poll_dirs = [Path(path) for path in (poll_dirs or [downloads_dir])]
    for directory in effective_poll_dirs:
        directory.mkdir(parents=True, exist_ok=True)
    known_paths = _capture_download_snapshot(effective_poll_dirs)
    started_at = time.time()
    download_method = "playwright_event"
    event_error = ""
    downloaded_filename = ""
    source_path = ""
    try:
        with page.expect_download(timeout=event_timeout_ms) as download_info:
            _click_hidden_dropdown_item(excel_item)
        download = download_info.value
        downloaded_filename = download.suggested_filename
        _save_download(download, target_path)
    except PlaywrightTimeoutError as exc:
        event_error = f"playwright_timeout:{exc}"
        try:
            fallback_path = _wait_for_new_completed_xlsx(
                effective_poll_dirs,
                known_paths,
                started_at=started_at,
                timeout_seconds=fallback_timeout_seconds,
            )
        except TimeoutError as fallback_exc:
            diagnostics = {
                "sale_state": sale_state,
                "target_path": str(target_path),
                "event_timeout_ms": event_timeout_ms,
                "fallback_timeout_seconds": fallback_timeout_seconds,
                "event_error": event_error,
                "fallback_error": str(fallback_exc),
                "download_dirs": _describe_download_dirs(effective_poll_dirs, known_paths),
            }
            raise PricelistDownloadTransportError(
                f"download_event_and_filesystem_fallback_timeout:{sale_state}",
                diagnostics=diagnostics,
            ) from exc
        _copy_downloaded_file(fallback_path, target_path)
        download_method = "filesystem_fallback"
        downloaded_filename = fallback_path.name
        source_path = str(fallback_path)
    page.wait_for_timeout(1000)
    page.screenshot(path=str(downloads_dir / f"after_download_{sale_state}.png"), full_page=True)
    return {
        "sale_state": sale_state,
        "filter_label": state_label,
        "downloaded_filename": downloaded_filename,
        "download_method": download_method,
        "source_path": source_path,
        "saved_path": str(target_path),
        "event_timeout_ms": event_timeout_ms,
        "fallback_timeout_seconds": fallback_timeout_seconds,
        "fallback_poll_dirs": [str(path) for path in effective_poll_dirs],
        "event_error": event_error,
    }


def _save_download(download: Download, target_path: Path) -> None:
    target_path.parent.mkdir(parents=True, exist_ok=True)
    download.save_as(str(target_path))


def _configure_browser_download_dir(context, page: Page, download_dir: Path) -> dict[str, Any]:
    download_dir.mkdir(parents=True, exist_ok=True)
    diagnostics: dict[str, Any] = {
        "download_dir": str(download_dir),
        "cdp_set_download_behavior": "not_attempted",
    }
    try:
        session = context.new_cdp_session(page)
        session.send("Browser.setDownloadBehavior", {"behavior": "allow", "downloadPath": str(download_dir)})
        diagnostics["cdp_set_download_behavior"] = "ok"
    except Exception as exc:
        diagnostics["cdp_set_download_behavior"] = "failed"
        diagnostics["cdp_error"] = str(exc)
    return diagnostics


def _write_download_summary(run_dir: Path, summary: dict[str, Any]) -> None:
    summary_path = run_dir / "download_summary.json"
    summary["summary_path"] = str(summary_path)
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")


def run_kaspi_pricelist_download(
    *,
    store_name: str,
    email: str,
    password: str,
    run_dir: Path,
    headless: bool,
    event_timeout_ms: int = DOWNLOAD_EVENT_TIMEOUT_MS,
    fallback_timeout_seconds: int = DOWNLOAD_FALLBACK_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    run_dir.mkdir(parents=True, exist_ok=True)
    downloads_dir = run_dir / "downloads"
    downloads_dir.mkdir(parents=True, exist_ok=True)
    browser_downloads_dir = (downloads_dir / "_browser_downloads").resolve()
    summary: dict[str, Any] = {
        "run_dir": str(run_dir),
        "downloads_dir": str(downloads_dir),
        "browser_downloads_dir": str(browser_downloads_dir),
        "store_name": normalize_store_name(store_name),
        "download_event_timeout_ms": event_timeout_ms,
        "filesystem_fallback_timeout_seconds": fallback_timeout_seconds,
        "downloads": [],
        "status": "unknown",
    }
    with sync_playwright() as p:
        launch_kwargs = merchant_browser_launch_kwargs(headless=headless)
        launch_kwargs["downloads_path"] = str(browser_downloads_dir)
        browser = p.chromium.launch(**launch_kwargs)
        context = browser.new_context(accept_downloads=True, viewport={"width": 1440, "height": 1400})
        page = context.new_page()
        summary["download_behavior"] = _configure_browser_download_dir(context, page, browser_downloads_dir)
        login_kaspi_merchant(page, email=email, password=password)
        page.screenshot(path=str(run_dir / "logged_in_home.png"), full_page=True)
        _wait_for_products_page(page)
        try:
            poll_dirs = [downloads_dir, browser_downloads_dir]
            summary["downloads"].append(
                _download_excel(
                    page,
                    store_name=store_name,
                    sale_state="ACTIVE",
                    downloads_dir=downloads_dir,
                    poll_dirs=poll_dirs,
                    event_timeout_ms=event_timeout_ms,
                    fallback_timeout_seconds=fallback_timeout_seconds,
                )
            )
            summary["downloads"].append(
                _download_excel(
                    page,
                    store_name=store_name,
                    sale_state="ARCHIVE",
                    downloads_dir=downloads_dir,
                    poll_dirs=poll_dirs,
                    event_timeout_ms=event_timeout_ms,
                    fallback_timeout_seconds=fallback_timeout_seconds,
                )
            )
        except (PlaywrightTimeoutError, PricelistDownloadTransportError) as exc:
            page.screenshot(path=str(run_dir / "download_error.png"), full_page=True)
            summary["status"] = "failed"
            summary["error"] = str(exc) if isinstance(exc, PricelistDownloadTransportError) else f"playwright_timeout:{exc}"
            if isinstance(exc, PricelistDownloadTransportError):
                summary["download_diagnostics"] = exc.diagnostics
            context.close()
            browser.close()
            _write_download_summary(run_dir, summary)
            return summary
        context.close()
        browser.close()
    summary["status"] = "success"
    _write_download_summary(run_dir, summary)
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
