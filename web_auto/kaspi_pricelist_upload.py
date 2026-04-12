from __future__ import annotations

import argparse
import json
import re
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from playwright.sync_api import Page, TimeoutError as PlaywrightTimeoutError, sync_playwright

from .kaspi_merchant_common import (
    login_kaspi_merchant,
    merchant_browser_launch_kwargs,
    normalize_store_name,
    resolve_store_credentials,
)


PRICE_LIST_URL = "https://kaspi.kz/mc/#/price-list"
HISTORY_URL = "https://kaspi.kz/mc/#/history?page=1"


@dataclass(frozen=True)
class HistoryRow:
    filename: str
    status: str
    processed: int
    total: int
    uploaded_at: datetime
    detail_href: str


def parse_kaspi_history_datetime(value: str) -> datetime:
    return datetime.strptime(str(value or "").strip(), "%d.%m.%Y %H:%M")


def history_row_is_success(row: HistoryRow) -> bool:
    return row.status.strip().lower() == "файл загружен" and row.total > 0 and row.processed == row.total


def history_row_is_terminal_failure(row: HistoryRow) -> bool:
    return "ошибка загрузки файла" in row.status.strip().lower()


def history_row_is_processing(row: HistoryRow) -> bool:
    status = row.status.strip().lower()
    return "идет загрузка" in status or "идёт загрузка" in status or "обрабатывается" in status


def history_row_to_payload(row: HistoryRow) -> dict[str, Any]:
    payload = asdict(row)
    payload["uploaded_at"] = row.uploaded_at.isoformat()
    return payload


def detail_url_from_history_row(row: HistoryRow) -> str:
    href = str(row.detail_href or "").strip()
    if not href:
        return ""
    if href.startswith("http://") or href.startswith("https://"):
        return href
    return f"https://kaspi.kz/mc/{href.lstrip('/')}"


def resolve_upload_file_paths(
    *,
    archive: str | Path | None = None,
    active: str | Path | None = None,
    files: list[str | Path] | None = None,
) -> list[Path]:
    ordered: list[Path] = []
    if files:
        for item in files:
            path = Path(item)
            if path not in ordered:
                ordered.append(path)
        if ordered:
            return ordered
    if archive:
        ordered.append(Path(archive))
    if active:
        active_path = Path(active)
        if active_path not in ordered:
            ordered.append(active_path)
    if not ordered:
        raise ValueError("at least one upload file is required")
    return ordered


def should_extend_history_poll_deadline(row: HistoryRow | None, *, already_extended: bool) -> bool:
    if already_extended or row is None:
        return False
    return history_row_is_processing(row)


def detect_upload_block_reason(body_text: str) -> str:
    text = " ".join(str(body_text or "").split())
    if not text:
        return ""
    match = re.search(r"Превышен лимит на выставление товара\.\s*Можно изменить в\s*(\d{1,2}:\d{2})", text, re.I)
    if match:
        return f"listing_limit_until_{match.group(1)}"
    if "неверный формат файла" in text.lower():
        return "invalid_file_format"
    return ""


def select_latest_matching_history_row(rows: list[HistoryRow], filename: str, *, after_dt: datetime) -> HistoryRow | None:
    basename = Path(filename).name
    matches = [row for row in rows if row.filename == basename and row.uploaded_at >= after_dt]
    if not matches:
        return None
    matches.sort(key=lambda row: row.uploaded_at, reverse=True)
    return matches[0]


def _extract_detail_error_text(page: Page) -> str:
    text = page.locator("body").inner_text()
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("В ячейке "):
            return line
    return text[:3000].strip()


def _extract_detail_metrics(page: Page) -> dict[str, int]:
    text = page.locator("body").inner_text()
    patterns = {
        "total": r"Всего товаров:\s*(\d+)",
        "unrecognized": r"Нераспознанные товары:\s*(\d+)",
        "restricted": r"Ограниченные товары:\s*(\d+)",
        "errors": r"Товары с ошибками:\s*(\d+)",
        "warnings": r"Товары с предупреждениями:\s*(\d+)",
        "unchanged": r"Товары без изменений:\s*(\d+)",
    }
    out: dict[str, int] = {}
    for key, pattern in patterns.items():
        match = re.search(pattern, text)
        out[key] = int(match.group(1)) if match else 0
    return out


def _parse_history_rows(page: Page) -> list[HistoryRow]:
    rows: list[HistoryRow] = []
    table_rows = page.locator("table tbody tr")
    for i in range(table_rows.count()):
        tr = table_rows.nth(i)
        cells = [tr.locator("td").nth(j).inner_text().strip() for j in range(tr.locator("td").count())]
        if len(cells) < 4:
            continue
        filename = cells[0].strip()
        status = cells[1].strip()
        processed_match = re.search(r"(\d+)\s+из\s+(\d+)", cells[2])
        processed = int(processed_match.group(1)) if processed_match else 0
        total = int(processed_match.group(2)) if processed_match else 0
        uploaded_at = parse_kaspi_history_datetime(cells[3].strip())
        detail_href = ""
        for j in range(tr.locator("a").count()):
            href = tr.locator("a").nth(j).get_attribute("href") or ""
            if "/history/detail/" in href:
                detail_href = href
                break
        rows.append(
            HistoryRow(
                filename=filename,
                status=status,
                processed=processed,
                total=total,
                uploaded_at=uploaded_at,
                detail_href=detail_href,
            )
        )
    return rows


def _ensure_upload_page(page: Page) -> None:
    page.goto(PRICE_LIST_URL, wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(3000)
    page.locator('input[type="file"]').wait_for(timeout=15000)
    page.get_by_text("Загрузить файл вручную").wait_for(timeout=15000)


def _snapshot_upload_page(page: Page) -> dict[str, Any]:
    body = page.locator("body").inner_text()
    return {
        "url": page.url,
        "body_excerpt": body[:3000],
        "input_count": page.locator('input[type="file"]').count(),
        "button_count": page.locator("button").count(),
    }


def _upload_one_file(
    page: Page,
    *,
    file_path: Path,
    run_dir: Path,
    timeout_seconds: int,
    processing_grace_seconds: int,
) -> dict[str, Any]:
    start_dt = datetime.now().replace(second=0, microsecond=0)
    _ensure_upload_page(page)
    before = _snapshot_upload_page(page)
    page.screenshot(path=str(run_dir / f"before_upload_{file_path.name}.png"), full_page=True)
    page.locator('input[type="file"]').set_input_files(str(file_path))
    page.wait_for_timeout(1000)
    page.screenshot(path=str(run_dir / f"after_attach_{file_path.name}.png"), full_page=True)
    page.get_by_text("Загрузить", exact=True).click()
    page.wait_for_timeout(2000)
    page.screenshot(path=str(run_dir / f"after_click_{file_path.name}.png"), full_page=True)
    block_reason = detect_upload_block_reason(page.locator("body").inner_text())
    if block_reason:
        return {
            "file": str(file_path),
            "started_at": start_dt.isoformat(timespec="minutes"),
            "before_page": before,
            "status": "blocked",
            "block_reason": block_reason,
        }

    deadline = time.time() + max(timeout_seconds, 30)
    found: HistoryRow | None = None
    deadline_extended = False
    while time.time() < deadline:
        page.goto(HISTORY_URL, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(3000)
        rows = _parse_history_rows(page)
        found = select_latest_matching_history_row(rows, file_path.name, after_dt=start_dt)
        if found and history_row_is_success(found):
            detail_metrics: dict[str, int] = {}
            detail_url = detail_url_from_history_row(found)
            if detail_url:
                try:
                    page.goto(detail_url, wait_until="domcontentloaded", timeout=60000)
                    page.wait_for_timeout(3000)
                    page.screenshot(path=str(run_dir / f"history_success_{file_path.name}.png"), full_page=True)
                    detail_metrics = _extract_detail_metrics(page)
                except PlaywrightTimeoutError:
                    detail_metrics = {}
            return {
                "file": str(file_path),
                "started_at": start_dt.isoformat(timespec="minutes"),
                "before_page": before,
                "history_row": history_row_to_payload(found),
                "detail_metrics": detail_metrics,
                "status": "success",
            }
        if found and history_row_is_terminal_failure(found):
            detail_error = ""
            detail_metrics: dict[str, int] = {}
            detail_url = detail_url_from_history_row(found)
            if detail_url:
                try:
                    page.goto(detail_url, wait_until="domcontentloaded", timeout=60000)
                    page.wait_for_timeout(3000)
                    page.screenshot(path=str(run_dir / f"history_error_{file_path.name}.png"), full_page=True)
                    detail_error = _extract_detail_error_text(page)
                    detail_metrics = _extract_detail_metrics(page)
                except PlaywrightTimeoutError:
                    detail_error = "failed_to_open_history_detail"
            return {
                "file": str(file_path),
                "started_at": start_dt.isoformat(timespec="minutes"),
                "before_page": before,
                "history_row": history_row_to_payload(found),
                "detail_error": detail_error,
                "detail_metrics": detail_metrics,
                "status": "failed",
            }
        if found:
            page.screenshot(path=str(run_dir / f"history_seen_{file_path.name}.png"), full_page=True)
            if should_extend_history_poll_deadline(found, already_extended=deadline_extended):
                deadline = max(deadline, time.time() + max(processing_grace_seconds, 60))
                deadline_extended = True
        time.sleep(5)

    return {
        "file": str(file_path),
        "started_at": start_dt.isoformat(timespec="minutes"),
        "before_page": before,
        "history_row": history_row_to_payload(found) if found else {},
        "status": "timeout",
    }


def run_kaspi_pricelist_upload(
    *,
    store_name: str,
    email: str,
    password: str,
    file_paths: list[Path],
    run_dir: Path,
    headless: bool,
    timeout_seconds: int = 900,
    processing_grace_seconds: int = 900,
) -> dict[str, Any]:
    run_dir.mkdir(parents=True, exist_ok=True)
    summary: dict[str, Any] = {"run_dir": str(run_dir), "uploads": [], "status": "unknown"}
    with sync_playwright() as p:
        browser = p.chromium.launch(**merchant_browser_launch_kwargs(headless=headless))
        page = browser.new_page(viewport={"width": 1440, "height": 1400})
        login_kaspi_merchant(page, email=email, password=password)
        page.screenshot(path=str(run_dir / "logged_in_home.png"), full_page=True)
        for file_path in file_paths:
            result = _upload_one_file(
                page,
                file_path=file_path,
                run_dir=run_dir,
                timeout_seconds=timeout_seconds,
                processing_grace_seconds=processing_grace_seconds,
            )
            summary["uploads"].append(result)
            if result["status"] != "success":
                summary["status"] = "failed"
                summary["store_name"] = normalize_store_name(store_name)
                browser.close()
                return summary
        browser.close()
    summary["status"] = "success"
    summary["store_name"] = normalize_store_name(store_name)
    return summary


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--env-file", type=Path, default=Path("~/Docs/Autonomous_business/.env"))
    parser.add_argument("--store", default="STORE-B")
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--file", action="append", dest="files", required=True)
    parser.add_argument("--headless", action="store_true")
    parser.add_argument("--timeout-seconds", type=int, default=900)
    parser.add_argument("--processing-grace-seconds", type=int, default=900)
    args = parser.parse_args()

    creds = resolve_store_credentials(args.store, args.env_file)

    summary = run_kaspi_pricelist_upload(
        store_name=creds["store_name"],
        email=creds["email"],
        password=creds["password"],
        file_paths=resolve_upload_file_paths(files=args.files),
        run_dir=args.run_dir,
        headless=args.headless,
        timeout_seconds=args.timeout_seconds,
        processing_grace_seconds=args.processing_grace_seconds,
    )
    summary_path = args.run_dir / "summary.json"
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if summary["status"] == "success" else 4


if __name__ == "__main__":
    raise SystemExit(main())
