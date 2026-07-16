from __future__ import annotations

import argparse
import csv
import json
import math
import os
import sqlite3
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

import yaml

from .kaspi_marketing_directapi_controls import (
    ENV_CONFIRM_GATE,
    ENV_CONFIRM_VALUE,
    SCHEMA_VERSION as DIRECTAPI_SCHEMA_VERSION,
    run_directapi_control,
)


SCHEMA_VERSION = "web_auto.kaspi_marketing_bid_autopilot.v1"
CONFIG_SCHEMA_VERSION = "web_auto.kaspi_marketing_bid_autopilot_config.v1"
DEFAULT_DB_PATH = Path("data/kaspi_marketing.sqlite")
DEFAULT_RUN_ROOT = Path("runs/bid_autopilot")
DEFAULT_LEDGER_PATH = DEFAULT_RUN_ROOT / "ledger.jsonl"
DEFAULT_CONFIG_PATH = Path("config/tasks/bid_autopilot.yaml")
DEFAULT_CAMPAIGN_IDS = ("2380614", "2626530", "2903195", "2910290", "2695637")
DEFAULT_CAMPAIGN_FAMILIES = {
    "2380614": "LINE51",
    "2626530": "LINE51",
    "2903195": "LINE31",
    "2910290": "LINE31",
    "2695637": "LINE31",
}
DEFAULT_STORE = "ACMEWEAR"
DEFAULT_MERCHANT_ID = "759051"
DEFAULT_STORE_CODE = "30137883"
DEFAULT_STEP_PCT = 0.15
MAX_STEP_PCT = 0.20
MIN_ELASTICITY_RATIO = 0.75
CPO_CONTRIBUTION_RATIO = 0.75
REVERT_COST_GROWTH_THRESHOLD = 0.30
REVERT_VIEWS_GROWTH_THRESHOLD = 0.15
FLOOR_BID_KZT = 40
CEILING_BID_KZT = 200
RECENT_STEP_COOLDOWN_HOURS = 24
DEFAULT_MAX_STEPS_PER_RUN = 3
ENV_STANDING_INSTRUMENT = "WEB_AUTO_BID_AUTOPILOT_STANDING_INSTRUMENT"
ENV_UNIT_CONTRIBUTION = "WEB_AUTO_BID_AUTOPILOT_UNIT_CONTRIBUTION_KZT"
OWNER_AUTHORITY_LABEL = "BID_AUTOPILOT"

DirectAPIRunner = Callable[..., dict[str, Any]]


@dataclass(frozen=True)
class DailyProductMetrics:
    date: str
    campaign_id: str
    campaign_name: str
    sku: str
    merchant_sku: str
    product_status: str
    bid_kzt: int
    views: int
    cost_kzt: float
    orders: int
    ingested_at: str


@dataclass(frozen=True)
class MetricWindow:
    start_date: str
    end_date: str
    days: int
    avg_views: float
    total_views: int
    total_cost_kzt: float
    total_orders: int


@dataclass(frozen=True)
class LastBidStep:
    campaign_id: str
    sku: str
    step_date: str
    previous_bid_kzt: int
    new_bid_kzt: int
    pre_window: MetricWindow
    post_window: MetricWindow


def _timestamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _parse_int(value: Any, default: int = 0) -> int:
    if value is None or value == "":
        return default
    try:
        return int(round(float(value)))
    except Exception:
        return default


def _parse_float(value: Any, default: float = 0.0) -> float:
    if value is None or value == "":
        return default
    try:
        return float(value)
    except Exception:
        return default


def load_autopilot_config(path: Path) -> dict[str, Any]:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ValueError(f"autopilot_config_unreadable:{path}:{type(exc).__name__}") from exc
    except Exception as exc:
        raise ValueError(f"autopilot_config_parse_failed:{type(exc).__name__}:{exc}") from exc
    if not isinstance(raw, dict):
        raise ValueError("autopilot_config_root_must_be_mapping")
    if raw.get("schema_version") != CONFIG_SCHEMA_VERSION:
        raise ValueError(f"autopilot_config_schema_mismatch:{raw.get('schema_version')!r}")
    if str(raw.get("store") or "") != DEFAULT_STORE:
        raise ValueError("autopilot_config_store_must_be_ACMEWEAR")
    if str(raw.get("merchant_id") or "") != DEFAULT_MERCHANT_ID:
        raise ValueError("autopilot_config_merchant_id_mismatch")
    if str(raw.get("store_code") or "") != DEFAULT_STORE_CODE:
        raise ValueError("autopilot_config_store_code_mismatch")

    schedule = raw.get("schedule")
    if not isinstance(schedule, dict):
        raise ValueError("autopilot_config_schedule_must_be_mapping")
    if str(schedule.get("local_time") or "") != "11:30":
        raise ValueError("autopilot_config_schedule_must_be_11_30")
    if str(schedule.get("default_mode") or "") != "dry_run":
        raise ValueError("autopilot_config_default_mode_must_be_dry_run")

    safety = raw.get("safety")
    if not isinstance(safety, dict):
        raise ValueError("autopilot_config_safety_must_be_mapping")
    normalized_safety = {
        "step_pct": _parse_float(safety.get("step_pct")),
        "max_step_pct_24h": _parse_float(safety.get("max_step_pct_24h")),
        "floor_bid_kzt": _parse_int(safety.get("floor_bid_kzt")),
        "ceiling_bid_kzt": _parse_int(safety.get("ceiling_bid_kzt")),
        "max_steps_per_run": _parse_int(safety.get("max_steps_per_run")),
    }
    if not (0 < normalized_safety["step_pct"] <= normalized_safety["max_step_pct_24h"]):
        raise ValueError("autopilot_config_step_pct_out_of_bounds")
    if normalized_safety["max_step_pct_24h"] != MAX_STEP_PCT:
        raise ValueError("autopilot_config_max_step_pct_must_be_0_20")
    if normalized_safety["floor_bid_kzt"] != FLOOR_BID_KZT:
        raise ValueError("autopilot_config_floor_must_be_40")
    if normalized_safety["ceiling_bid_kzt"] != CEILING_BID_KZT:
        raise ValueError("autopilot_config_ceiling_must_be_200")
    if normalized_safety["max_steps_per_run"] != DEFAULT_MAX_STEPS_PER_RUN:
        raise ValueError("autopilot_config_max_steps_per_run_must_be_3")

    raw_bases = raw.get("unit_contribution_bases")
    if not isinstance(raw_bases, dict) or not raw_bases:
        raise ValueError("autopilot_config_unit_contribution_bases_missing")
    bases: dict[str, dict[str, Any]] = {}
    for basis_name, raw_basis in raw_bases.items():
        if not isinstance(raw_basis, dict):
            raise ValueError(f"autopilot_config_basis_must_be_mapping:{basis_name}")
        value = _parse_float(raw_basis.get("unit_contribution_kzt"))
        source_path_text = str(raw_basis.get("source_path") or "").strip()
        source_family = str(raw_basis.get("source_family") or "").strip()
        source_column = str(raw_basis.get("source_column") or "unit_contribution_kzt").strip()
        if value <= 0 or not source_path_text or not source_family or not source_column:
            raise ValueError(f"autopilot_config_basis_incomplete:{basis_name}")
        source_path = (path.parent / source_path_text).resolve()
        try:
            with source_path.open(newline="", encoding="utf-8") as handle:
                source_rows = list(csv.DictReader(handle))
        except OSError as exc:
            raise ValueError(
                f"autopilot_config_basis_source_unreadable:{basis_name}:{source_path}:{type(exc).__name__}"
            ) from exc
        matching_rows = [
            row
            for row in source_rows
            if str(row.get("family") or "").strip() == source_family
            and _parse_float(row.get(source_column), default=-1.0) == value
        ]
        if not matching_rows:
            raise ValueError(
                f"autopilot_config_basis_not_validated:{basis_name}:{source_family}:{value}"
            )
        bases[str(basis_name)] = {
            "unit_contribution_kzt": value,
            "source": f"{source_path}#family={source_family}:{source_column}={value:g}",
        }

    raw_campaigns = raw.get("campaigns")
    if not isinstance(raw_campaigns, list) or not raw_campaigns:
        raise ValueError("autopilot_config_campaigns_missing")
    campaign_ids: list[str] = []
    campaign_families: dict[str, str] = {}
    expected_skus: dict[str, str] = {}
    unit_contributions: dict[str, float] = {}
    unit_contribution_sources: dict[str, str] = {}
    increase_holds: dict[str, dict[str, str]] = {}
    for index, raw_campaign in enumerate(raw_campaigns):
        if not isinstance(raw_campaign, dict):
            raise ValueError(f"autopilot_config_campaign_must_be_mapping:{index}")
        campaign_id = str(raw_campaign.get("campaign_id") or "").strip()
        expected_sku = str(raw_campaign.get("expected_sku") or "").strip()
        basis_name = str(raw_campaign.get("unit_contribution_basis") or "").strip()
        if not campaign_id or not expected_sku or basis_name not in bases:
            raise ValueError(f"autopilot_config_campaign_incomplete:{index}")
        if campaign_id in campaign_ids:
            raise ValueError(f"autopilot_config_duplicate_campaign:{campaign_id}")
        campaign_ids.append(campaign_id)
        campaign_families[campaign_id] = basis_name
        expected_skus[campaign_id] = expected_sku
        unit_contributions[campaign_id] = float(bases[basis_name]["unit_contribution_kzt"])
        unit_contribution_sources[campaign_id] = str(bases[basis_name]["source"])
        hold_before = str(raw_campaign.get("increase_hold_before_date") or "").strip()
        if hold_before:
            try:
                datetime.fromisoformat(hold_before).date()
            except ValueError as exc:
                raise ValueError(
                    f"autopilot_config_increase_hold_date_invalid:{campaign_id}:{hold_before}"
                ) from exc
            increase_holds[campaign_id] = {
                "before_date": hold_before,
                "reason": str(raw_campaign.get("increase_hold_reason") or "").strip(),
                "authority": str(raw_campaign.get("increase_hold_authority") or "").strip(),
            }

    return {
        "path": str(path.resolve()),
        "campaign_ids": campaign_ids,
        "campaign_families": campaign_families,
        "expected_skus": expected_skus,
        "unit_contribution_by_campaign": unit_contributions,
        "unit_contribution_sources": unit_contribution_sources,
        "increase_holds": increase_holds,
        "safety": normalized_safety,
        "schedule": dict(schedule),
    }


def _parse_dt(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except Exception:
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone().replace(tzinfo=None)
    return parsed


def bounded_bid(
    *,
    current_bid: int,
    proposed_bid: int,
    floor_bid: int = FLOOR_BID_KZT,
    ceiling_bid: int = CEILING_BID_KZT,
    max_step_pct: float = MAX_STEP_PCT,
) -> int:
    if current_bid <= 0:
        raise ValueError("current_bid_must_be_positive")
    upper = math.floor(current_bid * (1.0 + max_step_pct))
    lower = math.ceil(current_bid * (1.0 - max_step_pct))
    bounded = min(proposed_bid, ceiling_bid, upper)
    bounded = max(bounded, floor_bid, lower)
    return int(bounded)


def growth_rate(before: float, after: float) -> float | None:
    if before <= 0:
        return None
    return (after - before) / before


def elasticity_ratio(
    *,
    previous_bid: int,
    new_bid: int,
    pre_views: float,
    post_views: float,
) -> float | None:
    bid_growth = growth_rate(float(previous_bid), float(new_bid))
    views_growth = growth_rate(float(pre_views), float(post_views))
    if bid_growth is None or views_growth is None or bid_growth <= 0:
        return None
    return views_growth / bid_growth


def elasticity_healthy(
    *,
    previous_bid: int,
    new_bid: int,
    pre_views: float,
    post_views: float,
    min_ratio: float = MIN_ELASTICITY_RATIO,
) -> bool:
    ratio = elasticity_ratio(
        previous_bid=previous_bid,
        new_bid=new_bid,
        pre_views=pre_views,
        post_views=post_views,
    )
    return ratio is not None and ratio >= min_ratio


def cpo_kzt(*, cost_kzt: float, orders: int) -> float:
    if orders <= 0:
        return math.inf
    return cost_kzt / orders


def should_revert(
    *,
    pre_cost_kzt: float,
    post_cost_kzt: float,
    pre_views: float,
    post_views: float,
    cost_growth_threshold: float = REVERT_COST_GROWTH_THRESHOLD,
    views_growth_threshold: float = REVERT_VIEWS_GROWTH_THRESHOLD,
) -> bool:
    cost_growth = growth_rate(pre_cost_kzt, post_cost_kzt)
    views_growth = growth_rate(pre_views, post_views)
    if cost_growth is None or views_growth is None:
        return False
    return cost_growth >= cost_growth_threshold and views_growth < views_growth_threshold


def _window(rows: Sequence[DailyProductMetrics]) -> MetricWindow | None:
    if not rows:
        return None
    total_views = sum(row.views for row in rows)
    total_cost = sum(row.cost_kzt for row in rows)
    total_orders = sum(row.orders for row in rows)
    return MetricWindow(
        start_date=rows[0].date,
        end_date=rows[-1].date,
        days=len(rows),
        avg_views=total_views / len(rows),
        total_views=total_views,
        total_cost_kzt=total_cost,
        total_orders=total_orders,
    )


def latest_bid_step(
    rows: Sequence[DailyProductMetrics],
    *,
    baseline_days: int = 3,
    post_days: int = 3,
) -> LastBidStep | None:
    if len(rows) < 2:
        return None
    last_index: int | None = None
    for index in range(1, len(rows)):
        if rows[index].bid_kzt != rows[index - 1].bid_kzt:
            last_index = index
    if last_index is None:
        return None

    pre_rows = list(rows[max(0, last_index - baseline_days) : last_index])
    post_rows = list(rows[last_index : min(len(rows), last_index + post_days)])
    pre_window = _window(pre_rows)
    post_window = _window(post_rows)
    if pre_window is None or post_window is None:
        return None

    current = rows[last_index]
    previous = rows[last_index - 1]
    return LastBidStep(
        campaign_id=current.campaign_id,
        sku=current.sku,
        step_date=current.date,
        previous_bid_kzt=previous.bid_kzt,
        new_bid_kzt=current.bid_kzt,
        pre_window=pre_window,
        post_window=post_window,
    )


def parse_unit_contribution_map(values: Sequence[str] | None, env_value: str | None = None) -> dict[str, float]:
    result: dict[str, float] = {}
    raw_items: list[str] = []
    if env_value:
        stripped = env_value.strip()
        if stripped.startswith("{"):
            payload = json.loads(stripped)
            if not isinstance(payload, dict):
                raise ValueError("unit_contribution_env_json_must_be_object")
            raw_items.extend(f"{key}={value}" for key, value in payload.items())
        else:
            raw_items.extend(part.strip() for part in stripped.split(",") if part.strip())
    raw_items.extend(values or [])
    for item in raw_items:
        if "=" not in item:
            raise ValueError(f"unit_contribution_must_be_campaign_equals_value:{item}")
        campaign_id, value = item.split("=", 1)
        campaign_id = campaign_id.strip()
        if not campaign_id:
            raise ValueError(f"unit_contribution_missing_campaign:{item}")
        result[campaign_id] = float(value)
    return result


def infer_family_label(rows: Sequence[DailyProductMetrics]) -> str:
    if not rows:
        return ""
    campaign_id = rows[-1].campaign_id
    if campaign_id in DEFAULT_CAMPAIGN_FAMILIES:
        return DEFAULT_CAMPAIGN_FAMILIES[campaign_id]
    text = " ".join(
        [
            rows[-1].campaign_name,
            rows[-1].merchant_sku,
            rows[-1].sku,
        ]
    ).upper()
    for family in ("LINE51", "LINE61", "LINE31"):
        if family in text:
            return family
    return ""


def resolve_unit_contribution(
    unit_map: Mapping[str, float],
    *,
    campaign_id: str,
    rows: Sequence[DailyProductMetrics],
) -> tuple[float | None, str]:
    if campaign_id in unit_map:
        return unit_map[campaign_id], campaign_id
    family = infer_family_label(rows)
    if family and family in unit_map:
        return unit_map[family], family
    return None, ""


def _latest_step_at(ledger_rows: Sequence[Mapping[str, Any]], campaign_id: str) -> datetime | None:
    latest: datetime | None = None
    for row in ledger_rows:
        if str(row.get("campaign_id") or "") != campaign_id:
            continue
        if str(row.get("change_status") or "") not in {"applied", "reverted"}:
            continue
        candidate = _parse_dt(row.get("applied_at") or row.get("generated_at_local"))
        if candidate is None:
            continue
        if latest is None or candidate > latest:
            latest = candidate
    return latest


def load_ledger_rows(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        payload = json.loads(line)
        if isinstance(payload, dict):
            rows.append(payload)
    return rows


def append_ledger_rows(path: Path, rows: Sequence[Mapping[str, Any]]) -> None:
    if not rows:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(dict(row), ensure_ascii=False, sort_keys=True) + "\n")


def load_campaign_rows(db_path: Path, campaign_id: str) -> list[DailyProductMetrics]:
    uri = f"file:{db_path}?mode=ro"
    with sqlite3.connect(uri, uri=True) as conn:
        conn.row_factory = sqlite3.Row
        raw_rows = conn.execute(
            """
            SELECT date, campaign_id, campaign_name, sku_key, product_status, bid_cpc,
                   views, cost, orders_total, json_sku, json_merchant_sku, ingested_at
            FROM campaign_product_daily_current
            WHERE campaign_id = ?
            ORDER BY date, ingested_at
            """,
            (campaign_id,),
        ).fetchall()

    latest_by_date: dict[str, sqlite3.Row] = {}
    for row in raw_rows:
        latest_by_date[str(row["date"])] = row

    rows: list[DailyProductMetrics] = []
    for date in sorted(latest_by_date):
        row = latest_by_date[date]
        sku = str(row["json_sku"] or row["sku_key"] or "").strip()
        rows.append(
            DailyProductMetrics(
                date=str(row["date"] or ""),
                campaign_id=str(row["campaign_id"] or ""),
                campaign_name=str(row["campaign_name"] or ""),
                sku=sku,
                merchant_sku=str(row["json_merchant_sku"] or "").strip(),
                product_status=str(row["product_status"] or ""),
                bid_kzt=_parse_int(row["bid_cpc"]),
                views=_parse_int(row["views"]),
                cost_kzt=_parse_float(row["cost"]),
                orders=_parse_int(row["orders_total"]),
                ingested_at=str(row["ingested_at"] or ""),
            )
        )
    return rows


def evaluate_campaign(
    *,
    rows: Sequence[DailyProductMetrics],
    unit_contribution_kzt: float | None,
    ledger_rows: Sequence[Mapping[str, Any]],
    now: datetime,
    step_pct: float = DEFAULT_STEP_PCT,
    floor_bid: int = FLOOR_BID_KZT,
    ceiling_bid: int = CEILING_BID_KZT,
    max_step_pct: float = MAX_STEP_PCT,
    cpo_days: int = 3,
    baseline_days: int = 3,
    post_days: int = 3,
) -> dict[str, Any]:
    if not rows:
        return {"decision": "HOLD", "action": None, "reasons": ["missing_campaign_rows"]}
    if step_pct <= 0 or step_pct > MAX_STEP_PCT:
        return {"decision": "HOLD", "action": None, "reasons": [f"step_pct_out_of_bounds:{step_pct}"]}

    current = rows[-1]
    reasons: list[str] = []
    last_step = latest_bid_step(rows, baseline_days=baseline_days, post_days=post_days)
    cpo_window = _window(rows[-cpo_days:])
    recent_step_at = _latest_step_at(ledger_rows, current.campaign_id)
    recent_step_blocked = bool(recent_step_at and now - recent_step_at < timedelta(hours=RECENT_STEP_COOLDOWN_HOURS))

    if last_step and last_step.new_bid_kzt > last_step.previous_bid_kzt:
        if should_revert(
            pre_cost_kzt=last_step.pre_window.total_cost_kzt,
            post_cost_kzt=last_step.post_window.total_cost_kzt,
            pre_views=last_step.pre_window.avg_views,
            post_views=last_step.post_window.avg_views,
        ):
            target = bounded_bid(
                current_bid=current.bid_kzt,
                proposed_bid=last_step.previous_bid_kzt,
                floor_bid=floor_bid,
                ceiling_bid=ceiling_bid,
                max_step_pct=max_step_pct,
            )
            if target == last_step.previous_bid_kzt:
                return {
                    "decision": "AUTO_REVERT",
                    "action": {
                        "action_type": "auto_revert",
                        "campaign_id": current.campaign_id,
                        "campaign_name": current.campaign_name,
                        "sku": current.sku,
                        "merchant_sku": current.merchant_sku,
                        "current_bid_kzt": current.bid_kzt,
                        "target_bid_kzt": target,
                        "previous_bid_kzt": last_step.previous_bid_kzt,
                        "reason": "cost_up_30pct_views_under_15pct_vs_pre_step_baseline",
                    },
                    "reasons": [],
                    "last_step": _last_step_payload(last_step),
                    "cpo_window": asdict(cpo_window) if cpo_window else None,
                }
            reasons.append("revert_blocked_by_daily_step_bound")

    if recent_step_blocked:
        reasons.append("step_in_last_24h")
    if unit_contribution_kzt is None:
        reasons.append("missing_unit_contribution_kzt")
    if cpo_window is None:
        reasons.append("missing_cpo_window")
    else:
        observed_cpo = cpo_kzt(cost_kzt=cpo_window.total_cost_kzt, orders=cpo_window.total_orders)
        if unit_contribution_kzt is not None and observed_cpo > (CPO_CONTRIBUTION_RATIO * unit_contribution_kzt):
            reasons.append("cpo_above_0_75x_unit_contribution")
    if last_step is None:
        reasons.append("missing_trailing_bid_step")
    elif last_step.new_bid_kzt <= last_step.previous_bid_kzt:
        reasons.append("last_bid_step_was_not_an_increase")
    elif not elasticity_healthy(
        previous_bid=last_step.previous_bid_kzt,
        new_bid=last_step.new_bid_kzt,
        pre_views=last_step.pre_window.avg_views,
        post_views=last_step.post_window.avg_views,
    ):
        reasons.append("elasticity_below_0_75x_bid_growth")

    if reasons:
        return {
            "decision": "HOLD",
            "action": None,
            "reasons": reasons,
            "last_step": _last_step_payload(last_step) if last_step else None,
            "cpo_window": asdict(cpo_window) if cpo_window else None,
        }

    proposed = int(current.bid_kzt * (1.0 + step_pct) + 0.5)
    target = bounded_bid(
        current_bid=current.bid_kzt,
        proposed_bid=proposed,
        floor_bid=floor_bid,
        ceiling_bid=ceiling_bid,
        max_step_pct=max_step_pct,
    )
    if target <= current.bid_kzt:
        return {
            "decision": "HOLD",
            "action": None,
            "reasons": ["ceiling_or_bounds_prevent_step_up"],
            "last_step": _last_step_payload(last_step) if last_step else None,
            "cpo_window": asdict(cpo_window) if cpo_window else None,
        }

    return {
        "decision": "STEP_UP",
        "action": {
            "action_type": "step_up",
            "campaign_id": current.campaign_id,
            "campaign_name": current.campaign_name,
            "sku": current.sku,
            "merchant_sku": current.merchant_sku,
            "current_bid_kzt": current.bid_kzt,
            "target_bid_kzt": target,
            "reason": "healthy_elasticity_and_cpo_with_no_recent_step",
        },
        "reasons": [],
        "last_step": _last_step_payload(last_step) if last_step else None,
        "cpo_window": asdict(cpo_window) if cpo_window else None,
    }


def enforce_portfolio_step_cap(
    evaluations: Sequence[Mapping[str, Any]],
    *,
    max_steps_per_run: int = DEFAULT_MAX_STEPS_PER_RUN,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    if max_steps_per_run < 1:
        raise ValueError("max_steps_per_run_must_be_positive")
    candidates: list[tuple[int, int, dict[str, Any]]] = []
    for index, evaluation in enumerate(evaluations):
        action = evaluation.get("action")
        if not isinstance(action, Mapping):
            continue
        priority = 0 if str(action.get("action_type") or "") == "auto_revert" else 1
        candidates.append((priority, index, dict(action)))
    selected_indexes = {
        index
        for _priority, index, _action in sorted(candidates, key=lambda item: (item[0], item[1]))[
            :max_steps_per_run
        ]
    }

    capped_evaluations: list[dict[str, Any]] = []
    executable_actions: list[dict[str, Any]] = []
    plan_only_actions: list[dict[str, Any]] = []
    for index, evaluation in enumerate(evaluations):
        payload = dict(evaluation)
        action = payload.get("action")
        if not isinstance(action, Mapping):
            capped_evaluations.append(payload)
            continue
        action_payload = dict(action)
        if index in selected_indexes:
            executable_actions.append(action_payload)
        else:
            flagged_action = {
                **action_payload,
                "flag": "portfolio_daily_step_cap_exceeded",
                "original_decision": str(payload.get("decision") or ""),
            }
            plan_only_actions.append(flagged_action)
            payload["decision"] = "PLAN_ONLY"
            payload["plan_only_action"] = action_payload
            payload["action"] = None
            payload["reasons"] = ["portfolio_daily_step_cap_exceeded"]
        capped_evaluations.append(payload)
    return capped_evaluations, executable_actions, plan_only_actions


def _last_step_payload(step: LastBidStep | None) -> dict[str, Any] | None:
    if step is None:
        return None
    ratio = elasticity_ratio(
        previous_bid=step.previous_bid_kzt,
        new_bid=step.new_bid_kzt,
        pre_views=step.pre_window.avg_views,
        post_views=step.post_window.avg_views,
    )
    return {
        "campaign_id": step.campaign_id,
        "sku": step.sku,
        "step_date": step.step_date,
        "previous_bid_kzt": step.previous_bid_kzt,
        "new_bid_kzt": step.new_bid_kzt,
        "pre_window": asdict(step.pre_window),
        "post_window": asdict(step.post_window),
        "elasticity_ratio": ratio,
        "elasticity_healthy": ratio is not None and ratio >= MIN_ELASTICITY_RATIO,
        "revert_triggered": should_revert(
            pre_cost_kzt=step.pre_window.total_cost_kzt,
            post_cost_kzt=step.post_window.total_cost_kzt,
            pre_views=step.pre_window.avg_views,
            post_views=step.post_window.avg_views,
        ),
    }


def standing_instrument_path(env: Mapping[str, str] | None = None) -> Path | None:
    env_values = env if env is not None else os.environ
    raw = str(env_values.get(ENV_STANDING_INSTRUMENT) or "").strip()
    return Path(raw).expanduser() if raw else None


def standing_instrument_ready(env: Mapping[str, str] | None = None) -> bool:
    path = standing_instrument_path(env)
    return bool(path and path.is_file())


def build_directapi_plan(
    *,
    actions: Sequence[Mapping[str, Any]],
    target_date: str,
    owner_approval_file: Path | None,
) -> dict[str, Any]:
    plan: dict[str, Any] = {
        "schema_version": DIRECTAPI_SCHEMA_VERSION,
        "store": DEFAULT_STORE,
        "merchant_id": DEFAULT_MERCHANT_ID,
        "store_code": DEFAULT_STORE_CODE,
        "target_date": target_date,
        "actions": [],
    }
    if owner_approval_file is not None:
        plan["owner_approval_file"] = str(owner_approval_file)
        plan["owner_approval_label"] = OWNER_AUTHORITY_LABEL
    for action in actions:
        plan["actions"].append(
            {
                "action_id": (
                    f"bid_autopilot_{action['action_type']}_"
                    f"{action['campaign_id']}_{action['current_bid_kzt']}_to_{action['target_bid_kzt']}"
                ),
                "operation": "product_bid",
                "campaign_id": str(action["campaign_id"]),
                "sku": str(action["sku"]),
                "expected_current_bid": int(action["current_bid_kzt"]),
                "new_bid": int(action["target_bid_kzt"]),
            }
        )
    return plan


def render_report(summary: Mapping[str, Any]) -> str:
    lines = [
        "# Kaspi Marketing Bid Autopilot Build Run",
        "",
        f"Gate: {summary.get('gate')}",
        "",
        f"- status: `{summary.get('status')}`",
        f"- mode: `{summary.get('mode')}`",
        f"- generated_at_local: `{summary.get('generated_at_local')}`",
        f"- run_dir: `{summary.get('run_dir')}`",
        f"- db_path: `{summary.get('db_path')}`",
        f"- ledger_path: `{summary.get('ledger_path')}`",
        f"- live_writes_executed: `{summary.get('live_writes_executed')}`",
        f"- directapi_plan_path: `{summary.get('directapi_plan_path') or ''}`",
        f"- directapi_dry_run_summary_path: `{summary.get('directapi_dry_run_summary_path') or ''}`",
        f"- directapi_confirm_summary_path: `{summary.get('directapi_confirm_summary_path') or ''}`",
        "",
        "## Evaluations",
        "",
    ]
    for item in summary.get("evaluations", []):
        lines.append(
            f"- `{item.get('campaign_id')}` `{item.get('decision')}`: "
            f"{', '.join(item.get('reasons') or ['action_planned'])}"
        )
    lines.extend(
        [
            "",
            "## Safety",
            "",
            f"- Portfolio action cap: `{summary.get('safety', {}).get('max_steps_per_run')}` steps per run; "
            f"plan-only overflow: `{summary.get('safety', {}).get('plan_only_action_count')}`.",
            f"- Confirm mode requires `{ENV_STANDING_INSTRUMENT}` to point to an existing owner instrument file.",
            f"- Product BID apply is delegated to the existing `directapi-control` resolver/confirm path.",
            "- This run records no secrets, cookies, tokens, authorization headers, storage state, or PII.",
            "",
        ]
    )
    return "\n".join(lines)


def run_bid_autopilot(
    *,
    db_path: Path = DEFAULT_DB_PATH,
    ledger_path: Path = DEFAULT_LEDGER_PATH,
    run_root: Path = DEFAULT_RUN_ROOT,
    campaign_ids: Sequence[str] = DEFAULT_CAMPAIGN_IDS,
    unit_contribution_by_campaign: Mapping[str, float] | None = None,
    unit_contribution_sources: Mapping[str, str] | None = None,
    expected_skus: Mapping[str, str] | None = None,
    increase_holds: Mapping[str, Mapping[str, str]] | None = None,
    config_path: str = "",
    dry_run: bool = True,
    confirm: bool = False,
    timestamp: str | None = None,
    env: Mapping[str, str] | None = None,
    directapi_runner: DirectAPIRunner = run_directapi_control,
    now: datetime | None = None,
    step_pct: float = DEFAULT_STEP_PCT,
    floor_bid: int = FLOOR_BID_KZT,
    ceiling_bid: int = CEILING_BID_KZT,
    max_step_pct: float = MAX_STEP_PCT,
    max_steps_per_run: int = DEFAULT_MAX_STEPS_PER_RUN,
) -> dict[str, Any]:
    if dry_run == confirm:
        raise ValueError("choose_exactly_one_of_dry_run_or_confirm")

    generated_at = datetime.now().isoformat(timespec="seconds")
    run_id = f"{timestamp or _timestamp()}_bid_autopilot"
    run_dir = run_root / run_id
    run_dir.mkdir(parents=True, exist_ok=False)
    env_values = env if env is not None else os.environ
    now_value = now or datetime.now()
    unit_map = dict(unit_contribution_by_campaign or {})
    unit_sources = dict(unit_contribution_sources or {})
    expected_sku_map = dict(expected_skus or {})
    increase_hold_map = dict(increase_holds or {})
    ledger_rows = load_ledger_rows(ledger_path)

    evaluations: list[dict[str, Any]] = []
    readback: dict[str, Any] = {}
    data_errors: list[str] = []
    for campaign_id in campaign_ids:
        try:
            rows = load_campaign_rows(db_path, str(campaign_id))
        except Exception as exc:
            rows = []
            data_errors.append(f"{campaign_id}:db_read_failed:{type(exc).__name__}:{exc}")
        unit_contribution, unit_contribution_source_key = resolve_unit_contribution(
            unit_map,
            campaign_id=str(campaign_id),
            rows=rows,
        )
        unit_contribution_source = str(
            unit_sources.get(str(campaign_id))
            or unit_sources.get(unit_contribution_source_key)
            or unit_contribution_source_key
        )
        readback[str(campaign_id)] = [asdict(row) for row in rows[-5:]]
        current = rows[-1] if rows else None
        expected_sku = str(expected_sku_map.get(str(campaign_id)) or "")
        if current and expected_sku and current.sku != expected_sku:
            mismatch = f"configured_sku_mismatch:{current.sku}!={expected_sku}"
            data_errors.append(f"{campaign_id}:{mismatch}")
            evaluation = {"decision": "HOLD", "action": None, "reasons": [mismatch]}
        else:
            evaluation = evaluate_campaign(
                rows=rows,
                unit_contribution_kzt=unit_contribution,
                ledger_rows=ledger_rows,
                now=now_value,
                step_pct=step_pct,
                floor_bid=floor_bid,
                ceiling_bid=ceiling_bid,
                max_step_pct=max_step_pct,
            )
        increase_hold = dict(increase_hold_map.get(str(campaign_id)) or {})
        if evaluation.get("decision") == "STEP_UP" and increase_hold.get("before_date"):
            hold_before_date = datetime.fromisoformat(increase_hold["before_date"]).date()
            if now_value.date() < hold_before_date:
                evaluation = {
                    **evaluation,
                    "decision": "HOLD",
                    "action": None,
                    "reasons": [
                        f"owner_increase_hold_before:{increase_hold['before_date']}",
                        str(increase_hold.get("reason") or "owner_recorded_increase_hold"),
                    ],
                }
        evaluation_payload = {
            "campaign_id": str(campaign_id),
            "campaign_name": current.campaign_name if current else "",
            "latest_date": current.date if current else "",
            "current_bid_kzt": current.bid_kzt if current else None,
            "unit_contribution_kzt": unit_contribution,
            "unit_contribution_source": unit_contribution_source,
            "expected_sku": expected_sku,
            "increase_hold": increase_hold,
            **evaluation,
        }
        evaluations.append(evaluation_payload)

    evaluations, actions, plan_only_actions = enforce_portfolio_step_cap(
        evaluations,
        max_steps_per_run=max_steps_per_run,
    )

    _write_json(run_dir / "READBACK_LOCAL_CURRENT.json", readback)
    _write_json(
        run_dir / "EVALUATION.json",
        {
            "schema_version": SCHEMA_VERSION,
            "evaluations": evaluations,
            "executable_actions": actions,
            "plan_only_actions": plan_only_actions,
            "max_steps_per_run": max_steps_per_run,
        },
    )

    owner_instrument = standing_instrument_path(env_values)
    owner_instrument_for_plan = owner_instrument if owner_instrument and owner_instrument.is_file() else None
    directapi_plan_path = ""
    directapi_dry_summary: dict[str, Any] = {}
    directapi_confirm_summary: dict[str, Any] = {}
    live_writes_executed = False
    status = "no_actions_planned"
    gate = "GREEN"

    if actions:
        status = (
            "actions_planned_dry_run_ready_with_portfolio_cap_flags"
            if plan_only_actions
            else "actions_planned_dry_run_ready"
        )
        target_date = max(str(item.get("latest_date") or "") for item in evaluations) or now_value.date().isoformat()
        directapi_plan = build_directapi_plan(
            actions=actions,
            target_date=target_date,
            owner_approval_file=owner_instrument_for_plan,
        )
        plan_file = run_dir / "directapi_control_plan.json"
        _write_json(plan_file, directapi_plan)
        directapi_plan_path = str(plan_file)
        directapi_dry_summary = directapi_runner(
            plan_file=plan_file,
            dry_run=True,
            confirm=False,
            run_root=run_dir / "directapi",
            timestamp=f"{timestamp or _timestamp()}_dryrun",
            env=dict(env_values),
        )
        if confirm:
            if not owner_instrument_for_plan:
                gate = "YELLOW"
                status = "blocked_standing_instrument_missing"
            else:
                confirm_env = dict(env_values)
                confirm_env[ENV_CONFIRM_GATE] = ENV_CONFIRM_VALUE
                directapi_confirm_summary = directapi_runner(
                    plan_file=plan_file,
                    dry_run=False,
                    confirm=True,
                    run_root=run_dir / "directapi",
                    timestamp=f"{timestamp or _timestamp()}_confirm",
                    env=confirm_env,
                )
                gate = str(directapi_confirm_summary.get("gate") or "YELLOW")
                status = f"directapi_confirm_{directapi_confirm_summary.get('status')}"
                live_writes_executed = bool(directapi_confirm_summary.get("live_writes_executed"))

    if data_errors:
        gate = "YELLOW"
        status = "data_read_incomplete"

    ledger_payloads: list[dict[str, Any]] = []
    if actions or plan_only_actions:
        change_status = "planned_dry_run"
        applied_at = ""
        if confirm:
            if live_writes_executed and gate == "GREEN":
                change_status = "applied"
                applied_at = generated_at
            else:
                change_status = "blocked_confirm"
        ledger_candidates = [(action, True) for action in actions] + [
            (action, False) for action in plan_only_actions
        ]
        for action, included_in_directapi_plan in ledger_candidates:
            action_change_status = (
                change_status if included_in_directapi_plan else "planned_portfolio_cap_exceeded"
            )
            ledger_payloads.append(
                {
                    "schema_version": SCHEMA_VERSION,
                    "generated_at_local": generated_at,
                    "applied_at": applied_at,
                    "run_dir": str(run_dir),
                    "campaign_id": action["campaign_id"],
                    "campaign_name": action["campaign_name"],
                    "sku": action["sku"],
                    "merchant_sku": action["merchant_sku"],
                    "action_type": action["action_type"],
                    "old_bid_kzt": action["current_bid_kzt"],
                    "new_bid_kzt": action["target_bid_kzt"],
                    "change_status": action_change_status,
                    "owner_authority_label": OWNER_AUTHORITY_LABEL,
                    "owner_instrument_path": str(owner_instrument_for_plan or ""),
                    "directapi_plan_path": directapi_plan_path if included_in_directapi_plan else "",
                    "included_in_directapi_plan": included_in_directapi_plan,
                }
            )
        append_ledger_rows(ledger_path, ledger_payloads)

    summary = {
        "schema_version": SCHEMA_VERSION,
        "generated_at_local": generated_at,
        "run_id": run_id,
        "run_dir": str(run_dir),
        "mode": "confirm" if confirm else "dry_run",
        "status": status,
        "gate": gate,
        "db_path": str(db_path),
        "ledger_path": str(ledger_path),
        "config_path": config_path,
        "campaign_ids": list(campaign_ids),
        "evaluations": evaluations,
        "planned_actions": actions,
        "plan_only_actions": plan_only_actions,
        "safety": {
            "step_pct": step_pct,
            "max_step_pct_24h": max_step_pct,
            "floor_bid_kzt": floor_bid,
            "ceiling_bid_kzt": ceiling_bid,
            "max_steps_per_run": max_steps_per_run,
            "executable_action_count": len(actions),
            "plan_only_action_count": len(plan_only_actions),
        },
        "data_errors": data_errors,
        "directapi_plan_path": directapi_plan_path,
        "directapi_dry_run_summary_path": str(directapi_dry_summary.get("summary_path") or ""),
        "directapi_confirm_summary_path": str(directapi_confirm_summary.get("summary_path") or ""),
        "directapi_dry_run_summary": directapi_dry_summary,
        "directapi_confirm_summary": directapi_confirm_summary,
        "ledger_rows_appended": len(ledger_payloads),
        "live_writes_executed": live_writes_executed,
        "standing_instrument_env": ENV_STANDING_INSTRUMENT,
        "standing_instrument_present": bool(owner_instrument_for_plan),
        "secrets_or_tokens_recorded": False,
    }
    _write_json(run_dir / "SUMMARY.json", summary)
    (run_dir / "REPORT.md").write_text(render_report(summary), encoding="utf-8")
    return summary


def _parse_campaign_ids(args: argparse.Namespace, default_campaign_ids: Sequence[str]) -> list[str]:
    campaign_ids: list[str] = []
    for value in args.campaign_id or []:
        campaign_ids.append(str(value).strip())
    if args.campaign_ids:
        campaign_ids.extend(part.strip() for part in args.campaign_ids.split(",") if part.strip())
    return campaign_ids or list(default_campaign_ids)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Build and run the Kaspi Marketing BID autopilot evaluation.")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="Evaluate and dry-run only. This is the default.")
    mode.add_argument("--confirm", action="store_true", help="Attempt confirm path only when standing instrument exists.")
    parser.add_argument("--db-path", default=str(DEFAULT_DB_PATH), help="Local Kaspi Marketing SQLite path.")
    parser.add_argument("--ledger-path", default=str(DEFAULT_LEDGER_PATH), help="Autopilot JSONL ledger path.")
    parser.add_argument("--run-root", default=str(DEFAULT_RUN_ROOT), help="Run root for evidence packets.")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG_PATH), help="Validated enrollment and safety config.")
    parser.add_argument("--campaign-id", action="append", help="Campaign id to enroll; repeatable.")
    parser.add_argument("--campaign-ids", help="Comma-separated campaign ids to enroll.")
    parser.add_argument(
        "--unit-contribution-kzt",
        action="append",
        default=[],
        help="Campaign contribution as campaign_id=value. Repeatable. Env JSON/comma source is also supported.",
    )
    parser.add_argument("--step-pct", type=float, help="Optional step-up percentage override, max 0.20.")
    parser.add_argument("--timestamp", help="Override timestamp for deterministic artifacts.")
    parser.add_argument("--json", action="store_true", help="Print JSON summary.")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    dry_run = not args.confirm
    try:
        config = load_autopilot_config(Path(args.config))
        runtime_unit_map = parse_unit_contribution_map(
            args.unit_contribution_kzt,
            env_value=os.environ.get(ENV_UNIT_CONTRIBUTION),
        )
        unit_map = dict(config["unit_contribution_by_campaign"])
        unit_map.update(runtime_unit_map)
        unit_sources = dict(config["unit_contribution_sources"])
        unit_sources.update({key: "runtime_cli_or_env_override" for key in runtime_unit_map})
        safety = config["safety"]
        summary = run_bid_autopilot(
            db_path=Path(args.db_path),
            ledger_path=Path(args.ledger_path),
            run_root=Path(args.run_root),
            campaign_ids=_parse_campaign_ids(args, config["campaign_ids"]),
            unit_contribution_by_campaign=unit_map,
            unit_contribution_sources=unit_sources,
            expected_skus=config["expected_skus"],
            increase_holds=config["increase_holds"],
            config_path=config["path"],
            dry_run=dry_run,
            confirm=bool(args.confirm),
            timestamp=args.timestamp,
            env=os.environ,
            step_pct=args.step_pct if args.step_pct is not None else safety["step_pct"],
            floor_bid=safety["floor_bid_kzt"],
            ceiling_bid=safety["ceiling_bid_kzt"],
            max_step_pct=safety["max_step_pct_24h"],
            max_steps_per_run=safety["max_steps_per_run"],
        )
    except Exception as exc:
        print(f"bid_autopilot_failed:{type(exc).__name__}:{exc}")
        return 2
    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    else:
        print(
            "Kaspi Marketing BID autopilot: "
            f"status={summary.get('status')} gate={summary.get('gate')} run_dir={summary.get('run_dir')}"
        )
    return 0 if summary.get("gate") in {"GREEN", "YELLOW"} else 4


if __name__ == "__main__":
    raise SystemExit(main())
