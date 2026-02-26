from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable

from .kaspi_hourly_snapshot import canonicalize_offer_url, extract_offer_code_from_url, init_snapshot_db


_CONFIGURATOR_RE = re.compile(
    r"BACKEND\.components\.configurator\s*=\s*(\{.*?\})\s*;",
    flags=re.IGNORECASE | re.DOTALL,
)
_TRAILING_CODE_RE = re.compile(r"^(?P<prefix>.*/[^/]*-)(?P<code>\d{6,})$")


@dataclass(frozen=True)
class VariantMember:
    run_date: str
    canonical_offer_url: str
    variant_offer_url: str
    product_code: str
    size_label: str
    source_payload_hash: str


def _format_characteristic_values(values: Any) -> str:
    if not isinstance(values, list):
        return ""
    out: list[str] = []
    for item in values:
        if not isinstance(item, dict):
            continue
        value = str(item.get("value") or "").strip()
        dimension = str(item.get("dimension") or "").strip()
        if value and dimension:
            out.append(f"{value} {dimension}")
        elif value:
            out.append(value)
    return " / ".join(out)


def collect_variant_members_from_payload(payload: dict[str, Any]) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    seen_codes: set[str] = set()

    def walk(node: Any) -> None:
        if isinstance(node, list):
            for item in node:
                walk(item)
            return
        if not isinstance(node, dict):
            return
        product_code = str(node.get("productCode") or "").strip()
        if product_code.isdigit() and len(product_code) >= 6 and product_code not in seen_codes:
            char = node.get("characteristic") if isinstance(node.get("characteristic"), dict) else {}
            size_label = _format_characteristic_values(char.get("values"))
            rows.append({"product_code": product_code, "size_label": size_label})
            seen_codes.add(product_code)
        matrix = node.get("matrix")
        if isinstance(matrix, list):
            for item in matrix:
                walk(item)

    walk(payload.get("matrix"))
    return rows


def init_variant_dim_db(conn) -> None:
    init_snapshot_db(conn)


def upsert_variant_members(conn, members: list[VariantMember]) -> int:
    if not members:
        return 0
    payload = [
        (
            m.run_date,
            m.canonical_offer_url,
            m.variant_offer_url,
            m.product_code,
            m.size_label,
            m.source_payload_hash,
            datetime.now().isoformat(timespec="seconds"),
        )
        for m in members
    ]
    conn.executemany(
        """
        INSERT OR REPLACE INTO dim_offer_variant_family (
            run_date, canonical_offer_url, variant_offer_url, product_code,
            size_label, source_payload_hash, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        payload,
    )
    return len(payload)


def _extract_configurator_payload(html: str) -> dict[str, Any]:
    text = str(html or "")
    if not text:
        return {}
    m = _CONFIGURATOR_RE.search(text)
    if not m:
        return {}
    blob = str(m.group(1)).strip()
    if not blob:
        return {}
    try:
        payload = json.loads(blob)
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _ensure_has_variants_url(url: str) -> str:
    raw = str(url or "").strip()
    if not raw:
        return ""
    if "hasvariants=true" in raw.lower():
        return raw
    if "?" in raw:
        return f"{raw}&hasVariants=true"
    return f"{raw}?hasVariants=true"


def _build_variant_url(seed_url: str, product_code: str) -> str:
    raw = str(seed_url or "").strip()
    code = str(product_code or "").strip()
    if not raw or not code.isdigit():
        return ""
    split = urllib.parse.urlsplit(raw)
    host = (split.netloc or "").lower()
    path = (split.path or "").rstrip("/")
    m = _TRAILING_CODE_RE.match(path)
    if not m:
        return canonicalize_offer_url(raw)
    probe_path = f"{m.group('prefix')}{code}"
    return canonicalize_offer_url(f"https://{host}{probe_path}")


def _fetch_html(url: str, timeout_seconds: int = 30) -> tuple[str, str]:
    req = urllib.request.Request(
        url=url,
        method="GET",
        headers={
            "user-agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/122.0.0.0 Safari/537.36"
            )
        },
    )
    with urllib.request.urlopen(req, timeout=timeout_seconds) as resp:
        final_url = str(getattr(resp, "url", url))
        body = resp.read().decode("utf-8", errors="replace")
    return final_url, body


def build_variant_members_for_offer_url(
    *,
    offer_url: str,
    run_date: str,
    timeout_seconds: int = 45,
) -> tuple[list[VariantMember], str]:
    target = _ensure_has_variants_url(offer_url)
    if not target:
        return [], ""
    page_url, html = _fetch_html(target, timeout_seconds=timeout_seconds)
    canonical_seed = canonicalize_offer_url(page_url) or canonicalize_offer_url(offer_url)
    payload = _extract_configurator_payload(html)
    payload_hash = hashlib.sha1(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
    members_payload = collect_variant_members_from_payload(payload)
    if not members_payload:
        product_code = extract_offer_code_from_url(canonical_seed)
        if not product_code:
            return [], payload_hash
        return (
            [
                VariantMember(
                    run_date=run_date,
                    canonical_offer_url=canonical_seed,
                    variant_offer_url=canonical_seed,
                    product_code=product_code,
                    size_label="",
                    source_payload_hash=payload_hash,
                )
            ],
            payload_hash,
        )

    members: list[VariantMember] = []
    for item in members_payload:
        product_code = str(item.get("product_code") or "").strip()
        if not product_code:
            continue
        variant_url = _build_variant_url(canonical_seed, product_code) or canonical_seed
        members.append(
            VariantMember(
                run_date=run_date,
                canonical_offer_url=canonical_seed,
                variant_offer_url=variant_url,
                product_code=product_code,
                size_label=str(item.get("size_label") or "").strip(),
                source_payload_hash=payload_hash,
            )
        )
    return members, payload_hash


def run_daily_variant_refresh(
    *,
    sqlite_path: str | Path,
    offer_urls: Iterable[str],
    run_date: str | None = None,
    timeout_seconds: int = 45,
) -> dict[str, Any]:
    db_path = Path(sqlite_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    canonical_urls = sorted({canonicalize_offer_url(u) for u in offer_urls if canonicalize_offer_url(u)})
    effective_date = run_date or date.today().isoformat()

    summary: dict[str, Any] = {
        "run_date": effective_date,
        "offers_total": len(canonical_urls),
        "offers_succeeded": 0,
        "offers_failed": 0,
        "members_written": 0,
        "errors": [],
    }

    conn = sqlite3.connect(db_path)
    try:
        init_variant_dim_db(conn)
        for offer_url in canonical_urls:
            try:
                members, _payload_hash = build_variant_members_for_offer_url(
                    offer_url=offer_url,
                    run_date=effective_date,
                    timeout_seconds=timeout_seconds,
                )
                summary["members_written"] += upsert_variant_members(conn, members)
                summary["offers_succeeded"] += 1
            except Exception as exc:
                summary["offers_failed"] += 1
                summary["errors"].append({"offer_url": offer_url, "error": f"{type(exc).__name__}: {exc}"})
        conn.commit()
    finally:
        conn.close()
    return summary
