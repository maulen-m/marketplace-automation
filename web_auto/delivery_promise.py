from __future__ import annotations

import json
from datetime import datetime, time, timedelta
from pathlib import Path
from typing import Any


DEFAULT_DELIVERY_PROMISE_ROOT = Path("runs/delivery_promise_watch")
DEFAULT_SAME_DAY_CUTOFF_LOCAL = "15:00"


def _parse_date(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    if "T" in text:
        text = text.split("T", 1)[0]
    if " " in text:
        text = text.split(" ", 1)[0]
    try:
        return datetime.strptime(text, "%Y-%m-%d")
    except ValueError:
        return None


def _now_text() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _parse_observed_datetime(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    text = text.replace("T", " ").split("+", 1)[0].split("Z", 1)[0].strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def _parse_cutoff_time(value: Any) -> time | None:
    text = str(value or "").strip()
    if not text:
        return None
    text = text.replace("GMT+5", "").replace("+05", "").strip()
    for fmt in ("%H:%M", "%H:%M:%S"):
        try:
            return datetime.strptime(text, fmt).time()
        except ValueError:
            continue
    return None


def _is_after_same_day_cutoff(
    *,
    observed_at: Any,
    same_day_cutoff_local: str = DEFAULT_SAME_DAY_CUTOFF_LOCAL,
) -> bool:
    observed_dt = _parse_observed_datetime(observed_at)
    cutoff = _parse_cutoff_time(same_day_cutoff_local)
    if observed_dt is None or cutoff is None:
        return False
    return observed_dt.time() > cutoff


def promise_delta_days(*, displayed_date: Any, expected_date: Any) -> int | None:
    displayed = _parse_date(displayed_date)
    expected = _parse_date(expected_date)
    if displayed is None or expected is None:
        return None
    return (displayed.date() - expected.date()).days


def classify_delivery_promise(
    delta_days: int | None,
    *,
    observed_at: Any = None,
    same_day_cutoff_local: str = DEFAULT_SAME_DAY_CUTOFF_LOCAL,
) -> dict[str, Any]:
    base = {
        "same_day_shipping_cutoff_local": same_day_cutoff_local,
        "delivery_cutoff_awareness": True,
        "delivery_cutoff_status": "not_applicable",
        "exact_prior_penalty_clearance": "unknown_uncaptured",
    }
    if delta_days is None:
        return {
            "status": "UNKNOWN",
            "severity": "unknown",
            "experiment_contamination": "unknown",
            **base,
        }
    if delta_days <= 0:
        return {
            "status": "OK",
            "severity": "green",
            "experiment_contamination": "none",
            **base,
        }
    if delta_days == 1:
        if _is_after_same_day_cutoff(
            observed_at=observed_at,
            same_day_cutoff_local=same_day_cutoff_local,
        ):
            return {
                "status": "CUTOFF_NORMAL_POST_15",
                "severity": "green",
                "experiment_contamination": "none_cutoff_normal_post_15",
                **base,
                "delivery_cutoff_status": "post_cutoff_normal",
            }
        return {
            "status": "PENALTY_LIKELY",
            "severity": "yellow",
            "experiment_contamination": "delivery_promise_plus_one",
            **base,
            "delivery_cutoff_status": "pre_cutoff_or_unknown",
        }
    return {
        "status": "SEVERE_DELAY",
        "severity": "red",
        "experiment_contamination": "delivery_promise_plus_multi_day",
        **base,
        "delivery_cutoff_status": "multi_day_delay",
    }


def normalize_delivery_capture_health(capture: dict[str, Any]) -> dict[str, Any]:
    """Return a cutoff-aware copy of a capture, preserving raw evidence fields."""
    out = dict(capture or {})
    try:
        delta = int(out["delivery_promise_delta_days"]) if out.get("delivery_promise_delta_days") is not None else None
    except (TypeError, ValueError):
        delta = None
    cutoff = str(out.get("same_day_shipping_cutoff_local") or DEFAULT_SAME_DAY_CUTOFF_LOCAL)
    health = classify_delivery_promise(
        delta,
        observed_at=out.get("observed_at"),
        same_day_cutoff_local=cutoff,
    )
    out["health_status"] = health["status"]
    out["severity"] = health["severity"]
    out["experiment_contamination"] = health["experiment_contamination"]
    out["delivery_cutoff_awareness"] = health["delivery_cutoff_awareness"]
    out["delivery_cutoff_status"] = health["delivery_cutoff_status"]
    out["same_day_shipping_cutoff_local"] = health["same_day_shipping_cutoff_local"]
    out["exact_prior_penalty_clearance"] = health["exact_prior_penalty_clearance"]
    return out


def build_delivery_capture(
    *,
    store: str,
    city: str,
    city_id: str,
    sku_key: str,
    offer_url: str,
    displayed_date: str,
    expected_date: str,
    source: str,
    note: str = "",
    observed_at: str | None = None,
    same_day_cutoff_local: str = DEFAULT_SAME_DAY_CUTOFF_LOCAL,
) -> dict[str, Any]:
    observed_text = observed_at or _now_text()
    delta = promise_delta_days(displayed_date=displayed_date, expected_date=expected_date)
    capture = {
        "observed_at": observed_text,
        "store": str(store or "").strip(),
        "city": str(city or "").strip(),
        "city_id": str(city_id or "").strip(),
        "sku_key": str(sku_key or "").strip(),
        "offer_url": str(offer_url or "").strip(),
        "displayed_delivery_date": str(displayed_date or "").strip(),
        "expected_baseline_date": str(expected_date or "").strip(),
        "delivery_promise_delta_days": delta,
        "same_day_shipping_cutoff_local": same_day_cutoff_local,
        "source": str(source or "").strip(),
        "note": str(note or "").strip(),
    }
    return normalize_delivery_capture_health(capture)


def build_delivery_capture_from_days(
    *,
    store: str,
    city: str,
    city_id: str,
    sku_key: str,
    offer_url: str,
    delivery_days: int | None,
    expected_days: int = 1,
    expected_date: str | None = None,
    source: str = "kaspi_offer_view_api",
    note: str = "",
    observed_at: str | None = None,
    same_day_cutoff_local: str = DEFAULT_SAME_DAY_CUTOFF_LOCAL,
) -> dict[str, Any]:
    observed_text = observed_at or _now_text()
    observed_dt = _parse_observed_datetime(observed_text) or datetime.now()
    displayed_date = ""
    if delivery_days is not None:
        displayed_date = (observed_dt.date() + timedelta(days=max(int(delivery_days), 0))).isoformat()
    expected = str(expected_date or "").strip()
    if not expected:
        expected = (observed_dt.date() + timedelta(days=max(int(expected_days), 0))).isoformat()
    return build_delivery_capture(
        store=store,
        city=city,
        city_id=city_id,
        sku_key=sku_key,
        offer_url=offer_url,
        displayed_date=displayed_date,
        expected_date=expected,
        source=source,
        note=note,
        observed_at=observed_text,
        same_day_cutoff_local=same_day_cutoff_local,
    )


def fetch_delivery_promise_capture(
    *,
    store: str,
    merchant_id: str,
    city: str,
    city_id: str,
    sku_key: str,
    offer_url: str,
    expected_days: int = 1,
    expected_date: str | None = None,
    source: str = "kaspi_offer_view_api",
    note: str = "",
) -> dict[str, Any]:
    from playwright.sync_api import sync_playwright

    from .repricer_competitors import _fetch_kaspi_delivery_days_by_mid_for_offer

    with sync_playwright() as p:
        request_context = p.request.new_context()
        try:
            days_by_mid, requests_made = _fetch_kaspi_delivery_days_by_mid_for_offer(
                request_context=request_context,
                offer_link=offer_url,
                city_id=city_id,
            )
        finally:
            request_context.dispose()

    delivery_days = days_by_mid.get(str(merchant_id).strip())
    capture_note = note
    if delivery_days is None:
        suffix = f"merchant_id={merchant_id} not found in Kaspi offer-view response"
        capture_note = f"{capture_note}; {suffix}".strip("; ")
    else:
        suffix = f"requests_made={requests_made}; merchant_id={merchant_id}; delivery_days={delivery_days}"
        capture_note = f"{capture_note}; {suffix}".strip("; ")
    return build_delivery_capture_from_days(
        store=store,
        city=city,
        city_id=city_id,
        sku_key=sku_key,
        offer_url=offer_url,
        delivery_days=delivery_days,
        expected_days=expected_days,
        expected_date=expected_date,
        source=source,
        note=capture_note,
    )


def record_delivery_capture(capture: dict[str, Any], root: Path | str = DEFAULT_DELIVERY_PROMISE_ROOT) -> dict[str, Any]:
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    observed_at = str(capture.get("observed_at") or _now_text())
    day = observed_at[:10] if len(observed_at) >= 10 else datetime.now().strftime("%Y-%m-%d")
    jsonl_path = root / f"captures_{day}.jsonl"
    with jsonl_path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(capture, ensure_ascii=False, default=str) + "\n")
    heartbeat = {
        "generated_at": _now_text(),
        "last_success_at": observed_at,
        "freshness_status": "OK",
        "latest_capture": capture,
        "jsonl_path": str(jsonl_path),
    }
    heartbeat_path = root / "latest_heartbeat.json"
    heartbeat_path.write_text(json.dumps(heartbeat, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return {
        "status": capture.get("health_status"),
        "delta_days": capture.get("delivery_promise_delta_days"),
        "jsonl_path": str(jsonl_path),
        "heartbeat_path": str(heartbeat_path),
    }


def record_delivery_watch_failure(
    *,
    error: str,
    root: Path | str = DEFAULT_DELIVERY_PROMISE_ROOT,
    store: str = "",
    city: str = "",
    city_id: str = "",
    sku_key: str = "",
    offer_url: str = "",
) -> dict[str, Any]:
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    heartbeat = {
        "generated_at": _now_text(),
        "last_success_at": None,
        "heartbeat_status": "red",
        "status": "capture_failed",
        "error": str(error or "").strip(),
        "context": {
            "store": str(store or "").strip(),
            "city": str(city or "").strip(),
            "city_id": str(city_id or "").strip(),
            "sku_key": str(sku_key or "").strip(),
            "offer_url": str(offer_url or "").strip(),
        },
    }
    heartbeat_path = root / "latest_heartbeat.json"
    heartbeat_path.write_text(json.dumps(heartbeat, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return {
        "status": "capture_failed",
        "delta_days": None,
        "heartbeat_path": str(heartbeat_path),
        "error": heartbeat["error"],
    }


def latest_delivery_heartbeat(root: Path | str = DEFAULT_DELIVERY_PROMISE_ROOT) -> dict[str, Any] | None:
    heartbeat_path = Path(root) / "latest_heartbeat.json"
    if not heartbeat_path.exists():
        return None
    try:
        data = json.loads(heartbeat_path.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    capture = data.get("latest_capture")
    if isinstance(capture, dict):
        data = dict(data)
        data["latest_capture"] = normalize_delivery_capture_health(capture)
    return data
