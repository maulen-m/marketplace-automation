from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable, Mapping


OWNER_DECISION_ID = "forbidden_kaspi_offer_cards_nike_longsleeve_2026_07_03"
OWNER_DECISION_LABEL = "owner decision 2026-07-03 forbidden Nike long-sleeve Kaspi offer cards"
ACMEWEAR_LS_REMOVAL_OWNER_DECISION_ID = "acmewear_ls31_ls21_blk_sale_removal_owner_order_2026_07_06"
ACMEWEAR_LS_REMOVAL_OWNER_DECISION_LABEL = (
    "owner order 2026-07-06 ACMEWEAR LS31/LS21 BLK child bundles removed from sale"
)
BERSERK_OWNER_DECISION_ID = "forbidden_kaspi_offer_cards_berserk_2026_07_17"
BERSERK_OWNER_DECISION_LABEL = (
    "owner decision 2026-07-17 forbidden Berserk photo-mismatched Kaspi offer cards"
)
BERSERK_FORBIDDEN_PRODUCT_CODES = {
    "17490091",
    "18209877",
    "30132395",
    "30321330",
    "30341507",
    "052039068",
    "120765596",
    "120765717",
    "120765723",
    "120765725",
    "120765729",
    "120770066",
    "121207859",
    "121207970",
    "121208018",
    "121208087",
    "121208216",
    "121208442",
    "121460063",
    "121460065",
    "121460073",
    "121625146",
    "121625147",
    "121625347",
    "121934234",
    "121934256",
    "121934275",
    "122188558",
    "122659793",
    "122661759",
    "122661796",
    "128750634",
    "129966843",
    "132571892",
    "132571923",
    "135106879",
    "135502266",
    "135502267",
    "135502268",
    "137440176",
    "138284689",
    "140937883",
    "142101032",
    "142102722",
    "144019144",
    "145325867",
    "145700542",
    "145700543",
    "145700546",
    "145700547",
    "145700548",
    "145700549",
    "145700550",
    "146683357",
    "146716915",
    "146716916",
    "146716917",
    "147648414",
    "147768547",
    "147772841",
    "147772845",
    "150318455",
    "150318457",
    "150318620",
    "150318626",
    "150318645",
    "150318674",
    "150318694",
    "231117565",
    "268608760",
    "598963275",
    "625888484",
    "635798142",
    "697939657",
    "863780079",
}
BERSERK_FORBIDDEN_ARTICLE_PREFIXES = (
    "CL_NEW-CLO_MEN_BERSERK-RUSH_",
    "CL_NEW-CLO_MEN_BERSERK-SHIRT_",
)

FORBIDDEN_KASPI_OFFER_PRODUCT_CODES = {
    # Owner decision 2026-07-03: these Kaspi cards are Nike long sleeves, not sellable Nike T-shirts.
    "110261375",
    "120980444",
    "120980449",
    "132848895",
    "152381039",
    "157715221",
    # Owner order 2026-07-06: ACMEWEAR LS31/LS21 BLK child bundles must not be saleable on ACMEWEAR.
    "165486887",
    "165487403",
} | BERSERK_FORBIDDEN_PRODUCT_CODES
FORBIDDEN_KASPI_OFFER_URL_FRAGMENTS = {
    "sportivnyi-kostjum-18107200-643074",
    "suit-31-ls-st",
    "suit-21-ls-st",
}

FORBIDDEN_KASPI_OFFER_PRODUCT_CODE_RULES = {
    code: (
        OWNER_DECISION_ID,
        OWNER_DECISION_LABEL,
        "forbidden Nike long sleeve",
    )
    for code in (
        "110261375",
        "120980444",
        "120980449",
        "132848895",
        "152381039",
        "157715221",
    )
}
FORBIDDEN_KASPI_OFFER_PRODUCT_CODE_RULES.update(
    {
        "165486887": (
            ACMEWEAR_LS_REMOVAL_OWNER_DECISION_ID,
            ACMEWEAR_LS_REMOVAL_OWNER_DECISION_LABEL,
            "forbidden ACMEWEAR SUIT-31-LS BLK child bundle",
        ),
        "165487403": (
            ACMEWEAR_LS_REMOVAL_OWNER_DECISION_ID,
            ACMEWEAR_LS_REMOVAL_OWNER_DECISION_LABEL,
            "forbidden ACMEWEAR SUIT-21-LS BLK child bundle",
        ),
    }
)
FORBIDDEN_KASPI_OFFER_PRODUCT_CODE_RULES.update(
    {
        code: (
            BERSERK_OWNER_DECISION_ID,
            BERSERK_OWNER_DECISION_LABEL,
            "forbidden Berserk photo-mismatched offer card",
        )
        for code in BERSERK_FORBIDDEN_PRODUCT_CODES
    }
)
FORBIDDEN_KASPI_OFFER_ARTICLE_PREFIX_RULES = {
    "SUIT-31-LS-ST-": (
        ACMEWEAR_LS_REMOVAL_OWNER_DECISION_ID,
        ACMEWEAR_LS_REMOVAL_OWNER_DECISION_LABEL,
        "forbidden ACMEWEAR SUIT-31-LS BLK child bundle",
    ),
    "SUIT-21-LS-ST-": (
        ACMEWEAR_LS_REMOVAL_OWNER_DECISION_ID,
        ACMEWEAR_LS_REMOVAL_OWNER_DECISION_LABEL,
        "forbidden ACMEWEAR SUIT-21-LS BLK child bundle",
    ),
    **{
        prefix: (
            BERSERK_OWNER_DECISION_ID,
            BERSERK_OWNER_DECISION_LABEL,
            "forbidden Berserk photo-mismatched offer card",
        )
        for prefix in BERSERK_FORBIDDEN_ARTICLE_PREFIXES
    },
}
FORBIDDEN_KASPI_OFFER_URL_FRAGMENT_RULES = {
    "sportivnyi-kostjum-18107200-643074": (
        OWNER_DECISION_ID,
        OWNER_DECISION_LABEL,
        "forbidden Nike long sleeve",
    ),
    "suit-31-ls-st": (
        ACMEWEAR_LS_REMOVAL_OWNER_DECISION_ID,
        ACMEWEAR_LS_REMOVAL_OWNER_DECISION_LABEL,
        "forbidden ACMEWEAR SUIT-31-LS BLK child bundle",
    ),
    "suit-21-ls-st": (
        ACMEWEAR_LS_REMOVAL_OWNER_DECISION_ID,
        ACMEWEAR_LS_REMOVAL_OWNER_DECISION_LABEL,
        "forbidden ACMEWEAR SUIT-21-LS BLK child bundle",
    ),
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
_STORE_ID_ALIASES = {
    "30137883": "ACMEWEAR",
    "30000001": "UNIVERSAL",
    "30000002": "STOREB",
}
_ACMEWEAR_SCOPED_OWNER_DECISIONS = {ACMEWEAR_LS_REMOVAL_OWNER_DECISION_ID}


@dataclass(frozen=True)
class ForbiddenCardMatch:
    field: str
    match_type: str
    value: str
    owner_decision: str
    owner_decision_label: str
    description: str

    @property
    def reason(self) -> str:
        return f"{self.owner_decision}: {self.description} {self.match_type}={self.value}"


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


def _normalize_store_token(value: Any) -> str:
    text = _clean_text(value).upper().replace("-", "").replace(" ", "")
    if not text:
        return ""
    return _STORE_ID_ALIASES.get(text, "STOREB" if text == "STOREB" else text)


def _store_candidates(row: Mapping[str, Any], store_name: str | None) -> set[str]:
    candidates = {_normalize_store_token(store_name)}
    for field in ("store", "store_name", "store_code", "merchant_id", "merchant_store_id"):
        candidates.add(_normalize_store_token(_field_value(row, field)))
    return {candidate for candidate in candidates if candidate}


def _rule_applies_to_store(owner_decision: str, row: Mapping[str, Any], store_name: str | None) -> bool:
    if owner_decision not in _ACMEWEAR_SCOPED_OWNER_DECISIONS:
        return True
    return "ACMEWEAR" in _store_candidates(row, store_name)


def _match_product_codes(
    row: Mapping[str, Any],
    field: str,
    codes: set[str],
    match_type: str,
    store_name: str | None,
) -> ForbiddenCardMatch | None:
    for code in sorted(codes):
        rule = FORBIDDEN_KASPI_OFFER_PRODUCT_CODE_RULES.get(code)
        if rule is None:
            continue
        owner_decision, owner_decision_label, description = rule
        if not _rule_applies_to_store(owner_decision, row, store_name):
            continue
        return ForbiddenCardMatch(
            field=field,
            match_type=match_type,
            value=code,
            owner_decision=owner_decision,
            owner_decision_label=owner_decision_label,
            description=description,
        )
    return None


def forbidden_card_match_for_row(
    row: Mapping[str, Any],
    *,
    article_fields: Iterable[str] = ARTICLE_FIELDS,
    product_code_fields: Iterable[str] = PRODUCT_CODE_FIELDS,
    url_fields: Iterable[str] = URL_FIELDS,
    store_name: str | None = None,
) -> ForbiddenCardMatch | None:
    for field in product_code_fields:
        match = _match_product_codes(row, field, extract_product_codes(_field_value(row, field)), "product_code", store_name)
        if match:
            return match

    for field in article_fields:
        raw_value = _field_value(row, field)
        text = _clean_text(raw_value)
        text_upper = text.upper()
        for prefix, rule in sorted(FORBIDDEN_KASPI_OFFER_ARTICLE_PREFIX_RULES.items()):
            if text_upper.startswith(prefix):
                owner_decision, owner_decision_label, description = rule
                if not _rule_applies_to_store(owner_decision, row, store_name):
                    continue
                return ForbiddenCardMatch(
                    field=field,
                    match_type="article_prefix",
                    value=prefix,
                    owner_decision=owner_decision,
                    owner_decision_label=owner_decision_label,
                    description=description,
                )
        match = _match_product_codes(row, field, extract_product_codes(raw_value), "article_product_code", store_name)
        if match:
            return match

    for field in url_fields:
        text = _clean_text(_field_value(row, field)).lower()
        if not text:
            continue
        match = _match_product_codes(row, field, extract_product_codes(text), "product_code", store_name)
        if match:
            return match
        for fragment, rule in sorted(FORBIDDEN_KASPI_OFFER_URL_FRAGMENT_RULES.items()):
            if fragment in text:
                owner_decision, owner_decision_label, description = rule
                if not _rule_applies_to_store(owner_decision, row, store_name):
                    continue
                return ForbiddenCardMatch(
                    field=field,
                    match_type="URL fragment",
                    value=fragment,
                    owner_decision=owner_decision,
                    owner_decision_label=owner_decision_label,
                    description=description,
                )

    return None


def forbidden_card_reason_for_row(row: Mapping[str, Any], *, store_name: str | None = None) -> str:
    match = forbidden_card_match_for_row(row, store_name=store_name)
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
    store_name: str | None = None,
) -> list[dict[str, str]]:
    details: list[dict[str, str]] = []
    for row_index, row in enumerate(rows, start=1):
        match = forbidden_card_match_for_row(row, store_name=store_name)
        if match is None or not row_has_sellable_stock(row):
            continue
        row_label = _clean_text(_field_value(row, row_label_field)) or f"row_{row_index}"
        details.append(
            {
                "row_label": row_label,
                "match_field": match.field,
                "match_type": match.match_type,
                "match_value": match.value,
                "owner_decision": match.owner_decision,
                "owner_decision_label": match.owner_decision_label,
                "reason": match.reason,
            }
        )
    return details
