from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import yaml

DEFAULT_OFFER_FLOW_CONFIG = Path("config/offer_flows.yaml")

OFFER_FLOW_GOALS = (
    "new-offer-package-preflight",
    "new-offer-upload",
    "existing-card-onboarding",
    "existing-offer-maintenance",
    "rejected-offer-replay",
    "post-publication-enrichment",
)

OFFER_FLOW_ARTIFACT_STATES = (
    "contract-only",
    "prepared-zip-queue",
    "reviewed-offer-upload-workbook",
    "existing-merchant-rows",
    "pending-trash-rows",
    "published-live-offers",
)

OFFER_FLOW_WRITE_SURFACES = (
    "upstream-readonly",
    "merchant-import-ui",
    "existing-card-ui",
    "merchant-pricelist-ui",
    "merchant-dispute-api",
    "handoff-only",
)

OFFER_FLOW_REPRICER_FOLLOWUPS = (
    "none",
    "verify-import-and-dumping",
    "verify-import-and-redflag",
)

OFFER_FLOW_REQUIRED_INPUT_KEYS = (
    "store",
    "zip_queue_path",
    "handoff_path",
    "workbook_path",
    "active_path",
    "archive_path",
    "session_doc_path",
    "public_urls_path",
)

_STORE_ALIASES = {
    "ALL": "ALL",
    "ANY": "ALL",
    "*": "ALL",
    "UNIVERSAL": "UNIVERSAL",
    "ACMEWEAR": "ACMEWEAR",
    "STORE-B": "STORE-B",
    "STOREB": "STORE-B",
    "STORE_B": "STORE-B",
    "M GROUP": "STORE-B",
    "MELVIS": "MELVIS",
    "11KZ": "11KZ",
    "11_KZ": "11KZ",
}


class OfferFlowRegistryError(ValueError):
    pass


@dataclass
class OfferFlow:
    flow_id: str
    title: str
    active: bool
    goal: str
    artifact_state: str
    owner_surface: str
    stores: list[str]
    write_surface: str
    priority: int
    summary: str
    primary_entrypoint: str
    command_examples: list[str]
    dry_run_supported: bool
    confirm_required: bool
    verify_required: bool
    repricer_followup: str
    required_inputs: list[str]
    preflight_checks: list[str]
    success_checks: list[str]
    stoplines: list[str]
    fallback_flow_ids: list[str] = field(default_factory=list)
    docs_refs: list[str] = field(default_factory=list)
    skill_refs: list[str] = field(default_factory=list)
    artifact_roots: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def store_matches(self, store: str | None) -> bool:
        if store is None:
            return True
        normalized = normalize_offer_flow_store(store)
        return "ALL" in self.stores or normalized in self.stores

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class OfferFlowRegistry:
    registry_version: int
    doc_output_path: str
    flows: list[OfferFlow]

    def get_flow(self, flow_id: str) -> OfferFlow:
        for flow in self.flows:
            if flow.flow_id == flow_id:
                return flow
        raise OfferFlowRegistryError(f"Unknown offer flow id: {flow_id}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "registry_version": self.registry_version,
            "doc_output_path": self.doc_output_path,
            "flows": [flow.to_dict() for flow in self.flows],
        }


@dataclass
class OfferFlowSelection:
    flow: OfferFlow
    score: int
    reasons: list[str]
    candidate_count: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "flow": self.flow.to_dict(),
            "score": self.score,
            "reasons": list(self.reasons),
            "candidate_count": self.candidate_count,
        }


def normalize_offer_flow_store(value: str) -> str:
    normalized = str(value or "").strip().upper().replace("-", "_").replace(" ", "_")
    alias_key = normalized.replace("__", "_")
    if alias_key in {"STORE_B", "STOREB"}:
        return "STORE-B"
    if alias_key in {"11_KZ", "11KZ"}:
        return "11KZ"
    direct = _STORE_ALIASES.get(str(value or "").strip().upper())
    if direct:
        return direct
    alias = _STORE_ALIASES.get(alias_key)
    if alias:
        return alias
    raise OfferFlowRegistryError(f"Unknown store value for offer flow registry: {value}")


def _require_mapping(value: Any, message: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise OfferFlowRegistryError(message)
    return value


def _require_list(value: Any, message: str) -> list[Any]:
    if not isinstance(value, list):
        raise OfferFlowRegistryError(message)
    return value


def _require_nonempty_text(value: Any, message: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise OfferFlowRegistryError(message)
    return text


def _normalize_stores(raw_values: Any, flow_id: str) -> list[str]:
    items = _require_list(raw_values, f"{flow_id}.stores must be a list")
    normalized = [normalize_offer_flow_store(item) for item in items]
    if not normalized:
        raise OfferFlowRegistryError(f"{flow_id}.stores must not be empty")
    if "ALL" in normalized and len(normalized) > 1:
        raise OfferFlowRegistryError(f"{flow_id}.stores must contain only ALL when ALL is present")
    deduped: list[str] = []
    for item in normalized:
        if item not in deduped:
            deduped.append(item)
    return deduped


def _normalize_text_list(raw_values: Any, message: str) -> list[str]:
    items = _require_list(raw_values, message)
    output: list[str] = []
    for item in items:
        text = str(item or "").strip()
        if text:
            output.append(text)
    return output


def _load_flow(raw: dict[str, Any]) -> OfferFlow:
    flow_id = _require_nonempty_text(raw.get("flow_id"), "flow_id is required")
    title = _require_nonempty_text(raw.get("title"), f"{flow_id}.title is required")
    goal = _require_nonempty_text(raw.get("goal"), f"{flow_id}.goal is required")
    artifact_state = _require_nonempty_text(raw.get("artifact_state"), f"{flow_id}.artifact_state is required")
    owner_surface = _require_nonempty_text(raw.get("owner_surface"), f"{flow_id}.owner_surface is required")
    write_surface = _require_nonempty_text(raw.get("write_surface"), f"{flow_id}.write_surface is required")
    summary = _require_nonempty_text(raw.get("summary"), f"{flow_id}.summary is required")
    primary_entrypoint = _require_nonempty_text(raw.get("primary_entrypoint"), f"{flow_id}.primary_entrypoint is required")

    if goal not in OFFER_FLOW_GOALS:
        raise OfferFlowRegistryError(f"{flow_id}.goal must be one of: {', '.join(OFFER_FLOW_GOALS)}")
    if artifact_state not in OFFER_FLOW_ARTIFACT_STATES:
        raise OfferFlowRegistryError(f"{flow_id}.artifact_state must be one of: {', '.join(OFFER_FLOW_ARTIFACT_STATES)}")
    if write_surface not in OFFER_FLOW_WRITE_SURFACES:
        raise OfferFlowRegistryError(f"{flow_id}.write_surface must be one of: {', '.join(OFFER_FLOW_WRITE_SURFACES)}")

    repricer_followup = str(raw.get("repricer_followup", "none")).strip()
    if repricer_followup not in OFFER_FLOW_REPRICER_FOLLOWUPS:
        raise OfferFlowRegistryError(
            f"{flow_id}.repricer_followup must be one of: {', '.join(OFFER_FLOW_REPRICER_FOLLOWUPS)}"
        )

    try:
        priority = int(raw.get("priority", 100))
    except Exception as exc:  # pragma: no cover - defensive branch
        raise OfferFlowRegistryError(f"{flow_id}.priority must be an integer") from exc
    if priority < 0:
        raise OfferFlowRegistryError(f"{flow_id}.priority must be >= 0")

    required_inputs = _normalize_text_list(raw.get("required_inputs", []), f"{flow_id}.required_inputs must be a list")
    for key in required_inputs:
        if key not in OFFER_FLOW_REQUIRED_INPUT_KEYS:
            raise OfferFlowRegistryError(
                f"{flow_id}.required_inputs contains unsupported key `{key}`; "
                f"supported keys: {', '.join(OFFER_FLOW_REQUIRED_INPUT_KEYS)}"
            )

    return OfferFlow(
        flow_id=flow_id,
        title=title,
        active=bool(raw.get("active", True)),
        goal=goal,
        artifact_state=artifact_state,
        owner_surface=owner_surface,
        stores=_normalize_stores(raw.get("stores"), flow_id),
        write_surface=write_surface,
        priority=priority,
        summary=summary,
        primary_entrypoint=primary_entrypoint,
        command_examples=_normalize_text_list(raw.get("command_examples", []), f"{flow_id}.command_examples must be a list"),
        dry_run_supported=bool(raw.get("dry_run_supported", False)),
        confirm_required=bool(raw.get("confirm_required", True)),
        verify_required=bool(raw.get("verify_required", True)),
        repricer_followup=repricer_followup,
        required_inputs=required_inputs,
        preflight_checks=_normalize_text_list(raw.get("preflight_checks", []), f"{flow_id}.preflight_checks must be a list"),
        success_checks=_normalize_text_list(raw.get("success_checks", []), f"{flow_id}.success_checks must be a list"),
        stoplines=_normalize_text_list(raw.get("stoplines", []), f"{flow_id}.stoplines must be a list"),
        fallback_flow_ids=_normalize_text_list(raw.get("fallback_flow_ids", []), f"{flow_id}.fallback_flow_ids must be a list"),
        docs_refs=_normalize_text_list(raw.get("docs_refs", []), f"{flow_id}.docs_refs must be a list"),
        skill_refs=_normalize_text_list(raw.get("skill_refs", []), f"{flow_id}.skill_refs must be a list"),
        artifact_roots=_normalize_text_list(raw.get("artifact_roots", []), f"{flow_id}.artifact_roots must be a list"),
        notes=_normalize_text_list(raw.get("notes", []), f"{flow_id}.notes must be a list"),
    )


def load_offer_flow_registry(path: str | Path = DEFAULT_OFFER_FLOW_CONFIG) -> OfferFlowRegistry:
    registry_path = Path(path)
    if not registry_path.exists():
        raise OfferFlowRegistryError(f"Offer flow registry file not found: {registry_path}")
    raw = yaml.safe_load(registry_path.read_text(encoding="utf-8"))
    data = _require_mapping(raw, "Offer flow registry must be a YAML mapping")

    try:
        registry_version = int(data.get("registry_version", 1))
    except Exception as exc:  # pragma: no cover - defensive branch
        raise OfferFlowRegistryError("registry_version must be an integer") from exc
    if registry_version < 1:
        raise OfferFlowRegistryError("registry_version must be >= 1")

    doc_output_path = str(data.get("doc_output_path", "Docs/offer_ops/OFFER_METHOD_MATRIX.generated.md")).strip()
    if not doc_output_path:
        raise OfferFlowRegistryError("doc_output_path must not be empty")

    flows_raw = _require_list(data.get("flows"), "flows must be a list")
    flows = [_load_flow(_require_mapping(item, "each flow must be a mapping")) for item in flows_raw]
    if not flows:
        raise OfferFlowRegistryError("flows must not be empty")

    seen_ids: set[str] = set()
    for flow in flows:
        if flow.flow_id in seen_ids:
            raise OfferFlowRegistryError(f"Duplicate flow_id: {flow.flow_id}")
        seen_ids.add(flow.flow_id)

    for flow in flows:
        for fallback_id in flow.fallback_flow_ids:
            if fallback_id not in seen_ids:
                raise OfferFlowRegistryError(f"{flow.flow_id}.fallback_flow_ids contains unknown flow id: {fallback_id}")

    return OfferFlowRegistry(
        registry_version=registry_version,
        doc_output_path=doc_output_path,
        flows=sorted(flows, key=lambda flow: (flow.priority, flow.flow_id)),
    )


def filter_offer_flows(
    registry: OfferFlowRegistry,
    *,
    goal: str | None = None,
    artifact_state: str | None = None,
    store: str | None = None,
    include_inactive: bool = False,
) -> list[OfferFlow]:
    flows = registry.flows
    if not include_inactive:
        flows = [flow for flow in flows if flow.active]
    if goal:
        flows = [flow for flow in flows if flow.goal == goal]
    if artifact_state:
        flows = [flow for flow in flows if flow.artifact_state == artifact_state]
    if store:
        flows = [flow for flow in flows if flow.store_matches(store)]
    return sorted(flows, key=lambda flow: (flow.priority, flow.flow_id))


def select_offer_flow(
    registry: OfferFlowRegistry,
    *,
    goal: str,
    artifact_state: str,
    store: str | None = None,
    include_inactive: bool = False,
) -> OfferFlowSelection | None:
    candidates = filter_offer_flows(
        registry,
        goal=goal,
        artifact_state=artifact_state,
        store=store,
        include_inactive=include_inactive,
    )
    if not candidates:
        return None

    scored: list[tuple[int, OfferFlow, list[str]]] = []
    for flow in candidates:
        reasons = [
            f"goal={goal}",
            f"artifact_state={artifact_state}",
        ]
        score = 1000 - flow.priority
        if store:
            if "ALL" in flow.stores:
                reasons.append(f"store={normalize_offer_flow_store(store)} via ALL")
            else:
                reasons.append(f"store={normalize_offer_flow_store(store)} exact")
                score += 25
        if flow.dry_run_supported:
            score += 5
            reasons.append("dry-run-supported")
        if flow.verify_required:
            score += 3
            reasons.append("verify-required")
        scored.append((score, flow, reasons))

    scored.sort(key=lambda item: (-item[0], item[1].priority, item[1].flow_id))
    best_score, best_flow, reasons = scored[0]
    return OfferFlowSelection(
        flow=best_flow,
        score=best_score,
        reasons=reasons,
        candidate_count=len(candidates),
    )


def render_offer_flow_registry_markdown(registry: OfferFlowRegistry) -> str:
    lines: list[str] = [
        "# Offer Method Matrix (Generated)",
        "",
        "Generated from `config/offer_flows.yaml` via `web-auto offer-flow render-doc`.",
        "",
        "## Registry Table",
        "",
        "| Flow ID | Goal | Artifact State | Stores | Write Surface | Dry Run | Verify | Fallbacks |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
    ]

    for flow in registry.flows:
        fallback_text = ", ".join(flow.fallback_flow_ids) if flow.fallback_flow_ids else "-"
        dry_text = "yes" if flow.dry_run_supported else "no"
        verify_text = "yes" if flow.verify_required else "no"
        lines.append(
            f"| `{flow.flow_id}` | `{flow.goal}` | `{flow.artifact_state}` | `{', '.join(flow.stores)}` | "
            f"`{flow.write_surface}` | {dry_text} | {verify_text} | {fallback_text} |"
        )

    for flow in registry.flows:
        lines.extend(
            [
                "",
                f"## {flow.title}",
                "",
                f"- `flow_id`: `{flow.flow_id}`",
                f"- `active`: `{str(flow.active).lower()}`",
                f"- `goal`: `{flow.goal}`",
                f"- `artifact_state`: `{flow.artifact_state}`",
                f"- `stores`: `{', '.join(flow.stores)}`",
                f"- `owner_surface`: `{flow.owner_surface}`",
                f"- `write_surface`: `{flow.write_surface}`",
                f"- `primary_entrypoint`: `{flow.primary_entrypoint}`",
                f"- `repricer_followup`: `{flow.repricer_followup}`",
                "",
                flow.summary,
                "",
            ]
        )

        if flow.required_inputs:
            lines.append("### Required Inputs")
            lines.append("")
            for item in flow.required_inputs:
                lines.append(f"- `{item}`")
            lines.append("")

        if flow.command_examples:
            lines.append("### Command Examples")
            lines.append("")
            for command in flow.command_examples:
                lines.append(f"- `{command}`")
            lines.append("")

        if flow.preflight_checks:
            lines.append("### Preflight Checks")
            lines.append("")
            for item in flow.preflight_checks:
                lines.append(f"- {item}")
            lines.append("")

        if flow.success_checks:
            lines.append("### Success Checks")
            lines.append("")
            for item in flow.success_checks:
                lines.append(f"- {item}")
            lines.append("")

        if flow.stoplines:
            lines.append("### Stoplines")
            lines.append("")
            for item in flow.stoplines:
                lines.append(f"- {item}")
            lines.append("")

        if flow.docs_refs:
            lines.append("### Docs Refs")
            lines.append("")
            for item in flow.docs_refs:
                lines.append(f"- `{item}`")
            lines.append("")

        if flow.skill_refs:
            lines.append("### Skill Refs")
            lines.append("")
            for item in flow.skill_refs:
                lines.append(f"- `{item}`")
            lines.append("")

    return "\n".join(lines).rstrip() + "\n"
