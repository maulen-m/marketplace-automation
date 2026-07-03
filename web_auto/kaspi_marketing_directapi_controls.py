from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Mapping

import yaml


SCHEMA_VERSION = "web_auto.kaspi_marketing_directapi_controls.v1"
TOOLING_STAGE = "campaign_suspend_live_gated_v1"
DEFAULT_RUN_ROOT = Path("runs/kaspi_marketing_directapi_controls")
ENV_CONFIRM_GATE = "WEB_AUTO_KASPI_MARKETING_DIRECTAPI_LIVE_APPLY"
ENV_CONFIRM_VALUE = "I_UNDERSTAND_THIS_IS_A_LIVE_KASPI_MARKETING_WRITE"
MAX_BID_KZT = 5000
MAX_DAILY_BUDGET_KZT = 1_000_000
FUTURE_LIVE_APPROVAL_PHRASE_PREFIX = "OWNER APPROVES KASPI MARKETING EXACT LIVE APPLY "
FAILED_CAMPAIGN_RESUME_ENDPOINT = "/advertising/products/api/v1.0/campaign/resume"
MARKETING_API_ORIGIN = "https://marketing.kaspi.kz"
PAUSED_CAMPAIGN_STATES = {"paused", "suspended", "suspendedbyyou", "disabled"}
FORBIDDEN_SURFACES = [
    "campaign_creation",
    "price_change",
    "stock_change",
    "repricer_write",
    "meta_write",
    "crm_write",
    "scheduler_write",
    "autonomous_business_write",
]
LiveApplyRunner = Callable[..., dict[str, Any]]


class DirectAPIControlPlanError(ValueError):
    def __init__(self, issues: list[str]) -> None:
        self.issues = issues
        super().__init__("; ".join(issues))


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _read_plan_file(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise DirectAPIControlPlanError([f"plan_file_not_found:{path}"])
    text = path.read_text(encoding="utf-8")
    try:
        if path.suffix.lower() == ".json":
            data = json.loads(text)
        else:
            data = yaml.safe_load(text)
    except Exception as exc:
        raise DirectAPIControlPlanError([f"plan_file_parse_failed:{type(exc).__name__}:{exc}"]) from exc
    if not isinstance(data, dict):
        raise DirectAPIControlPlanError(["plan_root_must_be_mapping"])
    return data


def _required_text(source: Mapping[str, Any], key: str, issues: list[str], prefix: str) -> str:
    value = source.get(key)
    text = str(value).strip() if value is not None else ""
    if not text:
        issues.append(f"{prefix}missing_{key}")
    return text


def _optional_text(source: Mapping[str, Any], key: str) -> str:
    value = source.get(key)
    return str(value).strip() if value is not None else ""


def _as_whole_int(value: Any, *, field: str, issues: list[str], prefix: str) -> int | None:
    if value is None:
        issues.append(f"{prefix}missing_{field}")
        return None
    if isinstance(value, bool):
        issues.append(f"{prefix}{field}_must_be_integer")
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if value.is_integer():
            return int(value)
        issues.append(f"{prefix}{field}_must_be_whole_integer")
        return None
    text = str(value).strip()
    if not text:
        issues.append(f"{prefix}missing_{field}")
        return None
    try:
        number = float(text.replace("\u00a0", "").replace(" ", "").replace(",", "."))
    except Exception:
        issues.append(f"{prefix}{field}_must_be_integer")
        return None
    if not number.is_integer():
        issues.append(f"{prefix}{field}_must_be_whole_integer")
        return None
    return int(number)


def _validate_positive_bounded(
    value: int | None,
    *,
    field: str,
    maximum: int,
    issues: list[str],
    prefix: str,
) -> None:
    if value is None:
        return
    if value <= 0:
        issues.append(f"{prefix}{field}_must_be_positive")
    if value > maximum:
        issues.append(f"{prefix}{field}_implausibly_high:{value}>{maximum}")


def _product_bid_action(
    *,
    root: Mapping[str, Any],
    action: Mapping[str, Any],
    prefix: str,
    issues: list[str],
) -> dict[str, Any] | None:
    campaign_id = _required_text(action, "campaign_id", issues, prefix)
    sku = _required_text(action, "sku", issues, prefix)
    expected_bid = _as_whole_int(action.get("expected_current_bid"), field="expected_current_bid", issues=issues, prefix=prefix)
    new_bid = _as_whole_int(action.get("new_bid"), field="new_bid", issues=issues, prefix=prefix)
    _validate_positive_bounded(expected_bid, field="expected_current_bid", maximum=MAX_BID_KZT, issues=issues, prefix=prefix)
    _validate_positive_bounded(new_bid, field="new_bid", maximum=MAX_BID_KZT, issues=issues, prefix=prefix)
    if not campaign_id or not sku or expected_bid is None or new_bid is None:
        return None

    merchant_id = str(root["merchant_id"]).strip()
    return {
        "operation": "product_bid",
        "method": "PUT",
        "endpoint_path": (
            f"/advertising/products/api/v1/merchant/{merchant_id}"
            f"/campaign/{campaign_id}/products/update-bid"
        ),
        "content_type": "application/json",
        "payload_redacted": {"skuList": [sku], "bid": new_bid},
        "preflight_gates": {
            "campaign_id": campaign_id,
            "sku": sku,
            "expected_current_bid": expected_bid,
        },
        "target_values": {"new_bid": new_bid},
    }


def _campaign_budget_action(
    *,
    root: Mapping[str, Any],
    action: Mapping[str, Any],
    prefix: str,
    issues: list[str],
) -> dict[str, Any] | None:
    campaign_id = _required_text(action, "campaign_id", issues, prefix)
    expected_budget = _as_whole_int(
        action.get("expected_current_budget"),
        field="expected_current_budget",
        issues=issues,
        prefix=prefix,
    )
    new_budget = _as_whole_int(action.get("new_daily_budget"), field="new_daily_budget", issues=issues, prefix=prefix)
    _validate_positive_bounded(
        expected_budget,
        field="expected_current_budget",
        maximum=MAX_DAILY_BUDGET_KZT,
        issues=issues,
        prefix=prefix,
    )
    _validate_positive_bounded(
        new_budget,
        field="new_daily_budget",
        maximum=MAX_DAILY_BUDGET_KZT,
        issues=issues,
        prefix=prefix,
    )
    if not campaign_id or expected_budget is None or new_budget is None:
        return None

    merchant_id = str(root["merchant_id"]).strip()
    return {
        "operation": "campaign_budget",
        "method": "PUT",
        "endpoint_path": f"/advertising/products/api/v3/merchant/{merchant_id}/Campaign/update",
        "content_type": "multipart/form-data",
        "payload_redacted": {
            "Id": campaign_id,
            "DailyBudget": new_budget,
            "Name": "<from_preflight_campaign_core>",
            "State": "<from_preflight_campaign_core>",
            "StartDate": "<from_preflight_campaign_core>",
            "DefaultBid": "<from_preflight_campaign_core>",
            "targetLocationIds[]": "<from_preflight_campaign_core>",
            "EndDate": "<from_preflight_campaign_core_if_present>",
        },
        "preflight_gates": {
            "campaign_id": campaign_id,
            "expected_current_budget": expected_budget,
            "required_core_fields": [
                "name",
                "state",
                "dailyBudget",
                "startDate",
                "defaultBid",
                "targetLocationIds",
            ],
        },
        "target_values": {"new_daily_budget": new_budget},
    }


def _status_to_archived_flag(value: str, issues: list[str], prefix: str) -> bool | None:
    normalized = str(value or "").strip().lower()
    if normalized in {"active", "enabled", "enable", "false", "0", "no", "not_archived"}:
        return False
    if normalized in {"paused", "archive", "archived", "disabled", "disable", "true", "1", "yes"}:
        return True
    issues.append(f"{prefix}target_product_status_unsupported:{value}")
    return None


def _reject_forbidden_endpoint_override(action: Mapping[str, Any], issues: list[str], prefix: str) -> None:
    endpoint = str(action.get("endpoint_path") or action.get("endpoint") or "").strip()
    if not endpoint:
        return
    if endpoint.endswith(FAILED_CAMPAIGN_RESUME_ENDPOINT) or endpoint == FAILED_CAMPAIGN_RESUME_ENDPOINT:
        issues.append(f"{prefix}forbidden_campaign_resume_endpoint_missing_merchant:{endpoint}")


def _product_status_action(
    *,
    root: Mapping[str, Any],
    action: Mapping[str, Any],
    prefix: str,
    issues: list[str],
) -> dict[str, Any] | None:
    campaign_id = _required_text(action, "campaign_id", issues, prefix)
    sku = _required_text(action, "sku", issues, prefix)
    expected_status = _required_text(action, "expected_current_product_status", issues, prefix)
    target_status = _required_text(action, "target_product_status", issues, prefix)
    archived = _status_to_archived_flag(target_status, issues, prefix) if target_status else None
    if not campaign_id or not sku or not expected_status or archived is None:
        return None

    merchant_id = str(root["merchant_id"]).strip()
    return {
        "operation": "product_status",
        "method": "PUT",
        "endpoint_path": (
            f"/advertising/products/api/v1/merchant/{merchant_id}"
            f"/campaign/{campaign_id}/products/update-status"
        ),
        "content_type": "application/json",
        "payload_redacted": {"skuList": [sku], "archived": archived},
        "preflight_gates": {
            "campaign_id": campaign_id,
            "sku": sku,
            "expected_current_product_status": expected_status,
        },
        "target_values": {"target_product_status": target_status, "archived": archived},
    }


def _campaign_resume_action(
    *,
    root: Mapping[str, Any],
    action: Mapping[str, Any],
    prefix: str,
    issues: list[str],
) -> dict[str, Any] | None:
    campaign_id = _required_text(action, "campaign_id", issues, prefix)
    expected_state = _required_text(action, "expected_current_campaign_state", issues, prefix)
    target_state = str(action.get("target_campaign_state") or "Enabled").strip()
    if not campaign_id or not expected_state:
        return None

    merchant_id = str(root["merchant_id"]).strip()
    return {
        "operation": "campaign_resume",
        "method": "PUT",
        "endpoint_path": f"/advertising/products/api/v1.0/merchant/{merchant_id}/campaign/resume",
        "content_type": "application/json",
        "payload_redacted": {"campaignIds": [campaign_id]},
        "preflight_gates": {
            "campaign_id": campaign_id,
            "expected_current_campaign_state": expected_state,
        },
        "target_values": {"target_campaign_state": target_state},
    }


def _campaign_suspend_action(
    *,
    root: Mapping[str, Any],
    action: Mapping[str, Any],
    prefix: str,
    issues: list[str],
) -> dict[str, Any] | None:
    campaign_id = _required_text(action, "campaign_id", issues, prefix)
    expected_state = _required_text(action, "expected_current_campaign_state", issues, prefix)
    target_state = str(action.get("target_campaign_state") or "Paused").strip()
    if target_state not in {"Paused", "Suspended", "SuspendedByYou"}:
        issues.append(f"{prefix}target_campaign_state_unsupported_for_suspend:{target_state}")
    if not campaign_id or not expected_state or issues:
        return None

    merchant_id = str(root["merchant_id"]).strip()
    return {
        "operation": "campaign_suspend",
        "method": "PUT",
        "endpoint_path": f"/advertising/products/api/v1/merchant/{merchant_id}/Campaign/suspend",
        "content_type": "multipart/form-data",
        "payload_redacted": {"campaignIds": [campaign_id]},
        "preflight_gates": {
            "campaign_id": campaign_id,
            "expected_current_campaign_state": expected_state,
            **(
                {"expected_product_sku": _optional_text(action, "expected_product_sku")}
                if _optional_text(action, "expected_product_sku")
                else {}
            ),
            **(
                {"expected_merchant_sku": _optional_text(action, "expected_merchant_sku")}
                if _optional_text(action, "expected_merchant_sku")
                else {}
            ),
        },
        "target_values": {"target_campaign_state": target_state},
    }


def _resume_recovery_action(
    *,
    root: Mapping[str, Any],
    action: Mapping[str, Any],
    prefix: str,
    issues: list[str],
) -> dict[str, Any] | None:
    campaign_id = _required_text(action, "campaign_id", issues, prefix)
    partial_state = action.get("exact_partial_state")
    if not isinstance(partial_state, dict):
        issues.append(f"{prefix}missing_exact_partial_state")
        partial_state = {}
    required_partial_fields = [
        "campaign_state",
        "daily_budget_kzt",
        "product_status",
        "bid_kzt",
        "price_kzt",
    ]
    partial_gates: dict[str, Any] = {}
    for key in required_partial_fields:
        value = partial_state.get(key)
        text = str(value).strip() if value is not None else ""
        if not text:
            issues.append(f"{prefix}exact_partial_state_missing_{key}")
        else:
            partial_gates[key] = value
    red_artifacts = action.get("red_artifacts")
    if not isinstance(red_artifacts, (dict, list)) or not red_artifacts:
        issues.append(f"{prefix}missing_preserved_red_artifacts")
    if not campaign_id or issues:
        return None

    merchant_id = str(root["merchant_id"]).strip()
    return {
        "operation": "resume_recovery",
        "method": "PUT",
        "endpoint_path": f"/advertising/products/api/v1.0/merchant/{merchant_id}/campaign/resume",
        "content_type": "application/json",
        "payload_redacted": {"campaignIds": [campaign_id]},
        "preflight_gates": {
            "campaign_id": campaign_id,
            "exact_partial_state": partial_gates,
            "preserved_red_artifacts": red_artifacts,
        },
        "target_values": {"target_campaign_state": "Enabled"},
        "recovery_only": True,
    }


def load_and_resolve_plan(plan_file: Path) -> dict[str, Any]:
    raw = _read_plan_file(plan_file)
    issues: list[str] = []
    if raw.get("schema_version") != SCHEMA_VERSION:
        issues.append(f"schema_version_mismatch:{raw.get('schema_version')!r}")
    for key in ("store", "merchant_id", "store_code", "target_date"):
        _required_text(raw, key, issues, "")
    actions = raw.get("actions")
    if not isinstance(actions, list) or not actions:
        issues.append("actions_must_be_non_empty_list")
        actions = []
    if sum(1 for item in actions if isinstance(item, dict) and item.get("operation") == "resume_recovery") > 0:
        if len(actions) != 1:
            issues.append("resume_recovery_must_be_the_only_action")

    seen_ids: set[str] = set()
    resolved_actions: list[dict[str, Any]] = []
    if not issues:
        for index, item in enumerate(actions):
            prefix = f"actions[{index}]."
            if not isinstance(item, dict):
                issues.append(f"{prefix}must_be_mapping")
                continue
            _reject_forbidden_endpoint_override(item, issues, prefix)
            action_id = _required_text(item, "action_id", issues, prefix)
            if action_id:
                if action_id in seen_ids:
                    issues.append(f"{prefix}duplicate_action_id:{action_id}")
                seen_ids.add(action_id)
            operation = _required_text(item, "operation", issues, prefix)
            resolved_control: dict[str, Any] | None
            if operation == "product_bid":
                resolved_control = _product_bid_action(root=raw, action=item, prefix=prefix, issues=issues)
            elif operation == "campaign_budget":
                resolved_control = _campaign_budget_action(root=raw, action=item, prefix=prefix, issues=issues)
            elif operation == "product_status":
                resolved_control = _product_status_action(root=raw, action=item, prefix=prefix, issues=issues)
            elif operation == "campaign_resume":
                resolved_control = _campaign_resume_action(root=raw, action=item, prefix=prefix, issues=issues)
            elif operation == "campaign_suspend":
                resolved_control = _campaign_suspend_action(root=raw, action=item, prefix=prefix, issues=issues)
            elif operation == "resume_recovery":
                resolved_control = _resume_recovery_action(root=raw, action=item, prefix=prefix, issues=issues)
            elif operation:
                issues.append(f"{prefix}unknown_operation:{operation}")
                resolved_control = None
            else:
                resolved_control = None
            if resolved_control and action_id:
                resolved_actions.append(
                    {
                        "action_id": action_id,
                        "operation": operation,
                        "campaign_id": str(item.get("campaign_id")).strip(),
                        "control": resolved_control,
                    }
                )

    if issues:
        raise DirectAPIControlPlanError(issues)

    owner_approval_text = str(raw.get("owner_approval_text") or "").strip()
    owner_approval_file = str(raw.get("owner_approval_file") or "").strip()
    return {
        "schema_version": SCHEMA_VERSION,
        "source_plan_file": str(plan_file),
        "store": str(raw["store"]).strip(),
        "merchant_id": str(raw["merchant_id"]).strip(),
        "store_code": str(raw["store_code"]).strip(),
        "target_date": str(raw["target_date"]).strip(),
        "action_count": len(resolved_actions),
        "actions": resolved_actions,
        "operation_endpoint_set": sorted({action["control"]["endpoint_path"] for action in resolved_actions}),
        "owner_approval_present": bool(owner_approval_text or owner_approval_file),
        "owner_approval_source": "field" if owner_approval_text else ("file" if owner_approval_file else ""),
        "live_write_authorized": False,
        "forbidden_surfaces": list(FORBIDDEN_SURFACES),
        "safety": {
            "dry_run_first": True,
            "live_writes_implemented": True,
            "live_apply_operations": ["campaign_suspend"],
            "secrets_or_tokens_required_for_dry_run": False,
            "max_bid_kzt": MAX_BID_KZT,
            "max_daily_budget_kzt": MAX_DAILY_BUDGET_KZT,
            "failed_resume_endpoint_blocked": FAILED_CAMPAIGN_RESUME_ENDPOINT,
            "future_live_approval_phrase_prefix": FUTURE_LIVE_APPROVAL_PHRASE_PREFIX,
        },
    }


def _timestamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def _pick(data: Any, *paths: str) -> Any:
    for path in paths:
        current = data
        ok = True
        for part in path.split("."):
            if isinstance(current, dict) and part in current:
                current = current[part]
            else:
                ok = False
                break
        if ok and current not in (None, ""):
            return current
    return None


def _payload_rows(data: Any) -> list[dict[str, Any]]:
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    if isinstance(data, dict):
        for key in ("data", "items", "products", "rows", "campaigns"):
            value = data.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
    return []


def _normalize_state(value: Any) -> str:
    return "".join(ch for ch in str(value or "").strip().lower() if ch.isalnum())


def _campaign_state(payload: Any) -> str:
    value = _pick(payload, "state", "status", "data.state", "data.status", "campaign.state", "campaign.status")
    return str(value or "").strip()


def _campaign_identity(payload: Any, fallback: str) -> str:
    value = _pick(payload, "id", "campaignId", "campaign_id", "data.id", "data.campaignId", "data.campaign_id")
    return str(value or fallback).strip()


def _campaign_list_identity_count(payload: Any, campaign_id: str) -> int:
    count = 0
    for row in _payload_rows(payload):
        row_id = str(
            _pick(row, "id", "campaignId", "campaign_id", "campaign.id", "campaign.campaignId") or ""
        ).strip()
        if row_id == campaign_id:
            count += 1
    return count


def _product_rows_matching(payload: Any, *, expected_product_sku: str, expected_merchant_sku: str) -> list[dict[str, Any]]:
    matches: list[dict[str, Any]] = []
    for row in _payload_rows(payload):
        sku = str(_pick(row, "sku", "skuKey", "id", "productId", "jsonSku") or "").strip()
        merchant_sku = str(_pick(row, "merchantSku", "merchantSKU", "offerId", "jsonMerchantSku") or "").strip()
        if expected_product_sku and sku != expected_product_sku:
            continue
        if expected_merchant_sku and merchant_sku != expected_merchant_sku:
            continue
        matches.append(row)
    return matches


def _state_is_paused(value: str) -> bool:
    return _normalize_state(value) in PAUSED_CAMPAIGN_STATES


def _write_live_payloads(
    *,
    run_dir: Path,
    prefix: str,
    campaign_id: str,
    campaign_list: Any,
    core: Any,
    products: Any,
) -> dict[str, str]:
    paths = {
        "campaign_list": str(run_dir / f"{prefix}_{campaign_id}_campaign_list.json"),
        "core": str(run_dir / f"{prefix}_{campaign_id}_campaign_core.json"),
        "products": str(run_dir / f"{prefix}_{campaign_id}_campaign_products.json"),
    }
    _write_json(Path(paths["campaign_list"]), campaign_list)
    _write_json(Path(paths["core"]), core)
    _write_json(Path(paths["products"]), products)
    return paths


def _default_campaign_suspend_live_runner(
    *,
    resolved: Mapping[str, Any],
    run_dir: Path,
    env_file: Path | None,
    headless: bool,
) -> dict[str, Any]:
    from playwright.sync_api import sync_playwright

    from .kaspi_marketing import (
        CAMPAIGN_CORE_URL,
        CAMPAIGN_PRODUCTS_URL,
        MARKETING_CAMPAIGNS_URL,
        build_marketing_headers,
        login_kaspi_marketing,
        resolve_marketing_credentials,
        _request_json,
    )
    from .kaspi_merchant_common import merchant_browser_launch_kwargs

    store = str(resolved["store"])
    merchant_id = str(resolved["merchant_id"])
    store_code = str(resolved["store_code"])
    target_date = str(resolved["target_date"])
    actions = list(resolved.get("actions") or [])
    creds = resolve_marketing_credentials(store, env_file=env_file, merchant_id=merchant_id, store_code=store_code)
    if creds.merchant_id != merchant_id:
        raise RuntimeError(f"merchant_id_mismatch:{creds.merchant_id}!={merchant_id}")
    if creds.store_code != store_code:
        raise RuntimeError(f"store_code_mismatch:{creds.store_code}!={store_code}")

    preflight_errors: list[str] = []
    postverify_errors: list[str] = []
    action_results: list[dict[str, Any]] = []
    artifact_paths: list[str] = []
    live_writes_executed = False
    campaign_list_url = (
        f"{MARKETING_API_ORIGIN}/advertising/products/api/v5/merchant/{merchant_id}"
        f"/Campaigns?StartDate={target_date}&EndDate={target_date}"
    )

    with sync_playwright() as p:
        browser = p.chromium.launch(**merchant_browser_launch_kwargs(headless=headless))
        try:
            context = browser.new_context(viewport={"width": 1440, "height": 1400})
            page = context.new_page()
            login_kaspi_marketing(page, login_value=creds.login, password_value=creds.password)
            headers = build_marketing_headers(context.cookies(MARKETING_API_ORIGIN), MARKETING_CAMPAIGNS_URL)

            preflight: dict[str, dict[str, Any]] = {}
            for action in actions:
                campaign_id = str(action["campaign_id"])
                gates = action["control"]["preflight_gates"]
                campaign_list = _request_json(context, url=campaign_list_url, headers=headers)
                core = _request_json(
                    context,
                    url=CAMPAIGN_CORE_URL.format(merchant_id=merchant_id, campaign_id=campaign_id),
                    headers=headers,
                )
                products = _request_json(
                    context,
                    url=CAMPAIGN_PRODUCTS_URL.format(
                        merchant_id=merchant_id,
                        campaign_id=campaign_id,
                        date=target_date,
                    ),
                    headers=headers,
                )
                artifact_paths.extend(
                    _write_live_payloads(
                        run_dir=run_dir,
                        prefix="pre",
                        campaign_id=campaign_id,
                        campaign_list=campaign_list,
                        core=core,
                        products=products,
                    ).values()
                )
                list_count = _campaign_list_identity_count(campaign_list, campaign_id)
                core_id = _campaign_identity(core, campaign_id)
                state = _campaign_state(core)
                expected_state = str(gates["expected_current_campaign_state"])
                already_target = _state_is_paused(state)
                expected_product_sku = str(gates.get("expected_product_sku") or "")
                expected_merchant_sku = str(gates.get("expected_merchant_sku") or "")
                product_match_count = None
                if expected_product_sku or expected_merchant_sku:
                    product_match_count = len(
                        _product_rows_matching(
                            products,
                            expected_product_sku=expected_product_sku,
                            expected_merchant_sku=expected_merchant_sku,
                        )
                    )
                    if product_match_count != 1:
                        preflight_errors.append(f"{campaign_id}:product_identity_match_count:{product_match_count}")
                if list_count != 1:
                    preflight_errors.append(f"{campaign_id}:campaign_list_identity_count:{list_count}")
                if core_id != campaign_id:
                    preflight_errors.append(f"{campaign_id}:campaign_core_identity:{core_id}")
                if _normalize_state(state) != _normalize_state(expected_state) and not already_target:
                    preflight_errors.append(f"{campaign_id}:campaign_state:{state}!={expected_state}")
                preflight[campaign_id] = {
                    "state": state,
                    "already_target": already_target,
                    "product_match_count": product_match_count,
                }

            if preflight_errors:
                return {
                    "gate": "YELLOW",
                    "status": "blocked_preflight_failed",
                    "preflight_errors": preflight_errors,
                    "postverify_errors": [],
                    "live_writes_executed": False,
                    "live_apply_implemented": True,
                    "action_results": action_results,
                    "artifact_paths": artifact_paths,
                }

            for action in actions:
                campaign_id = str(action["campaign_id"])
                endpoint_url = f"{MARKETING_API_ORIGIN}{action['control']['endpoint_path']}"
                if preflight[campaign_id]["already_target"]:
                    action_results.append(
                        {
                            "action_id": action["action_id"],
                            "campaign_id": campaign_id,
                            "status": "already_target_state_noop",
                            "pre_state": preflight[campaign_id]["state"],
                        }
                    )
                    continue

                xsrf = ""
                for cookie in context.cookies(MARKETING_API_ORIGIN):
                    if cookie.get("name") == "XSRF-TOKEN":
                        xsrf = str(cookie.get("value") or "")
                        break
                response = page.evaluate(
                    """
                    async ({url, campaignId, xsrf}) => {
                      const form = new FormData();
                      form.append("campaignIds[]", campaignId);
                      const headers = {
                        "accept": "application/json, text/plain, */*",
                        "x-requested-with": "XMLHttpRequest"
                      };
                      if (xsrf) headers["x-xsrf-token"] = xsrf;
                      const resp = await fetch(url, {
                        method: "PUT",
                        headers,
                        body: form,
                        credentials: "include"
                      });
                      const text = await resp.text();
                      let bodyJson = null;
                      try { bodyJson = JSON.parse(text); } catch (err) {}
                      return {
                        status: resp.status,
                        ok: resp.ok,
                        body_json: bodyJson,
                        body_text_snippet: text.slice(0, 1000)
                      };
                    }
                    """,
                    {"url": endpoint_url, "campaignId": campaign_id, "xsrf": xsrf},
                )
                artifact_path = run_dir / f"apply_{campaign_id}_suspend_response.json"
                _write_json(artifact_path, response)
                artifact_paths.append(str(artifact_path))
                live_writes_executed = True
                action_results.append(
                    {
                        "action_id": action["action_id"],
                        "campaign_id": campaign_id,
                        "status": "suspend_request_sent",
                        "response_status": response.get("status"),
                        "response_ok": response.get("ok"),
                    }
                )
                if not response.get("ok"):
                    postverify_errors.append(f"{campaign_id}:suspend_response_status:{response.get('status')}")

            page.wait_for_timeout(2000)
            headers = build_marketing_headers(context.cookies(MARKETING_API_ORIGIN), MARKETING_CAMPAIGNS_URL)
            for action in actions:
                campaign_id = str(action["campaign_id"])
                campaign_list = _request_json(context, url=campaign_list_url, headers=headers)
                core = _request_json(
                    context,
                    url=CAMPAIGN_CORE_URL.format(merchant_id=merchant_id, campaign_id=campaign_id),
                    headers=headers,
                )
                products = _request_json(
                    context,
                    url=CAMPAIGN_PRODUCTS_URL.format(
                        merchant_id=merchant_id,
                        campaign_id=campaign_id,
                        date=target_date,
                    ),
                    headers=headers,
                )
                artifact_paths.extend(
                    _write_live_payloads(
                        run_dir=run_dir,
                        prefix="post",
                        campaign_id=campaign_id,
                        campaign_list=campaign_list,
                        core=core,
                        products=products,
                    ).values()
                )
                list_count = _campaign_list_identity_count(campaign_list, campaign_id)
                state = _campaign_state(core)
                if list_count != 1:
                    postverify_errors.append(f"{campaign_id}:post_campaign_list_identity_count:{list_count}")
                if not _state_is_paused(state):
                    postverify_errors.append(f"{campaign_id}:post_campaign_state_not_paused:{state}")
        finally:
            browser.close()

    return {
        "gate": "RED" if postverify_errors else "GREEN",
        "status": "campaign_suspend_live_apply_verified" if not postverify_errors else "postverify_failed",
        "preflight_errors": preflight_errors,
        "postverify_errors": postverify_errors,
        "live_writes_executed": live_writes_executed,
        "live_apply_implemented": True,
        "action_results": action_results,
        "artifact_paths": artifact_paths,
    }


def _render_closeout(*, summary: Mapping[str, Any], resolved: Mapping[str, Any]) -> str:
    gate = str(summary.get("gate") or "YELLOW")
    mode = str(summary.get("mode") or "")
    title = "Kaspi Marketing DirectAPI Control Closeout"
    command = "./web-auto --json kaspi-marketing directapi-control --plan-file <path> --dry-run"
    if mode == "confirm":
        command = "./web-auto --json kaspi-marketing directapi-control --plan-file <path> --confirm"
    lines = [
        f"# {title}",
        "",
        f"Gate: {gate}",
        "",
        f"Generated local: `{summary.get('generated_at_local')}`",
        "",
        "## Command",
        "",
        f"`{command}`",
        "",
        "## Result",
        "",
        f"- status: `{summary.get('status')}`",
        f"- tooling stage: `{summary.get('tooling_stage')}`",
        f"- mode: `{summary.get('mode')}`",
        f"- plan file: `{resolved.get('source_plan_file')}`",
        f"- action count: `{resolved.get('action_count')}`",
        f"- live writes executed: `{summary.get('live_writes_executed')}`",
        f"- live apply implemented: `{summary.get('live_apply_implemented')}`",
        "",
        "## Artifacts",
        "",
        f"- resolved plan: `{summary.get('resolved_plan_path')}`",
        f"- dry-run summary: `{summary.get('dry_run_summary_path')}`",
        f"- closeout: `{summary.get('closeout_path')}`",
        "",
        "## Controls",
        "",
    ]
    for action in resolved.get("actions", []):
        control = action["control"]
        lines.append(
            f"- `{action['action_id']}` `{action['operation']}`: "
            f"{control['method']} {control['endpoint_path']}"
        )
    lines.extend(
        [
            "",
            "## Safety",
            "",
            "- Dry-run resolver-only mode performs no authentication and no external mutation.",
            f"- Future live apply requires `--confirm` plus `{ENV_CONFIRM_GATE}={ENV_CONFIRM_VALUE}`.",
            "- Confirmed live apply is implemented only for exact `campaign_suspend` plans with owner approval present.",
            "- No secrets, cookies, token values, authorization headers, storage state, or PII are written.",
            "",
        ]
    )
    if summary.get("preflight_errors"):
        lines.extend(["## Preflight Errors", ""])
        lines.extend(f"- `{item}`" for item in summary["preflight_errors"])
        lines.append("")
    if summary.get("postverify_errors"):
        lines.extend(["## Post-Verify Errors", ""])
        lines.extend(f"- `{item}`" for item in summary["postverify_errors"])
        lines.append("")
    if summary.get("block_reason"):
        lines.extend(["## Block Reason", "", f"`{summary['block_reason']}`", ""])
    return "\n".join(lines)


def run_directapi_control(
    *,
    plan_file: Path,
    dry_run: bool,
    confirm: bool,
    run_root: Path = DEFAULT_RUN_ROOT,
    timestamp: str | None = None,
    env: Mapping[str, str] | None = None,
    env_file: Path | None = None,
    headless: bool = False,
    live_runner: LiveApplyRunner | None = None,
) -> dict[str, Any]:
    if dry_run == confirm:
        raise DirectAPIControlPlanError(["choose_exactly_one_of_dry_run_or_confirm"])

    resolved = load_and_resolve_plan(plan_file)
    run_id = f"{timestamp or _timestamp()}_directapi_control"
    run_dir = run_root / run_id
    run_dir.mkdir(parents=True, exist_ok=False)

    mode = "dry_run" if dry_run else "confirm"
    gate = "GREEN"
    status = "dry_run_resolver_ready"
    block_reason = ""
    live_writes_executed = False
    live_apply_implemented = True
    live_result: dict[str, Any] = {}

    if confirm:
        gate = "YELLOW"
        env_values = env if env is not None else os.environ
        if env_values.get(ENV_CONFIRM_GATE) != ENV_CONFIRM_VALUE:
            status = "blocked_confirm_env_gate_missing"
            block_reason = f"missing_required_env_gate:{ENV_CONFIRM_GATE}"
        elif not resolved.get("owner_approval_present"):
            status = "blocked_owner_approval_missing"
            block_reason = "missing_owner_approval_phrase_file_or_field"
        elif {action["operation"] for action in resolved["actions"]} != {"campaign_suspend"}:
            status = "blocked_live_apply_supported_only_for_campaign_suspend"
            block_reason = "live_apply_supported_only_for_campaign_suspend"
        else:
            runner = live_runner or _default_campaign_suspend_live_runner
            live_result = runner(
                resolved=resolved,
                run_dir=run_dir,
                env_file=env_file,
                headless=headless,
            )
            gate = str(live_result.get("gate") or "RED")
            status = str(live_result.get("status") or "live_apply_result_missing_status")
            live_writes_executed = bool(live_result.get("live_writes_executed"))
            live_apply_implemented = bool(live_result.get("live_apply_implemented", True))

    generated_at = datetime.now().isoformat(timespec="seconds")
    resolved_payload = {
        **resolved,
        "run_id": run_id,
        "run_dir": str(run_dir),
        "generated_at_local": generated_at,
        "mode": mode,
    }
    resolved_plan_path = run_dir / "CONTROL_PLAN_RESOLVED.json"
    summary_path = run_dir / ("DIRECTAPI_DRY_RUN_SUMMARY.json" if dry_run else "DIRECTAPI_LIVE_APPLY_SUMMARY.json")
    closeout_path = run_dir / "COMMAND_CLOSEOUT.md"

    summary = {
        "schema_version": SCHEMA_VERSION,
        "tooling_stage": TOOLING_STAGE,
        "generated_at_local": generated_at,
        "status": status,
        "gate": gate,
        "mode": mode,
        "run_id": run_id,
        "run_dir": str(run_dir),
        "plan_file": str(plan_file),
        "resolved_plan_path": str(resolved_plan_path),
        "dry_run_summary_path": str(summary_path) if dry_run else "",
        "live_apply_summary_path": str(summary_path) if confirm else "",
        "summary_path": str(summary_path),
        "closeout_path": str(closeout_path),
        "action_count": resolved["action_count"],
        "operation_endpoint_set": resolved["operation_endpoint_set"],
        "owner_approval_present": resolved["owner_approval_present"],
        "live_write_authorized": resolved["live_write_authorized"],
        "live_writes_executed": live_writes_executed,
        "live_apply_implemented": live_apply_implemented,
        "block_reason": block_reason,
        "preflight_errors": list(live_result.get("preflight_errors") or []),
        "postverify_errors": list(live_result.get("postverify_errors") or []),
        "live_action_results": list(live_result.get("action_results") or []),
        "live_artifact_paths": list(live_result.get("artifact_paths") or []),
        "requires_env_gate_for_future_confirm": {
            "name": ENV_CONFIRM_GATE,
            "expected_value": ENV_CONFIRM_VALUE,
        },
        "actions": [
            {
                "action_id": action["action_id"],
                "operation": action["operation"],
                "campaign_id": action["campaign_id"],
                "method": action["control"]["method"],
                "endpoint_path": action["control"]["endpoint_path"],
                "payload_redacted": action["control"]["payload_redacted"],
                "preflight_gates": action["control"]["preflight_gates"],
            }
            for action in resolved["actions"]
        ],
        "secrets_or_tokens_recorded": False,
    }

    _write_json(resolved_plan_path, resolved_payload)
    _write_json(summary_path, summary)
    closeout_path.write_text(_render_closeout(summary=summary, resolved=resolved_payload), encoding="utf-8")
    return summary
