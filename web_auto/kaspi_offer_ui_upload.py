from __future__ import annotations

import argparse
import csv
import json
import re
import subprocess
import time
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from openpyxl import load_workbook

ASTANA_TZ = ZoneInfo("Asia/Almaty")
DEFAULT_ENTRY_URL = "https://kaspi.kz/mc/#/add-product/v2/link-catalog"

REQUIRED_COLUMNS = [
    "upload_enabled",
    "store_code",
    "merchant_id",
    "variant_url",
    "expected_kaspi_heading",
    "size_rus",
    "merchant_sku_article",
    "merchant_offer_name",
    "price_kzt",
    "stock_pp1",
    "stock_pp2",
]

_STORE_ALIASES = {
    "universal": "UNIVERSAL",
    "storeb": "STOREB",
    "store-b": "STOREB",
    "acmewear": "ACMEWEAR",
    "store-c": "MELVIS",
    "store-d": "11KZ",
}


def _norm_text(value: Any) -> str:
    if value is None:
        return ""
    return " ".join(str(value).strip().split())


def _to_int(value: Any) -> int:
    if value is None:
        raise ValueError("empty numeric value")
    text = str(value).strip()
    if not text:
        raise ValueError("empty numeric value")
    text = text.replace(",", ".")
    # Handle Excel style float-ish values.
    return int(float(text))


def _normalize_barcode(value: Any) -> str:
    text = _norm_text(value)
    if not text:
        return ""
    digits = re.sub(r"\D", "", text)
    if 8 <= len(digits) <= 14:
        return digits
    return ""


def _normalize_size_rus(value: Any) -> str:
    text = _norm_text(value)
    if not text:
        return ""
    num_text = text.replace(",", ".")
    if re.fullmatch(r"\d+(\.\d+)?", num_text):
        return str(int(float(num_text)))
    return text.upper()


def _extract_offer_code_from_variant_url(variant_url: Any) -> str:
    text = _norm_text(variant_url)
    if not text:
        return ""
    m = re.search(r"-(\d{6,})(?:[/?#]|$)", text)
    return m.group(1) if m else ""


def normalize_store_code(value: Any) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    key = raw.lower().replace("_", "-")
    key = re.sub(r"\s+", "", key)
    if key in _STORE_ALIASES:
        return _STORE_ALIASES[key]
    # Preserve canonical uppercase for unknown but explicit codes.
    return raw.upper()


def parse_store_codes(value: str | list[str] | None) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        parts = [str(v).strip() for v in value if str(v).strip()]
    else:
        parts = [chunk.strip() for chunk in str(value).split(",") if chunk.strip()]
    out: list[str] = []
    for part in parts:
        code = normalize_store_code(part)
        if code and code not in out:
            out.append(code)
    return out


def _truthy_yes(value: Any) -> bool:
    text = str(value or "").strip().lower()
    return text in {"1", "yes", "y", "true", "да"}


def load_offer_upload_rows(
    workbook_path: str | Path,
    *,
    sheet_name: str = "offer upload",
    store_codes: list[str] | None = None,
) -> list[dict[str, Any]]:
    path = Path(workbook_path)
    wb = load_workbook(path, data_only=True)
    if sheet_name not in wb.sheetnames:
        raise ValueError(f"missing sheet: {sheet_name}")
    ws = wb[sheet_name]
    headers = [_norm_text(ws.cell(1, c).value) for c in range(1, ws.max_column + 1)]
    idx = {h: i + 1 for i, h in enumerate(headers) if h}
    missing = [name for name in REQUIRED_COLUMNS if name not in idx]
    if missing:
        raise ValueError(f"missing required columns: {', '.join(missing)}")

    filter_codes = set(store_codes or [])
    rows: list[dict[str, Any]] = []
    for r in range(2, ws.max_row + 1):
        upload_enabled = ws.cell(r, idx["upload_enabled"]).value
        if not _truthy_yes(upload_enabled):
            continue

        store_code = normalize_store_code(ws.cell(r, idx["store_code"]).value)
        if filter_codes and store_code not in filter_codes:
            continue

        row: dict[str, Any] = {"row_number": r, "upload_enabled": "yes", "store_code": store_code}
        for name in REQUIRED_COLUMNS:
            if name in {"upload_enabled", "store_code"}:
                continue
            row[name] = ws.cell(r, idx[name]).value

        # Optional columns used by runtime and validations.
        row["color"] = ws.cell(r, idx.get("color", 0)).value if idx.get("color") else ""
        row["ui_entry_url"] = ws.cell(r, idx.get("ui_entry_url", 0)).value if idx.get("ui_entry_url") else DEFAULT_ENTRY_URL
        row["ingest_method"] = ws.cell(r, idx.get("ingest_method", 0)).value if idx.get("ingest_method") else ""

        rows.append(row)
    return rows


def validate_upload_rows(rows: list[dict[str, Any]], *, required_store_codes: list[str]) -> dict[str, Any]:
    errors: list[str] = []
    warnings: list[str] = []
    required = [normalize_store_code(s) for s in required_store_codes if normalize_store_code(s)]

    if not rows:
        errors.append("no active rows to upload")
        return {"ok": False, "errors": errors, "warnings": warnings, "rows_total": 0}

    by_store = Counter(normalize_store_code(r.get("store_code")) for r in rows)
    for store in required:
        if by_store.get(store, 0) == 0:
            errors.append(f"store {store} has no active rows")

    # Ensure black color coverage in required stores.
    for store in required:
        has_black = any(
            normalize_store_code(r.get("store_code")) == store
            and str(r.get("color") or "").strip().lower() == "black"
            for r in rows
        )
        if not has_black:
            errors.append(f"black color row missing for store {store}")

    required_fields = [
        "merchant_id",
        "variant_url",
        "expected_kaspi_heading",
        "size_rus",
        "merchant_sku_article",
        "merchant_offer_name",
        "price_kzt",
        "stock_pp1",
        "stock_pp2",
    ]
    for row in rows:
        row_no = row.get("row_number", "?")
        for field in required_fields:
            if _norm_text(row.get(field)) == "":
                errors.append(f"row {row_no}: missing {field}")
        for field in ("price_kzt", "stock_pp1", "stock_pp2", "merchant_id"):
            try:
                _to_int(row.get(field))
            except Exception:
                errors.append(f"row {row_no}: invalid numeric {field}={row.get(field)!r}")
        if _norm_text(row.get("barcode")) and not _normalize_barcode(row.get("barcode")):
            warnings.append(f"row {row_no}: barcode ignored (invalid length)")

    seen: set[tuple[str, str, str]] = set()
    for row in rows:
        key = (
            normalize_store_code(row.get("store_code")),
            _norm_text(row.get("merchant_sku_article")),
            _norm_text(row.get("variant_url")),
        )
        if key in seen:
            errors.append(f"duplicate upload key: {key[0]}::{key[1]}::{key[2]}")
        else:
            seen.add(key)

    return {
        "ok": len(errors) == 0,
        "errors": errors,
        "warnings": warnings,
        "rows_total": len(rows),
        "rows_by_store": dict(by_store),
    }


def _run_osascript_jxa(script: str) -> str:
    proc = subprocess.run(["osascript", "-l", "JavaScript"], input=script, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or proc.stdout).strip() or "osascript jxa failed")
    return proc.stdout.strip()


def _execute_tab_js(window_index: int, js_code: str) -> str:
    script = f"""
const Chrome = Application('Google Chrome');
const jsCode = {json.dumps(js_code, ensure_ascii=False)};
const out = Chrome.windows[{int(window_index) - 1}].activeTab().execute({{javascript: jsCode}});
if (typeof out === 'undefined' || out === null) {{
  '';
}} else if (typeof out === 'string') {{
  out;
}} else {{
  JSON.stringify(out);
}}
""".strip()
    return _run_osascript_jxa(script)


def _set_tab_url(window_index: int, url: str) -> None:
    script = f"""
const Chrome = Application('Google Chrome');
Chrome.windows[{int(window_index) - 1}].activeTab().url = {json.dumps(url, ensure_ascii=False)};
'ok';
""".strip()
    _run_osascript_jxa(script)


def _list_chrome_windows() -> list[dict[str, Any]]:
    script = r'''
const Chrome = Application('Google Chrome');
const out = [];
const wins = Chrome.windows();
for (let i = 0; i < wins.length; i++) {
  const t = wins[i].activeTab();
  out.push({
    index: i + 1,
    title: String(t.title() || ''),
    url: String(t.url() || ''),
  });
}
JSON.stringify(out);
'''
    raw = _run_osascript_jxa(script)
    data = json.loads(raw or "[]")
    return data if isinstance(data, list) else []


def _window_bounds(window_index: int) -> tuple[int, int, int, int]:
    script = f'''
tell application "Google Chrome"
  set b to bounds of window {int(window_index)}
  return (item 1 of b as text) & "," & (item 2 of b as text) & "," & (item 3 of b as text) & "," & (item 4 of b as text)
end tell
'''.strip()
    proc = subprocess.run(["osascript"], input=script, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or proc.stdout).strip() or "failed to read Chrome window bounds")
    raw = (proc.stdout or "").strip()
    parts = [int(p.strip()) for p in raw.split(",")]
    if len(parts) != 4:
        raise RuntimeError(f"invalid bounds from osascript: {raw}")
    return parts[0], parts[1], parts[2], parts[3]


def _capture_window_screenshot(window_index: int, out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        left, top, right, bottom = _window_bounds(window_index)
        width = max(1, right - left)
        height = max(1, bottom - top)
        rect = f"{left},{top},{width},{height}"
        proc = subprocess.run(
            ["screencapture", "-x", "-R", rect, str(out_path)],
            capture_output=True,
            text=True,
        )
        if proc.returncode != 0:
            raise RuntimeError((proc.stderr or proc.stdout).strip() or "screencapture failed")
    except Exception:
        # Screenshot failures should not block upload run.
        return


def _merchant_id_js() -> str:
    return r"""
(() => {
  const txt = document.body ? (document.body.innerText || '') : '';
  const m = txt.match(/ID\s*[-–:]?\s*(\d{6,})/i);
  if (m) return m[1];
  try {
    const lsMid = (localStorage.getItem('merchantUid') || '').trim();
    if (/^\d{6,}$/.test(lsMid)) return lsMid;
  } catch (e) {}
  try {
    const lastMid = (localStorage.getItem('lastSuccessUid') || '').trim();
    if (/^\d{6,}$/.test(lastMid)) return lastMid;
  } catch (e) {}
  const cands = Array.from(document.querySelectorAll('*')).map(el => String(el.textContent || '').trim()).filter(Boolean);
  for (const t of cands) {
    const mm = t.match(/ID\s*[-–:]?\s*(\d{6,})/i);
    if (mm) return mm[1];
  }
  return '';
})();
""".strip()


def parse_window_map(value: str) -> dict[str, int]:
    out: dict[str, int] = {}
    for chunk in str(value or "").split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        if ":" not in chunk:
            raise ValueError(f"invalid window map item: {chunk}")
        store, win = chunk.split(":", 1)
        code = normalize_store_code(store)
        out[code] = int(win.strip())
    return out


def list_store_window_candidates() -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for win in _list_chrome_windows():
        idx = int(win.get("index", 0))
        merchant_id = ""
        if idx > 0:
            try:
                merchant_id = _execute_tab_js(idx, _merchant_id_js()).strip()
            except Exception:
                merchant_id = ""
        out.append(
            {
                "window_index": idx,
                "merchant_id": merchant_id,
                "title": str(win.get("title") or ""),
                "url": str(win.get("url") or ""),
            }
        )
    return out


def _auto_map_windows(rows: list[dict[str, Any]]) -> dict[str, int]:
    target_mid_by_store: dict[str, str] = {}
    for row in rows:
        code = normalize_store_code(row.get("store_code"))
        if code and code not in target_mid_by_store:
            target_mid_by_store[code] = str(_to_int(row.get("merchant_id")))

    windows = _list_chrome_windows()
    resolved_by_mid: dict[str, int] = {}
    for win in windows:
        widx = int(win.get("index", 0))
        if widx <= 0:
            continue
        try:
            merchant_id = _execute_tab_js(widx, _merchant_id_js()).strip()
        except Exception:
            continue
        if merchant_id:
            resolved_by_mid[merchant_id] = widx

    out: dict[str, int] = {}
    for store, mid in target_mid_by_store.items():
        if mid in resolved_by_mid:
            out[store] = resolved_by_mid[mid]
    return out


def _choose_window_index(mapped_index: int, expected_merchant_id: str, candidates: list[dict[str, Any]]) -> int:
    expected = str(expected_merchant_id or "").strip()
    if not expected:
        return int(mapped_index)
    by_index: dict[int, str] = {}
    for c in candidates:
        try:
            idx = int(c.get("window_index", 0))
        except Exception:
            continue
        if idx <= 0:
            continue
        by_index[idx] = str(c.get("merchant_id") or "").strip()

    mapped_mid = by_index.get(int(mapped_index), "")
    if mapped_mid == expected:
        return int(mapped_index)

    for idx, mid in sorted(by_index.items()):
        if mid == expected:
            return idx
    return int(mapped_index)


def _step_search_js(variant_url: str) -> str:
    return f"""
(() => {{
  const needle = {json.dumps(str(variant_url), ensure_ascii=False)};
  const visible = (el) => {{
    if (!el) return false;
    const st = window.getComputedStyle(el);
    const r = el.getBoundingClientRect();
    return st.display !== 'none' && st.visibility !== 'hidden' && r.width > 0 && r.height > 0;
  }};
  const norm = (v) => String(v || '').trim().toLowerCase();
  const ctx = (el) => {{
    const parts = [el.placeholder, el.name, el.id, el.getAttribute && el.getAttribute('aria-label')];
    let p = el.parentElement;
    for (let i = 0; i < 3 && p; i++) {{
      parts.push(p.innerText || '');
      p = p.parentElement;
    }}
    return norm(parts.join(' '));
  }};
  const inputs = Array.from(document.querySelectorAll('input')).filter(visible)
    .filter(el => el.type !== 'hidden' && !el.disabled && !el.readOnly);
  const target = inputs.find(el => /ссылк|url|link|вариант|названию|артикул|поиск/i.test(ctx(el)));
  if (!target) return JSON.stringify({{ok:false, reason:'search_input_not_found'}});
  target.focus();
  target.select && target.select();
  target.value = needle;
  ['input','change','keyup'].forEach(ev => target.dispatchEvent(new Event(ev, {{ bubbles:true }})));
  target.dispatchEvent(new KeyboardEvent('keydown', {{ key:'Enter', bubbles:true }}));
  const buttons = Array.from(document.querySelectorAll('button')).filter(visible);
  const searchBtn = buttons.find(btn => /поиск/i.test(norm(btn.innerText)));
  if (searchBtn) searchBtn.click();
  return JSON.stringify({{ok:true}});
}})();
""".strip()


def _step_probe_search_input_js() -> str:
    return r"""
(() => {
  const visible = (el) => {
    if (!el) return false;
    const st = window.getComputedStyle(el);
    const r = el.getBoundingClientRect();
    return st.display !== 'none' && st.visibility !== 'hidden' && r.width > 0 && r.height > 0;
  };
  const norm = (v) => String(v || '').trim().toLowerCase();
  const ctx = (el) => {
    const parts = [el.placeholder, el.name, el.id, el.getAttribute && el.getAttribute('aria-label')];
    let p = el.parentElement;
    for (let i = 0; i < 3 && p; i++) {
      parts.push(p.innerText || '');
      p = p.parentElement;
    }
    return norm(parts.join(' '));
  };
  const inputs = Array.from(document.querySelectorAll('input')).filter(visible)
    .filter(el => el.type !== 'hidden' && !el.disabled && !el.readOnly);
  const target = inputs.find(el => /ссылк|url|link|вариант|названию|артикул|поиск/i.test(ctx(el)));
  return JSON.stringify({ok:true, ready: !!target});
})();
""".strip()


def _step_verify_offer_identity_js(expected_heading: str, offer_code: str) -> str:
    return f"""
(() => {{
  const heading = String({json.dumps(str(expected_heading), ensure_ascii=False)} || '').trim().toLowerCase();
  const offerCode = String({json.dumps(str(offer_code), ensure_ascii=False)} || '').trim();
  const txt = String(document.body?.innerText || '').toLowerCase();
  if (heading && !txt.includes(heading)) {{
    return JSON.stringify({{ok:false, reason:'heading_mismatch'}});
  }}
  if (offerCode) {{
    const rx = new RegExp('\\\\b' + offerCode + '\\\\b');
    if (!rx.test(String(document.body?.innerText || ''))) {{
      return JSON.stringify({{ok:false, reason:'offer_code_mismatch'}});
    }}
  }}
  return JSON.stringify({{ok:true}});
}})();
""".strip()


def _step_choose_card_js(expected_heading: str, offer_code: str = "") -> str:
    return f"""
(() => {{
  const expected = String({json.dumps(str(expected_heading), ensure_ascii=False)} || '').trim().toLowerCase();
  const offerCode = String({json.dumps(str(offer_code), ensure_ascii=False)} || '').trim();
  const visible = (el) => {{
    if (!el) return false;
    const st = window.getComputedStyle(el);
    const r = el.getBoundingClientRect();
    return st.display !== 'none' && st.visibility !== 'hidden' && r.width > 0 && r.height > 0;
  }};
  const norm = (v) => String(v || '').trim().toLowerCase();
  const collectContext = (el) => {{
    const roots = [
      '.product-list__item',
      '.product',
      '[class*="product-list__item"]',
      '[class*="product__"]',
      '[class*="product"]',
      'article',
      'li',
      'tr',
      '[class*="card"]',
      '[class*="item"]',
      '[class*="row"]',
    ];
    for (const selector of roots) {{
      const root = el.closest && el.closest(selector);
      if (root) {{
        const txt = norm(root.innerText);
        if (txt && txt.length <= 300) return txt;
      }}
    }}
    let node = el;
    for (let i = 0; i < 6 && node; i++) {{
      const txt = norm(node.innerText);
      if (txt && txt.length <= 300) return txt;
      node = node.parentElement;
    }}
    return norm(el.innerText);
  }};
  const headingScore = (txt) => {{
    if (!expected) return 0;
    if (txt.includes(expected)) return 100;
    return expected.split(/\\s+/).filter(token => token.length >= 3 && txt.includes(token)).length;
  }};
  const offerCodeScore = (txt) => {{
    if (!offerCode) return 0;
    const rx = new RegExp('\\\\b' + offerCode + '\\\\b');
    return rx.test(txt) ? 1000 : 0;
  }};
  const buttons = Array.from(document.querySelectorAll('button')).filter(visible);
  const chooseButtons = buttons.filter(btn => /выбрать/i.test(norm(btn.innerText)));
  let best = null;
  for (const btn of chooseButtons) {{
    const txt = collectContext(btn);
    const score = headingScore(txt) + offerCodeScore(txt);
    if (!best || score > best.score) {{
      best = {{ btn, score, txt }};
    }}
  }}
  if (best && best.score > 0) {{
    best.btn.click();
    return JSON.stringify({{
      ok: true,
      mode: 'scored_match',
      heading_score: headingScore(best.txt),
      offer_code_score: offerCodeScore(best.txt),
    }});
  }}
  if (chooseButtons.length > 0) {{
    chooseButtons[0].click();
    return JSON.stringify({{ok:true, mode:'fallback_first_choose'}});
  }}
  return JSON.stringify({{ok:false, reason:'choose_button_not_found'}});
}})();
""".strip()


def _step_select_size_js(size_rus: Any) -> str:
    return f"""
(() => {{
  const sizeNeedle = String({json.dumps(str(size_rus), ensure_ascii=False)});
  const visible = (el) => {{
    if (!el) return false;
    const st = window.getComputedStyle(el);
    const r = el.getBoundingClientRect();
    return st.display !== 'none' && st.visibility !== 'hidden' && r.width > 0 && r.height > 0;
  }};
  const norm = (v) => String(v || '').trim().toLowerCase();
  const querySizeOptions = () => Array.from(document.querySelectorAll('.matrix__values.icon-box, .matrix__values')).filter(visible);
  const sizeOptions = querySizeOptions();
  const availableSizes = sizeOptions.map(el => String(el.innerText || '').trim()).filter(Boolean);
  const getSelected = () => querySizeOptions().find(
    el => /active/i.test(String(el.className || '')) && norm(el.innerText) === norm(sizeNeedle)
  );
  const activateSize = (el) => {{
    if (!el) return;
    const targets = [el, el.firstElementChild].filter(Boolean);
    el.scrollIntoView && el.scrollIntoView({{ block: 'center', inline: 'center' }});
    targets.forEach(target => {{
      const rect = target.getBoundingClientRect ? target.getBoundingClientRect() : null;
      const init = {{
        bubbles: true,
        cancelable: true,
        view: window,
        clientX: rect ? rect.left + rect.width / 2 : 0,
        clientY: rect ? rect.top + rect.height / 2 : 0,
      }};
      ['pointerdown', 'mousedown', 'mouseup', 'click'].forEach(ev => {{
        try {{
          target.dispatchEvent(new MouseEvent(ev, init));
        }} catch (e) {{}}
      }});
      try {{
        target.click && target.click();
      }} catch (e) {{}}
    }});
  }};
  const matching = sizeOptions.filter(el => norm(el.innerText) === norm(sizeNeedle));
  const selectedBefore = getSelected();
  if (selectedBefore) {{
    return JSON.stringify({{
      ok: true,
      already_selected: true,
      selected_before: String(selectedBefore.innerText || '').trim(),
      available_sizes: availableSizes,
    }});
  }}
  if (matching.length === 0) {{
    const deepNode = Array.from(document.querySelectorAll('.matrix__values, .matrix__values *')).find(
      el => norm(el.innerText) === norm(sizeNeedle)
    );
    const fallback = deepNode && (deepNode.closest('.matrix__values') || deepNode);
    if (!fallback) {{
      return JSON.stringify({{
        ok: false,
        reason: 'size_option_not_found',
        available_sizes: availableSizes,
      }});
    }}
    activateSize(fallback);
    return JSON.stringify({{
      ok: true,
      clicked_target: String(fallback.innerText || '').trim(),
      selected_before: '',
      available_sizes: availableSizes,
    }});
  }}
  activateSize(matching[0]);
  return JSON.stringify({{
    ok: true,
    clicked_target: String(matching[0].innerText || '').trim(),
    selected_before: '',
    available_sizes: availableSizes,
  }});
}})();
""".strip()


def _step_verify_selected_size_js(size_rus: Any) -> str:
    return f"""
(() => {{
  const sizeNeedle = String({json.dumps(str(size_rus), ensure_ascii=False)});
  const visible = (el) => {{
    if (!el) return false;
    const st = window.getComputedStyle(el);
    const r = el.getBoundingClientRect();
    return st.display !== 'none' && st.visibility !== 'hidden' && r.width > 0 && r.height > 0;
  }};
  const norm = (v) => String(v || '').trim().toLowerCase();
  const sizeOptions = Array.from(document.querySelectorAll('.matrix__values.icon-box, .matrix__values')).filter(visible);
  const availableSizes = sizeOptions.map(el => String(el.innerText || '').trim()).filter(Boolean);
  const selected = sizeOptions.find(el => /active/i.test(String(el.className || '')));
  const selectedSize = selected ? String(selected.innerText || '').trim() : '';
  if (selected && norm(selected.innerText) === norm(sizeNeedle)) {{
    return JSON.stringify({{
      ok: true,
      selected_size: selectedSize,
      available_sizes: availableSizes,
    }});
  }}
  return JSON.stringify({{
    ok: false,
    reason: 'size_not_selected',
    selected_size: selectedSize,
    available_sizes: availableSizes,
  }});
}})();
""".strip()


def _step_fill_identity_fields_js(article: str, name: str) -> str:
    return f"""
(() => {{
  const articleVal = String({json.dumps(str(article), ensure_ascii=False)});
  const nameVal = String({json.dumps(str(name), ensure_ascii=False)});
  const visible = (el) => {{
    if (!el) return false;
    const st = window.getComputedStyle(el);
    const r = el.getBoundingClientRect();
    return st.display !== 'none' && st.visibility !== 'hidden' && r.width > 0 && r.height > 0;
  }};
  const norm = (v) => String(v || '').trim().toLowerCase();

  const contextText = (el) => {{
    const parts = [el.placeholder, el.name, el.id, el.getAttribute && el.getAttribute('aria-label')];
    let p = el.parentElement;
    for (let i = 0; i < 3 && p; i++) {{
      parts.push(p.innerText || '');
      p = p.parentElement;
    }}
    return norm(parts.join(' '));
  }};

  const inputs = Array.from(document.querySelectorAll('input')).filter(visible)
    .filter(el => el.type !== 'hidden' && !el.disabled && !el.readOnly);
  const articleInput = inputs.find(el => /артикул|article/i.test(contextText(el))) || inputs[0];
  const nameInput = inputs.find(el => /наименование|товар|название/i.test(contextText(el))) || inputs[1];
  if (!articleInput || !nameInput) return JSON.stringify({{ok:false, reason:'article_or_name_input_not_found'}});

  const setVal = (el, val) => {{
    el.focus();
    el.select && el.select();
    el.value = val;
    ['input','change','keyup'].forEach(ev => el.dispatchEvent(new Event(ev, {{ bubbles:true }})));
  }};
  setVal(articleInput, articleVal);
  setVal(nameInput, nameVal);

  if (String(articleInput.value || '').trim() !== articleVal.trim()) {{
    return JSON.stringify({{ok:false, reason:'article_value_not_applied'}});
  }}
  if (String(nameInput.value || '').trim() !== nameVal.trim()) {{
    return JSON.stringify({{ok:false, reason:'name_value_not_applied'}});
  }}

  const buttons = Array.from(document.querySelectorAll('button')).filter(visible);
  const nextBtn = buttons.find(btn => /продолжить/i.test(norm(btn.innerText)));
  if (!nextBtn) return JSON.stringify({{ok:false, reason:'continue_button_not_found'}});
  nextBtn.click();
  return JSON.stringify({{ok:true}});
}})();
""".strip()


def _step_fill_info_js(size_rus: Any, article: str, name: str) -> str:
    return _step_fill_identity_fields_js(article, name)


def _step_fill_price_js(price: Any, pp1: Any, pp2: Any, barcode: str) -> str:
    return f"""
(() => {{
  const priceVal = String({json.dumps(str(price), ensure_ascii=False)});
  const pp1Val = String({json.dumps(str(pp1), ensure_ascii=False)});
  const pp2Val = String({json.dumps(str(pp2), ensure_ascii=False)});
  const barcodeVal = String({json.dumps(str(barcode or ''), ensure_ascii=False)});
  const visible = (el) => {{
    if (!el) return false;
    const st = window.getComputedStyle(el);
    const r = el.getBoundingClientRect();
    return st.display !== 'none' && st.visibility !== 'hidden' && r.width > 0 && r.height > 0;
  }};
  const norm = (v) => String(v || '').trim().toLowerCase();
  const dialogs = Array.from(document.querySelectorAll('[role=\"dialog\"], .modal, .modal-content, .v-dialog, .dialog'))
    .filter(visible);
  const priceWarning = dialogs.find(el => /вы не указали цену/i.test(norm(el.innerText)));
  if (priceWarning) {{
    const warnButtons = Array.from(priceWarning.querySelectorAll('button')).filter(visible);
    const fixBtn = warnButtons.find(btn => /указать цену/i.test(norm(btn.innerText)));
    if (fixBtn) {{
      fixBtn.click();
      return JSON.stringify({{ok:false, reason:'price_required_popup_closed'}});
    }}
    return JSON.stringify({{ok:false, reason:'price_required_popup_no_button'}});
  }}

  let container = dialogs.find(el => /цена и остатки/i.test(norm(el.innerText)));
  if (!container) {{
    const candidates = Array.from(document.querySelectorAll('section,div')).filter(visible);
    container = candidates.find(el => /цена и остатки/i.test(norm(el.innerText)) && el.querySelectorAll('input').length >= 3) || null;
  }}
  if (!container) return JSON.stringify({{ok:false, reason:'price_modal_not_found'}});

  const inputs = Array.from(container.querySelectorAll('input')).filter(visible)
    .filter(el => el.type !== 'hidden' && !el.disabled && !el.readOnly);
  if (inputs.length < 2) return JSON.stringify({{ok:false, reason:'not_enough_inputs_for_price_stock'}});

  const ctx = (el) => {{
    const parts = [el.placeholder, el.name, el.id, el.getAttribute && el.getAttribute('aria-label')];
    let p = el.parentElement;
    for (let i = 0; i < 3 && p; i++) {{
      parts.push(p.innerText || '');
      p = p.parentElement;
    }}
    return norm(parts.join(' '));
  }};

  const pick = (regexes) => inputs.find(el => regexes.some(rx => rx.test(ctx(el))));
  let priceInput = pick([/цена/, /price/, /₸/i]) || inputs[0];
  const used = new Set([priceInput]);
  let barcodeInput = null;
  const barcodeCandidate = pick([/штрих|barcode|bar code/i]);
  if (barcodeCandidate && !used.has(barcodeCandidate)) {{
    barcodeInput = barcodeCandidate;
    used.add(barcodeInput);
  }}
  const stockInputs = inputs.filter(el => !used.has(el));
  if (stockInputs.length < 1) return JSON.stringify({{ok:false, reason:'stock_inputs_not_found'}});

  const setVal = (el, val) => {{
    el.focus();
    el.select && el.select();
    const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
    setter.call(el, String(val || ''));
    el.dispatchEvent(new InputEvent('input', {{ bubbles:true, inputType:'insertText', data: String(val || '') }}));
    el.dispatchEvent(new Event('change', {{ bubbles:true }}));
    el.dispatchEvent(new KeyboardEvent('keyup', {{ key:'0', bubbles:true }}));
    el.blur && el.blur();
  }};

  setVal(priceInput, priceVal);
  setVal(stockInputs[0], pp1Val);
  if (stockInputs[1]) setVal(stockInputs[1], pp2Val);
  if (barcodeInput) setVal(barcodeInput, barcodeVal);

  const digits = (v) => String(v || '').replace(/[^0-9]/g, '');
  if (digits(priceInput.value) !== digits(priceVal)) return JSON.stringify({{ok:false, reason:'price_value_not_applied'}});
  if (digits(stockInputs[0].value) !== digits(pp1Val)) return JSON.stringify({{ok:false, reason:'pp1_value_not_applied'}});
  if (stockInputs[1] && digits(stockInputs[1].value) !== digits(pp2Val)) return JSON.stringify({{ok:false, reason:'pp2_value_not_applied'}});
  if (barcodeInput && barcodeVal && digits(barcodeInput.value) !== digits(barcodeVal)) {{
    return JSON.stringify({{ok:false, reason:'barcode_value_not_applied'}});
  }}
  return JSON.stringify({{ok:true, reason:'price_values_applied'}});
}})();
""".strip()


def _step_click_save_price_js() -> str:
    return r"""
(() => {
  const visible = (el) => {
    if (!el) return false;
    const st = window.getComputedStyle(el);
    const r = el.getBoundingClientRect();
    return st.display !== 'none' && st.visibility !== 'hidden' && r.width > 0 && r.height > 0;
  };
  const norm = (v) => String(v || '').trim().toLowerCase();
  const dialogs = Array.from(document.querySelectorAll('[role="dialog"], .modal, .modal-content, .v-dialog, .dialog'))
    .filter(visible);
  const priceWarning = dialogs.find(el => /вы не указали цену/i.test(norm(el.innerText)));
  if (priceWarning) {
    const warnButtons = Array.from(priceWarning.querySelectorAll('button')).filter(visible);
    const fixBtn = warnButtons.find(btn => /указать цену/i.test(norm(btn.innerText)));
    if (fixBtn) {
      fixBtn.click();
      return JSON.stringify({ok:false, reason:'price_required_popup_closed'});
    }
    return JSON.stringify({ok:false, reason:'price_required_popup_no_button'});
  }

  const scope = dialogs.find(el => /цена и остатки/i.test(norm(el.innerText))) || document;
  const buttons = Array.from(scope.querySelectorAll('button')).filter(visible);
  const globalButtons = Array.from(document.querySelectorAll('button')).filter(visible);
  const saveBtn = buttons.find(btn => /сохранить изменения/i.test(norm(btn.innerText)))
    || globalButtons.find(btn => /сохранить изменения/i.test(norm(btn.innerText)));
  if (!saveBtn) return JSON.stringify({ok:false, reason:'save_button_not_found'});
  saveBtn.click();
  return JSON.stringify({ok:true});
})();
""".strip()


def _step_detect_price_warning_js() -> str:
    return r"""
(() => {
  const visible = (el) => {
    if (!el) return false;
    const st = window.getComputedStyle(el);
    const r = el.getBoundingClientRect();
    return st.display !== 'none' && st.visibility !== 'hidden' && r.width > 0 && r.height > 0;
  };
  const norm = (v) => String(v || '').trim().toLowerCase();
  const dialogs = Array.from(document.querySelectorAll('[role="dialog"], .modal, .modal-content, .v-dialog, .dialog'))
    .filter(visible);
  const warning = dialogs.find(el => /вы не указали цену/i.test(norm(el.innerText)));
  return JSON.stringify({ok:true, warning_present: !!warning});
})();
""".strip()


def _step_verify_success_js() -> str:
    return r"""
(() => {
  const txt = String(document.body?.innerText || '');
  if (/ваш товар успешно добавлен/i.test(txt)) return JSON.stringify({ok:true});
  if (/товар.*уже существует|уже есть в вашем магазине|уже добавлен/i.test(txt)) {
    return JSON.stringify({ok:true, mode:'already_exists'});
  }
  return JSON.stringify({ok:false, reason:'success_message_not_found'});
})();
""".strip()


def _json_result(raw: str) -> dict[str, Any]:
    try:
        obj = json.loads(raw or "{}")
        if isinstance(obj, dict):
            return obj
    except Exception:
        pass
    return {"ok": False, "reason": f"invalid_json:{raw}"}


def _write_run_log_header(path: Path) -> None:
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


def _append_run_log(path: Path, row: list[Any]) -> None:
    with path.open("a", newline="", encoding="utf-8") as fh:
        wr = csv.writer(fh)
        wr.writerow(row)


def _execute_row(
    window_index: int,
    row: dict[str, Any],
    *,
    step_delay_seconds: float,
    step_screenshot_dir: Path | None = None,
) -> tuple[bool, str]:
    expected_mid = str(_to_int(row.get("merchant_id")))
    normalized_barcode = _normalize_barcode(row.get("barcode"))
    normalized_size_rus = _normalize_size_rus(row.get("size_rus"))
    offer_code = _extract_offer_code_from_variant_url(row.get("variant_url"))

    def resolve_window() -> int:
        nonlocal window_index
        try:
            candidates = list_store_window_candidates()
            window_index = _choose_window_index(int(window_index), expected_mid, candidates)
        except Exception:
            window_index = int(window_index)
        return int(window_index)

    def snap(stage: str) -> None:
        if not step_screenshot_dir:
            return
        row_no = int(row.get("row_number") or 0)
        article = re.sub(r"[^a-zA-Z0-9._-]+", "_", str(row.get("merchant_sku_article") or "")).strip("_")
        if not article:
            article = "no_article"
        fname = f"row{row_no:04d}_{stage}_{article[:80]}.png"
        _capture_window_screenshot(resolve_window(), step_screenshot_dir / fname)

    entry_url = str(row.get("ui_entry_url") or DEFAULT_ENTRY_URL)
    direct_code_mode = ("link-catalog" in entry_url.lower()) and ("code=" in entry_url.lower())

    _set_tab_url(resolve_window(), entry_url)
    time.sleep(max(step_delay_seconds, 1.2))
    snap("01_open")

    mid_raw = _execute_tab_js(resolve_window(), _merchant_id_js())
    actual_mid = str(mid_raw or "").strip()
    if actual_mid and actual_mid != expected_mid:
        snap("err_mid_mismatch")
        return False, f"merchant_id_mismatch window={actual_mid} expected={expected_mid}"

    if not direct_code_mode:
        search_ready = False
        for _ in range(12):
            probe = _json_result(_execute_tab_js(resolve_window(), _step_probe_search_input_js()))
            if bool(probe.get("ready")):
                search_ready = True
                break
            time.sleep(0.8)
        if not search_ready:
            snap("err_search")
            return False, "search_failed:search_input_not_found"

        res = _json_result(_execute_tab_js(resolve_window(), _step_search_js(str(row.get("variant_url") or ""))))
        if not res.get("ok"):
            snap("err_search")
            return False, f"search_failed:{res.get('reason')}"
        time.sleep(step_delay_seconds)
        snap("02_search")

        res = _json_result(
            _execute_tab_js(
                resolve_window(),
                _step_choose_card_js(
                    str(row.get("expected_kaspi_heading") or ""),
                    offer_code,
                ),
            )
        )
        if not res.get("ok"):
            snap("err_choose")
            return False, f"choose_card_failed:{res.get('reason')}"
        time.sleep(step_delay_seconds + 0.6)
        snap("03_choose")

        identity_check = _json_result(
            _execute_tab_js(
                resolve_window(),
                _step_verify_offer_identity_js(
                    str(row.get("expected_kaspi_heading") or ""),
                    "",
                ),
            )
        )
        if not identity_check.get("ok"):
            snap("err_identity")
            return False, f"identity_failed:{identity_check.get('reason')}"
    else:
        snap("02_direct_code_entry")

    size_ok = False
    size_reason = ""
    for i in range(4):
        res_select = _json_result(
            _execute_tab_js(
                resolve_window(),
                _step_select_size_js(normalized_size_rus),
            )
        )
        if not res_select.get("ok"):
            size_reason = str(res_select.get("reason") or "size_select_failed")
            snap("err_size_select")
            break
        time.sleep(step_delay_seconds + 0.4)
        res_verify = _json_result(
            _execute_tab_js(
                resolve_window(),
                _step_verify_selected_size_js(normalized_size_rus),
            )
        )
        if res_verify.get("ok"):
            size_ok = True
            break
        size_reason = str(res_verify.get("reason") or "size_not_selected")
        snap(f"04_size_retry{i+1}")
        time.sleep(step_delay_seconds + 0.4)
    if not size_ok:
        snap("err_fill_info")
        return False, f"fill_info_failed:{size_reason}"
    snap("04_size_selected")

    identity_check = _json_result(
        _execute_tab_js(
            resolve_window(),
            _step_verify_offer_identity_js(
                str(row.get("expected_kaspi_heading") or ""),
                offer_code,
            ),
        )
    )
    if not identity_check.get("ok"):
        snap("err_identity")
        return False, f"identity_failed:{identity_check.get('reason')}"

    res = _json_result(
        _execute_tab_js(
            resolve_window(),
            _step_fill_identity_fields_js(
                str(row.get("merchant_sku_article") or ""),
                str(row.get("merchant_offer_name") or ""),
            ),
        )
    )
    if not res.get("ok"):
        snap("err_fill_info")
        return False, f"fill_info_failed:{res.get('reason')}"
    time.sleep(step_delay_seconds + 0.6)
    snap("05_fill_info")

    price_ok = False
    price_reason = ""
    for i in range(4):
        res_fill = _json_result(
            _execute_tab_js(
                resolve_window(),
                _step_fill_price_js(
                    row.get("price_kzt"),
                    row.get("stock_pp1"),
                    row.get("stock_pp2"),
                    normalized_barcode,
                ),
            )
        )
        if not res_fill.get("ok"):
            price_reason = str(res_fill.get("reason") or "unknown")
            snap(f"06_fill_price_retry{i+1}")
            if price_reason in {
                "price_required_popup_closed",
                "price_modal_not_found",
                "not_enough_inputs_for_price_stock",
                "pp_inputs_not_found",
            }:
                time.sleep(step_delay_seconds + 0.8)
                continue
            break

        # Let UI model settle before attempting save; this prevents race with validation popups.
        time.sleep(step_delay_seconds + 0.9)
        res_save = _json_result(_execute_tab_js(resolve_window(), _step_click_save_price_js()))
        if res_save.get("ok"):
            # Server-side validation is async; verify popup does not reappear after save.
            time.sleep(step_delay_seconds + 0.6)
            post_save = _json_result(_execute_tab_js(resolve_window(), _step_detect_price_warning_js()))
            if bool(post_save.get("warning_present")):
                price_reason = "price_required_popup_after_save"
                snap(f"06_fill_price_retry{i+1}")
                # Attempt to close popup and retry fill+save.
                _execute_tab_js(resolve_window(), _step_click_save_price_js())
                time.sleep(step_delay_seconds + 0.8)
                continue
            price_ok = True
            break
        price_reason = str(res_save.get("reason") or "unknown")
        snap(f"05_fill_price_retry{i+1}")
        if price_reason in {
            "price_required_popup_closed",
            "price_required_popup_no_button",
            "save_button_not_found",
        }:
            time.sleep(step_delay_seconds + 0.8)
            continue
        break
    if not price_ok:
        snap("err_fill_price")
        return False, f"fill_price_failed:{price_reason}"
    time.sleep(step_delay_seconds + 0.6)
    snap("06_fill_price")

    verify_ok = False
    verify_reason = "success_message_not_found"
    for i in range(5):
        res = _json_result(_execute_tab_js(resolve_window(), _step_verify_success_js()))
        if res.get("ok"):
            verify_ok = True
            break
        verify_reason = str(res.get("reason") or "success_message_not_found")
        snap(f"07_verify_retry{i+1}")
        time.sleep(step_delay_seconds + 0.5)
    if not verify_ok:
        snap("err_verify")
        return False, f"verify_failed:{verify_reason}"
    snap("08_success")

    return True, ""


def run_kaspi_offer_ui_upload(
    *,
    workbook_path: str | Path,
    store_codes: str | list[str],
    dry_run: bool,
    confirm: bool,
    output_root: str | Path = "runs/kaspi_offer_ui_upload",
    window_map: str | None = None,
    step_delay_seconds: float = 1.8,
    retries: int = 1,
    step_screenshots: bool = False,
) -> dict[str, Any]:
    stores = parse_store_codes(store_codes)
    if not stores:
        stores = ["UNIVERSAL", "STOREB"]

    rows = load_offer_upload_rows(workbook_path, store_codes=stores)
    validation = validate_upload_rows(rows, required_store_codes=stores)

    run_id = datetime.now(ASTANA_TZ).strftime("%Y%m%d_%H%M%S")
    run_dir = Path(output_root) / run_id
    summary: dict[str, Any] = {
        "run_id": run_id,
        "status": "dry_run" if dry_run or not confirm else "success",
        "workbook_path": str(workbook_path),
        "store_codes": stores,
        "rows_total": len(rows),
        "rows_by_store": validation.get("rows_by_store", {}),
        "validation": validation,
        "run_dir": str(run_dir),
        "run_log_csv": str(run_dir / "run_log.csv"),
        "success": 0,
        "failed": 0,
        "errors": [],
        "window_map": {},
        "step_screenshots": bool(step_screenshots),
    }

    if not validation.get("ok"):
        summary["status"] = "preflight_failed"
        summary["errors"] = list(validation.get("errors", []))
        return summary

    if dry_run or not confirm:
        return summary

    run_dir.mkdir(parents=True, exist_ok=True)
    run_log_path = run_dir / "run_log.csv"
    _write_run_log_header(run_log_path)

    if window_map:
        mapped = parse_window_map(window_map)
    else:
        mapped = _auto_map_windows(rows)
    summary["window_map"] = mapped

    for store in stores:
        if store not in mapped:
            summary["status"] = "preflight_failed"
            summary["errors"].append(f"window mapping missing for store {store}")
    if summary["status"] == "preflight_failed":
        return summary

    max_attempts = max(1, int(retries) + 1)
    for row in rows:
        store = normalize_store_code(row.get("store_code"))
        candidates = list_store_window_candidates()
        expected_mid = str(_to_int(row.get("merchant_id")))
        mapped_win = int(mapped[store])
        win = _choose_window_index(mapped_win, expected_mid, candidates)
        mapped[store] = int(win)
        summary["window_map"] = dict(mapped)
        ok = False
        err = ""
        row_shots_dir = (run_dir / "screenshots") if step_screenshots else None
        for _ in range(max_attempts):
            try:
                ok, err = _execute_row(
                    win,
                    row,
                    step_delay_seconds=step_delay_seconds,
                    step_screenshot_dir=row_shots_dir,
                )
            except Exception as exc:
                ok, err = False, f"exception:{exc}"
            if ok:
                break
            time.sleep(0.6)

        if ok:
            summary["success"] += 1
            status = "ok"
        else:
            summary["failed"] += 1
            status = "failed"
            summary["errors"].append(
                f"row {row.get('row_number')} store {store}: {err}"
            )

        _append_run_log(
            run_log_path,
            [
                datetime.now(ASTANA_TZ).isoformat(),
                store,
                row.get("merchant_id"),
                row.get("row_number"),
                row.get("merchant_sku_article"),
                row.get("variant_url"),
                status,
                err,
            ],
        )

    if summary["failed"] > 0:
        summary["status"] = "partial" if summary["success"] > 0 else "failed"

    (run_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Kaspi offer UI upload runner")
    parser.add_argument("--workbook", help="Workbook path with 'offer upload' sheet")
    parser.add_argument("--stores", default="UNIVERSAL,STOREB", help="Comma-separated store codes")
    parser.add_argument("--dry-run", action="store_true", help="Only validate and build summary")
    parser.add_argument("--confirm", action="store_true", help="Execute real UI writes")
    parser.add_argument("--output-root", default="runs/kaspi_offer_ui_upload", help="Run artifacts root")
    parser.add_argument("--window-map", help="Manual mapping STORE:WINDOW_INDEX,...")
    parser.add_argument("--step-delay-seconds", type=float, default=1.8, help="Delay between UI steps")
    parser.add_argument("--retries", type=int, default=1, help="Retries per row")
    parser.add_argument("--step-screenshots", action="store_true", help="Capture window screenshots for each row step")
    parser.add_argument("--list-windows", action="store_true", help="Print Chrome window candidates with detected merchant IDs")
    parser.add_argument("--json", action="store_true", help="Print JSON summary")
    args = parser.parse_args(argv)

    if args.list_windows:
        windows = list_store_window_candidates()
        if args.json:
            print(json.dumps({"windows": windows}, ensure_ascii=False, indent=2))
        else:
            for w in windows:
                print(
                    f"window={w['window_index']} merchant_id={w['merchant_id'] or '-'} "
                    f"title={w['title'][:80]} url={w['url'][:120]}"
                )
        return 0

    if not args.workbook:
        parser.error("--workbook is required unless --list-windows is used")

    summary = run_kaspi_offer_ui_upload(
        workbook_path=args.workbook,
        store_codes=args.stores,
        dry_run=args.dry_run,
        confirm=args.confirm,
        output_root=args.output_root,
        window_map=args.window_map,
        step_delay_seconds=args.step_delay_seconds,
        retries=args.retries,
        step_screenshots=args.step_screenshots,
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
