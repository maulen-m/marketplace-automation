from __future__ import annotations

import argparse
import json
import time
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from playwright.sync_api import Locator, Page, sync_playwright

from .kaspi_merchant_common import (
    login_kaspi_merchant,
    merchant_browser_launch_kwargs,
    normalize_store_name,
    resolve_store_credentials,
)


ASTANA_TZ = ZoneInfo("Asia/Almaty")
DEFAULT_ENV_FILE = Path("~/Docs/Autonomous_business/.env")
PENDING_TRASH_HASH = "#/products/pending/TRASH/1"
PENDING_TRASH_URL = f"https://kaspi.kz/mc/{PENDING_TRASH_HASH}"
PENDING_COUNT_URL_TEMPLATE = "https://mc.shop.kaspi.kz/content/pending/mc/product/{merchant_code}/count"
PENDING_TRASH_LIST_URL_TEMPLATE = "https://mc.shop.kaspi.kz/content/pending/mc/product/trash/{merchant_code}"
PENDING_TRASH_DISPUTE_URL_TEMPLATE = "https://mc.shop.kaspi.kz/content/pending/mc/product/trash/dispute?merchantCode={merchant_code}"
DEFAULT_PAGE_SIZE = 100
DEFAULT_VERIFY_TIMEOUT_SECONDS = 90
DEFAULT_VERIFY_POLL_SECONDS = 5
FIXED_DISPUTE_COMMENT = "ACMEWEAR наш собственный бренд. Регистрация товарного знака в завершающей стадии."
KNOWN_SKIP_CODE = "30137883#CL_OF_ARC_WM_LINE31_C-014_MISTY-BLUE_ST_2XL"
KNOWN_SKIP_REASON = "FAKE_PRODUCT"
FORBIDDEN_SET_REASON = "FORBIDDEN_SET"
VISIBLE_CHECK_CODES_LIMIT = 2
DEFAULT_STORE_MERCHANT_CODES = {
    "ACMEWEAR": "30137883",
}


def now_local() -> datetime:
    return datetime.now(ASTANA_TZ)


def default_run_dir(store_name: str) -> Path:
    stamp = now_local().strftime("%Y%m%d_%H%M%S")
    safe_store = normalize_store_name(store_name).lower().replace("-", "")
    return Path("runs") / "kaspi_pending_trash_dispute" / f"{stamp}_{safe_store}"


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


def _append_jsonl(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(payload, ensure_ascii=False, default=str))
        fh.write("\n")


def _trim_excerpt(value: Any, *, limit: int = 500) -> str:
    text = " ".join(str(value or "").split())
    return text[:limit]


def validate_dispute_comment(comment: str) -> str:
    text = " ".join(str(comment or "").split())
    if not text:
        raise ValueError("dispute comment must not be empty")
    if len(text) > 500:
        raise ValueError(f"dispute comment exceeds 500 characters: {len(text)}")
    return text


def resolve_pending_merchant_code(store_name: str, merchant_code: str | None = None) -> str:
    override = str(merchant_code or "").strip()
    if override:
        return override
    store = normalize_store_name(store_name)
    resolved = DEFAULT_STORE_MERCHANT_CODES.get(store, "")
    if not resolved:
        raise ValueError(f"merchant code override is required for store {store}")
    return resolved


def build_trash_fetch_payload(*, page: int, page_size: int, search_term: str = "") -> dict[str, Any]:
    return {
        "page": int(page),
        "searchTerm": str(search_term or ""),
        "pageSize": int(page_size),
        "approvalStatus": "TRASH",
        "type": "UNSUITABLE_PRODUCT",
    }


def build_dispute_payload(*, trash_product_code: str, comment: str) -> dict[str, str]:
    return {
        "trashProductCode": str(trash_product_code or "").strip(),
        "comment": validate_dispute_comment(comment),
    }


def reason_code(row: dict[str, Any]) -> str:
    return str((row.get("reason") or {}).get("code") or "").strip()


def merchant_product_code(row: dict[str, Any]) -> str:
    raw_code = str(row.get("originalCode") or row.get("code") or "").strip()
    if raw_code.startswith("30137883#"):
        return raw_code.split("#", 1)[1]
    return raw_code


def count_by_reason(rows: list[dict[str, Any]]) -> dict[str, int]:
    counts = Counter(reason_code(row) or "UNKNOWN" for row in rows)
    return dict(sorted(counts.items()))


def partition_trash_rows(rows: list[dict[str, Any]]) -> dict[str, Any]:
    actionable: list[dict[str, Any]] = []
    already_disputed: list[dict[str, Any]] = []
    known_skips: list[dict[str, Any]] = []
    unexpected: list[dict[str, Any]] = []

    for row in rows:
        code = str(row.get("code") or "").strip()
        reason = reason_code(row)
        dispute_exists = bool(row.get("disputeExists"))
        if reason == FORBIDDEN_SET_REASON:
            if dispute_exists:
                already_disputed.append(row)
            else:
                actionable.append(row)
            continue
        if code == KNOWN_SKIP_CODE and reason == KNOWN_SKIP_REASON and dispute_exists:
            known_skips.append(row)
            continue
        unexpected.append(row)

    return {
        "actionable": actionable,
        "already_disputed": already_disputed,
        "known_skips": known_skips,
        "unexpected": unexpected,
    }


def verify_submitted_codes(initial_actionable_codes: list[str], final_rows: list[dict[str, Any]]) -> dict[str, Any]:
    final_by_code = {str(row.get("code") or "").strip(): row for row in final_rows}
    absent_codes: list[str] = []
    present_disputed_codes: list[str] = []
    present_without_dispute_codes: list[str] = []

    for code in initial_actionable_codes:
        row = final_by_code.get(code)
        if row is None:
            absent_codes.append(code)
            continue
        if bool(row.get("disputeExists")):
            present_disputed_codes.append(code)
            continue
        present_without_dispute_codes.append(code)

    return {
        "ok": len(present_without_dispute_codes) == 0,
        "initial_actionable_count": len(initial_actionable_codes),
        "absent_count": len(absent_codes),
        "present_disputed_count": len(present_disputed_codes),
        "remaining_actionable_count": len(present_without_dispute_codes),
        "absent_codes": absent_codes,
        "present_disputed_codes": present_disputed_codes,
        "remaining_actionable_codes": present_without_dispute_codes,
    }


def pick_ui_check_codes(submitted_codes: list[str]) -> list[str]:
    unique_codes: list[str] = []
    for code in submitted_codes:
        if code and code not in unique_codes:
            unique_codes.append(code)
    if len(unique_codes) <= VISIBLE_CHECK_CODES_LIMIT:
        return unique_codes
    return [unique_codes[0], unique_codes[-1]]


def _fetch_text_response(page: Page, *, url: str, method: str = "GET", payload: dict[str, Any] | None = None) -> dict[str, Any]:
    raw = page.evaluate(
        """async ({ url, method, payload }) => {
            const init = { method, credentials: 'include', headers: {} };
            if (payload !== null) {
              init.headers['content-type'] = 'application/json';
              init.body = JSON.stringify(payload);
            }
            const response = await fetch(url, init);
            return {
              status: response.status,
              text: await response.text(),
            };
        }""",
        {
            "url": url,
            "method": str(method or "GET").upper(),
            "payload": payload,
        },
    )
    status = int(raw.get("status", 0) or 0)
    text = str(raw.get("text", "") or "")
    parsed: Any
    try:
        parsed = json.loads(text) if text else {}
    except Exception:
        parsed = {}
    return {
        "status": status,
        "text": text,
        "json": parsed,
    }


def _open_pending_trash_page(page: Page) -> None:
    page.evaluate(f"window.location.hash = '{PENDING_TRASH_HASH}'")
    page.wait_for_timeout(3000)
    page.wait_for_function(
        "() => document.body && document.body.innerText.includes('Нераспознанные товары') && document.body.innerText.includes('Отклонены')",
        timeout=30000,
    )


def fetch_pending_counts(page: Page, *, merchant_code: str) -> dict[str, Any]:
    return _fetch_text_response(page, url=PENDING_COUNT_URL_TEMPLATE.format(merchant_code=merchant_code), method="GET")


def fetch_trash_page(page: Page, *, merchant_code: str, page_number: int, page_size: int, search_term: str = "") -> dict[str, Any]:
    payload = build_trash_fetch_payload(page=page_number, page_size=page_size, search_term=search_term)
    return _fetch_text_response(
        page,
        url=PENDING_TRASH_LIST_URL_TEMPLATE.format(merchant_code=merchant_code),
        method="POST",
        payload=payload,
    )


def fetch_all_trash_rows(page: Page, *, merchant_code: str, page_size: int) -> dict[str, Any]:
    first = fetch_trash_page(page, merchant_code=merchant_code, page_number=1, page_size=page_size)
    if first["status"] != 200:
        raise RuntimeError(f"trash list fetch failed with status {first['status']}")
    payload = dict(first.get("json") or {})
    rows = list(payload.get("data") or [])
    page_count = int(payload.get("pageCount", 1) or 1)
    pages = [
        {
            "page": 1,
            "status": first["status"],
            "row_count": len(payload.get("data") or []),
        }
    ]
    for page_number in range(2, page_count + 1):
        current = fetch_trash_page(page, merchant_code=merchant_code, page_number=page_number, page_size=page_size)
        if current["status"] != 200:
            raise RuntimeError(f"trash list fetch page {page_number} failed with status {current['status']}")
        current_payload = dict(current.get("json") or {})
        current_rows = list(current_payload.get("data") or [])
        rows.extend(current_rows)
        pages.append(
            {
                "page": page_number,
                "status": current["status"],
                "row_count": len(current_rows),
            }
        )
    return {
        "page_count": page_count,
        "rows": rows,
        "pages": pages,
    }


def submit_dispute(page: Page, *, merchant_code: str, trash_product_code: str, comment: str) -> dict[str, Any]:
    payload = build_dispute_payload(trash_product_code=trash_product_code, comment=comment)
    return _fetch_text_response(
        page,
        url=PENDING_TRASH_DISPUTE_URL_TEMPLATE.format(merchant_code=merchant_code),
        method="POST",
        payload=payload,
    )


def _visible_locator_count(locator: Locator) -> int:
    visible = 0
    for item in locator.all():
        try:
            if item.is_visible():
                visible += 1
        except Exception:
            continue
    return visible


def _visible_search_input(page: Page) -> Locator:
    candidates = page.locator('input[data-testid="search_input"]').all()
    for locator in candidates:
        try:
            if locator.is_visible():
                return locator
        except Exception:
            continue
    raise RuntimeError("visible search input not found on pending trash page")


def run_ui_spot_check(page: Page, *, product_code: str, screenshot_path: Path) -> dict[str, Any]:
    _open_pending_trash_page(page)
    search = _visible_search_input(page)
    search.fill(str(product_code or "").strip())
    page.get_by_role("button", name="Поиск").click()
    page.wait_for_timeout(2500)
    body = page.locator("body").inner_text()
    found_in_table = str(product_code or "").strip() in body
    detail_button_count = _visible_locator_count(page.get_by_text("Детали уточнения", exact=True))
    result: dict[str, Any] = {
        "product_code": str(product_code or "").strip(),
        "found_in_table": found_in_table,
        "detail_button_count": detail_button_count,
        "repeat_button_count": None,
        "status": "unknown",
    }
    if detail_button_count < 1:
        result["status"] = "pass_absent" if not found_in_table else "fail_missing_detail_button"
        page.screenshot(path=str(screenshot_path), full_page=True)
        return result
    page.get_by_text("Детали уточнения", exact=True).first.click()
    page.wait_for_timeout(2000)
    repeat_button_count = _visible_locator_count(page.get_by_text("Добавить товар повторно", exact=True))
    result["repeat_button_count"] = repeat_button_count
    result["status"] = "pass" if repeat_button_count == 0 else "fail_repeat_button_visible"
    page.screenshot(path=str(screenshot_path), full_page=True)
    return result


def run_kaspi_pending_trash_dispute(
    *,
    store_name: str,
    email: str,
    password: str,
    merchant_code: str,
    run_dir: Path,
    comment: str,
    confirm: bool,
    headless: bool,
    page_size: int = DEFAULT_PAGE_SIZE,
    verify_timeout_seconds: int = DEFAULT_VERIFY_TIMEOUT_SECONDS,
    verify_poll_seconds: int = DEFAULT_VERIFY_POLL_SECONDS,
    ui_verify: bool = True,
) -> dict[str, Any]:
    run_dir.mkdir(parents=True, exist_ok=True)
    raw_dir = run_dir / "raw"
    screenshots_dir = run_dir / "screenshots"
    summary: dict[str, Any] = {
        "run_dir": str(run_dir),
        "store_name": normalize_store_name(store_name),
        "merchant_code": str(merchant_code),
        "pending_trash_url": PENDING_TRASH_URL,
        "comment": validate_dispute_comment(comment),
        "confirm": bool(confirm),
        "headless": bool(headless),
        "page_size": int(page_size),
        "started_at": now_local().isoformat(),
        "status": "unknown",
    }
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(**merchant_browser_launch_kwargs(headless=headless))
            page = browser.new_page(viewport={"width": 1440, "height": 1600})
            login_kaspi_merchant(page, email=email, password=password)
            page.screenshot(path=str(screenshots_dir / "logged_in_home.png"), full_page=True)
            _open_pending_trash_page(page)

            counts_response = fetch_pending_counts(page, merchant_code=merchant_code)
            if counts_response["status"] != 200:
                raise RuntimeError(f"pending count fetch failed with status {counts_response['status']}")
            _write_json(raw_dir / "initial_counts.json", counts_response.get("json") or {})

            initial_fetch = fetch_all_trash_rows(page, merchant_code=merchant_code, page_size=page_size)
            initial_rows = list(initial_fetch["rows"])
            partition = partition_trash_rows(initial_rows)
            actionable_rows = list(partition["actionable"])
            already_disputed_rows = list(partition["already_disputed"])
            known_skip_rows = list(partition["known_skips"])
            unexpected_rows = list(partition["unexpected"])

            _write_json(raw_dir / "initial_trash_rows.json", initial_rows)
            _write_json(
                raw_dir / "initial_partition.json",
                {
                    "reason_counts": count_by_reason(initial_rows),
                    "actionable_codes": [row.get("code") for row in actionable_rows],
                    "already_disputed_codes": [row.get("code") for row in already_disputed_rows],
                    "known_skip_codes": [row.get("code") for row in known_skip_rows],
                    "unexpected_codes": [row.get("code") for row in unexpected_rows],
                },
            )

            summary["preflight"] = {
                "counts": counts_response.get("json") or {},
                "page_count": initial_fetch["page_count"],
                "rows_total": len(initial_rows),
                "reason_counts": count_by_reason(initial_rows),
                "actionable_count": len(actionable_rows),
                "already_disputed_count": len(already_disputed_rows),
                "known_skip_count": len(known_skip_rows),
                "unexpected_count": len(unexpected_rows),
            }
            summary["known_skip_rows"] = [
                {
                    "code": row.get("code"),
                    "originalCode": row.get("originalCode"),
                    "reason": reason_code(row),
                    "disputeExists": bool(row.get("disputeExists")),
                }
                for row in known_skip_rows
            ]

            if unexpected_rows:
                summary["unexpected_rows"] = [
                    {
                        "code": row.get("code"),
                        "originalCode": row.get("originalCode"),
                        "reason": reason_code(row),
                        "disputeExists": bool(row.get("disputeExists")),
                    }
                    for row in unexpected_rows
                ]
                summary["status"] = "failed_preflight"
                return summary

            if not confirm:
                summary["status"] = "dry_run"
                return summary

            if not actionable_rows:
                summary["status"] = "nothing_to_do"
                return summary

            submissions_log = run_dir / "submit_results.jsonl"
            submitted_codes: list[str] = []
            submission_results: list[dict[str, Any]] = []

            for row in actionable_rows:
                trash_code = str(row.get("code") or "").strip()
                product_code = merchant_product_code(row)
                row_result: dict[str, Any] = {
                    "trashProductCode": trash_code,
                    "merchantProductCode": product_code,
                    "reason": reason_code(row),
                    "attempts": [],
                    "timestamp": now_local().isoformat(),
                }
                final_attempt: dict[str, Any] | None = None
                for attempt_no in (1, 2):
                    attempt_response = submit_dispute(
                        page,
                        merchant_code=merchant_code,
                        trash_product_code=trash_code,
                        comment=summary["comment"],
                    )
                    attempt_payload = {
                        "attempt": attempt_no,
                        "status": attempt_response["status"],
                        "response_excerpt": _trim_excerpt(attempt_response["text"]),
                    }
                    row_result["attempts"].append(attempt_payload)
                    final_attempt = attempt_payload
                    if 200 <= attempt_response["status"] < 300:
                        submitted_codes.append(trash_code)
                        row_result["status"] = "submitted"
                        break
                    if attempt_response["status"] in {401, 403}:
                        row_result["status"] = "failed_auth"
                        break
                    if attempt_response["status"] >= 500 and attempt_no == 1:
                        time.sleep(1)
                        continue
                    row_result["status"] = "failed_submit"
                    break

                row_result["final_status"] = row_result.get("status", "failed_submit")
                row_result["final_http_status"] = int((final_attempt or {}).get("status") or 0)
                submission_results.append(row_result)
                _append_jsonl(submissions_log, row_result)

                if row_result["final_status"] != "submitted":
                    summary["submit_results"] = submission_results
                    summary["status"] = row_result["final_status"]
                    return summary

            deadline = time.time() + max(int(verify_timeout_seconds), 5)
            final_rows: list[dict[str, Any]] = []
            verification: dict[str, Any] = {}
            while True:
                final_fetch = fetch_all_trash_rows(page, merchant_code=merchant_code, page_size=page_size)
                final_rows = list(final_fetch["rows"])
                verification = verify_submitted_codes(
                    [str(row.get("code") or "").strip() for row in actionable_rows],
                    final_rows,
                )
                verification["page_count"] = final_fetch["page_count"]
                verification["final_reason_counts"] = count_by_reason(final_rows)
                if verification["ok"] or time.time() >= deadline:
                    break
                time.sleep(max(int(verify_poll_seconds), 1))

            _write_json(raw_dir / "final_trash_rows.json", final_rows)
            summary["submit_results"] = submission_results
            summary["verify"] = verification

            ui_checks: list[dict[str, Any]] = []
            if ui_verify and verification.get("ok"):
                for idx, trash_code in enumerate(pick_ui_check_codes(submitted_codes), start=1):
                    product_code = merchant_product_code({"code": trash_code})
                    ui_checks.append(
                        run_ui_spot_check(
                            page,
                            product_code=product_code,
                            screenshot_path=screenshots_dir / f"ui_check_{idx}_{product_code}.png",
                        )
                    )
            summary["ui_checks"] = ui_checks

            if not verification.get("ok"):
                summary["status"] = "failed_verify"
            elif any(not str(check.get("status") or "").startswith("pass") for check in ui_checks):
                summary["status"] = "failed_ui_verify"
            else:
                summary["status"] = "success"
            browser.close()
            return summary
    finally:
        summary["finished_at"] = now_local().isoformat()
        _write_json(run_dir / "summary.json", summary)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Replay Kaspi pending-trash disputes for rejected offers")
    parser.add_argument("--env-file", type=Path, default=DEFAULT_ENV_FILE)
    parser.add_argument("--store", default="ACMEWEAR")
    parser.add_argument("--merchant-code", default="")
    parser.add_argument("--run-dir", type=Path)
    parser.add_argument("--comment", default=FIXED_DISPUTE_COMMENT)
    parser.add_argument("--page-size", type=int, default=DEFAULT_PAGE_SIZE)
    parser.add_argument("--verify-timeout-seconds", type=int, default=DEFAULT_VERIFY_TIMEOUT_SECONDS)
    parser.add_argument("--verify-poll-seconds", type=int, default=DEFAULT_VERIFY_POLL_SECONDS)
    parser.add_argument("--confirm", action="store_true")
    parser.add_argument("--headless", action="store_true")
    parser.add_argument("--headed", action="store_true")
    parser.add_argument("--skip-ui-verify", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    headless = True
    if args.headed:
        headless = False
    if args.headless:
        headless = True

    creds = resolve_store_credentials(args.store, args.env_file)
    merchant_code = resolve_pending_merchant_code(creds["store_name"], args.merchant_code)
    run_dir = args.run_dir or default_run_dir(creds["store_name"])

    summary = run_kaspi_pending_trash_dispute(
        store_name=creds["store_name"],
        email=creds["email"],
        password=creds["password"],
        merchant_code=merchant_code,
        run_dir=run_dir,
        comment=args.comment,
        confirm=args.confirm,
        headless=headless,
        page_size=args.page_size,
        verify_timeout_seconds=args.verify_timeout_seconds,
        verify_poll_seconds=args.verify_poll_seconds,
        ui_verify=not args.skip_ui_verify,
    )
    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    else:
        print(f"status={summary.get('status')} run_dir={summary.get('run_dir')}")
    return 0 if summary.get("status") in {"success", "dry_run", "nothing_to_do"} else 4


if __name__ == "__main__":
    raise SystemExit(main())
