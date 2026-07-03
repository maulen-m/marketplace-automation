#!/usr/bin/env python3
"""Apply verified Kaspi height/weight facts to the Google Ops Board.

Default mode is dry-run. Live writes require:

- --apply
- --confirm-kaspi-order-size-facts-write
- ENABLE_GOOGLE_OPS_BOARD_WRITE=1
- ENABLE_KASPI_ORDER_SIZE_FACTS_WRITE=1

Only blank SalesRaw_Today HEIGHT, WEIGHT, and MY_SIZE cells are updated. Newly
agent-written MY_SIZE cells are marked with bold text and an orange background.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote


PROJECT_ROOT = Path(__file__).resolve().parents[1]
INVENTORY_ROOT = PROJECT_ROOT / "inventory"
if str(INVENTORY_ROOT) not in sys.path:
    sys.path.insert(0, str(INVENTORY_ROOT))

from kaspi_order_size_agent_logic import (  # noqa: E402
    build_agent_my_size_conditional_format_request,
    build_agent_my_size_direct_format_requests,
)
from kaspi_order_size_board_facts_logic import (  # noqa: E402
    TARGET_TAB,
    column_letter,
    clean_cell,
    plan_board_fact_updates,
)


NARROW_WRITE_ENV_GATE = "ENABLE_KASPI_ORDER_SIZE_FACTS_WRITE"


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _default_ab_root() -> Path:
    return PROJECT_ROOT.parent / "Autonomous_business"


def _load_google_board_helpers(ab_root: Path):
    root = ab_root.resolve()
    if not (root / "core" / "integrations" / "google_ops_board.py").is_file():
        raise RuntimeError(f"Autonomous Business root is invalid: {root}")
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    from core.integrations.google_ops_board import (  # type: ignore
        GoogleOpsBoardClient,
        extract_rows_with_positions_from_matrix,
        load_ops_board_contract,
        resolve_service_account_json,
        resolve_spreadsheet_id,
    )

    return (
        GoogleOpsBoardClient,
        extract_rows_with_positions_from_matrix,
        load_ops_board_contract,
        resolve_service_account_json,
        resolve_spreadsheet_id,
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--facts-json", type=Path, required=True)
    parser.add_argument(
        "--autonomous-business-root",
        type=Path,
        default=Path(os.environ.get("AUTONOMOUS_BUSINESS_ROOT") or _default_ab_root()),
    )
    parser.add_argument("--service-account-json", type=Path)
    parser.add_argument("--spreadsheet-id")
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--confirm-kaspi-order-size-facts-write", action="store_true")
    return parser


def _authority_blockers(*, apply: bool, confirmed: bool, contract_write_env_gate: str) -> list[str]:
    if not apply:
        return []
    blockers: list[str] = []
    if not confirmed:
        blockers.append("confirm_kaspi_order_size_facts_write_missing")
    if os.environ.get(contract_write_env_gate) != "1":
        blockers.append(f"{contract_write_env_gate}_not_1")
    if os.environ.get(NARROW_WRITE_ENV_GATE) != "1":
        blockers.append(f"{NARROW_WRITE_ENV_GATE}_not_1")
    return blockers


def _build_client(args: argparse.Namespace, contract: Any, helpers: tuple[Any, ...]):
    GoogleOpsBoardClient = helpers[0]
    resolve_service_account_json = helpers[3]
    resolve_spreadsheet_id = helpers[4]
    service_account_json = resolve_service_account_json(args.service_account_json, contract=contract)
    spreadsheet_id = resolve_spreadsheet_id(args.spreadsheet_id, contract=contract)
    return GoogleOpsBoardClient.from_service_account_file(spreadsheet_id, service_account_json)


def _format_is_agent_orange_bold(cell: dict[str, Any]) -> bool:
    fmt = dict(cell.get("effectiveFormat") or {})
    bg = dict(fmt.get("backgroundColor") or {})
    text_format = dict(fmt.get("textFormat") or {})
    red = float(bg.get("red") or 0)
    green = float(bg.get("green") or 0)
    blue = float(bg.get("blue") or 0)
    return bool(text_format.get("bold")) and red >= 0.95 and 0.45 <= green <= 0.75 and blue <= 0.2


def _read_format_by_row(
    *,
    client: Any,
    tab_name: str,
    column_letter_value: str,
    sheet_rows: list[int],
) -> dict[int, dict[str, Any]]:
    if not sheet_rows:
        return {}
    start = min(sheet_rows)
    end = max(sheet_rows)
    encoded_range = quote(f"{tab_name}!{column_letter_value}{start}:{column_letter_value}{end}")
    url = (
        f"https://sheets.googleapis.com/v4/spreadsheets/{client.spreadsheet_id}"
        f"?includeGridData=true&ranges={encoded_range}"
        "&fields=sheets(data(startRow,rowData(values(formattedValue,effectiveFormat(backgroundColor,textFormat(bold,foregroundColor)),userEnteredFormat(backgroundColor,textFormat(bold,foregroundColor))))))"
    )
    payload = client._request("GET", url).json()
    sheets = payload.get("sheets") or []
    if not sheets:
        return {}
    data = (sheets[0].get("data") or [{}])[0]
    start_row_index = int(data.get("startRow") or (start - 1))
    out: dict[int, dict[str, Any]] = {}
    for offset, row_data in enumerate(data.get("rowData") or []):
        values = row_data.get("values") or []
        if values:
            out[start_row_index + offset + 1] = values[0]
    return out


def _verify_value_updates(
    *,
    board_matrix: list[list[Any]],
    extract_rows_with_positions_from_matrix: Any,
    headers: list[str],
    updates: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    positioned = extract_rows_with_positions_from_matrix(headers, board_matrix)
    by_row = {int(item["sheet_row"]): dict(item.get("row") or {}) for item in positioned}
    failures: list[dict[str, Any]] = []
    for update in updates:
        sheet_row = int(update["sheet_row"])
        row = by_row.get(sheet_row) or {}
        actual_value = clean_cell(row.get(update["column"]))
        planned_value = clean_cell(update.get("value"))
        if actual_value != planned_value:
            failures.append(
                {
                    "sheet_row": sheet_row,
                    "order_hash": update.get("order_hash"),
                    "column": update.get("column"),
                    "planned_value": planned_value,
                    "actual_value": actual_value,
                }
            )
    return failures


def _verify_format_updates(format_by_row: dict[int, dict[str, Any]], sheet_rows: list[int]) -> list[dict[str, Any]]:
    failures: list[dict[str, Any]] = []
    for sheet_row in sorted(set(int(row) for row in sheet_rows)):
        if not _format_is_agent_orange_bold(format_by_row.get(sheet_row) or {}):
            failures.append({"sheet_row": sheet_row, "format_ok": False})
    return failures


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    output_dir = args.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    facts_payload = json.loads(args.facts_json.read_text(encoding="utf-8"))
    facts = list(facts_payload.get("facts") or [])
    helpers = _load_google_board_helpers(args.autonomous_business_root)
    extract_rows_with_positions_from_matrix = helpers[1]
    load_ops_board_contract = helpers[2]
    contract = load_ops_board_contract()
    tab_contract = contract.tabs[TARGET_TAB]
    headers = list(tab_contract.headers)
    my_size_column_index = headers.index("MY_SIZE") + 1
    my_size_column_letter = column_letter(my_size_column_index)

    blockers = _authority_blockers(
        apply=bool(args.apply),
        confirmed=bool(args.confirm_kaspi_order_size_facts_write),
        contract_write_env_gate=contract.write_env_gate,
    )

    client = _build_client(args, contract, helpers)
    board_matrix = client.get_tab_values(TARGET_TAB)
    positioned_rows = extract_rows_with_positions_from_matrix(headers, board_matrix)
    plan = plan_board_fact_updates(positioned_rows=positioned_rows, facts=facts, headers=headers)

    google_board_write_performed = False
    format_write_performed = False
    conditional_format_fallback_applied = False
    value_readback_failures: list[dict[str, Any]] = []
    format_readback_failures: list[dict[str, Any]] = []
    if blockers:
        gate = "YELLOW_KASPI_ORDER_SIZE_FACTS_WRITE_BLOCKED"
    elif args.apply:
        client.update_cells(plan.cell_updates)
        google_board_write_performed = bool(plan.cell_updates)
        if plan.my_size_format_rows:
            sheet_id = client.get_sheet_id_map()[TARGET_TAB]
            client.batch_update(
                build_agent_my_size_direct_format_requests(
                    sheet_id=sheet_id,
                    sheet_rows=plan.my_size_format_rows,
                    my_size_column_index_1based=my_size_column_index,
                )
            )
            format_write_performed = True
        fresh_matrix = client.get_tab_values(TARGET_TAB)
        value_readback_failures = _verify_value_updates(
            board_matrix=fresh_matrix,
            extract_rows_with_positions_from_matrix=extract_rows_with_positions_from_matrix,
            headers=headers,
            updates=plan.cell_updates,
        )
        format_by_row = _read_format_by_row(
            client=client,
            tab_name=TARGET_TAB,
            column_letter_value=my_size_column_letter,
            sheet_rows=plan.my_size_format_rows,
        )
        format_readback_failures = _verify_format_updates(format_by_row, plan.my_size_format_rows)
        if format_readback_failures:
            fallback_request = build_agent_my_size_conditional_format_request(
                sheet_id=client.get_sheet_id_map()[TARGET_TAB],
                sheet_rows=plan.my_size_format_rows,
                my_size_column_index_1based=my_size_column_index,
            )
            if fallback_request is not None:
                client.batch_update([fallback_request])
                conditional_format_fallback_applied = True
                format_by_row = _read_format_by_row(
                    client=client,
                    tab_name=TARGET_TAB,
                    column_letter_value=my_size_column_letter,
                    sheet_rows=plan.my_size_format_rows,
                )
                format_readback_failures = _verify_format_updates(format_by_row, plan.my_size_format_rows)
        gate = (
            "GREEN_KASPI_ORDER_SIZE_FACTS_WRITTEN_VERIFIED"
            if not value_readback_failures and not format_readback_failures
            else "RED_KASPI_ORDER_SIZE_FACTS_READBACK_FAILED"
        )
    else:
        gate = "GREEN_KASPI_ORDER_SIZE_FACTS_DRY_RUN_READY"

    counts_by_column: dict[str, int] = {}
    for update in plan.cell_updates:
        column = str(update.get("column") or "")
        counts_by_column[column] = counts_by_column.get(column, 0) + 1
    manifest = {
        "gate": gate,
        "run_at": datetime.now().isoformat(timespec="seconds"),
        "target_tab": TARGET_TAB,
        "facts_seen": len(facts),
        "planned_cell_updates": len(plan.cell_updates),
        "planned_updates_by_column": counts_by_column,
        "planned_my_size_format_rows": len(plan.my_size_format_rows),
        "skipped_count": len(plan.skipped),
        "blockers": blockers,
        "google_board_write_performed": google_board_write_performed,
        "my_size_format_write_performed": format_write_performed,
        "conditional_format_fallback_applied": conditional_format_fallback_applied,
        "value_readback_failures": value_readback_failures,
        "format_readback_failures": format_readback_failures,
        "raw_customer_text_exported": False,
        "raw_session_material_exported": False,
        "kaspi_chat_write_performed": False,
        "telegram_send_performed": False,
    }
    _write_json(output_dir / "manifest.json", manifest)
    _write_json(output_dir / "planned_updates_redacted.json", plan.cell_updates)
    _write_json(output_dir / "skipped_redacted.json", plan.skipped)
    print(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if gate.startswith("GREEN") else 2


if __name__ == "__main__":
    raise SystemExit(main())
