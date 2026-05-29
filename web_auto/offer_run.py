from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from .offer_flow_registry import OfferFlow, OfferFlowRegistry, select_offer_flow
from .kaspi_merchant_common import resolve_store_credentials
from .kaspi_pricelist_download import run_kaspi_pricelist_download
from .kaspi_pricelist_ops import apply_intent, build_store_snapshot, emit_outputs, verify_uploaded_state
from .kaspi_pricelist_upload import run_kaspi_pricelist_upload

DEFAULT_OFFER_RUN_ROOT = Path("runs/offer_flow_runs")
DISPATCHABLE_FLOW_IDS = {"kaspi-pricelist-sync"}

_INPUT_LABELS = {
    "store": "store",
    "zip_queue_path": "zip_queue_path",
    "handoff_path": "handoff_path",
    "workbook_path": "workbook_path",
    "active_path": "active_path",
    "archive_path": "archive_path",
    "session_doc_path": "session_doc_path",
    "public_urls_path": "public_urls_path",
}


class OfferRunError(ValueError):
    pass


@dataclass
class OfferRunRequest:
    flow_id: str | None = None
    goal: str | None = None
    artifact_state: str | None = None
    store: str | None = None
    confirm: bool = False
    dry_run: bool = False
    verify: bool = False
    include_inactive: bool = False
    zip_queue_path: str | None = None
    handoff_path: str | None = None
    workbook_path: str | None = None
    active_path: str | None = None
    archive_path: str | None = None
    session_doc_path: str | None = None
    public_urls_path: str | None = None
    intent: str = ""
    sku: str = ""
    sku_key: str = ""
    group_url: str = ""
    target_price: str = ""
    offers_book_path: str = "exports/offers_book.xlsx"
    truth_xlsx_path: str = "exports/repricer_unified_truth.xlsx"
    dispatch: bool = False
    headless: bool = False
    headed: bool = False
    timeout_seconds: int = 900
    processing_grace_seconds: int = 900
    note: str = ""
    run_root: str = str(DEFAULT_OFFER_RUN_ROOT)

    def input_values(self) -> dict[str, str]:
        return {
            key: str(value).strip()
            for key, value in {
                "store": self.store,
                "zip_queue_path": self.zip_queue_path,
                "handoff_path": self.handoff_path,
                "workbook_path": self.workbook_path,
                "active_path": self.active_path,
                "archive_path": self.archive_path,
                "session_doc_path": self.session_doc_path,
                "public_urls_path": self.public_urls_path,
            }.items()
            if str(value or "").strip()
        }


@dataclass
class OfferRunPlan:
    flow: OfferFlow
    request: OfferRunRequest
    run_dir: str
    mode: str
    selected_by: str
    required_inputs: dict[str, str]
    warnings: list[str] = field(default_factory=list)
    next_commands: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "flow": self.flow.to_dict(),
            "request": asdict(self.request),
            "run_dir": self.run_dir,
            "mode": self.mode,
            "selected_by": self.selected_by,
            "required_inputs": dict(self.required_inputs),
            "warnings": list(self.warnings),
            "next_commands": list(self.next_commands),
        }


def _json_dump(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


def _resolve_mode(flow: OfferFlow, request: OfferRunRequest) -> tuple[str, list[str]]:
    warnings: list[str] = []
    if request.confirm and request.dry_run:
        raise OfferRunError("offer-run cannot use --confirm and --dry-run together")
    if request.confirm:
        if flow.verify_required and not request.verify:
            raise OfferRunError(f"{flow.flow_id} requires --verify when --confirm is used")
        return "confirm", warnings
    if request.dry_run:
        if not flow.dry_run_supported:
            raise OfferRunError(f"{flow.flow_id} does not support --dry-run")
        return "dry_run", warnings
    if flow.dry_run_supported:
        warnings.append("defaulted_to_dry_run")
        return "dry_run", warnings
    warnings.append("confirm_not_given_preflight_only")
    return "preflight_only", warnings


def _resolve_flow(registry: OfferFlowRegistry, request: OfferRunRequest) -> tuple[OfferFlow, str, list[str]]:
    if request.flow_id:
        flow = registry.get_flow(request.flow_id)
        if not request.include_inactive and not flow.active:
            raise OfferRunError(f"{flow.flow_id} is inactive; re-run with --allow-inactive to use it")
        if request.store and not flow.store_matches(request.store):
            raise OfferRunError(f"{flow.flow_id} does not support store {request.store}")
        return flow, "explicit_flow_id", []

    if not request.goal or not request.artifact_state:
        raise OfferRunError("offer-run requires --flow or the pair --goal and --artifact-state")

    selection = select_offer_flow(
        registry,
        goal=request.goal,
        artifact_state=request.artifact_state,
        store=request.store,
        include_inactive=request.include_inactive,
    )
    if selection is None:
        raise OfferRunError(
            f"No offer flow matched goal={request.goal} artifact_state={request.artifact_state}"
            + (f" store={request.store}" if request.store else "")
        )
    return selection.flow, "registry_selection", list(selection.reasons)


def _required_inputs_or_error(flow: OfferFlow, request: OfferRunRequest) -> dict[str, str]:
    values = request.input_values()
    missing = [key for key in flow.required_inputs if not values.get(key)]
    if missing:
        labels = ", ".join(_INPUT_LABELS.get(key, key) for key in missing)
        raise OfferRunError(f"{flow.flow_id} is missing required inputs: {labels}")
    return {key: values[key] for key in flow.required_inputs}


def _build_run_dir(flow: OfferFlow, run_root: str | Path) -> Path:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_flow = flow.flow_id.replace("_", "-")
    return Path(run_root) / f"{stamp}_{safe_flow}"


def build_offer_run_plan(registry: OfferFlowRegistry, request: OfferRunRequest) -> OfferRunPlan:
    flow, selected_by, selection_reasons = _resolve_flow(registry, request)
    required_inputs = _required_inputs_or_error(flow, request)
    mode, mode_warnings = _resolve_mode(flow, request)
    warnings = selection_reasons + mode_warnings
    next_commands = list(flow.command_examples)
    run_dir = _build_run_dir(flow, request.run_root)
    return OfferRunPlan(
        flow=flow,
        request=request,
        run_dir=str(run_dir),
        mode=mode,
        selected_by=selected_by,
        required_inputs=required_inputs,
        warnings=warnings,
        next_commands=next_commands,
    )


def render_offer_run_markdown(plan: OfferRunPlan) -> str:
    lines = [
        f"# Offer Run - {plan.flow.title}",
        "",
        f"- `flow_id`: `{plan.flow.flow_id}`",
        f"- `mode`: `{plan.mode}`",
        f"- `selected_by`: `{plan.selected_by}`",
        f"- `run_dir`: `{plan.run_dir}`",
        f"- `store_scope`: `{plan.request.store or 'n/a'}`",
        "",
        plan.flow.summary,
        "",
    ]

    if plan.required_inputs:
        lines.append("## Required Inputs")
        lines.append("")
        for key, value in plan.required_inputs.items():
            lines.append(f"- `{key}`: `{value}`")
        lines.append("")

    if plan.warnings:
        lines.append("## Warnings")
        lines.append("")
        for item in plan.warnings:
            lines.append(f"- {item}")
        lines.append("")

    if plan.flow.preflight_checks:
        lines.append("## Preflight Checks")
        lines.append("")
        for item in plan.flow.preflight_checks:
            lines.append(f"- {item}")
        lines.append("")

    if plan.next_commands:
        lines.append("## Next Commands")
        lines.append("")
        for item in plan.next_commands:
            lines.append(f"- `{item}`")
        lines.append("")

    if plan.flow.success_checks:
        lines.append("## Success Checks")
        lines.append("")
        for item in plan.flow.success_checks:
            lines.append(f"- {item}")
        lines.append("")

    if plan.flow.stoplines:
        lines.append("## Stoplines")
        lines.append("")
        for item in plan.flow.stoplines:
            lines.append(f"- {item}")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def write_offer_run_bundle(plan: OfferRunPlan) -> dict[str, Any]:
    run_dir = Path(plan.run_dir)
    run_dir.mkdir(parents=True, exist_ok=False)
    _json_dump(run_dir / "request.json", asdict(plan.request))
    _json_dump(run_dir / "resolved_flow.json", plan.flow.to_dict())
    _json_dump(run_dir / "plan.json", plan.to_dict())
    (run_dir / "execution_checklist.md").write_text(render_offer_run_markdown(plan), encoding="utf-8")
    summary = {
        "status": "planned",
        "flow_id": plan.flow.flow_id,
        "mode": plan.mode,
        "selected_by": plan.selected_by,
        "run_dir": str(run_dir),
        "required_inputs": dict(plan.required_inputs),
        "warnings": list(plan.warnings),
        "dry_run_supported": plan.flow.dry_run_supported,
        "confirm_required": plan.flow.confirm_required,
        "verify_required": plan.flow.verify_required,
        "dispatch_requested": plan.request.dispatch,
        "dispatch_supported": plan.flow.flow_id in DISPATCHABLE_FLOW_IDS,
    }
    _json_dump(run_dir / "summary.json", summary)
    return summary


def _load_default_dispatch_dependencies() -> dict[str, Any]:
    return {
        "resolve_store_credentials": resolve_store_credentials,
        "run_kaspi_pricelist_download": run_kaspi_pricelist_download,
        "build_store_snapshot": build_store_snapshot,
        "apply_intent": apply_intent,
        "emit_outputs": emit_outputs,
        "run_kaspi_pricelist_upload": run_kaspi_pricelist_upload,
        "verify_uploaded_state": verify_uploaded_state,
    }


def _update_offer_run_summary(run_dir: Path, extra_payload: dict[str, Any]) -> dict[str, Any]:
    summary_path = run_dir / "summary.json"
    current = json.loads(summary_path.read_text(encoding="utf-8"))
    current.update(extra_payload)
    _json_dump(summary_path, current)
    return current


def execute_offer_run_plan(
    plan: OfferRunPlan,
    *,
    env_file: str | None = None,
    dependencies: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if plan.flow.flow_id not in DISPATCHABLE_FLOW_IDS:
        raise OfferRunError(f"{plan.flow.flow_id} is not dispatchable yet")
    if plan.flow.flow_id != "kaspi-pricelist-sync":
        raise OfferRunError(f"Dispatch handler not implemented for flow {plan.flow.flow_id}")
    if not str(plan.request.intent or "").strip():
        raise OfferRunError("kaspi-pricelist-sync dispatch requires --intent")

    deps = _load_default_dispatch_dependencies()
    if dependencies:
        deps.update(dependencies)

    run_dir = Path(plan.run_dir)
    dispatch_request = {
        "env_file": str(env_file or ""),
        "headless": bool(plan.request.headless or not plan.request.headed),
        "intent": plan.request.intent,
        "sku": plan.request.sku,
        "sku_key": plan.request.sku_key,
        "group_url": plan.request.group_url,
        "target_price": plan.request.target_price,
        "offers_book_path": plan.request.offers_book_path,
        "truth_xlsx_path": plan.request.truth_xlsx_path,
        "timeout_seconds": int(plan.request.timeout_seconds),
        "processing_grace_seconds": int(plan.request.processing_grace_seconds),
    }
    _json_dump(run_dir / "dispatch_request.json", dispatch_request)

    env_path = Path(env_file) if env_file else Path("~/Docs/Autonomous_business/.env")
    store_name = str(plan.request.store or "").strip()
    if not store_name:
        raise OfferRunError("kaspi-pricelist-sync dispatch requires --store")

    creds = deps["resolve_store_credentials"](store_name, env_path)
    effective_run_dir = run_dir / "native_flow"
    effective_run_dir.mkdir(parents=True, exist_ok=True)
    headless = bool(plan.request.headless or not plan.request.headed)

    if plan.request.active_path and plan.request.archive_path:
        active_path = Path(plan.request.active_path)
        archive_path = Path(plan.request.archive_path)
        download_summary: dict[str, Any] = {"status": "skipped", "reason": "using_explicit_source_paths"}
    else:
        download_summary = deps["run_kaspi_pricelist_download"](
            store_name=creds["store_name"],
            email=creds["email"],
            password=creds["password"],
            run_dir=effective_run_dir / "download",
            headless=headless,
        )
        if download_summary.get("status") != "success":
            result = {
                "status": "failed",
                "flow_id": plan.flow.flow_id,
                "mode": plan.mode,
                "run_dir": str(run_dir),
                "download": download_summary,
            }
            _json_dump(run_dir / "dispatch_result.json", result)
            _update_offer_run_summary(run_dir, {"status": "failed", "dispatch_status": "failed", "dispatch_result": result})
            return result
        downloads_by_state = {row["sale_state"]: Path(row["saved_path"]) for row in download_summary.get("downloads", [])}
        active_path = downloads_by_state["ACTIVE"]
        archive_path = downloads_by_state["ARCHIVE"]

    snapshot = deps["build_store_snapshot"](
        active_path=active_path,
        archive_path=archive_path,
        store_name=creds["store_name"],
        offers_book_path=Path(plan.request.offers_book_path),
        truth_xlsx_path=Path(plan.request.truth_xlsx_path),
    )
    mutated_df = deps["apply_intent"](
        snapshot.snapshot_df,
        intent=plan.request.intent,
        sku=plan.request.sku,
        sku_key=plan.request.sku_key,
        group_url=plan.request.group_url,
        target_price=plan.request.target_price,
    )
    build_summary = deps["emit_outputs"](snapshot, mutated_df=mutated_df, output_dir=effective_run_dir / "outputs")

    result: dict[str, Any] = {
        "status": "success",
        "flow_id": plan.flow.flow_id,
        "mode": plan.mode,
        "run_dir": str(run_dir),
        "store_name": creds["store_name"],
        "download": download_summary,
        "build": build_summary,
    }
    if build_summary.get("status", "ready") != "ready":
        result["status"] = "blocked"
        result["block_reason"] = "build_summary_blocked"

    if plan.mode == "confirm" and result["status"] == "success":
        result["status"] = "blocked"
        result["block_reason"] = "legacy_archive_active_pricelist_upload_disabled_use_safe_active_patch"
        result["upload"] = {
            "status": "skipped",
            "reason": "offer-run legacy dispatch would upload ARCHIVE plus ACTIVE; use kaspi-pricelist safe-active-patch for guarded live changes",
        }
        _json_dump(run_dir / "dispatch_result.json", result)
        _update_offer_run_summary(run_dir, {"status": "blocked", "dispatch_status": "blocked", "dispatch_result": result})
        return result

    if plan.mode == "confirm":
        result["upload"] = {"status": "skipped", "reason": "build_summary_blocked"}

    _json_dump(run_dir / "dispatch_result.json", result)
    dispatch_status = "executed"
    if result["status"] == "blocked":
        dispatch_status = "blocked"
    elif result["status"] == "failed":
        dispatch_status = "failed"
    _update_offer_run_summary(
        run_dir,
        {
            "status": result["status"],
            "dispatch_status": dispatch_status,
            "dispatch_result": result,
        },
    )
    return result
