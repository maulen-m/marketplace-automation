from __future__ import annotations

from typing import Any

from .competition_rules import GLOBAL_LONG_DELIVERY_DAYS_THRESHOLD
from .repricer_competitors import _is_line52_offer_row, _parse_competitor_price_value


def parse_price_int(value: Any) -> int | None:
    parsed = _parse_competitor_price_value(value)
    if parsed is None:
        return None
    out = int(parsed)
    if out <= 0:
        return None
    return out


def extract_external_competitor_floor(
    row: dict[str, Any] | None,
    *,
    store_id: int,
    exclude_not_competitors: bool,
    delivery_days_by_mid: dict[str, int] | None = None,
) -> int | None:
    if not isinstance(row, dict):
        return None

    competitors = row.get("competitors") or []
    if not isinstance(competitors, list):
        return None

    ignored_mids: set[str] = set()
    if exclude_not_competitors:
        not_competitors = row.get("not_competitors") or []
        if isinstance(not_competitors, list):
            ignored_mids = {str(x) for x in not_competitors if x is not None}

    store_mid = str(store_id)
    days_by_mid = delivery_days_by_mid or {}
    out: int | None = None
    for comp in competitors:
        if not isinstance(comp, dict):
            continue
        mid = comp.get("mid")
        if mid is not None:
            mid_str = str(mid)
            if mid_str == store_mid:
                continue
            if mid_str in ignored_mids:
                continue
            try:
                delivery_days = int(days_by_mid[mid_str])
            except Exception:
                delivery_days = None
            if delivery_days is not None and delivery_days >= GLOBAL_LONG_DELIVERY_DAYS_THRESHOLD:
                continue

        price_int = parse_price_int(comp.get("price"))
        if price_int is None:
            continue
        if out is None or price_int < out:
            out = price_int

    return out


def compute_external_anchor_target(external_floor_price: int, *, minus_kzt: int) -> int:
    return max(0, int(external_floor_price) - max(0, int(minus_kzt)))


def is_line52_locked_9990_row(row: dict[str, Any] | None, *, current_min_price: int) -> bool:
    return int(current_min_price) == 9990 and _is_line52_offer_row(row, "")


def should_skip_line52_locked_9990(
    *,
    row: dict[str, Any] | None,
    current_min_price: int,
    store_id: int,
    allowed_store_ids: set[int] | None = None,
) -> bool:
    if not is_line52_locked_9990_row(row, current_min_price=current_min_price):
        return False
    if allowed_store_ids is None:
        return True
    return int(store_id) in allowed_store_ids


def should_apply_external_anchor(*, current_min_price: int, target_min_price: int, only_if_below: bool) -> bool:
    if only_if_below:
        return int(current_min_price) < int(target_min_price)
    return int(current_min_price) != int(target_min_price)


def needs_external_max_fix(*, current_max_price: int | None, target_min_price: int) -> bool:
    if current_max_price is None:
        return True
    return int(current_max_price) < int(target_min_price)


def needs_external_live_price_fix(*, current_price: int | None, target_min_price: int) -> bool:
    if current_price is None:
        return False
    return int(current_price) < int(target_min_price)
