from __future__ import annotations

from dataclasses import asdict
import json
import logging
import re
import time
import traceback
import csv
from urllib.parse import urlencode
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from playwright.sync_api import TimeoutError as PWTimeout
from playwright.sync_api import sync_playwright

from .checkpoint import Checkpoint, Progress, load_checkpoint, progress_key, save_checkpoint
from .config import TaskConfig
from .competition_rules import (
    classify_competition_scope,
    competition_floor_kzt,
    effective_competition_floor_kzt,
    is_scope_partner_ignore_exempt,
    set_line52_pricing_profile,
    scoped_competitor_ignore_reason,
)
from .delivery_days_cache import (
    DEFAULT_DELIVERY_DAYS_CACHE_PATH,
    get_cached_delivery_days,
    load_delivery_days_cache,
    put_cached_delivery_days,
    save_delivery_days_cache,
)
from .utils import build_target_sets, ensure_env, normalize_name

KASPI_ASTANA_CITY_ID = "750000000"
_KASPI_OFFER_CODE_RE = re.compile(r"-(\d+)(?:[/?#]|$)")
_DIGIT_SPACE_RE = re.compile(r"(?<=\d)[\s\u00A0](?=\d)")
_PRICE_NUMBER_RE = re.compile(r"-?\d+(?:[.,]\d+)?")
ASTANA_TZ = timezone(timedelta(hours=5))
KASPI_DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/122.0.0.0 Safari/537.36"
)
KASPI_ACCEPT = "application/json, text/plain, */*"
KASPI_ACCEPT_LANGUAGE = "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7"
DEFAULT_FLOOR_MAP_PATH = Path(__file__).resolve().parents[1] / "exports/pricelist_snapshots/min_price_floor_35pct_by_sku_v6.csv"
DELIVERY_DAYS_CACHE_TTL_HOURS = 24


def _extract_competition_scope_for_row(row: dict[str, Any] | None, row_text: str | None = "") -> str | None:
    return classify_competition_scope(row, row_text)


def _is_line52_offer_row(row: dict[str, Any] | None, row_text: str | None = "") -> bool:
    return _extract_competition_scope_for_row(row, row_text) == "LINE52"


def _load_floor_map_from_csv(path: Path) -> dict[str, int]:
    out: dict[str, int] = {}
    p = Path(path)
    if not p.exists():
        return out
    with p.open("r", encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            sku_key = str(row.get("SKU_key") or row.get("sku_key") or "").strip().upper()
            if not sku_key:
                continue
            raw = row.get("Min_price_35pct") or row.get("min_price_35pct") or ""
            try:
                value = int(float(str(raw).replace(" ", "").replace(",", ".")))
            except Exception:
                continue
            prev = out.get(sku_key)
            out[sku_key] = max(prev or 0, value)
    return out


def _parse_competitor_price_value(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if not text:
        return None
    compact = text.replace("₸", "")
    compact = _DIGIT_SPACE_RE.sub("", compact)
    compact = compact.replace("\u00A0", "").replace(" ", "")
    match = _PRICE_NUMBER_RE.search(compact)
    if not match:
        return None
    num = match.group(0)
    if "," in num and "." in num:
        num = num.replace(",", "")
    elif "," in num:
        left, right = num.split(",", 1)
        if len(right) <= 2:
            num = f"{left}.{right}"
        else:
            num = f"{left}{right}"
    try:
        return float(num)
    except ValueError:
        return None


def _canonical_offer_link(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    text = text.split("?", 1)[0].split("#", 1)[0]
    return text.rstrip("/").lower()


def _extract_kaspi_offer_code(value: Any) -> str | None:
    link = str(value or "").strip()
    if not link:
        return None
    match = _KASPI_OFFER_CODE_RE.search(link)
    if not match:
        return None
    return match.group(1)


def _delivery_days_to_astana(delivery_iso: Any) -> int | None:
    text = str(delivery_iso or "").strip()
    if not text:
        return None
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    today = datetime.now(ASTANA_TZ).date()
    delivery_date = dt.astimezone(ASTANA_TZ).date()
    return max((delivery_date - today).days, 0)


def _kaspi_offer_headers(referer: str) -> dict[str, str]:
    return {
        "content-type": "application/json; charset=UTF-8",
        "origin": "https://kaspi.kz",
        "referer": referer,
        "accept": KASPI_ACCEPT,
        "user-agent": KASPI_DEFAULT_USER_AGENT,
        "accept-language": KASPI_ACCEPT_LANGUAGE,
    }


def _fetch_kaspi_delivery_days_by_mid_for_offer(
    *,
    request_context,
    offer_link: str,
    city_id: str = KASPI_ASTANA_CITY_ID,
    limit: int = 50,
    max_pages: int = 20,
) -> tuple[dict[str, int], int]:
    code = _extract_kaspi_offer_code(offer_link)
    if not code:
        return {}, 0

    canonical = _canonical_offer_link(offer_link)
    referer = f"{canonical}/?c={city_id}" if canonical else f"https://kaspi.kz/shop/p/-{code}/?c={city_id}"
    out: dict[str, int] = {}
    requests_made = 0

    for page_idx in range(max_pages):
        payload = {
            "cityId": str(city_id),
            "id": str(code),
            "merchantUID": [],
            "limit": int(limit),
            "page": int(page_idx),
            "sortOption": "PRICE",
        }
        resp = request_context.post(
            f"https://kaspi.kz/yml/offer-view/offers/{code}",
            data=json.dumps(payload),
            headers=_kaspi_offer_headers(referer),
        )
        requests_made += 1
        if resp.status != 200:
            break
        try:
            payload_out = resp.json() or {}
        except Exception:
            break
        offers = payload_out.get("offers") or []
        if not isinstance(offers, list) or not offers:
            break

        for offer in offers:
            if not isinstance(offer, dict):
                continue
            mid = offer.get("merchantId")
            if mid is None:
                continue
            days = _delivery_days_to_astana(offer.get("delivery"))
            if days is None:
                continue
            mid_key = str(mid)
            existing = out.get(mid_key)
            if existing is None or days < existing:
                out[mid_key] = days

        if len(offers) < int(limit):
            break

    return out, requests_made


def _resolve_delivery_days_by_mid_for_offer(
    *,
    request_context,
    offer_link: str,
    in_run_cache: dict[str, dict[str, int]],
    persistent_cache: dict[str, Any] | None,
    ttl_hours: int = DELIVERY_DAYS_CACHE_TTL_HOURS,
) -> tuple[dict[str, int], int]:
    cache_key = _canonical_offer_link(offer_link)
    if not cache_key:
        return {}, 0
    if cache_key in in_run_cache:
        return in_run_cache.get(cache_key, {}), 0
    cached = get_cached_delivery_days(
        persistent_cache or {"version": 1, "entries": {}},
        cache_key,
        ttl_hours=ttl_hours,
    )
    if cached is not None:
        in_run_cache[cache_key] = cached
        return cached, 0
    fetched_days, req_count = _fetch_kaspi_delivery_days_by_mid_for_offer(
        request_context=request_context,
        offer_link=offer_link,
    )
    in_run_cache[cache_key] = fetched_days
    if persistent_cache is not None:
        put_cached_delivery_days(persistent_cache, cache_key, fetched_days)
    return fetched_days, req_count


def _is_partner_target_competitor(name: str, exact_targets: set[str], contains_targets: list[str]) -> bool:
    name_norm = normalize_name(name)
    if not name_norm:
        return False
    return name_norm in exact_targets or any(ct in name_norm for ct in contains_targets)


def _evaluate_competitor_ignore_need(
    *,
    competition_scope: str | None,
    competitive_floor_kzt: int | None,
    competitor_name: str,
    competitor_mid: Any,
    competitor_price: Any,
    competitor_delivery_days: int | None = None,
    store_id: int,
    exact_targets: set[str],
    contains_targets: list[str],
) -> tuple[bool, str]:
    mid_str = None if competitor_mid is None else str(competitor_mid)
    if mid_str is not None:
        if mid_str == str(store_id):
            return False, "self_store"

    if _is_partner_target_competitor(competitor_name, exact_targets, contains_targets):
        if is_scope_partner_ignore_exempt(
            scope=competition_scope,
            competitor_name_norm=normalize_name(competitor_name),
            competitor_mid=mid_str,
        ):
            return False, ""
        return True, "partner_store_target"

    price_val = _parse_competitor_price_value(competitor_price)
    reason = scoped_competitor_ignore_reason(
        scope=competition_scope,
        competitor_price_kzt=price_val,
        delivery_days=competitor_delivery_days,
        competitive_floor_kzt=competitive_floor_kzt,
    )
    if reason:
        return True, reason

    return False, ""


def _should_ignore_competitor_for_row(
    *,
    competition_scope: str | None,
    competitive_floor_kzt: int | None,
    competitor_name: str,
    competitor_mid: Any,
    competitor_price: Any,
    competitor_delivery_days: int | None = None,
    store_id: int,
    already_ignored_mids: set[str],
    exact_targets: set[str],
    contains_targets: list[str],
) -> tuple[bool, str]:
    mid_str = None if competitor_mid is None else str(competitor_mid)
    if mid_str is not None and mid_str in already_ignored_mids:
        return False, "already_ignored"
    return _evaluate_competitor_ignore_need(
        competition_scope=competition_scope,
        competitive_floor_kzt=competitive_floor_kzt,
        competitor_name=competitor_name,
        competitor_mid=competitor_mid,
        competitor_price=competitor_price,
        competitor_delivery_days=competitor_delivery_days,
        store_id=store_id,
        exact_targets=exact_targets,
        contains_targets=contains_targets,
    )


def _determine_competitor_toggle_action(
    *,
    competition_scope: str | None,
    competitive_floor_kzt: int | None,
    competitor_name: str,
    competitor_mid: Any,
    competitor_price: Any,
    competitor_delivery_days: int | None = None,
    store_id: int,
    already_ignored_mids: set[str],
    exact_targets: set[str],
    contains_targets: list[str],
) -> tuple[str, str]:
    mid_str = None if competitor_mid is None else str(competitor_mid)
    currently_ignored = bool(mid_str and mid_str in already_ignored_mids)
    should_ignore, reason = _evaluate_competitor_ignore_need(
        competition_scope=competition_scope,
        competitive_floor_kzt=competitive_floor_kzt,
        competitor_name=competitor_name,
        competitor_mid=competitor_mid,
        competitor_price=competitor_price,
        competitor_delivery_days=competitor_delivery_days,
        store_id=store_id,
        exact_targets=exact_targets,
        contains_targets=contains_targets,
    )
    if should_ignore:
        if currently_ignored:
            return "", "already_ignored"
        if mid_str is None:
            return "", reason
        return "set_true", reason

    if currently_ignored and competition_scope and competitive_floor_kzt is not None:
        return "set_false", "stale_ignore_valid_competitor"

    if currently_ignored:
        return "", "already_ignored"
    return "", reason


def _extract_competitor_price_from_modal_row(mrow) -> float | None:
    values: list[Any] = []
    try:
        price_cell = mrow.locator("td:nth-child(3)")
        if price_cell.count():
            values.append(price_cell.first.inner_text().strip())
    except Exception:
        pass
    try:
        values.append(mrow.inner_text().strip())
    except Exception:
        pass
    for value in values:
        parsed = _parse_competitor_price_value(value)
        if parsed is not None:
            return parsed
    return None


def _extract_competitor_mid_from_modal_row(mrow) -> int | None:
    patterns = [
        ("input[type=checkbox]", "data-mid"),
        ("input[type=checkbox]", "value"),
        ("input[type=checkbox]", "id"),
        ("input[type=checkbox]", "onclick"),
        ("tr", "onclick"),
    ]
    for selector, attr in patterns:
        try:
            source = mrow if selector == "tr" else mrow.locator(selector).first
            if source.count() == 0:
                continue
            raw = source.get_attribute(attr)
            if not raw:
                continue
            matches = re.findall(r"\d+", raw)
            if not matches:
                continue
            return int(matches[-1])
        except Exception:
            continue
    return None


def _close_login_modal(page) -> None:
    try:
        if page.locator("text=Необходимо войти на сайт!").first.is_visible(timeout=2000):
            if page.locator("#alert_close").count():
                page.locator("#alert_close").click(timeout=3000, force=True)
            else:
                page.locator(".modal").locator("button:has-text('Закрыть')").first.click(timeout=3000, force=True)
            page.evaluate(
                """
                () => {
                  const backdrops = document.querySelectorAll('.modal-backdrop');
                  backdrops.forEach(b => b.remove());
                  const modal = document.querySelector('#alert_text')?.closest('.modal');
                  if (modal) { modal.classList.remove('show'); modal.style.display='none'; }
                }
                """
            )
    except Exception:
        pass


def _wait_table_ready(page, timeout_ms: int) -> None:
    page.wait_for_selector("#incoming_data_table_processing", state="hidden", timeout=timeout_ms)
    page.wait_for_function(
        """
        () => {
          const t = document.querySelector('#incoming_data_table');
          if (!t) return false;
          return t.querySelectorAll('tbody tr').length > 0;
        }
        """,
        timeout=timeout_ms,
    )


def _goto_first_page(page, timeout_ms: int) -> None:
    nav = page.evaluate(
        r"""
        () => {
          let dtNavigated = false;
          try {
            const jq = window.jQuery || window.$;
            if (jq && jq.fn && jq.fn.dataTable) {
              const dt = jq('#incoming_data_table').DataTable();
              const info = dt.page.info();
              if (info && info.page > 0) {
                dt.page('first').draw('page');
                dtNavigated = true;
              }
            }
          } catch (e) {}
          if (dtNavigated) return { navigated: true, method: 'dt' };
          const first = document.querySelector('.dt-paging-button.first');
          if (first && !first.classList.contains('disabled')) {
            first.click();
            return { navigated: true, method: 'dom' };
          }
          return { navigated: false, method: 'none' };
        }
        """
    )
    if nav.get("navigated"):
        _wait_table_ready(page, timeout_ms)


def _open_competitors_modal(page, link_locator, modal_timeout_ms: int) -> None:
    try:
        link_locator.click(timeout=5000)
    except Exception:
        # Fallback: execute onclick handler
        onclick = link_locator.get_attribute("onclick")
        if onclick:
            page.evaluate("(code) => { try { eval(code); return true; } catch (e) { return false; } }", onclick)
        else:
            raise
    page.wait_for_selector("#competitors_modal", timeout=modal_timeout_ms)


def _close_competitors_modal(page) -> None:
    page.evaluate(
        """
        () => {
          const modal = document.querySelector('#competitors_modal');
          if (!modal) return false;
          const btn = modal.querySelector('#competitors_close');
          if (btn) { btn.click(); return true; }
          const icon = modal.querySelector('.btn-close, [aria-label="Close"], [aria-label="Закрыть"]');
          if (icon) { icon.click(); return true; }
          return false;
        }
        """
    )


def _next_page(page) -> dict[str, Any]:
    return page.evaluate(
        r"""
        () => {
          const infoEl = document.querySelector('#incoming_data_table_info');
          const text = infoEl ? infoEl.textContent.trim() : '';
          let start = null, end = null, total = null;
          const m = text.match(/Записи с\s+(\d+)\s+до\s+(\d+)\s+из\s+(\d+)\s+записей/);
          if (m) {
            start = Number(m[1]);
            end = Number(m[2]);
            total = Number(m[3]);
          }
          let dtNavigated = false;
          try {
            const jq = window.jQuery || window.$;
            if (jq && jq.fn && jq.fn.dataTable) {
              const dt = jq('#incoming_data_table').DataTable();
              const info = dt.page.info();
              if (info && info.page < info.pages - 1) {
                dt.page('next').draw('page');
                dtNavigated = true;
              }
            }
          } catch (e) {}
          if (dtNavigated) {
            return { navigated: true, method: 'dt', start, end, total };
          }
          const next = document.querySelector('.dt-paging-button.next');
          if (next && !next.classList.contains('disabled')) {
            next.click();
            return { navigated: true, method: 'dom', start, end, total };
          }
          return { navigated: false, method: 'none', start, end, total };
        }
        """
    )


def _scan_remaining_targets(
    *,
    page,
    exact_targets: set[str],
    contains_targets: list[str],
    floor_by_sku_key: dict[str, int],
    timeout_ms: int,
    modal_timeout_ms: int,
    run_dir: Path,
    store_id: int,
) -> int:
    remaining = 0
    try:
        _goto_first_page(page, timeout_ms)
    except Exception:
        pass

    page_index = 1
    while True:
        _close_login_modal(page)
        try:
            _wait_table_ready(page, timeout_ms)
        except PWTimeout:
            _capture_artifact(page, run_dir, f"store_{store_id}_review_page_{page_index}_timeout")
            break

        rows = page.locator("#incoming_data_table tbody tr")
        row_count = rows.count()
        if row_count == 0:
            break

        for row_idx in range(row_count):
            row = rows.nth(row_idx)
            link = row.locator("a[onclick^='show_competitors']")
            if link.count() == 0:
                continue
            row_text = row.inner_text().strip()
            competition_scope = _extract_competition_scope_for_row(None, row_text)
            competitive_floor = effective_competition_floor_kzt(None, floor_by_sku_key, row_text)

            opened = False
            for _ in range(3):
                try:
                    _open_competitors_modal(page, link.first, modal_timeout_ms)
                    opened = True
                    break
                except Exception:
                    _close_login_modal(page)
            if not opened:
                _capture_artifact(page, run_dir, f"store_{store_id}_review_row_{row_idx}_modal_open")
                continue

            modal_rows = page.locator("#competitors_modal table tbody tr")
            modal_count = modal_rows.count()
            for i in range(modal_count):
                mrow = modal_rows.nth(i)
                name = mrow.locator("td:nth-child(2)").inner_text().strip()
                checkbox = mrow.locator("input[type=checkbox]")
                if checkbox.count() == 0:
                    continue
                comp_price = _extract_competitor_price_from_modal_row(mrow)
                comp_mid = _extract_competitor_mid_from_modal_row(mrow)
                should_ignore, _ = _should_ignore_competitor_for_row(
                    competition_scope=competition_scope,
                    competitive_floor_kzt=competitive_floor,
                    competitor_name=name,
                    competitor_mid=comp_mid,
                    competitor_price=comp_price,
                    store_id=store_id,
                    already_ignored_mids=set(),
                    exact_targets=exact_targets,
                    contains_targets=contains_targets,
                )
                if not should_ignore:
                    continue
                if not checkbox.is_checked():
                    remaining += 1
            _close_competitors_modal(page)

        nav = _next_page(page)
        if not nav.get("navigated"):
            break
        page_index += 1

    return remaining


def _capture_artifact(page, run_dir: Path, label: str) -> None:
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in label)
    try:
        page.screenshot(path=str(run_dir / f"{safe}.png"), full_page=True)
    except Exception:
        pass
    try:
        html = page.content()
        (run_dir / f"{safe}.html").write_text(html, encoding="utf-8")
    except Exception:
        pass


def _extract_row_id(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        match = re.search(r"(\d+)$", value)
        if match:
            return int(match.group(1))
    return None


def _api_headers(bot_token: str, referer: str) -> dict[str, str]:
    return {
        "authorize": bot_token,
        "x-requested-with": "XMLHttpRequest",
        "content-type": "application/json; charset=UTF-8",
        "accept": "application/json, text/javascript, */*; q=0.01",
        "origin": "https://repricer.kz",
        "referer": referer,
    }


def _form_headers(bot_token: str, referer: str) -> dict[str, str]:
    return {
        "authorize": bot_token,
        "x-requested-with": "XMLHttpRequest",
        "content-type": "application/x-www-form-urlencoded; charset=UTF-8",
        "accept": "*/*",
        "origin": "https://repricer.kz",
        "referer": referer,
    }


def _get_datatable_params(page) -> dict[str, Any] | None:
    params = page.evaluate(
        """
        () => {
          if (!window.incoming_table) return null;
          return window.incoming_table.ajax.params();
        }
        """
    )
    if params is None:
        return None
    if isinstance(params, str):
        try:
            return json.loads(params)
        except json.JSONDecodeError:
            return None
    if isinstance(params, dict):
        return params
    return None


def _get_records_total(page) -> int | None:
    return page.evaluate(
        """
        () => {
          if (!window.incoming_table) return null;
          const info = window.incoming_table.page.info();
          return info ? info.recordsTotal : null;
        }
        """
    )


def _get_bot_token(page) -> str | None:
    return page.evaluate(
        """
        () => {
          const v = localStorage.getItem('bot_token');
          if (!v) return null;
          try {
            return JSON.parse(v);
          } catch (e) {
            return null;
          }
        }
        """
    )


def _safe_response_text(resp, limit: int = 200) -> str | None:
    try:
        text = resp.text()
    except Exception:
        return None
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > limit:
        return text[:limit] + "…"
    return text


def run_repricer_competitors(
    *,
    config: TaskConfig,
    dry_run: bool,
    confirm: bool,
    resume: bool,
    account_name: str | None,
    store_ids: list[int] | None,
    checkpoint_path: str | None,
    artifacts_dir: str | None,
    slowmo_ms: int | None,
    timeout_ms: int | None,
    modal_timeout_ms: int | None,
    headless: bool,
    headed: bool,
    profile_dir: str | None,
    storage_state: str | None,
    upload_after: bool,
) -> dict[str, Any]:
    run_cfg = config.run
    set_line52_pricing_profile(config.line52_pricing_profile)

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
    effective_modal_timeout_ms = int(modal_timeout_ms or run_cfg.modal_timeout_ms)
    effective_slowmo_ms = int(slowmo_ms or run_cfg.slowmo_ms)

    effective_headless = run_cfg.headless
    if headless:
        effective_headless = True
    if headed:
        effective_headless = False

    effective_profile_dir = profile_dir or run_cfg.browser_profile_dir
    effective_storage_state = storage_state or run_cfg.storage_state_path
    effective_upload_after = upload_after or run_cfg.upload_after

    exact_targets, contains_targets = build_target_sets(config.targets, config.contains_targets)
    floor_by_sku_key = _load_floor_map_from_csv(DEFAULT_FLOOR_MAP_PATH)

    checkpoint = load_checkpoint(effective_checkpoint, config.task_id)

    summary = {
        "task_id": config.task_id,
        "run_id": run_id,
        "dry_run": effective_dry_run,
        "line52_pricing_profile": asdict(config.line52_pricing_profile),
        "products_visited": 0,
        "modals_opened": 0,
        "checkboxes_changed": 0,
        "errors": 0,
        "per_store": {},
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

                store_key = progress_key(account.name, store_id)
                if not resume:
                    checkpoint.progress.pop(store_key, None)

                store_summary = summary["per_store"].setdefault(str(store_id), {
                    "products_visited": 0,
                    "modals_opened": 0,
                    "checkboxes_changed": 0,
                    "errors": 0,
                })

                try:
                    page.goto(f"{base_url}?token={token}", wait_until="domcontentloaded")
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

                    # Ensure we start from first page
                    try:
                        _goto_first_page(page, effective_timeout_ms)
                    except Exception:
                        pass

                    page_index = 1
                    while True:
                        _close_login_modal(page)
                        rows = page.locator("#incoming_data_table tbody tr")
                        row_count = rows.count()
                        if row_count == 0:
                            record_error(f"No rows found on store {store_id}, page {page_index}")
                            break

                        for row_idx in range(row_count):
                            if resume and store_key in checkpoint.progress:
                                resume_point = checkpoint.progress[store_key]
                                if page_index < resume_point.page_index:
                                    continue
                                if page_index == resume_point.page_index and row_idx <= resume_point.row_index:
                                    continue

                            row = rows.nth(row_idx)
                            link = row.locator("a[onclick^='show_competitors']")
                            if link.count() == 0:
                                checkpoint.progress[store_key] = Progress(page_index=page_index, row_index=row_idx)
                                save_checkpoint(effective_checkpoint, checkpoint)
                                continue
                            row_text = row.inner_text().strip()
                            competition_scope = _extract_competition_scope_for_row(None, row_text)
                            competitive_floor = effective_competition_floor_kzt(None, floor_by_sku_key, row_text)

                            opened = False
                            last_exc: Exception | None = None
                            for attempt in range(run_cfg.max_retries):
                                try:
                                    _open_competitors_modal(page, link.first, effective_modal_timeout_ms)
                                    opened = True
                                    break
                                except Exception as exc:
                                    last_exc = exc
                                    _close_login_modal(page)
                            if not opened:
                                record_error(
                                    f"Modal open failed store {store_id} row {row_idx}",
                                    last_exc,
                                    {"store_id": store_id, "page_index": page_index, "row_index": row_idx},
                                )
                                _capture_artifact(page, run_dir, f"store_{store_id}_row_{row_idx}_modal_open")
                                checkpoint.progress[store_key] = Progress(page_index=page_index, row_index=row_idx)
                                save_checkpoint(effective_checkpoint, checkpoint)
                                continue

                            summary["modals_opened"] += 1
                            store_summary["modals_opened"] += 1

                            try:
                                modal_rows = page.locator("#competitors_modal table tbody tr")
                                modal_count = modal_rows.count()
                                for i in range(modal_count):
                                    mrow = modal_rows.nth(i)
                                    name = mrow.locator("td:nth-child(2)").inner_text().strip()
                                    checkbox = mrow.locator("input[type=checkbox]")
                                    if checkbox.count() == 0:
                                        continue
                                    if checkbox.is_checked():
                                        continue
                                    comp_price = _extract_competitor_price_from_modal_row(mrow)
                                    comp_mid = _extract_competitor_mid_from_modal_row(mrow)
                                    should_ignore, _ = _should_ignore_competitor_for_row(
                                        competition_scope=competition_scope,
                                        competitive_floor_kzt=competitive_floor,
                                        competitor_name=name,
                                        competitor_mid=comp_mid,
                                        competitor_price=comp_price,
                                        store_id=store_id,
                                        already_ignored_mids=set(),
                                        exact_targets=exact_targets,
                                        contains_targets=contains_targets,
                                    )
                                    if not should_ignore:
                                        continue
                                    if not effective_dry_run:
                                        checkbox.check(force=True)
                                    summary["checkboxes_changed"] += 1
                                    store_summary["checkboxes_changed"] += 1
                            except Exception as exc:
                                record_error(
                                    f"Modal processing failed store {store_id} row {row_idx}",
                                    exc,
                                    {"store_id": store_id, "page_index": page_index, "row_index": row_idx},
                                )
                                _capture_artifact(page, run_dir, f"store_{store_id}_row_{row_idx}_modal_process")

                            _close_competitors_modal(page)

                            summary["products_visited"] += 1
                            store_summary["products_visited"] += 1

                            checkpoint.progress[store_key] = Progress(page_index=page_index, row_index=row_idx)
                            save_checkpoint(effective_checkpoint, checkpoint)

                        # next page
                        nav = _next_page(page)
                        if not nav.get("navigated"):
                            if nav.get("total") and nav.get("end") and nav["total"] > nav["end"]:
                                record_error(
                                    f"Pagination stalled store {store_id} page {page_index}",
                                    None,
                                    {"store_id": store_id, "page_index": page_index, "range": nav},
                                )
                                _capture_artifact(page, run_dir, f"store_{store_id}_page_{page_index}_pagination")
                            break
                        page_index += 1
                        try:
                            _wait_table_ready(page, effective_timeout_ms)
                        except PWTimeout as exc:
                            record_error(
                                f"Next page load timeout store {store_id} page {page_index}",
                                exc,
                                {"store_id": store_id, "page_index": page_index},
                            )
                            _capture_artifact(page, run_dir, f"store_{store_id}_page_{page_index}_timeout")
                            break

                    if effective_upload_after and not effective_dry_run:
                        remaining = _scan_remaining_targets(
                            page=page,
                            exact_targets=exact_targets,
                            contains_targets=contains_targets,
                            floor_by_sku_key=floor_by_sku_key,
                            timeout_ms=effective_timeout_ms,
                            modal_timeout_ms=effective_modal_timeout_ms,
                            run_dir=run_dir,
                            store_id=store_id,
                        )
                        summary.setdefault("remaining_after_review", {})[str(store_id)] = remaining
                        if remaining == 0:
                            try:
                                page.locator("#upload_kaspi_data").first.click(timeout=8000)
                                summary.setdefault("upload_clicked", []).append(str(store_id))
                            except Exception as exc:
                                record_error(
                                    f"Upload to Kaspi click failed store {store_id}",
                                    exc,
                                    {"store_id": store_id},
                                )
                                _capture_artifact(page, run_dir, f"store_{store_id}_upload_click")
                        else:
                            record_error(
                                f"Upload skipped; remaining unchecked targets store {store_id}",
                                None,
                                {"store_id": store_id, "remaining": remaining},
                            )

                except Exception as exc:
                    record_error(f"Store processing failed store {store_id}", exc, {"store_id": store_id})
                    _capture_artifact(page, run_dir, f"store_{store_id}_fatal")
                    store_summary["errors"] += 1

            if close_context:
                context.close()

    summary_path = run_dir / "summary.json"
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    return summary


def run_repricer_competitors_api(
    *,
    config: TaskConfig,
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
    api_verify: bool,
) -> dict[str, Any]:
    run_cfg = config.run
    set_line52_pricing_profile(config.line52_pricing_profile)

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

    exact_targets, contains_targets = build_target_sets(config.targets, config.contains_targets)
    floor_by_sku_key = _load_floor_map_from_csv(DEFAULT_FLOOR_MAP_PATH)
    delivery_days_cache_payload = load_delivery_days_cache(DEFAULT_DELIVERY_DAYS_CACHE_PATH)
    delivery_days_cache_dirty = False

    checkpoint = load_checkpoint(effective_checkpoint, f"{config.task_id}_api")

    summary: dict[str, Any] = {
        "task_id": config.task_id,
        "run_id": run_id,
        "dry_run": effective_dry_run,
        "api_mode": True,
        "line52_pricing_profile": asdict(config.line52_pricing_profile),
        "products_visited": 0,
        "modals_opened": 0,
        "checkboxes_changed": 0,
        "api_requests": 0,
        "api_sets_attempted": 0,
        "api_sets_succeeded": 0,
        "delivery_lookup_requests": 0,
        "delivery_days_ignored": 0,
        "errors": 0,
        "per_store": {},
        "remaining_targets": {},
        "remaining_after_verify": {},
        "artifacts_dir": str(run_dir),
        "checkpoint_path": effective_checkpoint,
    }
    delivery_days_cache: dict[str, dict[str, int]] = {}

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

                store_key = progress_key(f"{account.name}_api", store_id)
                if not resume:
                    checkpoint.progress.pop(store_key, None)

                store_summary = summary["per_store"].setdefault(
                    str(store_id),
                    {
                        "products_visited": 0,
                        "checkboxes_changed": 0,
                        "api_requests": 0,
                        "api_sets_attempted": 0,
                        "api_sets_succeeded": 0,
                        "delivery_lookup_requests": 0,
                        "delivery_days_ignored": 0,
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

                    records_total = _get_records_total(page)
                    if records_total is None:
                        records_total = 0

                    length = int(params.get("length", 100))
                    draw = int(params.get("draw", 1))

                    remaining_targets = 0
                    start = 0
                    page_index = 1

                    while start < max(records_total, 1):
                        params["start"] = start
                        params["length"] = length
                        params["draw"] = draw
                        params["mid"] = store_id

                        headers = _api_headers(bot_token, referer)
                        resp = context.request.post(
                            "https://repricer.kz/price_strategy_data_mid/",
                            data=json.dumps(params),
                            headers=headers,
                        )
                        summary["api_requests"] += 1
                        store_summary["api_requests"] += 1

                        if resp.status != 200:
                            err_text = _safe_response_text(resp)
                            record_error(
                                f"API fetch failed store {store_id} page {page_index}",
                                None,
                                {
                                    "store_id": store_id,
                                    "page_index": page_index,
                                    "status": resp.status,
                                    "response": err_text,
                                },
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

                            competitors = row.get("competitors") or []
                            not_competitors = row.get("not_competitors") or []
                            not_set = {str(x) for x in not_competitors if x is not None}
                            competition_scope = _extract_competition_scope_for_row(row, "")
                            competitive_floor = effective_competition_floor_kzt(row, floor_by_sku_key)
                            delivery_days_by_mid: dict[str, int] = {}
                            row_link = row.get("link")
                            cache_key = _canonical_offer_link(row_link)
                            if competition_scope and cache_key:
                                delivery_days_by_mid, req_count = _resolve_delivery_days_by_mid_for_offer(
                                    request_context=context.request,
                                    offer_link=str(row_link or ""),
                                    in_run_cache=delivery_days_cache,
                                    persistent_cache=delivery_days_cache_payload,
                                    ttl_hours=DELIVERY_DAYS_CACHE_TTL_HOURS,
                                )
                                if req_count > 0:
                                    delivery_days_cache_dirty = True
                                    summary["delivery_lookup_requests"] += req_count
                                    store_summary["delivery_lookup_requests"] += req_count

                            for comp in competitors:
                                if not isinstance(comp, dict):
                                    continue
                                name = comp.get("name") or ""
                                mid = comp.get("mid")
                                comp_price = comp.get("price")
                                comp_delivery_days = None
                                if mid is not None:
                                    comp_delivery_days = delivery_days_by_mid.get(str(mid))
                                action, reason = _determine_competitor_toggle_action(
                                    competition_scope=competition_scope,
                                    competitive_floor_kzt=competitive_floor,
                                    competitor_name=name,
                                    competitor_mid=mid,
                                    competitor_price=comp_price,
                                    competitor_delivery_days=comp_delivery_days,
                                    store_id=store_id,
                                    already_ignored_mids=not_set,
                                    exact_targets=exact_targets,
                                    contains_targets=contains_targets,
                                )
                                if not action:
                                    continue
                                if mid is None:
                                    continue
                                if reason == "competition_scope_long_delivery_10plus_days":
                                    summary["delivery_days_ignored"] += 1
                                    store_summary["delivery_days_ignored"] += 1

                                remaining_targets += 1
                                if effective_dry_run:
                                    summary["checkboxes_changed"] += 1
                                    store_summary["checkboxes_changed"] += 1
                                    if action == "set_true":
                                        not_set.add(str(mid))
                                    elif action == "set_false":
                                        not_set.discard(str(mid))
                                    continue

                                summary["api_sets_attempted"] += 1
                                store_summary["api_sets_attempted"] += 1
                                form_headers = _form_headers(bot_token, referer)
                                form_data = urlencode(
                                    {
                                        "id": str(row_id),
                                        "mid": str(mid),
                                        "set_or_not": "true" if action == "set_true" else "false",
                                    }
                                )
                                resp_set = context.request.post(
                                    "https://repricer.kz/set_not_competitor/",
                                    data=form_data,
                                    headers=form_headers,
                                )
                                summary["api_requests"] += 1
                                store_summary["api_requests"] += 1
                                if resp_set.status == 200:
                                    summary["api_sets_succeeded"] += 1
                                    store_summary["api_sets_succeeded"] += 1
                                    summary["checkboxes_changed"] += 1
                                    store_summary["checkboxes_changed"] += 1
                                    if action == "set_true":
                                        not_set.add(str(mid))
                                    else:
                                        not_set.discard(str(mid))
                                else:
                                    err_text = _safe_response_text(resp_set)
                                    record_error(
                                        f"API set failed store {store_id} row {row_id}",
                                        None,
                                        {
                                            "store_id": store_id,
                                            "row_id": row_id,
                                            "status": resp_set.status,
                                            "response": err_text,
                                        },
                                    )

                            summary["products_visited"] += 1
                            store_summary["products_visited"] += 1

                            checkpoint.progress[store_key] = Progress(page_index=page_index, row_index=row_idx)
                            save_checkpoint(effective_checkpoint, checkpoint)

                        start += length
                        draw += 1
                        page_index += 1
                        if effective_slowmo_ms:
                            time.sleep(effective_slowmo_ms / 1000.0)

                    summary["remaining_targets"][str(store_id)] = remaining_targets

                    if api_verify and not effective_dry_run:
                        verify_remaining = 0
                        start = 0
                        page_index = 1
                        verify_draw = 1
                        while start < max(records_total, 1):
                            params["start"] = start
                            params["length"] = length
                            params["draw"] = verify_draw
                            params["mid"] = store_id

                            headers = _api_headers(bot_token, referer)
                            resp = context.request.post(
                                "https://repricer.kz/price_strategy_data_mid/",
                                data=json.dumps(params),
                                headers=headers,
                            )
                            summary["api_requests"] += 1
                            store_summary["api_requests"] += 1

                            if resp.status != 200:
                                err_text = _safe_response_text(resp)
                                record_error(
                                    f"API verify fetch failed store {store_id} page {page_index}",
                                    None,
                                    {
                                        "store_id": store_id,
                                        "page_index": page_index,
                                        "status": resp.status,
                                        "response": err_text,
                                    },
                                )
                                break

                            payload = resp.json()
                            rows = payload.get("data") or []
                            if not rows:
                                break

                            for row in rows:
                                competitors = row.get("competitors") or []
                                not_competitors = row.get("not_competitors") or []
                                not_set = {str(x) for x in not_competitors if x is not None}
                                competition_scope = _extract_competition_scope_for_row(row, "")
                                competitive_floor = effective_competition_floor_kzt(row, floor_by_sku_key)
                                delivery_days_by_mid: dict[str, int] = {}
                                row_link = row.get("link")
                                cache_key = _canonical_offer_link(row_link)
                                if competition_scope and cache_key:
                                    delivery_days_by_mid, req_count = _resolve_delivery_days_by_mid_for_offer(
                                        request_context=context.request,
                                        offer_link=str(row_link or ""),
                                        in_run_cache=delivery_days_cache,
                                        persistent_cache=delivery_days_cache_payload,
                                        ttl_hours=DELIVERY_DAYS_CACHE_TTL_HOURS,
                                    )
                                    if req_count > 0:
                                        delivery_days_cache_dirty = True
                                        summary["delivery_lookup_requests"] += req_count
                                        store_summary["delivery_lookup_requests"] += req_count
                                for comp in competitors:
                                    if not isinstance(comp, dict):
                                        continue
                                    name = comp.get("name") or ""
                                    mid = comp.get("mid")
                                    comp_price = comp.get("price")
                                    comp_delivery_days = None
                                    if mid is not None:
                                        comp_delivery_days = delivery_days_by_mid.get(str(mid))
                                    action, reason = _determine_competitor_toggle_action(
                                        competition_scope=competition_scope,
                                        competitive_floor_kzt=competitive_floor,
                                        competitor_name=name,
                                        competitor_mid=mid,
                                        competitor_price=comp_price,
                                        competitor_delivery_days=comp_delivery_days,
                                        store_id=store_id,
                                        already_ignored_mids=not_set,
                                        exact_targets=exact_targets,
                                        contains_targets=contains_targets,
                                    )
                                    if not action:
                                        continue
                                    if reason == "competition_scope_long_delivery_10plus_days":
                                        summary["delivery_days_ignored"] += 1
                                        store_summary["delivery_days_ignored"] += 1
                                    verify_remaining += 1

                            start += length
                            page_index += 1
                            verify_draw += 1
                            if effective_slowmo_ms:
                                time.sleep(effective_slowmo_ms / 1000.0)

                        summary["remaining_after_verify"][str(store_id)] = verify_remaining

                except Exception as exc:
                    record_error(f"Store processing failed store {store_id}", exc, {"store_id": store_id})
                    _capture_artifact(page, run_dir, f"store_{store_id}_fatal")
                    store_summary["errors"] += 1

            if close_context:
                context.close()

    if delivery_days_cache_dirty:
        save_delivery_days_cache(DEFAULT_DELIVERY_DAYS_CACHE_PATH, delivery_days_cache_payload)

    summary_path = run_dir / "summary.json"
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    return summary
