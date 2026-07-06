from __future__ import annotations

import argparse
import csv
import json
import re
import signal
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from playwright.sync_api import Locator, Page, sync_playwright

from .kaspi_merchant_common import login_kaspi_merchant, resolve_store_credentials
from .kaspi_offer_ui_upload import (
    DEFAULT_ENTRY_URL,
    _merchant_id_js,
    _normalize_barcode,
    _normalize_size_rus,
    _product_code_for_search,
    _step_choose_card_js,
    _step_close_stale_success_modal_js,
    _step_detect_price_warning_js,
    _step_fill_identity_fields_js,
    _step_probe_search_input_js,
    _step_search_js,
    _step_select_size_js,
    _step_verify_offer_identity_js,
    _step_verify_selected_size_js,
    _step_verify_success_js,
    load_offer_upload_rows,
    normalize_store_code,
    validate_upload_rows,
)
from .kaspi_price_floors import write_price_floor_clamp_report

ROOT = Path(__file__).resolve().parents[1]
ASTANA_TZ = ZoneInfo("Asia/Almaty")
DEFAULT_ENV_FILE = Path("~/Docs/Autonomous_business/.env")


class RowExecutionTimeout(RuntimeError):
    pass


def parse_step_result(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    text = str(raw or "").strip()
    if not text:
        return {}
    try:
        return json.loads(text)
    except Exception:
        return {"ok": False, "reason": text}


def modal_input_slot_plan(input_count: int) -> dict[str, int | None]:
    if input_count < 2:
        return {"price": None, "pp1": None, "pp2": None, "barcode": None}
    if input_count == 2:
        return {"price": 0, "pp1": 1, "pp2": None, "barcode": None}
    if input_count == 3:
        return {"price": 0, "pp1": 1, "pp2": None, "barcode": 2}
    return {"price": 0, "pp1": 1, "pp2": 2, "barcode": 3}


def digits_only(value: Any) -> str:
    if value is None:
        return ""
    return re.sub(r"\D", "", str(value))


def build_browser_launch_kwargs(*, headless: bool) -> dict[str, Any]:
    return {"channel": "chrome", "headless": bool(headless), "slow_mo": 150}


def format_row_exception(exc: Exception) -> str:
    return f"exception:{exc.__class__.__name__}:{exc}"


class _RowTimeoutGuard:
    def __init__(self, seconds: int) -> None:
        self.seconds = max(int(seconds), 0)
        self._enabled = hasattr(signal, "SIGALRM") and self.seconds > 0
        self._previous = None

    def _handle_timeout(self, _signum, _frame) -> None:
        raise RowExecutionTimeout(f"row_execution_timeout_{self.seconds}s")

    def __enter__(self) -> "_RowTimeoutGuard":
        if self._enabled:
            self._previous = signal.getsignal(signal.SIGALRM)
            signal.signal(signal.SIGALRM, self._handle_timeout)
            signal.setitimer(signal.ITIMER_REAL, self.seconds)
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        if self._enabled:
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, self._previous)
        return False


def read_locator_value(locator: Locator) -> str:
    try:
        return locator.input_value() or ""
    except Exception:
        try:
            return str(locator.evaluate("(el) => el && 'value' in el ? el.value : ''") or "")
        except Exception:
            return ""


def success_state_from_page(page: Page) -> dict[str, Any]:
    try:
        return parse_step_result(page.evaluate(_step_verify_success_js()))
    except Exception as exc:
        return {"ok": False, "reason": f"success_probe_failed:{exc.__class__.__name__}"}


def _click_price_required_popup(page: Page) -> bool:
    try:
        button = page.get_by_role("button", name=re.compile("Указать цену", re.I))
        if button.count() > 0:
            button.first.click(timeout=2500)
            return True
    except Exception:
        pass
    try:
        close_button = page.get_by_role("button", name=re.compile("Закрыть|Close", re.I))
        if close_button.count() > 0:
            close_button.first.click(timeout=2500)
            return True
    except Exception:
        pass
    return False


def _close_stale_success_modal(page: Page) -> dict[str, Any]:
    try:
        return parse_step_result(page.evaluate(_step_close_stale_success_modal_js()))
    except Exception as exc:
        return {"ok": False, "reason": f"close_success_failed:{exc.__class__.__name__}"}


def _capture_page_screenshot(page: Page, out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        page.screenshot(path=str(out_path), full_page=True)
    except Exception:
        return


def _append_run_log(path: Path, row: list[Any]) -> None:
    with path.open("a", newline="", encoding="utf-8") as fh:
        wr = csv.writer(fh)
        wr.writerow(row)


def _init_run_log(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        wr = csv.writer(fh)
        wr.writerow(
            [
                "timestamp",
                "store_code",
                "merchant_id",
                "row_number",
                "merchant_sku_article",
                "variant_url",
                "status",
                "error",
            ]
        )


def _set_locator_value(page: Page, locator: Locator, value: str, *, typing_delay_ms: int = 120) -> None:
    target = str(value)

    def is_applied() -> bool:
        current = read_locator_value(locator)
        if current == target:
            return True
        if digits_only(target):
            return digits_only(current) == digits_only(target)
        return current.strip() == target.strip()

    try:
        locator.click(timeout=2500)
    except Exception:
        try:
            locator.click(force=True, timeout=2500)
        except Exception:
            try:
                locator.evaluate(
                    """(el) => {
                      el.focus();
                      if (el.select) el.select();
                    }"""
                )
            except Exception:
                pass
    try:
        locator.fill(target, timeout=2500)
    except Exception:
        pass
    if is_applied():
        return
    try:
        locator.evaluate(
            """(el, nextValue) => {
              const proto = el instanceof HTMLTextAreaElement
                ? HTMLTextAreaElement.prototype
                : HTMLInputElement.prototype;
              const setter = Object.getOwnPropertyDescriptor(proto, 'value')?.set;
              if (setter) {
                setter.call(el, nextValue);
              } else {
                el.value = nextValue;
              }
              el.dispatchEvent(new Event('input', { bubbles: true }));
              el.dispatchEvent(new Event('change', { bubbles: true }));
              if (el.blur) el.blur();
            }""",
            target,
        )
    except Exception:
        pass
    if is_applied():
        return
    try:
        locator.press("Meta+A", timeout=1500)
    except Exception:
        try:
            page.keyboard.press("Meta+A")
        except Exception:
            pass
    try:
        locator.press("Backspace", timeout=1500)
    except Exception:
        try:
            page.keyboard.press("Backspace")
        except Exception:
            pass
    try:
        locator.press_sequentially(target, delay=typing_delay_ms, timeout=2500)
    except Exception:
        try:
            locator.type(target, delay=typing_delay_ms, timeout=2500)
        except Exception:
            page.keyboard.type(target, delay=typing_delay_ms)
    try:
        locator.blur(timeout=1500)
    except Exception:
        try:
            locator.evaluate("(el) => el.blur && el.blur()")
        except Exception:
            pass


def _type_price_locator_value(page: Page, locator: Locator, value: str, *, typing_delay_ms: int = 180) -> None:
    target = str(value)
    for _ in range(3):
        try:
            locator.click(timeout=2500)
        except Exception:
            locator.click(force=True, timeout=2500)
        try:
            locator.fill("", timeout=2500)
        except Exception:
            try:
                locator.press("Meta+A", timeout=1500)
                locator.press("Backspace", timeout=1500)
            except Exception:
                pass
        try:
            locator.press_sequentially(target, delay=typing_delay_ms, timeout=4000)
        except Exception:
            try:
                locator.type(target, delay=typing_delay_ms, timeout=4000)
            except Exception:
                page.keyboard.type(target, delay=typing_delay_ms)
        try:
            locator.press("Tab", timeout=1500)
        except Exception:
            try:
                locator.blur(timeout=1500)
            except Exception:
                pass
        page.wait_for_timeout(500)
        if digits_only(read_locator_value(locator)) == digits_only(target):
            return
    _set_locator_value(page, locator, target, typing_delay_ms=typing_delay_ms)


def _input_context(locator: Locator) -> str:
    try:
        return str(
            locator.evaluate(
                """(el) => {
                  const parts = [
                    el.placeholder,
                    el.name,
                    el.id,
                    el.getAttribute && el.getAttribute('aria-label'),
                    el.getAttribute && el.getAttribute('data-testid'),
                  ];
                  let p = el;
                  for (let i = 0; i < 5 && p; i++) {
                    parts.push(p.innerText || '');
                    p = p.parentElement;
                  }
                  return parts.join('\\n');
                }"""
            )
            or ""
        )
    except Exception:
        return ""


def _input_y(locator: Locator) -> float:
    try:
        return float(locator.evaluate("(el) => el.getBoundingClientRect().y") or 0)
    except Exception:
        return 0.0


def _price_modal_value_snapshot(page: Page) -> dict[str, Any]:
    try:
        return dict(
            page.evaluate(
                """() => {
                  const visible = (el) => {
                    const rect = el.getBoundingClientRect();
                    const style = window.getComputedStyle(el);
                    return rect.width > 0 && rect.height > 0 &&
                      style.visibility !== 'hidden' &&
                      style.display !== 'none';
                  };
                  const context = (el) => {
                    const parts = [
                      el.placeholder,
                      el.name,
                      el.id,
                      el.getAttribute('aria-label'),
                      el.getAttribute('data-testid'),
                    ];
                    let p = el;
                    for (let i = 0; i < 5 && p; i++) {
                      parts.push(p.innerText || '');
                      p = p.parentElement;
                    }
                    return parts.join('\\n').toLowerCase();
                  };
                  const inputs = Array.from(document.querySelectorAll('input'))
                    .filter(visible)
                    .map((el, idx) => {
                      const rect = el.getBoundingClientRect();
                      return {
                        idx,
                        value: el.value || '',
                        dataTestid: (el.getAttribute('data-testid') || '').toLowerCase(),
                        inputmode: (el.getAttribute('inputmode') || '').toLowerCase(),
                        context: context(el),
                        y: rect.y,
                      };
                    });
                  const stocks = inputs
                    .filter((item) => item.dataTestid === 'edit-stock-input' ||
                      (item.context.includes('остатки') && !item.context.includes('артикул')))
                    .sort((a, b) => a.y - b.y);
                  let prices = inputs
                    .filter((item) => !stocks.includes(item) &&
                      item.inputmode === 'numeric' &&
                      !item.context.includes('артикул') &&
                      !item.context.includes('наименование'));
                  if (!prices.length) {
                    prices = inputs.filter((item) => !stocks.includes(item) &&
                      item.context.includes('цена') &&
                      !item.context.includes('артикул') &&
                      !item.context.includes('наименование'));
                  }
                  if (stocks.length) {
                    const firstStockY = stocks[0].y;
                    const aboveStock = prices.filter((item) => item.y < firstStockY);
                    if (aboveStock.length) prices = aboveStock;
                  }
                  prices.sort((a, b) => a.y - b.y);
                  return {
                    inputs,
                    price: prices[0] ? prices[0].value : '',
                    pp1: stocks[0] ? stocks[0].value : '',
                    pp2: stocks[1] ? stocks[1].value : '',
                  };
                }"""
            )
            or {}
        )
    except Exception as exc:
        return {"error": f"{exc.__class__.__name__}:{exc}"}


def _fill_price_modal_with_playwright(
    page: Page,
    *,
    price: Any,
    pp1: Any,
    pp2: Any,
    barcode: str,
) -> tuple[bool, str]:
    dialogs = page.locator('[role="dialog"], .modal, .modal-content, .v-dialog, .dialog').filter(has_text="Цена и остатки")
    if dialogs.count() < 1:
        return False, "price_modal_not_found"

    def select_price_modal_inputs() -> tuple[Locator | None, Locator | None, Locator | None, int]:
        inputs = [locator for locator in page.locator("input").all() if locator.is_visible()]
        infos: list[dict[str, Any]] = []
        for idx, locator in enumerate(inputs):
            context = _input_context(locator)
            context_lower = context.lower()
            data_testid = (locator.get_attribute("data-testid") or "").strip().lower()
            inputmode = (locator.get_attribute("inputmode") or "").strip().lower()
            infos.append(
                {
                    "idx": idx,
                    "locator": locator,
                    "context": context,
                    "context_lower": context_lower,
                    "data_testid": data_testid,
                    "inputmode": inputmode,
                    "y": _input_y(locator),
                }
            )

        # Kaspi can keep the product identity form visible behind the modal. Do not
        # infer "first non-stock input" as price; classify by the actual label text.
        stock_infos = [
            info
            for info in infos
            if info["data_testid"] == "edit-stock-input"
            or ("остатки" in info["context_lower"] and "артикул" not in info["context_lower"])
        ]
        stock_infos.sort(key=lambda info: info["y"])

        price_candidates = [
            info
            for info in infos
            if info not in stock_infos
            and info["inputmode"] == "numeric"
            and "артикул" not in info["context_lower"]
            and "наименование" not in info["context_lower"]
        ]
        price_label_infos = [
            info
            for info in infos
            if info not in stock_infos
            and "цена" in info["context_lower"]
            and "артикул" not in info["context_lower"]
            and "наименование" not in info["context_lower"]
        ]
        price_candidates.extend(info for info in price_label_infos if info not in price_candidates)
        if stock_infos:
            first_stock_y = stock_infos[0]["y"]
            above_stock = [info for info in price_candidates if info["y"] < first_stock_y]
            if above_stock:
                price_candidates = above_stock
        price_candidates.sort(key=lambda info: info["y"])

        price_input = price_candidates[0]["locator"] if price_candidates else None
        pp1_input = stock_infos[0]["locator"] if len(stock_infos) >= 1 else None
        pp2_input = stock_infos[1]["locator"] if len(stock_infos) >= 2 else None
        return price_input, pp1_input, pp2_input, len(inputs)

    price_input, pp1_input, pp2_input, input_count = select_price_modal_inputs()
    if price_input is None or pp1_input is None:
        return False, f"not_enough_inputs_for_price_stock:{input_count}"

    # Kaspi resets the price input when stock pickup-point values change.
    # Fill stocks first, then price last, and only then verify all fields.
    for attempt in range(3):
        _type_price_locator_value(page, pp1_input, str(pp1), typing_delay_ms=120)
        page.wait_for_timeout(400 + attempt * 250)
        if digits_only(read_locator_value(pp1_input)) == digits_only(pp1):
            break
    if pp2_input is not None:
        for attempt in range(3):
            _type_price_locator_value(page, pp2_input, str(pp2), typing_delay_ms=120)
            page.wait_for_timeout(400 + attempt * 250)
            if digits_only(read_locator_value(pp2_input)) == digits_only(pp2):
                break
    for attempt in range(3):
        _type_price_locator_value(page, price_input, str(price), typing_delay_ms=180)
        page.wait_for_timeout(500 + attempt * 250)
        if digits_only(read_locator_value(price_input)) == digits_only(price):
            break
    else:
        return False, "price_value_not_applied"
    page.wait_for_timeout(800)
    fresh_price_input, fresh_pp1_input, fresh_pp2_input, _ = select_price_modal_inputs()
    price_input = fresh_price_input or price_input
    pp1_input = fresh_pp1_input or pp1_input
    pp2_input = fresh_pp2_input or pp2_input
    snapshot = _price_modal_value_snapshot(page)
    price_digits = digits_only(snapshot.get("price") or read_locator_value(price_input))
    pp1_digits = digits_only(snapshot.get("pp1") or read_locator_value(pp1_input))
    pp2_digits = digits_only(snapshot.get("pp2") or (read_locator_value(pp2_input) if pp2_input is not None else ""))
    if price_digits != digits_only(price):
        return False, f"price_value_not_applied:{snapshot}"
    if pp1_digits != digits_only(pp1):
        return False, f"pp1_value_not_applied:{snapshot}"
    if pp2_input is not None and pp2_digits != digits_only(pp2):
        return False, f"pp2_value_not_applied:{snapshot}"
    save_btn = page.get_by_role("button", name="Сохранить изменения")
    if save_btn.count() < 1:
        return False, "save_button_not_found"
    save_btn.first.click()
    return True, "price_values_applied"


def _execute_row_pw(
    page: Page,
    row: dict[str, Any],
    *,
    step_delay_seconds: float,
    step_screenshot_dir: Path | None = None,
) -> tuple[bool, str]:
    expected_mid = str(int(float(str(row.get("merchant_id") or "0"))))
    normalized_barcode = _normalize_barcode(row.get("barcode"))
    normalized_size_rus = _normalize_size_rus(row.get("size_rus"))
    offer_code = _product_code_for_search(row)

    def has_size_like_option(values: Any) -> bool:
        for value in values or []:
            text = str(value or "").strip().upper()
            if re.search(r"\d", text):
                return True
            if re.search(r"\b(?:XS|S|M|L|XL|2XL|3XL|4XL|5XL|6XL)\b", text):
                return True
        return False

    def snap(stage: str) -> None:
        if not step_screenshot_dir:
            return
        row_no = int(row.get("row_number") or 0)
        article = re.sub(r"[^a-zA-Z0-9._-]+", "_", str(row.get("merchant_sku_article") or "")).strip("_")
        if not article:
            article = "no_article"
        _capture_page_screenshot(page, step_screenshot_dir / f"row{row_no:04d}_{stage}_{article[:80]}.png")

    entry_url = str(row.get("ui_entry_url") or DEFAULT_ENTRY_URL)
    direct_code_mode = ("link-catalog" in entry_url.lower()) and ("code=" in entry_url.lower())

    _close_stale_success_modal(page)
    page.wait_for_timeout(400)
    page.goto(entry_url, wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(int(max(step_delay_seconds, 1.2) * 1000))
    _close_stale_success_modal(page)
    page.wait_for_timeout(400)
    if "#/add-product/v2" not in page.url:
        page.goto(entry_url, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(int(max(step_delay_seconds, 1.2) * 1000))
    snap("01_open")

    mid_raw = page.evaluate(_merchant_id_js())
    actual_mid = str(mid_raw or "").strip()
    if actual_mid and actual_mid != expected_mid:
        snap("err_mid_mismatch")
        return False, f"merchant_id_mismatch page={actual_mid} expected={expected_mid}"

    if not direct_code_mode:
        search_ready = False
        for _ in range(12):
            probe = parse_step_result(page.evaluate(_step_probe_search_input_js()))
            if bool(probe.get("ready")):
                search_ready = True
                break
            page.wait_for_timeout(800)
        if not search_ready:
            snap("err_search")
            return False, "search_failed:search_input_not_found"

        res = parse_step_result(page.evaluate(_step_search_js(offer_code)))
        if not res.get("ok"):
            snap("err_search")
            return False, f"search_failed:{res.get('reason')}"
        page.wait_for_timeout(int(step_delay_seconds * 1000))
        snap("02_search")

        res = parse_step_result(
            page.evaluate(
                _step_choose_card_js(
                    str(row.get("expected_kaspi_heading") or ""),
                    offer_code,
                )
            )
        )
        if not res.get("ok"):
            snap("err_choose")
            return False, f"choose_card_failed:{res.get('reason')}"
        page.wait_for_timeout(int((step_delay_seconds + 0.6) * 1000))
        snap("03_choose")

        identity_check = parse_step_result(
            page.evaluate(
                _step_verify_offer_identity_js(
                    str(row.get("expected_kaspi_heading") or ""),
                    "",
                )
            )
        )
        if not identity_check.get("ok"):
            snap("err_identity")
            return False, f"identity_failed:{identity_check.get('reason')}"
    else:
        snap("02_direct_code_entry")

    size_ok = False
    size_reason = ""
    for attempt in range(4):
        res_select = parse_step_result(page.evaluate(_step_select_size_js(normalized_size_rus)))
        if not res_select.get("ok"):
            size_reason = str(res_select.get("reason") or "size_select_failed")
            if (
                direct_code_mode
                and size_reason == "size_option_not_found"
                and not has_size_like_option(res_select.get("available_sizes"))
            ):
                identity_check = parse_step_result(
                    page.evaluate(
                        _step_verify_offer_identity_js(
                            str(row.get("expected_kaspi_heading") or ""),
                            offer_code,
                        )
                    )
                )
                if identity_check.get("ok"):
                    size_ok = True
                    size_reason = "direct_code_no_size_options_identity_verified"
                    break
            snap("err_size_select")
            break
        page.wait_for_timeout(int((step_delay_seconds + 0.4) * 1000))
        res_verify = parse_step_result(page.evaluate(_step_verify_selected_size_js(normalized_size_rus)))
        if res_verify.get("ok"):
            size_ok = True
            break
        size_reason = str(res_verify.get("reason") or "size_not_selected")
        snap(f"04_size_retry{attempt+1}")
        page.wait_for_timeout(int((step_delay_seconds + 0.4) * 1000))
    if not size_ok:
        snap("err_fill_info")
        return False, f"fill_info_failed:{size_reason}"
    snap("04_size_selected")

    identity_check = parse_step_result(
        page.evaluate(
            _step_verify_offer_identity_js(
                str(row.get("expected_kaspi_heading") or ""),
                offer_code,
            )
        )
    )
    if not identity_check.get("ok"):
        snap("err_identity")
        return False, f"identity_failed:{identity_check.get('reason')}"

    res: dict[str, Any] = {"ok": False, "reason": "identity_fill_not_attempted"}
    for _ in range(3):
        _close_stale_success_modal(page)
        res = parse_step_result(
            page.evaluate(
                _step_fill_identity_fields_js(
                    str(row.get("merchant_sku_article") or ""),
                    str(row.get("merchant_offer_name") or ""),
                )
            )
        )
        if res.get("ok") or res.get("reason") != "stale_success_modal_present":
            break
        page.wait_for_timeout(int((step_delay_seconds + 0.5) * 1000))
    if not res.get("ok"):
        snap("err_fill_info")
        return False, f"fill_info_failed:{res.get('reason')}"
    page.wait_for_timeout(int((step_delay_seconds + 0.6) * 1000))
    snap("05_fill_info")
    success = success_state_from_page(page)
    if success.get("ok"):
        snap("05b_success_after_fill_info")
        return True, ""

    price_ok = False
    price_reason = ""
    for _ in range(4):
        ok_fill, reason_fill = _fill_price_modal_with_playwright(
            page,
            price=row.get("price_kzt"),
            pp1=row.get("stock_pp1"),
            pp2=row.get("stock_pp2"),
            barcode=normalized_barcode,
        )
        if not ok_fill:
            success = success_state_from_page(page)
            if success.get("ok"):
                snap("06_success_after_price_fill_probe")
                return True, ""
            price_reason = reason_fill or "fill_price_failed"
            snap("err_fill_price")
            return False, f"fill_price_failed:{price_reason}"
        page.wait_for_timeout(int((step_delay_seconds + 1.0) * 1000))
        warn = parse_step_result(page.evaluate(_step_detect_price_warning_js()))
        if bool(warn.get("warning_present")):
            success = success_state_from_page(page)
            if success.get("ok"):
                snap("06_success_after_warning")
                return True, ""
            snap("06_price_warning")
            _click_price_required_popup(page)
            page.wait_for_timeout(int((step_delay_seconds + 0.8) * 1000))
            continue
        price_ok = True
        break
    if not price_ok:
        snap("err_price_warning_loop")
        return False, f"price_warning_loop_failed:{price_reason}"
    snap("06_save")

    success = parse_step_result(page.evaluate(_step_verify_success_js()))
    if not success.get("ok"):
        snap("err_success")
        return False, f"success_failed:{success.get('reason')}"
    snap("07_success")
    return True, ""


def run_kaspi_offer_ui_upload_playwright(
    *,
    workbook_path: str | Path,
    store_code: str,
    env_file: str | Path = DEFAULT_ENV_FILE,
    dry_run: bool = False,
    confirm: bool = False,
    headless: bool = False,
    output_root: str | Path = "runs/kaspi_offer_ui_upload_playwright",
    step_delay_seconds: float = 1.8,
    step_screenshots: bool = False,
    row_timeout_seconds: int = 180,
    allow_partial_color_batch: bool = False,
) -> dict[str, Any]:
    workbook_path = Path(workbook_path)
    env_file = Path(env_file)
    output_root = Path(output_root)
    run_id = datetime.now(ASTANA_TZ).strftime("%Y%m%d_%H%M%S")
    run_dir = output_root / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    run_log_path = run_dir / "run_log.csv"
    _init_run_log(run_log_path)

    normalized_store = normalize_store_code(store_code)
    rows = load_offer_upload_rows(workbook_path, store_codes=[normalized_store])
    validation = validate_upload_rows(
        rows,
        required_store_codes=[normalized_store],
        require_black_coverage=not allow_partial_color_batch,
    )
    price_floor_report = run_dir / "price_floor_clamps.csv"
    write_price_floor_clamp_report(price_floor_report, validation.get("price_floor_clamp_rows", []))
    summary = {
        "run_id": run_id,
        "status": "dry_run" if dry_run else "pending",
        "workbook_path": str(workbook_path),
        "store_code": normalized_store,
        "rows_total": len(rows),
        "validation": validation,
        "run_dir": str(run_dir),
        "run_log_csv": str(run_log_path),
        "price_floor_clamp_report": str(price_floor_report),
        "success": 0,
        "failed": 0,
        "errors": [],
        "headless": bool(headless),
    }
    if not validation.get("ok"):
        summary["status"] = "preflight_failed"
        summary["errors"] = validation.get("errors", [])
        (run_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        return summary
    if dry_run or not confirm:
        (run_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        return summary

    creds = resolve_store_credentials(normalized_store, env_file)
    with sync_playwright() as p:
        browser = p.chromium.launch(**build_browser_launch_kwargs(headless=headless))
        context = browser.new_context(viewport={"width": 1600, "height": 1000})
        page = context.new_page()
        login_kaspi_merchant(page, email=creds["email"], password=creds["password"])
        for row in rows:
            step_dir = run_dir / "screenshots" if step_screenshots else None
            try:
                with _RowTimeoutGuard(row_timeout_seconds):
                    ok, err = _execute_row_pw(page, row, step_delay_seconds=step_delay_seconds, step_screenshot_dir=step_dir)
            except Exception as exc:
                ok, err = False, format_row_exception(exc)
                if step_dir:
                    row_no = int(row.get("row_number") or 0)
                    article = re.sub(r"[^a-zA-Z0-9._-]+", "_", str(row.get("merchant_sku_article") or "")).strip("_")
                    if not article:
                        article = "no_article"
                    _capture_page_screenshot(page, step_dir / f"row{row_no:04d}_uncaught_{article[:80]}.png")
            status = "ok" if ok else "failed"
            if ok:
                summary["success"] += 1
            else:
                summary["failed"] += 1
                summary["errors"].append(err)
            _append_run_log(
                run_log_path,
                [
                    datetime.now(ASTANA_TZ).isoformat(),
                    normalized_store,
                    row.get("merchant_id"),
                    row.get("row_number"),
                    row.get("merchant_sku_article"),
                    row.get("variant_url"),
                    status,
                    err,
                ],
            )
        browser.close()

    summary["status"] = "success" if summary["failed"] == 0 else ("partial" if summary["success"] > 0 else "failed")
    (run_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Kaspi offer UI upload via Playwright merchant login")
    parser.add_argument("--workbook", required=True)
    parser.add_argument("--store", required=True)
    parser.add_argument("--env-file", default=str(DEFAULT_ENV_FILE))
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--confirm", action="store_true")
    parser.add_argument("--headless", action="store_true")
    parser.add_argument("--output-root", default="runs/kaspi_offer_ui_upload_playwright")
    parser.add_argument("--step-delay-seconds", type=float, default=1.8)
    parser.add_argument("--step-screenshots", action="store_true")
    parser.add_argument("--row-timeout-seconds", type=int, default=180)
    parser.add_argument("--allow-partial-color-batch", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    summary = run_kaspi_offer_ui_upload_playwright(
        workbook_path=args.workbook,
        store_code=args.store,
        env_file=args.env_file,
        dry_run=args.dry_run,
        confirm=args.confirm,
        headless=args.headless,
        output_root=args.output_root,
        step_delay_seconds=args.step_delay_seconds,
        step_screenshots=args.step_screenshots,
        row_timeout_seconds=args.row_timeout_seconds,
        allow_partial_color_batch=args.allow_partial_color_batch,
    )
    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    else:
        print(
            f"status={summary.get('status')} rows={summary.get('rows_total')} "
            f"success={summary.get('success')} failed={summary.get('failed')}"
        )
    return 0 if summary.get("status") in {"success", "partial", "dry_run"} else 4


if __name__ == "__main__":
    raise SystemExit(main())
