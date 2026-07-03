from __future__ import annotations

from typing import Any


def is_protected_merchant_sku(value: Any, prefixes: list[str] | tuple[str, ...]) -> bool:
    sku = str(value or "").strip()
    if not sku:
        return False
    return any(sku.startswith(prefix) for prefix in prefixes if str(prefix or "").strip())


def protected_row_identity(row: dict[str, Any], prefixes: list[str] | tuple[str, ...]) -> str:
    for key in ("merchant_sku", "kaspi_sku", "offerId", "json_merchant_sku"):
        value = str(row.get(key) or "").strip()
        if is_protected_merchant_sku(value, prefixes):
            return value
    return ""
