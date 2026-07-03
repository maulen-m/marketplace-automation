#!/usr/bin/env python3
"""Build a LINE31 size-fit analysis report from order truth and chat facts.

Inputs are private run artifacts:
- LINE31 order candidates from Autonomous Business order truth.
- Structured, no-raw-text chat facts extracted through the Kaspi direct-read path.

Outputs are redacted public artifacts:
- outcome/enriched dataset with hashed order references;
- summary JSON;
- static HTML report for sizing decisions.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import html
import json
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from statistics import median
from typing import Any, Iterable


SIZE_ORDER = ("S", "M", "L", "XL", "2XL", "3XL", "4XL")
SIZE_RANK = {size: index for index, size in enumerate(SIZE_ORDER)}
HEIGHT_BINS = ((140, 154), (155, 159), (160, 164), (165, 169), (170, 174), (175, 179), (180, 230))
WEIGHT_BINS = ((35, 49), (50, 54), (55, 59), (60, 64), (65, 69), (70, 79), (80, 180))
SIZE_MISFIT_CODES = {"SIZE_MISMATCH"}


@dataclass(frozen=True)
class SizeRange:
    size: str
    n: int
    height_min: int | None
    height_p50: int | None
    height_max: int | None
    weight_min: int | None
    weight_p50: int | None
    weight_max: int | None
    returned_n: int
    return_rate: float | None


def _load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _as_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(float(str(value).strip()))
    except (TypeError, ValueError):
        return None


def _hash(value: Any) -> str:
    return hashlib.sha256(_clean(value).encode("utf-8")).hexdigest()[:16]


def _line_hash(row: dict[str, Any]) -> str:
    parts = [
        _clean(row.get("order_id")),
        _clean(row.get("line_identity_key")),
        _clean(row.get("sku_id")),
        _clean(row.get("assigned_size")),
    ]
    return _hash("|".join(parts))


def _is_delivery_phase(row: dict[str, Any]) -> bool:
    internal = _clean(row.get("internal_status")).upper()
    kaspi = _clean(row.get("kaspi_status")).upper()
    if internal == "CANCELLED":
        return False
    return internal in {"ACCEPTED", "SHIPPED", "NEW"} or kaspi == "KASPI_DELIVERY"


def _is_returned(row: dict[str, Any]) -> bool:
    internal = _clean(row.get("internal_status")).upper()
    return internal == "RETURNED"


def _is_cancelled(row: dict[str, Any]) -> bool:
    return _clean(row.get("internal_status")).upper() == "CANCELLED"


def classify_outcome(row: dict[str, Any]) -> str:
    if _is_delivery_phase(row):
        return "delivery_phase_excluded"
    if _is_returned(row):
        return "returned"
    if _is_cancelled(row):
        return "cancelled_excluded"
    if _clean(row.get("internal_status")).upper() == "COMPLETED":
        return "buyout_no_return"
    return "other_excluded"


def _normalize_reason_class(item: dict[str, Any]) -> str:
    code = _clean(item.get("reason_code") or item.get("reason")).upper()
    description = _clean(item.get("reason_description") or item.get("reasonDescription")).lower()
    if code in SIZE_MISFIT_CODES or "не подошел размер" in description or "size" in description:
        return "size_misfit"
    if code == "UNSUITABLE" or "не понрав" in description:
        return "not_liked"
    if "повреж" in description or "damage" in description or code in {"DAMAGED", "PACKAGE_DAMAGED"}:
        return "damaged_or_packaging"
    if code or description:
        return "other"
    return "unknown"


def _normalize_refund_status(item: dict[str, Any]) -> str:
    status = _clean(item.get("status_description") or item.get("description")).lower()
    if "отмен" in status:
        return "refund_cancelled"
    if "отклон" in status:
        return "refund_rejected"
    if "оформлен" in status:
        return "refund_completed"
    if status:
        return "refund_other"
    return "unknown"


def _items_by_order(payload: dict[str, Any], key: str) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for item in payload.get(key, []):
        order_id = _clean(item.get("order_id"))
        if order_id:
            result[order_id] = item
    return result


def _bin_label(value: int | None, bins: tuple[tuple[int, int], ...], suffix: str) -> str:
    if value is None:
        return "unknown"
    for low, high in bins:
        if low <= value <= high:
            return f"{low}-{high}{suffix}" if high < 180 else f"{low}+{suffix}"
    return "out_of_range"


def _sanitized_line31_measurements(fact: dict[str, Any]) -> tuple[int | None, int | None, str, str]:
    height = _as_int(fact.get("height_cm"))
    weight = _as_int(fact.get("weight_kg"))
    notes = [str(item) for item in (fact.get("notes") or [])]
    confidence = _clean(fact.get("parse_confidence") or "LOW")
    if height is not None and weight is not None and height == weight and weight >= 120:
        weight = None
        notes.append("suspicious_weight_same_as_height_rejected")
        confidence = "MEDIUM"
    elif weight is not None and weight > 140:
        weight = None
        notes.append("suspicious_weight_outlier_rejected")
        confidence = "MEDIUM" if height is not None else "LOW"
    return height, weight, confidence, ";".join(notes)


def _size_sort_key(value: str) -> tuple[int, str]:
    return SIZE_RANK.get(value, 99), value


def _measurement_status(height: int | None, weight: int | None) -> str:
    if height is not None and weight is not None:
        return "height_weight_available"
    if height is not None:
        return "missing_weight"
    if weight is not None:
        return "missing_height"
    return "missing_height_weight"


def _median_int(values: Iterable[int]) -> int | None:
    nums = sorted(values)
    if not nums:
        return None
    return int(round(median(nums)))


def _pct(numerator: int, denominator: int) -> float | None:
    if denominator <= 0:
        return None
    return round(numerator / denominator * 100, 1)


def build_dataset(
    order_payload: dict[str, Any],
    facts_payload: dict[str, Any],
    refund_payload: dict[str, Any] | None = None,
    fit_notes_payload: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    facts_by_order = {_clean(item.get("order_id")): item for item in facts_payload.get("facts", [])}
    refund_by_order = _items_by_order(refund_payload or {}, "matched")
    notes_by_order = _items_by_order(fit_notes_payload or {}, "notes")
    rows: list[dict[str, Any]] = []
    for row in order_payload.get("rows", []):
        order_id = _clean(row.get("order_id"))
        fact = facts_by_order.get(order_id, {})
        refund = refund_by_order.get(order_id, {})
        fit_note = notes_by_order.get(order_id, {})
        height, weight, parse_confidence, notes = _sanitized_line31_measurements(fact)
        size = _clean(row.get("assigned_size") or row.get("my_size"))
        outcome = classify_outcome(row)
        return_reason_class = _normalize_reason_class(refund) if refund else "unknown"
        refund_status_class = _normalize_refund_status(refund) if refund else "unknown"
        measurement_status = _measurement_status(height, weight)
        rows.append(
            {
                "order_hash": _hash(order_id),
                "line_hash": _line_hash(row),
                "refund_application_hash": _hash(refund.get("application_number")) if refund.get("application_number") else "",
                "created_at": _clean(row.get("created_at")),
                "month_day": _clean(row.get("created_at"))[:10],
                "sku_id": _clean(row.get("sku_id")),
                "sku_key": _clean(row.get("sku_key")),
                "offer_name": _clean(row.get("kaspi_offer_name")),
                "ordered_size": size,
                "height_cm": height,
                "weight_kg": weight,
                "measurement_status": measurement_status,
                "explicit_customer_size": _clean(fact.get("explicit_size")),
                "fit_preference": _clean(fact.get("fit_preference")),
                "preference_weight_adjustment_kg": _as_int(fact.get("preference_weight_adjustment_kg")) or 0,
                "effective_weight_kg": _as_int(fact.get("effective_weight_kg")),
                "parse_confidence": parse_confidence,
                "chat_group_found": bool(fact.get("chat_group_found")),
                "has_customer_text": bool(fact.get("has_customer_text")),
                "notes": notes,
                "internal_status": _clean(row.get("internal_status")),
                "kaspi_status": _clean(row.get("kaspi_status")),
                "returned_to_warehouse": _clean(row.get("returned_to_warehouse")),
                "outcome_class": outcome,
                "refund_status_class": refund_status_class,
                "return_reason_class": return_reason_class,
                "return_reason_code": _clean(refund.get("reason_code")),
                "return_reason_found": bool(refund),
                "return_reason_source": _clean(refund.get("source") or (refund_payload or {}).get("source_endpoint")),
                "fit_issue_reported": _clean(fit_note.get("fit_issue")),
                "fit_direction": _clean(fit_note.get("fit_direction")),
                "fit_note_source": _clean(fit_note.get("source")),
                "fit_note_confidence": _clean(fit_note.get("confidence")),
                "included_in_fit_table": outcome == "buyout_no_return" and height is not None and weight is not None and size in SIZE_ORDER,
                "included_in_return_check": outcome == "returned" and height is not None and weight is not None and size in SIZE_ORDER,
                "included_in_size_misfit_return_check": (
                    outcome == "returned"
                    and return_reason_class == "size_misfit"
                    and height is not None
                    and weight is not None
                    and size in SIZE_ORDER
                ),
                "height_bin": _bin_label(height, HEIGHT_BINS, "cm"),
                "weight_bin": _bin_label(weight, WEIGHT_BINS, "kg"),
            }
        )
    return rows


def build_size_ranges(rows: list[dict[str, Any]]) -> list[SizeRange]:
    result: list[SizeRange] = []
    for size in SIZE_ORDER:
        buyouts = [row for row in rows if row["included_in_fit_table"] and row["ordered_size"] == size]
        returns = [row for row in rows if row["included_in_return_check"] and row["ordered_size"] == size]
        all_final = buyouts + returns
        heights = [int(row["height_cm"]) for row in buyouts if row["height_cm"] is not None]
        weights = [int(row["weight_kg"]) for row in buyouts if row["weight_kg"] is not None]
        result.append(
            SizeRange(
                size=size,
                n=len(buyouts),
                height_min=min(heights) if heights else None,
                height_p50=_median_int(heights),
                height_max=max(heights) if heights else None,
                weight_min=min(weights) if weights else None,
                weight_p50=_median_int(weights),
                weight_max=max(weights) if weights else None,
                returned_n=len(returns),
                return_rate=_pct(len(returns), len(all_final)),
            )
        )
    return result


def build_grid(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    cells: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    returns: dict[tuple[str, str], int] = Counter()
    for row in rows:
        if row["outcome_class"] == "buyout_no_return" and row["height_cm"] is not None and row["weight_kg"] is not None:
            cells[(row["height_bin"], row["weight_bin"])].append(row)
        elif row["outcome_class"] == "returned" and row["height_cm"] is not None and row["weight_kg"] is not None:
            returns[(row["height_bin"], row["weight_bin"])] += 1
    grid: list[dict[str, Any]] = []
    for h_low, h_high in HEIGHT_BINS:
        h_label = f"{h_low}-{h_high}cm" if h_high < 230 else f"{h_low}+cm"
        for w_low, w_high in WEIGHT_BINS:
            w_label = f"{w_low}-{w_high}kg" if w_high < 180 else f"{w_low}+kg"
            bucket = cells.get((h_label, w_label), [])
            size_counts = Counter(row["ordered_size"] for row in bucket)
            suggested = ""
            if size_counts:
                suggested = sorted(size_counts, key=lambda size: (size_counts[size], -_size_sort_key(size)[0]), reverse=True)[0]
            grid.append(
                {
                    "height_bin": h_label,
                    "weight_bin": w_label,
                    "suggested_size": suggested,
                    "buyout_n": len(bucket),
                    "return_n": returns.get((h_label, w_label), 0),
                    "sizes": dict(sorted(size_counts.items(), key=lambda item: _size_sort_key(item[0]))),
                }
            )
    return grid


def build_insights(rows: list[dict[str, Any]], size_ranges: list[SizeRange], grid: list[dict[str, Any]]) -> list[str]:
    insights: list[str] = []
    finalized_hw = [
        row
        for row in rows
        if row["height_cm"] is not None
        and row["weight_kg"] is not None
        and row["outcome_class"] in {"buyout_no_return", "returned"}
    ]
    buyouts = [row for row in finalized_hw if row["outcome_class"] == "buyout_no_return"]
    returns = [row for row in finalized_hw if row["outcome_class"] == "returned"]
    all_returns = [row for row in rows if row["outcome_class"] == "returned"]
    size_misfit_returns = [row for row in all_returns if row["return_reason_class"] == "size_misfit"]
    size_misfit_returns_with_hw = [
        row for row in size_misfit_returns if row["height_cm"] is not None and row["weight_kg"] is not None
    ]
    too_big_notes = [row for row in rows if row["fit_direction"] == "too_big"]
    if finalized_hw:
        insights.append(
            f"Only {len(finalized_hw)} finalized rows have full height and weight, so the table is a live evidence map, not a permanent LINE31 sizing law."
        )
    if size_misfit_returns:
        by_size = Counter(row["ordered_size"] for row in size_misfit_returns)
        size_text = ", ".join(f"{size}:{count}" for size, count in sorted(by_size.items(), key=lambda item: _size_sort_key(item[0])))
        insights.append(
            f"{len(size_misfit_returns)} actual returned row/s have refund reason size_misfit ({size_text}); {len(size_misfit_returns_with_hw)} of them currently have full height and weight."
        )
    if returns:
        by_size = Counter(row["ordered_size"] for row in returns)
        worst_size, worst_n = by_size.most_common(1)[0]
        insights.append(f"Returned rows with parameters cluster most visibly at size {worst_size} in this sample ({worst_n} row/s).")
    elif all_returns:
        insights.append(
            f"There are {len(all_returns)} returned LINE31 row/s, but none have full height and weight, so return-specific sizing mistakes are not yet machine-learnable."
        )
    sparse = [item.size for item in size_ranges if item.n == 0]
    if sparse:
        insights.append(f"No clean buyout-with-parameters evidence yet for: {', '.join(sparse)}.")
    overlap_cells = [
        cell for cell in grid if cell["buyout_n"] >= 2 and len(cell["sizes"]) >= 2
    ]
    if overlap_cells:
        cell = sorted(overlap_cells, key=lambda item: item["buyout_n"], reverse=True)[0]
        insights.append(
            f"Some height/weight buckets accepted multiple sizes; the largest overlap is {cell['height_bin']} and {cell['weight_bin']} with {cell['sizes']}."
        )
    loose = [row for row in rows if row["fit_preference"] == "loose"]
    if loose:
        insights.append(f"{len(loose)} customer replies asked for a looser fit; this supports keeping preference as a separate adjustment signal.")
    if too_big_notes:
        insights.append(
            f"{len(too_big_notes)} returned row/s have a secondary fit note saying part of the item was too big; treat this as skeptical customer-fit evidence, not a hard sizing rule."
        )
    delivery = [row for row in rows if row["outcome_class"] == "delivery_phase_excluded"]
    if delivery:
        insights.append(f"{len(delivery)} rows are still in delivery phase and were excluded from fit accuracy until final status is known.")
    if buyouts:
        top_size = Counter(row["ordered_size"] for row in buyouts).most_common(1)[0]
        insights.append(f"Current clean-buyout evidence is densest for {top_size[0]} ({top_size[1]} rows), so recommendations around that size are more reliable than sparse edges.")
    return insights


def workflow_options() -> list[dict[str, str]]:
    return [
        {
            "name": "Option 1 - Direct-read weekly fit table refresh",
            "recommendation": "Recommended",
            "description": "Use AB order truth for LINE31 finalized outcomes, direct Kaspi chat API reads for height/weight facts, then rebuild the redacted HTML and candidate table. Fast, low-risk, and no UI clicking except session proof.",
        },
        {
            "name": "Option 2 - Incremental daily cache plus weekly decision report",
            "recommendation": "Best long-term",
            "description": "Persist only message hashes and structured facts in the private size DB each day, then run the fit-table report weekly after delivery statuses finalize. This avoids rereading unchanged chats.",
        },
        {
            "name": "Option 3 - UI fallback for ambiguous rows only",
            "recommendation": "Fallback",
            "description": "Open merchant UI only for unmatched chats, partial replies, suspicious parse outliers, or returned rows without parameters. This keeps manual inspection focused where it changes the table.",
        },
        {
            "name": "Option 4 - Confidence-weighted LINE31 size engine",
            "recommendation": "After more data",
            "description": "Promote the observed buyout ranges into a LINE31 table only after enough finalized buyout and returned rows exist per size. Until then, use report evidence as operator guidance, not hard automation.",
        },
    ]


def summarize(
    rows: list[dict[str, Any]],
    facts_payload: dict[str, Any],
    refund_payload: dict[str, Any] | None = None,
    fit_notes_payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    size_ranges = build_size_ranges(rows)
    grid = build_grid(rows)
    outcome_counts = Counter(row["outcome_class"] for row in rows)
    confidence_counts = Counter(row["parse_confidence"] for row in rows)
    returned_rows = [row for row in rows if row["outcome_class"] == "returned"]
    size_misfit_returns = [row for row in returned_rows if row["return_reason_class"] == "size_misfit"]
    size_misfit_measurement_status_counts = Counter(row["measurement_status"] for row in size_misfit_returns)
    summary = {
        "gate": "GREEN_LINE31_SIZE_FIT_ANALYSIS_BUILT",
        "row_count": len(rows),
        "order_count_redacted": len({row["order_hash"] for row in rows}),
        "outcome_counts": dict(outcome_counts),
        "chat_direct_read_gate": facts_payload.get("gate"),
        "chat_candidates": facts_payload.get("candidates"),
        "chat_matched_groups": facts_payload.get("matched_groups"),
        "chat_history_failures": facts_payload.get("history_failures"),
        "parse_confidence_counts": dict(confidence_counts),
        "full_height_weight_rows": sum(row["height_cm"] is not None and row["weight_kg"] is not None for row in rows),
        "finalized_full_height_weight_rows": sum(
            row["height_cm"] is not None
            and row["weight_kg"] is not None
            and row["outcome_class"] in {"buyout_no_return", "returned"}
            for row in rows
        ),
        "fit_preferences": dict(Counter(row["fit_preference"] or "none" for row in rows)),
        "return_reason_gate": (refund_payload or {}).get("gate"),
        "return_reason_source_endpoint": (refund_payload or {}).get("source_endpoint"),
        "return_reason_matched_count": (refund_payload or {}).get("matched_count", 0),
        "returned_reason_counts": dict(Counter(row["return_reason_class"] for row in returned_rows)),
        "size_misfit_return_rows": len(size_misfit_returns),
        "size_misfit_return_with_full_hw_rows": sum(
            row["height_cm"] is not None and row["weight_kg"] is not None for row in size_misfit_returns
        ),
        "size_misfit_returns_by_size": dict(Counter(row["ordered_size"] for row in size_misfit_returns)),
        "size_misfit_return_measurement_status_counts": dict(size_misfit_measurement_status_counts),
        "fit_note_gate": (fit_notes_payload or {}).get("gate"),
        "fit_note_rows": len((fit_notes_payload or {}).get("notes", [])),
        "size_ranges": [item.__dict__ for item in size_ranges],
        "grid_cells_with_evidence": sum(1 for cell in grid if cell["buyout_n"] > 0),
        "insights": build_insights(rows, size_ranges, grid),
        "workflow_options": workflow_options(),
        "raw_customer_text_exported": False,
        "raw_session_material_exported": False,
        "raw_order_ids_exported_to_public_artifacts": False,
    }
    return summary


def _td(value: Any, class_name: str = "") -> str:
    cls = f' class="{class_name}"' if class_name else ""
    return f"<td{cls}>{html.escape('' if value is None else str(value))}</td>"


def render_html(rows: list[dict[str, Any]], summary: dict[str, Any], output_path: Path) -> None:
    size_ranges = [SizeRange(**item) for item in summary["size_ranges"]]
    grid = build_grid(rows)
    outcome_counts = summary["outcome_counts"]
    finalized = [row for row in rows if row["outcome_class"] in {"buyout_no_return", "returned"}]
    return_evidence = [
        row
        for row in rows
        if row["outcome_class"] == "returned" or row["return_reason_found"] or row["fit_issue_reported"]
    ]
    size_misfit_evidence = [
        row for row in rows if row["outcome_class"] == "returned" and row["return_reason_class"] == "size_misfit"
    ]
    title = "LINE31 Size Fit Evidence"
    range_rows = []
    for item in size_ranges:
        range_rows.append(
            "<tr>"
            + _td(item.size, "size")
            + _td(item.n)
            + _td(f"{item.height_min or ''} / {item.height_p50 or ''} / {item.height_max or ''}")
            + _td(f"{item.weight_min or ''} / {item.weight_p50 or ''} / {item.weight_max or ''}")
            + _td(item.returned_n)
            + _td("" if item.return_rate is None else f"{item.return_rate}%")
            + "</tr>"
        )
    grid_rows = []
    for h_low, h_high in HEIGHT_BINS:
        h_label = f"{h_low}-{h_high}cm" if h_high < 230 else f"{h_low}+cm"
        cells = [f"<th>{html.escape(h_label)}</th>"]
        for w_low, w_high in WEIGHT_BINS:
            w_label = f"{w_low}-{w_high}kg" if w_high < 180 else f"{w_low}+kg"
            cell = next(item for item in grid if item["height_bin"] == h_label and item["weight_bin"] == w_label)
            tone = "empty" if not cell["buyout_n"] else "warn" if cell["return_n"] else "good"
            label = cell["suggested_size"] or "?"
            detail = f"{cell['buyout_n']} buyout"
            if cell["return_n"]:
                detail += f" / {cell['return_n']} return"
            if cell["sizes"]:
                detail += " · " + ", ".join(f"{k}:{v}" for k, v in cell["sizes"].items())
            cells.append(f'<td class="heat {tone}"><b>{html.escape(label)}</b><span>{html.escape(detail)}</span></td>')
        grid_rows.append("<tr>" + "".join(cells) + "</tr>")
    detail_rows = []
    for row in sorted(finalized, key=lambda item: (item["month_day"], item["ordered_size"], item["order_hash"])):
        detail_rows.append(
            "<tr>"
            + _td(row["month_day"])
            + _td(row["order_hash"])
            + _td(row["ordered_size"], "size")
            + _td(row["height_cm"])
            + _td(row["weight_kg"])
            + _td(row["fit_preference"] or "")
            + _td(row["outcome_class"], "outcome")
            + _td(row["notes"])
            + "</tr>"
        )
    return_rows = []
    for row in sorted(return_evidence, key=lambda item: (item["month_day"], item["ordered_size"], item["order_hash"])):
        return_rows.append(
            "<tr>"
            + _td(row["month_day"])
            + _td(row["order_hash"])
            + _td(row["refund_application_hash"])
            + _td(row["ordered_size"], "size")
            + _td(row["height_cm"])
            + _td(row["weight_kg"])
            + _td(row["measurement_status"])
            + _td(row["outcome_class"], "outcome")
            + _td(row["refund_status_class"])
            + _td(row["return_reason_class"])
            + _td(row["fit_issue_reported"])
            + _td(row["fit_direction"])
            + _td(row["fit_note_confidence"])
            + "</tr>"
        )
    size_misfit_rows = []
    for row in sorted(size_misfit_evidence, key=lambda item: (item["month_day"], item["ordered_size"], item["order_hash"])):
        missing_note = ""
        if row["measurement_status"] == "missing_height_weight":
            missing_note = "Exact height/weight not captured in structured chat facts, refund list/detail, or private size-record DB."
        size_misfit_rows.append(
            "<tr>"
            + _td(row["month_day"])
            + _td(row["order_hash"])
            + _td(row["refund_application_hash"])
            + _td(row["ordered_size"], "size")
            + _td(row["height_cm"] if row["height_cm"] is not None else "not captured")
            + _td(row["weight_kg"] if row["weight_kg"] is not None else "not captured")
            + _td(row["measurement_status"])
            + _td(row["fit_issue_reported"])
            + _td(row["fit_direction"])
            + _td(row["fit_note_confidence"])
            + _td(missing_note)
            + "</tr>"
        )
    insight_items = "\n".join(f"<li>{html.escape(item)}</li>" for item in summary["insights"])
    workflow_rows = []
    for option in summary["workflow_options"]:
        workflow_rows.append(
            "<tr>"
            + _td(option["name"])
            + _td(option["recommendation"])
            + _td(option["description"])
            + "</tr>"
        )
    html_text = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{title}</title>
  <style>
    :root {{
      --ink: #17201b;
      --muted: #617068;
      --line: #d8ded7;
      --paper: #f7f8f4;
      --panel: #ffffff;
      --green: #dfeedd;
      --green-strong: #7aa66c;
      --amber: #ffd68a;
      --red: #f3b8aa;
      --blue: #dce8f5;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      background: var(--paper);
      color: var(--ink);
      font: 14px/1.45 ui-sans-serif, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }}
    header {{
      padding: 28px 32px 18px;
      border-bottom: 1px solid var(--line);
      background: linear-gradient(180deg, #ffffff 0%, #f3f6ef 100%);
    }}
    h1 {{ margin: 0 0 8px; font-size: 28px; letter-spacing: 0; }}
    h2 {{ margin: 26px 0 10px; font-size: 18px; letter-spacing: 0; }}
    p {{ max-width: 980px; color: var(--muted); margin: 0; }}
    main {{ padding: 22px 32px 40px; }}
    .metrics {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
      gap: 10px;
      margin: 18px 0 20px;
    }}
    .metric {{
      border: 1px solid var(--line);
      background: var(--panel);
      padding: 12px 14px;
      min-height: 72px;
    }}
    .metric b {{ display: block; font-size: 22px; margin-bottom: 2px; }}
    .metric span {{ color: var(--muted); }}
    table {{
      border-collapse: collapse;
      width: 100%;
      background: var(--panel);
      border: 1px solid var(--line);
      margin-bottom: 18px;
    }}
    th, td {{
      border-bottom: 1px solid var(--line);
      border-right: 1px solid var(--line);
      padding: 8px 9px;
      vertical-align: top;
      text-align: left;
    }}
    th {{ background: #eef2ec; font-weight: 700; }}
    .size {{ font-weight: 800; }}
    .heat {{ min-width: 110px; height: 66px; }}
    .heat b {{ display: block; font-size: 17px; }}
    .heat span {{ display: block; color: #405148; font-size: 12px; margin-top: 4px; }}
    .good {{ background: var(--green); }}
    .warn {{ background: var(--amber); }}
    .empty {{ background: #f9faf7; color: #89938c; }}
    .outcome {{ white-space: nowrap; }}
    .insights {{
      background: var(--panel);
      border: 1px solid var(--line);
      padding: 14px 18px;
      margin: 0 0 20px;
    }}
    .insights li {{ margin: 7px 0; }}
    .note {{
      padding: 12px 14px;
      background: var(--blue);
      border-left: 4px solid #6d91b8;
      margin-bottom: 18px;
      color: #24384c;
    }}
    @media (max-width: 860px) {{
      header, main {{ padding-left: 16px; padding-right: 16px; }}
      table {{ font-size: 12px; }}
      th, td {{ padding: 6px; }}
      .heat {{ min-width: 84px; }}
    }}
  </style>
</head>
<body>
  <header>
    <h1>{title}</h1>
    <p>Last 30 days LINE31 orders, joined to structured Kaspi chat height/weight facts. Delivery-phase rows are excluded from fit accuracy until they finalize. Public output is redacted: no raw customer text, no session material, no raw order IDs.</p>
  </header>
  <main>
    <section class="metrics">
      <div class="metric"><b>{summary['order_count_redacted']}</b><span>orders in LINE31 window</span></div>
      <div class="metric"><b>{summary['full_height_weight_rows']}</b><span>rows with full height+weight</span></div>
      <div class="metric"><b>{outcome_counts.get('buyout_no_return', 0)}</b><span>buyout/no-return rows</span></div>
      <div class="metric"><b>{outcome_counts.get('returned', 0)}</b><span>returned rows</span></div>
      <div class="metric"><b>{summary['size_misfit_return_rows']}</b><span>size-misfit return rows</span></div>
      <div class="metric"><b>{outcome_counts.get('delivery_phase_excluded', 0)}</b><span>delivery-phase excluded</span></div>
    </section>
    <div class="note">Use green cells as observed successful evidence, amber cells as areas with return caution, and blank cells as unknown. Sparse cells should not become hard automation rules yet.</div>
    <h2>Observed Buyout Size Ranges</h2>
    <table>
      <thead><tr><th>Size</th><th>Clean buyouts</th><th>Height min / median / max</th><th>Weight min / median / max</th><th>Returns with params</th><th>Return rate in finalized param rows</th></tr></thead>
      <tbody>{''.join(range_rows)}</tbody>
    </table>
    <h2>Height x Weight Evidence Grid</h2>
    <table>
      <thead><tr><th>Height \\ Weight</th>{''.join(f'<th>{w[0]}-{w[1]}kg</th>' if w[1] < 180 else f'<th>{w[0]}+kg</th>' for w in WEIGHT_BINS)}</tr></thead>
      <tbody>{''.join(grid_rows)}</tbody>
    </table>
    <h2>Step-by-step Insights</h2>
    <ol class="insights">{insight_items}</ol>
    <h2>Size-Misfit Return Details</h2>
    <div class="note">The failed size is the size that was ordered/sent and then returned with refund reason size_misfit. If height or weight says "not captured", that value was not found in the current structured chat facts or refund detail read, rather than being hidden.</div>
    <table>
      <thead><tr><th>Date</th><th>Order hash</th><th>Refund hash</th><th>Failed size</th><th>Height</th><th>Weight</th><th>Measurement status</th><th>Fit issue note</th><th>Direction</th><th>Note confidence</th><th>Evidence note</th></tr></thead>
      <tbody>{''.join(size_misfit_rows)}</tbody>
    </table>
    <h2>Return Reason Evidence</h2>
    <div class="note">Refund reasons come from the merchant refund direct-read endpoint and are joined by private raw order ID before this public report is rendered. Fit notes are secondary observations and should not override finalized refund status or height/weight evidence.</div>
    <table>
      <thead><tr><th>Date</th><th>Order hash</th><th>Refund hash</th><th>Failed/sent size</th><th>Height</th><th>Weight</th><th>Measurement status</th><th>Outcome</th><th>Refund status</th><th>Reason class</th><th>Fit issue note</th><th>Direction</th><th>Note confidence</th></tr></thead>
      <tbody>{''.join(return_rows)}</tbody>
    </table>
    <h2>Replicable Workflow Options</h2>
    <table>
      <thead><tr><th>Option</th><th>Use</th><th>Why it matters</th></tr></thead>
      <tbody>{''.join(workflow_rows)}</tbody>
    </table>
    <h2>Finalized Parameter Rows</h2>
    <table>
      <thead><tr><th>Date</th><th>Order hash</th><th>Size sent/ordered</th><th>Height</th><th>Weight</th><th>Preference</th><th>Outcome</th><th>Notes</th></tr></thead>
      <tbody>{''.join(detail_rows)}</tbody>
    </table>
  </main>
</body>
</html>
"""
    output_path.write_text(html_text, encoding="utf-8")


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    fieldnames = list(rows[0].keys())
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--orders-json", type=Path, required=True)
    parser.add_argument("--chat-facts-json", type=Path, required=True)
    parser.add_argument("--return-reasons-json", type=Path)
    parser.add_argument("--fit-notes-json", type=Path)
    parser.add_argument("--output-dir", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    orders = _load_json(args.orders_json)
    facts = _load_json(args.chat_facts_json)
    refund_reasons = _load_json(args.return_reasons_json) if args.return_reasons_json else {}
    fit_notes = _load_json(args.fit_notes_json) if args.fit_notes_json else {}
    rows = build_dataset(orders, facts, refund_reasons, fit_notes)
    summary = summarize(rows, facts, refund_reasons, fit_notes)
    (args.output_dir / "line31_size_fit_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    redacted_rows = [
        {key: value for key, value in row.items() if key not in {"offer_name"}}
        for row in rows
    ]
    (args.output_dir / "line31_size_fit_dataset_redacted.json").write_text(
        json.dumps({"rows": redacted_rows}, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    write_csv(args.output_dir / "line31_size_fit_dataset_redacted.csv", redacted_rows)
    render_html(rows, summary, args.output_dir / "line31_size_fit_report.html")
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
