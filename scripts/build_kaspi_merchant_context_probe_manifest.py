#!/usr/bin/env python3
"""Build a PII-safe Kaspi merchant-context proof manifest.

Input evidence must already be sanitized. This script validates whether the
evidence proves the active merchant account for direct-read or live UI use.
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

from kaspi_merchant_context_probe_logic import decide_merchant_context  # noqa: E402


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target-merchant-id", required=True)
    parser.add_argument("--evidence", type=Path, required=True, help="Sanitized merchant-context evidence JSON.")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--require-visible", action="store_true", help="Require visible selected merchant proof.")
    parser.add_argument("--require-x-merchant-header", action="store_true", help="Require matching X-Merchant-ID proof.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    evidence = _read_json(args.evidence)
    decision = decide_merchant_context(
        target_merchant_id=args.target_merchant_id,
        evidence=evidence,
        require_visible=bool(args.require_visible),
        require_x_merchant_header=bool(args.require_x_merchant_header),
    )
    manifest = {
        "accepted": decision.accepted,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "target_merchant_id": str(args.target_merchant_id),
        "merchant_id": decision.merchant_id,
        "blockers": list(decision.blockers),
        "warnings": list(decision.warnings),
        "proof_sources": list(decision.proof_sources),
        "visible_proof_sources": list(decision.visible_proof_sources),
        "supporting_proof_sources": list(decision.supporting_proof_sources),
        "require_visible": bool(args.require_visible),
        "require_x_merchant_header": bool(args.require_x_merchant_header),
        "raw_urls_exported": False,
        "raw_storage_exported": False,
        "raw_customer_text_exported": False,
        "raw_order_ids_exported": False,
        "raw_session_material_exported": False,
    }
    _write_json(args.output, manifest)
    print(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if decision.accepted else 2


if __name__ == "__main__":
    raise SystemExit(main())
