from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable, Mapping


OWNER_DECISION_ID = "forbidden_kaspi_offer_cards_nike_longsleeve_2026_07_03"
OWNER_DECISION_LABEL = "owner decision 2026-07-03 forbidden Nike long-sleeve Kaspi offer cards"

FORBIDDEN_KASPI_OFFER_PRODUCT_CODES = {
    # Owner decision 2026-07-03: these Kaspi cards are Nike long sleeves, not sellable Nike T-shirts.
    "110261375",
    "120980444",
    "120980449",
    "132848895",
    "152381039",
    "157715221",
}
FORBIDDEN_KASPI_OFFER_URL_FRAGMENTS = {
    "sportivnyi-kostjum-18107200-643074",
}

ARTICLE_FIELDS = (
    "SKU",
    "sku",
    "article",
    "seller_article",
    "merchant_article",
    "merchant_sku",
    "merchant_sku_article",
    "seller_sku",
    "shop_sku",
)
PRODUCT_CODE_FIELDS = ("product_code", "kaspi_product_code", "offer_code", "kaspi_sku", "sku_id")
URL_FIELDS = ("variant_url", "resolved_url", "live_link", "link", "url")
WAREHOUSE_COLUMNS = ("PP1", "PP2", "PP3", "PP4", "PP5")

_LONG_CODE_RE = re.compile(r"(?<!\d)(\d{6,})(?!\d)")


@dataclass(frozen=True)
class ForbiddenCardMatch:
    field: str
    match_type: str
    value: str

    @property
    def reason(self) -> str:
        return (
            f"{OWNER_DECISION_ID}: forbidden Nike long sleeve "
            f"{self.match_type}={self.value}"
        )


def _clean_text(value: Any) -> str:
    if value is None:
        return ""
    return " ".join(str(value).strip().split())


def _field_value(row: Mapping[str, Any], field: str) -> Any:
    getter = getattr(row, "get", None)
    if getter is None:
        return ""
    return getter(field, "")


def extract_product_codes(value: Any) -> set[str]:
    text = _clean_text(value)
    if not text:
        return set()
    return set(_LONG_CODE_RE.findall(text))


def forbidden_card_match_for_row(
    row: Mapping[str, Any],
    *,
    article_fields: Iterable[str] = ARTICLE_FIELDS,
    product_code_fields: Iterable[str] = PRODUCT_CODE_FIELDS,
    url_fields: Iterable[str] = URL_FIELDS,
) -> ForbiddenCardMatch | None:
    for field in product_code_fields:
        codes = extract_product_codes(_field_value(row, field))
        blocked = sorted(codes & FORBIDDEN_KASPI_OFFER_PRODUCT_CODES)
        if blocked:
            return ForbiddenCardMatch(field=field, match_type="product_code", value=",".join(blocked))

    for field in article_fields:
        codes = extract_product_codes(_field_value(row, field))
        blocked = sorted(codes & FORBIDDEN_KASPI_OFFER_PRODUCT_CODES)
        if blocked:
            return ForbiddenCardMatch(field=field, match_type="article_product_code", value=",".join(blocked))

    for field in url_fields:
        text = _clean_text(_field_value(row, field)).lower()
        if not text:
            continue
        blocked = sorted(extract_product_codes(text) & FORBIDDEN_KASPI_OFFER_PRODUCT_CODES)
        if blocked:
            return ForbiddenCardMatch(field=field, match_type="product_code", value=",".join(blocked))
        for fragment in sorted(FORBIDDEN_KASPI_OFFER_URL_FRAGMENTS):
            if fragment in text:
                return ForbiddenCardMatch(field=field, match_type="URL fragment", value=fragment)

    return None


def forbidden_card_reason_for_row(row: Mapping[str, Any]) -> str:
    match = forbidden_card_match_for_row(row)
    return match.reason if match else ""


def stock_cell_is_sellable(value: Any) -> bool:
    text = _clean_text(value).lower()
    if text in {"", "no", "\u043d\u0435\u0442", "0", "0.0"}:
        return False
    try:
        return float(text.replace(" ", "").replace(",", ".")) > 0
    except Exception:
        return True


def row_has_sellable_stock(row: Mapping[str, Any], *, warehouse_columns: Iterable[str] = WAREHOUSE_COLUMNS) -> bool:
    return any(stock_cell_is_sellable(_field_value(row, column)) for column in warehouse_columns)


def forbidden_saleable_row_details(
    rows: Iterable[Mapping[str, Any]],
    *,
    row_label_field: str = "SKU",
) -> list[dict[str, str]]:
    details: list[dict[str, str]] = []
    for row_index, row in enumerate(rows, start=1):
        match = forbidden_card_match_for_row(row)
        if match is None or not row_has_sellable_stock(row):
            continue
        row_label = _clean_text(_field_value(row, row_label_field)) or f"row_{row_index}"
        details.append(
            {
                "row_label": row_label,
                "match_field": match.field,
                "match_type": match.match_type,
                "match_value": match.value,
                "owner_decision": OWNER_DECISION_ID,
                "reason": match.reason,
            }
        )
    return details
