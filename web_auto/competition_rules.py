from __future__ import annotations

from dataclasses import dataclass, field
import re
from typing import Any
from inventory.size_text_extraction import extract_size_from_offer_text, extract_size_from_url

# Owner decision 2026-07-03: any competitor delivery more than 7 days out is
# treated as pre-order competition and ignored globally for both stores.
GLOBAL_LONG_DELIVERY_DAYS_THRESHOLD = 8
GLOBAL_LONG_DELIVERY_IGNORE_REASON = "global_long_delivery_8plus_days"
COMPETITION_SCOPE_DELIVERY_DAYS_THRESHOLD = 10
LINE52_PROBABLE_3XL_FLOOR_KZT = 8845
LINE52_PUBLIC_SIZE_FLOOR_OVERRIDES_KZT: dict[str, int] = {}

COMPETITION_SCOPE_FLOOR_SKU_KEYS: dict[str, str] = {
    "LINE52": "CL_OC_MEN_LINE52_BLACK",
    "HUS": "CL_NEW-CLO2_MEN_HUS_GREEN",
    "MEN_ROMBIK": "CL_NEW-CLO_MEN_ROMBIK_BLACK",
    "KID_ROMBIK": "CL_NEW-CLO_KID_ROMBIK_BLACK",
    "KID31": "CL_NEW-CLO_KIDS_KID-31_BLACK",
}

LINE52_PARTNER_COMPETITIVE_MIDS = {
    "12456074",  # ИП Бектасова А А
    "30431580",  # The One Million DLL
}

LINE52_PARTNER_COMPETITIVE_NAME_TOKENS = (
    "ип бектасова а а",
    "the one million dll",
)

_TOKEN_SPLIT_RE = re.compile(r"[^A-Z0-9]+")
_EXPLICIT_3XL_RE = re.compile(r"(?<![A-Z0-9])3XL(?![A-Z0-9])")
_EXPLICIT_4PLUS_RE = re.compile(r"(?<![A-Z0-9])(?:4XL|5XL|6XL|7XL|8XL)(?![A-Z0-9])")
_LINE52_MANUAL_4XL_EXCEPTION_SKUS = {
    "CL_OC_MEN_LINE52_BLACK_117193721_58_(4XL)",
}


@dataclass
class Line52PricingProfile:
    default_profile: str = "aggressive"
    probable_3xl_profile: str = "aggressive"
    conservative_floor_kzt: int = LINE52_PROBABLE_3XL_FLOOR_KZT
    profile_by_sku_key: dict[str, str] = field(default_factory=dict)
    profile_by_url: dict[str, str] = field(default_factory=dict)
    floor_by_public_size: dict[str, int] = field(default_factory=dict)


DEFAULT_LINE52_PRICING_PROFILE = Line52PricingProfile()
_ACTIVE_LINE52_PRICING_PROFILE = Line52PricingProfile()


def set_line52_pricing_profile(profile: Line52PricingProfile | Any | None) -> None:
    global _ACTIVE_LINE52_PRICING_PROFILE
    if profile is None:
        _ACTIVE_LINE52_PRICING_PROFILE = Line52PricingProfile(
            default_profile=DEFAULT_LINE52_PRICING_PROFILE.default_profile,
            probable_3xl_profile=DEFAULT_LINE52_PRICING_PROFILE.probable_3xl_profile,
            conservative_floor_kzt=DEFAULT_LINE52_PRICING_PROFILE.conservative_floor_kzt,
            floor_by_public_size=dict(DEFAULT_LINE52_PRICING_PROFILE.floor_by_public_size),
        )
        return

    default_profile = str(getattr(profile, "default_profile", "aggressive")).strip().lower()
    probable_3xl_profile = str(getattr(profile, "probable_3xl_profile", "aggressive")).strip().lower()
    conservative_floor_kzt = int(getattr(profile, "conservative_floor_kzt", LINE52_PROBABLE_3XL_FLOOR_KZT))
    profile_by_sku_key_raw = getattr(profile, "profile_by_sku_key", {}) or {}
    profile_by_url_raw = getattr(profile, "profile_by_url", {}) or {}
    floor_by_public_size_raw = getattr(profile, "floor_by_public_size", {}) or {}

    if default_profile not in {"aggressive", "conservative"}:
        raise ValueError("line52 default_profile must be aggressive or conservative")
    if probable_3xl_profile not in {"aggressive", "conservative"}:
        raise ValueError("line52 probable_3xl_profile must be aggressive or conservative")
    if conservative_floor_kzt <= 0:
        raise ValueError("line52 conservative_floor_kzt must be positive")
    if not isinstance(profile_by_sku_key_raw, dict):
        raise ValueError("line52 profile_by_sku_key must be a mapping")
    if not isinstance(profile_by_url_raw, dict):
        raise ValueError("line52 profile_by_url must be a mapping")
    if not isinstance(floor_by_public_size_raw, dict):
        raise ValueError("line52 floor_by_public_size must be a mapping")

    valid_profiles = {"aggressive", "conservative"}
    profile_by_sku_key: dict[str, str] = {}
    for key, value in profile_by_sku_key_raw.items():
        normalized_key = str(key or "").strip().upper()
        normalized_profile = str(value or "").strip().lower()
        if not normalized_key:
            continue
        if normalized_profile not in valid_profiles:
            raise ValueError("line52 profile_by_sku_key values must be aggressive or conservative")
        profile_by_sku_key[normalized_key] = normalized_profile

    profile_by_url: dict[str, str] = {}
    for key, value in profile_by_url_raw.items():
        normalized_key = _normalize_offer_url_key(key)
        normalized_profile = str(value or "").strip().lower()
        if not normalized_key:
            continue
        if normalized_profile not in valid_profiles:
            raise ValueError("line52 profile_by_url values must be aggressive or conservative")
        profile_by_url[normalized_key] = normalized_profile

    floor_by_public_size: dict[str, int] = {}
    for key, value in floor_by_public_size_raw.items():
        normalized_key = _normalize_public_size_key(key)
        if not normalized_key:
            raise ValueError("line52 floor_by_public_size keys must be valid adult public sizes")
        try:
            normalized_floor = int(value)
        except Exception as exc:  # pragma: no cover - defensive branch
            raise ValueError("line52 floor_by_public_size values must be positive integers") from exc
        if normalized_floor <= 0:
            raise ValueError("line52 floor_by_public_size values must be positive integers")
        floor_by_public_size[normalized_key] = normalized_floor

    _ACTIVE_LINE52_PRICING_PROFILE = Line52PricingProfile(
        default_profile=default_profile,
        probable_3xl_profile=probable_3xl_profile,
        conservative_floor_kzt=conservative_floor_kzt,
        profile_by_sku_key=profile_by_sku_key,
        profile_by_url=profile_by_url,
        floor_by_public_size=floor_by_public_size,
    )


def reset_line52_pricing_profile() -> None:
    set_line52_pricing_profile(DEFAULT_LINE52_PRICING_PROFILE)


def get_line52_pricing_profile() -> Line52PricingProfile:
    return Line52PricingProfile(
        default_profile=_ACTIVE_LINE52_PRICING_PROFILE.default_profile,
        probable_3xl_profile=_ACTIVE_LINE52_PRICING_PROFILE.probable_3xl_profile,
        conservative_floor_kzt=_ACTIVE_LINE52_PRICING_PROFILE.conservative_floor_kzt,
        profile_by_sku_key=dict(_ACTIVE_LINE52_PRICING_PROFILE.profile_by_sku_key),
        profile_by_url=dict(_ACTIVE_LINE52_PRICING_PROFILE.profile_by_url),
        floor_by_public_size=dict(_ACTIVE_LINE52_PRICING_PROFILE.floor_by_public_size),
    )


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


def _row_size_value(row: dict[str, Any] | None, *keys: str) -> str:
    if not isinstance(row, dict):
        return ""
    for key in keys:
        value = str(row.get(key) or "").strip()
        if value:
            return value
    return ""


def _normalize_offer_url_key(value: Any) -> str:
    text = str(value or "").strip().lower()
    if not text:
        return ""
    text = text.split("?", 1)[0].split("#", 1)[0].rstrip("/")
    return text


def _normalize_public_size_key(value: Any) -> str:
    text = str(value or "").strip().upper()
    if not text:
        return ""
    text = text.replace("Х", "X")
    if text == "XXL":
        text = "2XL"
    elif text == "XXXL":
        text = "3XL"
    elif text == "XXXXL":
        text = "4XL"
    return text if text in {"XS", "S", "M", "L", "XL", "2XL", "3XL", "4XL"} else ""


def line52_public_size_hint(row: dict[str, Any] | None, row_text: str | None = "") -> str:
    if not isinstance(row, dict):
        return ""

    raw_url = _row_size_value(row, "resolved_url", "link", "url")
    raw_name = _row_size_value(row, "resolved_kaspi_offer_name", "kaspi_offer_name", "merchant_title")
    blob = normalize_token_text(" ".join(x for x in (raw_url, raw_name, str(row_text or "")) if x))
    has_explicit_3xl = bool(_EXPLICIT_3XL_RE.search(blob))
    has_explicit_4plus = bool(_EXPLICIT_4PLUS_RE.search(blob))
    if has_explicit_3xl and not has_explicit_4plus:
        return "3XL"
    if has_explicit_4plus and not has_explicit_3xl:
        return "4XL"

    candidates: set[str] = set()
    for key in ("size_from_url", "size_from_offer_name", "effective_final_size", "final_attached_size", "Human_edit_size"):
        val = _normalize_public_size_key(row.get(key))
        if val:
            candidates.add(val)

    if raw_url:
        size_url, domain_url = extract_size_from_url(raw_url)
        if domain_url == "adult":
            val = _normalize_public_size_key(size_url)
            if val:
                candidates.add(val)
    if raw_name:
        size_name, domain_name = extract_size_from_offer_text(raw_name)
        if domain_name == "adult":
            val = _normalize_public_size_key(size_name)
            if val:
                candidates.add(val)

    return next(iter(candidates)) if len(candidates) == 1 else ""


def is_probable_line52_3xl(row: dict[str, Any] | None, row_text: str | None = "") -> bool:
    if classify_competition_scope(row, row_text) != "LINE52":
        return False
    sku = _row_size_value(row, "merchant_sku", "SKU", "sku")
    if sku.strip().upper() in _LINE52_MANUAL_4XL_EXCEPTION_SKUS:
        return False
    return line52_public_size_hint(row, row_text) == "3XL"


def resolve_line52_pricing_profile(row: dict[str, Any] | None, row_text: str | None = "") -> str | None:
    if classify_competition_scope(row, row_text) != "LINE52":
        return None
    profile = get_line52_pricing_profile()
    url_key = _normalize_offer_url_key(_row_size_value(row, "resolved_url", "link", "url"))
    if url_key and url_key in profile.profile_by_url:
        return profile.profile_by_url[url_key]
    sku_key = _row_size_value(row, "resolved_sku_key").upper()
    if sku_key and sku_key in profile.profile_by_sku_key:
        return profile.profile_by_sku_key[sku_key]
    if is_probable_line52_3xl(row, row_text):
        return profile.probable_3xl_profile
    return profile.default_profile


def resolve_line52_floor_override_kzt(row: dict[str, Any] | None, row_text: str | None = "") -> int | None:
    if classify_competition_scope(row, row_text) != "LINE52":
        return None
    public_size = line52_public_size_hint(row, row_text)
    if not public_size:
        return None
    raw = get_line52_pricing_profile().floor_by_public_size.get(public_size)
    if raw is None:
        return None
    try:
        floor = int(raw)
    except Exception:
        return None
    return floor if floor > 0 else None


def effective_competition_floor_kzt(
    row: dict[str, Any] | None,
    floor_by_sku_key: dict[str, int] | None,
    row_text: str | None = "",
) -> int | None:
    scope = classify_competition_scope(row, row_text)
    base_floor = competition_floor_kzt(scope, floor_by_sku_key)
    if scope != "LINE52":
        return base_floor
    size_floor_override = resolve_line52_floor_override_kzt(row, row_text)
    if size_floor_override is not None:
        return int(size_floor_override)
    profile_name = resolve_line52_pricing_profile(row, row_text)
    if profile_name != "conservative":
        return base_floor
    conservative_floor = get_line52_pricing_profile().conservative_floor_kzt
    if base_floor is None:
        return conservative_floor
    return max(int(base_floor), conservative_floor)


def scoped_competitor_ignore_reason(
    *,
    scope: str | None,
    competitor_price_kzt: int | float | None,
    delivery_days: int | None,
    competitive_floor_kzt: int | None,
) -> str:
    if delivery_days is not None:
        try:
            if int(delivery_days) >= GLOBAL_LONG_DELIVERY_DAYS_THRESHOLD:
                return GLOBAL_LONG_DELIVERY_IGNORE_REASON
            if int(delivery_days) >= COMPETITION_SCOPE_DELIVERY_DAYS_THRESHOLD:
                return "competition_scope_long_delivery_10plus_days"
        except Exception:
            pass
    if not scope:
        return ""
    if competitive_floor_kzt is None or competitor_price_kzt is None:
        return ""
    try:
        if float(competitor_price_kzt) < int(competitive_floor_kzt):
            return "competition_scope_competitor_price_below_min_floor"
    except Exception:
        return ""
    return ""


def is_scope_partner_ignore_exempt(
    *,
    scope: str | None,
    competitor_name_norm: str,
    competitor_mid: str | None,
) -> bool:
    if scope != "LINE52":
        return False
    mid = str(competitor_mid or "").strip()
    if mid and mid in LINE52_PARTNER_COMPETITIVE_MIDS:
        return True
    name_norm = str(competitor_name_norm or "").strip().casefold()
    if not name_norm:
        return False
    return any(token in name_norm for token in LINE52_PARTNER_COMPETITIVE_NAME_TOKENS)
