#!/usr/bin/env python3
"""Build a normalized fast-path readiness manifest for Kaspi size sweeps.

This wrapper is intentionally read-only. It lets scheduled agents close out the
fast extraction lane with consistent GREEN/YELLOW semantics after they perform
direct chat reads, incremental message-hash checks, and any UI fallback for
ambiguous rows.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
INVENTORY_ROOT = PROJECT_ROOT / "inventory"
if str(INVENTORY_ROOT) not in sys.path:
    sys.path.insert(0, str(INVENTORY_ROOT))

from kaspi_order_size_fast_path_logic import (  # noqa: E402
    count_ambiguous_facts,
    decide_fast_path_gate,
    select_incremental_changed_facts,
)


def _read_json(path: Path | None) -> Any:
    if path is None:
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _candidate_count_from_prior(payload: dict[str, Any], default: int) -> int:
    for key in (
        "candidates_seen",
        "eligible_candidates_after_board_size_guard",
        "control_plane_missing_size_rows",
        "candidate_count",
        "candidates",
    ):
        if key in payload:
            return _int(payload.get(key), default)
    nested = payload.get("fast_path_gate")
    if isinstance(nested, dict) and "candidates_seen" in nested:
        return _int(nested.get("candidates_seen"), default)
    return default


def _extract_facts(payload: Any) -> list[dict[str, Any]]:
    if not payload:
        return []
    if isinstance(payload, list):
        return [dict(item) for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        for key in ("facts", "records", "direct_read_facts", "verified_facts"):
            value = payload.get(key)
            if isinstance(value, list):
                return [dict(item) for item in value if isinstance(item, dict)]
    return []


def _extract_previous_hashes(payload: Any) -> list[str]:
    if not payload:
        return []
    if isinstance(payload, list):
        values = payload
    elif isinstance(payload, dict):
        values = payload.get("message_hashes") or payload.get("previous_message_hashes") or []
    else:
        values = []
    return [str(value or "").strip() for value in values if str(value or "").strip()]


def _manifest_blockers(payload: dict[str, Any] | None) -> list[str]:
    if not payload:
        return []
    values = payload.get("blockers") or []
    if isinstance(values, list):
        return [str(value) for value in values]
    return [str(values)]


def _merchant_context_status(payload: dict[str, Any] | None) -> tuple[bool | None, list[str], list[str], str, list[str]]:
    if payload is None:
        return None, [], [], "", []
    accepted = bool(payload.get("accepted") or payload.get("current_merchant_id_machine_proven"))
    raw_blockers = payload.get("blockers") or []
    if not isinstance(raw_blockers, list):
        raw_blockers = [raw_blockers]
    blocker = str(payload.get("blocker") or "").strip()
    blockers = [str(item) for item in raw_blockers if str(item or "").strip()]
    if blocker:
        blockers.append(blocker)
    if not accepted and not blockers:
        blockers.append("current_merchant_id_not_machine_proven")
    raw_warnings = payload.get("warnings") or []
    if not isinstance(raw_warnings, list):
        raw_warnings = [raw_warnings]
    warnings = [str(item) for item in raw_warnings if str(item or "").strip()]
    merchant_id = str(payload.get("merchant_id") or payload.get("target_merchant_id") or "").strip()
    proof_sources = payload.get("proof_sources") or []
    if not isinstance(proof_sources, list):
        proof_sources = [proof_sources]
    return accepted, blockers, warnings, merchant_id, [str(item) for item in proof_sources if str(item or "").strip()]


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prior-sweep-manifest", type=Path, help="Existing sweep manifest to normalize.")
    parser.add_argument("--direct-read-facts", type=Path, help="PII-safe facts parsed from direct-read chat payloads.")
    parser.add_argument("--merchant-context-proof", type=Path, help="PII-safe active merchant proof manifest.")
    parser.add_argument("--previous-message-hashes", type=Path, help="Optional previous message-hash cache JSON.")
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--candidates-seen", type=int)
    parser.add_argument("--direct-read-attempted", action="store_true")
    parser.add_argument("--direct-read-completed", action="store_true")
    parser.add_argument("--direct-read-unavailable", action="store_true")
    parser.add_argument("--ui-fallback-attempted", action="store_true")
    parser.add_argument("--ui-fallback-unresolved-rows", type=int, default=0)
    parser.add_argument("--unsafe-blocker", action="append", default=[])
    parser.add_argument("--blocker", action="append", default=[])
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    output_dir = args.output_dir.resolve()
    prior = _read_json(args.prior_sweep_manifest) or {}
    facts_payload = _read_json(args.direct_read_facts)
    merchant_context_payload = _read_json(args.merchant_context_proof)
    previous_hashes_payload = _read_json(args.previous_message_hashes)

    facts = _extract_facts(facts_payload)
    previous_hashes = _extract_previous_hashes(previous_hashes_payload)
    changed_facts, unchanged_facts = select_incremental_changed_facts(facts, previous_hashes)
    ambiguous_rows = count_ambiguous_facts(changed_facts)
    candidates_seen = (
        args.candidates_seen
        if args.candidates_seen is not None
        else _candidate_count_from_prior(prior, len(facts))
    )
    merchant_context_accepted, merchant_context_blockers, merchant_context_warnings, merchant_context_merchant_id, proof_sources = (
        _merchant_context_status(merchant_context_payload)
    )
    blockers = [*args.blocker, *_manifest_blockers(prior)]
    if merchant_context_accepted is False:
        blockers.extend(f"direct_read_session_unavailable:{blocker}" for blocker in merchant_context_blockers)
    direct_read_attempted = bool(
        args.direct_read_attempted
        or args.direct_read_completed
        or args.direct_read_unavailable
        or facts_payload is not None
        or merchant_context_payload is not None
        or prior.get("session_probe_path")
    )
    direct_read_completed_raw = bool(args.direct_read_completed or facts_payload is not None)
    direct_read_completed = direct_read_completed_raw and merchant_context_accepted is not False
    direct_read_unavailable = bool(
        args.direct_read_unavailable
        or merchant_context_accepted is False
        or (prior.get("session_probe_path") and not direct_read_completed)
    )

    decision = decide_fast_path_gate(
        candidates_seen=candidates_seen,
        direct_read_attempted=direct_read_attempted,
        direct_read_completed=direct_read_completed,
        direct_read_changed_messages=len(changed_facts),
        direct_read_unavailable=direct_read_unavailable,
        ambiguous_rows=ambiguous_rows,
        ui_fallback_attempted=bool(args.ui_fallback_attempted),
        ui_fallback_unresolved_rows=max(0, int(args.ui_fallback_unresolved_rows or 0)),
        unsafe_blockers=args.unsafe_blocker,
        blockers=blockers,
    )
    manifest = {
        "gate": decision.gate,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "candidates_seen": candidates_seen,
        "direct_read_attempted": direct_read_attempted,
        "direct_read_completed": direct_read_completed,
        "direct_read_completed_raw_before_merchant_context_gate": direct_read_completed_raw,
        "direct_read_unavailable": direct_read_unavailable,
        "merchant_context_accepted": merchant_context_accepted,
        "merchant_context_merchant_id": merchant_context_merchant_id,
        "merchant_context_proof_sources": proof_sources,
        "direct_read_changed_message_count": len(changed_facts),
        "direct_read_unchanged_message_count": len(unchanged_facts),
        "ambiguous_rows": ambiguous_rows,
        "ui_fallback_attempted": bool(args.ui_fallback_attempted),
        "ui_fallback_required": decision.ui_fallback_required,
        "ui_fallback_unresolved_rows": max(0, int(args.ui_fallback_unresolved_rows or 0)),
        "blockers": list(decision.blockers),
        "warnings": sorted(set(list(decision.warnings) + merchant_context_warnings)),
        "chrome_devtools_timeout_demoted": decision.chrome_devtools_timeout_demoted,
        "raw_customer_text_exported": False,
        "raw_order_ids_exported": False,
        "raw_session_material_exported": False,
        "kaspi_chat_write_performed": False,
        "telegram_send_performed": False,
    }
    _write_json(output_dir / "manifest.json", manifest)
    _write_json(
        output_dir / "changed_facts_index_redacted.json",
        [
            {
                "sequence": index,
                "order_ref": item.get("order_ref") or item.get("order_hash") or "",
                "message_hash": item.get("message_hash") or "",
                "parse_confidence": item.get("parse_confidence") or "",
                "height_cm_present": item.get("height_cm") is not None,
                "weight_kg_present": item.get("weight_kg") is not None,
                "notes": item.get("notes") or [],
            }
            for index, item in enumerate(changed_facts, start=1)
        ],
    )
    print(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if str(decision.gate).startswith("GREEN") else 2


if __name__ == "__main__":
    raise SystemExit(main())
