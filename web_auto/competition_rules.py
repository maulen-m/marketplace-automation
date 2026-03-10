from __future__ import annotations

import re
from typing import Any

COMPETITION_SCOPE_DELIVERY_DAYS_THRESHOLD = 10

COMPETITION_SCOPE_FLOOR_SKU_KEYS: dict[str, str] = {
    "LINE52": "CL_OC_MEN_LINE52_BLACK",
    "HUS": "CL_NEW-CLO2_MEN_HUS_GREEN",
    "MEN_ROMBIK": "CL_NEW-CLO_MEN_ROMBIK_BLACK",
    "KID_ROMBIK": "CL_NEW-CLO_KID_ROMBIK_BLACK",
    "KID31": "CL_NEW-CLO_KIDS_KID-31_BLACK",
}

_TOKEN_SPLIT_RE = re.compile(r"[^A-Z0-9]+")


def normalize_token_text(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).upper()
    text = _TOKEN_SPLIT_RE.sub(" ", text)
    return " ".join(text.split())


def build_row_haystack(row: dict[str, Any] | None, row_text: str | None = "") -> str:
    parts: list[str] = []
    if isinstance(row, dict):
        for key in (
            "resolved_sku_key",
            "merchant_sku",
            "kaspi_sku",
            "merchant_title",
            "resolved_kaspi_offer_name",
            "kaspi_offer_name",
            "link",
            "url",
        ):
            val = row.get(key)
            if val:
                parts.append(str(val))
    if row_text:
        parts.append(str(row_text))
    return " ".join(normalize_token_text(part) for part in parts if part)


def classify_competition_scope(row: dict[str, Any] | None, row_text: str | None = "") -> str | None:
    haystack = build_row_haystack(row, row_text)
    if not haystack:
        return None

    if "KID 31" in haystack or "KID31" in haystack:
        return "KID31"
    if "ROMBIK" in haystack and (" KID " in f" {haystack} " or " KIDS " in f" {haystack} "):
        return "KID_ROMBIK"
    if "ROMBIK" in haystack:
        return "MEN_ROMBIK"
    if "HUS" in haystack:
        return "HUS"
    if "LINE52" in haystack:
        return "LINE52"
    return None


def competition_floor_kzt(scope: str | None, floor_by_sku_key: dict[str, int] | None) -> int | None:
    if not scope or not floor_by_sku_key:
        return None
    sku_key = COMPETITION_SCOPE_FLOOR_SKU_KEYS.get(str(scope))
    if not sku_key:
        return None
    raw = floor_by_sku_key.get(sku_key)
    if raw is None:
        return None
    try:
        floor = int(raw)
    except Exception:
        return None
    return floor if floor > 0 else None


def scoped_competitor_ignore_reason(
    *,
    scope: str | None,
    competitor_price_kzt: int | float | None,
    delivery_days: int | None,
    competitive_floor_kzt: int | None,
) -> str:
    if not scope:
        return ""
    if delivery_days is not None:
        try:
            if int(delivery_days) >= COMPETITION_SCOPE_DELIVERY_DAYS_THRESHOLD:
                return "competition_scope_long_delivery_10plus_days"
        except Exception:
            pass
    if competitive_floor_kzt is None or competitor_price_kzt is None:
        return ""
    try:
        if float(competitor_price_kzt) < int(competitive_floor_kzt):
            return "competition_scope_competitor_price_below_min_floor"
    except Exception:
        return ""
    return ""
