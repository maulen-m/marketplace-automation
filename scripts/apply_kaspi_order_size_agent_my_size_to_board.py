#!/usr/bin/env python3
"""Apply automation-agent MY_SIZE values to Google Ops Board.

Default mode is dry-run. Live writes require:

- --apply
- --confirm-agent-my-size-write
- ENABLE_GOOGLE_OPS_BOARD_WRITE=1
- ENABLE_KASPI_ORDER_SIZE_AGENT_MY_SIZE_WRITE=1

Only blank SalesRaw_Today.MY_SIZE cells are updated. The script computes MY_SIZE
from existing HEIGHT/WEIGHT cells, skips LINE31 until its table exists, and marks
agent-written MY_SIZE cells with bold text plus an orange background.
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
    plan_agent_my_size_updates,
)


TARGET_TAB = "SalesRaw_Today"
SIZE_COLUMN = "MY_SIZE"
NARROW_WRITE_ENV_GATE = "ENABLE_KASPI_ORDER_SIZE_AGENT_MY_SIZE_WRITE"


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")


def _clean(value: Any) -> str:
    text = str(value or "").strip()
    if text.endswith(".0") and text[:-2].isdigit():
        return text[:-2]
    return text


def _column_letter(column_number: int) -> str:
    if column_number < 1:
        raise ValueError(f"Column number must be >= 1, got {column_number}")
    result = ""
    current = column_number
    while current:
        current, remainder = divmod(current - 1, 26)
        result = chr(65 + remainder) + result
    return result


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
    parser.add_argument("--autonomous-business-root", type=Path, default=Path(os.environ.get("AUTONOMOUS_BUSINESS_ROOT") or _default_ab_root()))
    parser.add_argument("--service-account-json", type=Path)
    parser.add_argument("--spreadsheet-id")
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--confirm-agent-my-size-write", action="store_true")
    return parser


def _default_output_dir() -> Path:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return PROJECT_ROOT / "exports" / "validation" / f"kaspi_order_size_agent_my_size_board_apply_{stamp}"


def _authority_blockers(*, apply: bool, confirmed: bool, contract_write_env_gate: str) -> list[str]:
    if not apply:
        return []
    blockers: list[str] = []
    if not confirmed:
        blockers.append("confirm_agent_my_size_write_missing")
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
    column_letter: str,
    sheet_rows: list[int],
) -> dict[int, dict[str, Any]]:
    if not sheet_rows:
        return {}
    start = min(sheet_rows)
    end = max(sheet_rows)
    encoded_range = quote(f"{tab_name}!{column_letter}{start}:{column_letter}{end}")
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


def _verify_updates(
    *,
    board_matrix: list[list[Any]],
    extract_rows_with_positions_from_matrix: Any,
    headers: list[str],
    updates: list[dict[str, Any]],
    format_by_row: dict[int, dict[str, Any]],
) -> tuple[bool, list[dict[str, Any]]]:
    positioned = extract_rows_with_positions_from_matrix(headers, board_matrix)
    by_row = {int(item["sheet_row"]): dict(item.get("row") or {}) for item in positioned}
    failures: list[dict[str, Any]] = []
    for update in updates:
        sheet_row = int(update["sheet_row"])
        row = by_row.get(sheet_row) or {}
        actual_value = _clean(row.get(SIZE_COLUMN))
        planned_value = _clean(update.get("value"))
        value_ok = actual_value == planned_value
        format_ok = _format_is_agent_orange_bold(format_by_row.get(sheet_row) or {})
        if not value_ok or not format_ok:
            failures.append(
                {
                    "sheet_row": sheet_row,
                    "order_id": update.get("order_id"),
                    "planned_value": planned_value,
                    "actual_value": actual_value,
                    "value_ok": value_ok,
                    "format_ok": format_ok,
                }
            )
    return not failures, failures


def _redacted_updates(updates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "range": item.get("range"),
            "sheet_row": item.get("sheet_row"),
            "order_id": item.get("order_id"),
            "height_cm": item.get("height_cm"),
            "weight_kg": item.get("weight_kg"),
            "planned_my_size": item.get("value"),
            "table_source": item.get("table_source"),
            "height_size": item.get("height_size"),
            "weight_size": item.get("weight_size"),
            "offer_size": item.get("offer_size"),
            "effective_weight_kg": item.get("effective_weight_kg"),
        }
        for item in updates
    ]


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    output_dir = (args.output_dir or _default_output_dir()).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    run_at = datetime.now().isoformat(timespec="seconds")

    helpers = _load_google_board_helpers(args.autonomous_business_root)
    extract_rows_with_positions_from_matrix = helpers[1]
    load_ops_board_contract = helpers[2]
    contract = load_ops_board_contract()
    tab_contract = contract.tabs[TARGET_TAB]
    headers = list(tab_contract.headers)
    my_size_col_index = headers.index(SIZE_COLUMN) + 1
    my_size_col_letter = _column_letter(my_size_col_index)

    blockers = _authority_blockers(
        apply=bool(args.apply),
        confirmed=bool(args.confirm_agent_my_size_write),
        contract_write_env_gate=contract.write_env_gate,
    )

    client = _build_client(args, contract, helpers)
    board_matrix = client.get_tab_values(TARGET_TAB)
    positioned_rows = extract_rows_with_positions_from_matrix(headers, board_matrix)
    updates, skipped = plan_agent_my_size_updates(
        positioned_rows,
        target_tab=TARGET_TAB,
        my_size_column_index_1based=my_size_col_index,
    )

    sheet_id = client.get_sheet_id_map()[TARGET_TAB]
    google_board_write_performed = False
    direct_format_write_performed = False
    conditional_format_fallback_applied = False
    readback_verified = False
    readback_failures: list[dict[str, Any]] = []

    if args.apply and not blockers and updates:
        client.update_cells(updates)
        google_board_write_performed = True
        direct_requests = build_agent_my_size_direct_format_requests(
            sheet_id=sheet_id,
            sheet_rows=[int(item["sheet_row"]) for item in updates],
            my_size_column_index_1based=my_size_col_index,
        )
        client.batch_update(direct_requests)
        direct_format_write_performed = bool(direct_requests)

        post_matrix = client.get_tab_values(TARGET_TAB)
        format_by_row = _read_format_by_row(
            client=client,
            tab_name=TARGET_TAB,
            column_letter=my_size_col_letter,
            sheet_rows=[int(item["sheet_row"]) for item in updates],
        )
        readback_verified, readback_failures = _verify_updates(
            board_matrix=post_matrix,
            extract_rows_with_positions_from_matrix=extract_rows_with_positions_from_matrix,
            headers=headers,
            updates=updates,
            format_by_row=format_by_row,
        )
        if readback_failures and any(not item.get("format_ok") for item in readback_failures):
            fallback_request = build_agent_my_size_conditional_format_request(
                sheet_id=sheet_id,
                sheet_rows=[int(item["sheet_row"]) for item in updates],
                my_size_column_index_1based=my_size_col_index,
            )
            if fallback_request is not None:
                client.batch_update([fallback_request])
                conditional_format_fallback_applied = True
                post_matrix = client.get_tab_values(TARGET_TAB)
                format_by_row = _read_format_by_row(
                    client=client,
                    tab_name=TARGET_TAB,
                    column_letter=my_size_col_letter,
                    sheet_rows=[int(item["sheet_row"]) for item in updates],
                )
                readback_verified, readback_failures = _verify_updates(
                    board_matrix=post_matrix,
                    extract_rows_with_positions_from_matrix=extract_rows_with_positions_from_matrix,
                    headers=headers,
                    updates=updates,
                    format_by_row=format_by_row,
                )

    if blockers:
        gate = "YELLOW_KASPI_ORDER_SIZE_AGENT_MY_SIZE_APPLY_BLOCKED_NO_WRITE"
        exit_code = 2
    elif args.apply and updates and not readback_verified:
        gate = "RED_KASPI_ORDER_SIZE_AGENT_MY_SIZE_APPLY_READBACK_FAILED"
        exit_code = 2
    elif args.apply and updates:
        gate = "GREEN_KASPI_ORDER_SIZE_AGENT_MY_SIZE_APPLIED_AND_FORMAT_VERIFIED"
        exit_code = 0
    elif args.apply:
        gate = "GREEN_KASPI_ORDER_SIZE_AGENT_MY_SIZE_NO_ELIGIBLE_BLANK_ROWS"
        exit_code = 0
    else:
        gate = "GREEN_KASPI_ORDER_SIZE_AGENT_MY_SIZE_PREFLIGHT_READY_NO_WRITE"
        exit_code = 0

    manifest = {
        "gate": gate,
        "run_at": run_at,
        "apply": bool(args.apply),
        "target_tab": TARGET_TAB,
        "planned_update_count": len(updates),
        "skipped_count": len(skipped),
        "blockers": blockers,
        "google_board_write_performed": google_board_write_performed,
        "direct_format_write_performed": direct_format_write_performed,
        "conditional_format_fallback_applied": conditional_format_fallback_applied,
        "post_apply_readback_verified": readback_verified,
        "readback_failures": readback_failures,
        "raw_customer_text_exported": False,
        "kaspi_chat_write_performed": False,
        "telegram_send_performed": False,
    }
    _write_json(output_dir / "manifest.json", manifest)
    _write_json(output_dir / "planned_my_size_updates_redacted.json", _redacted_updates(updates))
    _write_json(output_dir / "skipped_rows_redacted.json", skipped)
    print(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
