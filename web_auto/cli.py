from __future__ import annotations

import argparse
import json
import logging
import os
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from .auth import generate_storage_state
from .config import ConfigError, detect_task_id, load_config, load_dumping_config, load_min_price_sync_config
from .repricer_competitors import run_repricer_competitors, run_repricer_competitors_api
from .repricer_dumping import run_repricer_dumping_enable_api
from .repricer_items_export import export_repricer_items_to_sqlite
from .repricer_min_price_sync import run_repricer_min_price_sync_api
from .repricer_unified_truth import (
    DEFAULT_LINKS_BASE_XLSX,
    DEFAULT_SCRAPE_CATALOG_XLSX,
    DEFAULT_SCRAPE_PRICEWARS_DIR,
    export_repricer_unified_report,
    export_repricer_unified_truth,
)


def _setup_logging(verbose: bool, quiet: bool) -> None:
    level = logging.INFO
    if verbose:
        level = logging.DEBUG
    if quiet:
        level = logging.WARNING
    logging.basicConfig(level=level, format="%(message)s")


def _add_global_flags(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--env-file", help="Path to .env file", default=None)
    parser.add_argument("-v", "--verbose", action="store_true", help="Verbose logging")
    parser.add_argument("-q", "--quiet", action="store_true", help="Quiet logging")
    parser.add_argument("--json", action="store_true", help="JSON output")
    parser.add_argument("--plain", action="store_true", help="Plain text output")
    parser.add_argument("--no-input", action="store_true", help="Disable prompts")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="web-auto", description="Web automation CLI")
    _add_global_flags(parser)

    subparsers = parser.add_subparsers(dest="command", required=True)

    run_parser = subparsers.add_parser("run", help="Run a task")
    run_parser.add_argument(
        "task",
        choices=["repricer-competitors", "repricer-dumping-enable", "repricer-min-price-sync"],
        help="Task name",
    )
    run_parser.add_argument("--config", required=True, help="Config file path")
    run_parser.add_argument("--dry-run", action="store_true", help="Dry-run mode")
    run_parser.add_argument("--confirm", action="store_true", help="Confirm write actions")
    run_parser.add_argument("--resume", action="store_true", help="Resume from checkpoint")
    run_parser.add_argument("--account", help="Run only a specific account name")
    run_parser.add_argument("--store", type=int, action="append", help="Run only a specific store ID")
    run_parser.add_argument("--stores", help="Comma-separated store IDs")
    run_parser.add_argument("--checkpoint", help="Override checkpoint path")
    run_parser.add_argument("--artifacts", help="Override artifacts directory")
    run_parser.add_argument("--slowmo-ms", type=int, help="Slowmo delay in ms")
    run_parser.add_argument("--timeout-ms", type=int, help="Table load timeout in ms")
    run_parser.add_argument("--modal-timeout-ms", type=int, help="Modal open timeout in ms")
    run_parser.add_argument("--headless", action="store_true", help="Run headless")
    run_parser.add_argument("--headed", action="store_true", help="Run headful")
    run_parser.add_argument("--profile-dir", help="Chrome profile directory for auth")
    run_parser.add_argument("--storage-state", help="Storage state JSON path")
    run_parser.add_argument("--upload-after", action="store_true", help="Click 'Upload to Kaspi' after verification")
    run_parser.add_argument("--api", action="store_true", help="Use API mode (no UI modals)")
    run_parser.add_argument("--api-verify", action="store_true", help="Verify remaining targets after API write")
    run_parser.add_argument("--verify", action="store_true", help="Verify dumping enabled after API write")

    validate_parser = subparsers.add_parser("validate-config", help="Validate a config file")
    validate_parser.add_argument("--config", required=True, help="Config file path")

    auth_parser = subparsers.add_parser("auth", help="Generate storage_state.json")
    auth_parser.add_argument("--base-url", required=True, help="Base URL (e.g. https://repricer.kz/price_strategy/)")
    auth_parser.add_argument("--token-env", required=True, help="Env var name containing token")
    auth_parser.add_argument("--out", required=True, help="Output path for storage_state.json")
    auth_parser.add_argument("--profile-dir", help="Chrome profile dir to reuse authenticated session")
    auth_parser.add_argument("--headless", action="store_true", help="Run headless")
    auth_parser.add_argument("--headed", action="store_true", help="Run headful")

    export_parser = subparsers.add_parser("export", help="Export data")
    export_parser.add_argument(
        "task",
        choices=["repricer-items", "repricer-unified-truth", "repricer-unified-report"],
        help="Export task name",
    )
    export_parser.add_argument(
        "--config",
        default="config/tasks/repricer_competitors.yaml",
        help="Config file path",
    )
    export_parser.add_argument(
        "--out",
        default="data/repricer_items.sqlite",
        help="Output SQLite path",
    )
    export_parser.add_argument(
        "--db-out",
        default="data/repricer_unified_truth.sqlite",
        help="Unified truth DB output path",
    )
    export_parser.add_argument(
        "--xlsx-out",
        default="exports/repricer_unified_truth.xlsx",
        help="Unified truth workbook output path",
    )
    export_parser.add_argument(
        "--md-out",
        default="exports/repricer_unified_truth.md",
        help="Unified truth markdown report path",
    )
    export_parser.add_argument(
        "--external-truth-dir",
        default="Docs/external_db_truth",
        help="External DB truth directory",
    )
    export_parser.add_argument(
        "--legacy-snapshot",
        default="data/repricer_items.sqlite",
        help="Legacy Repricer snapshot SQLite path",
    )
    export_parser.add_argument(
        "--kaspi-accounts",
        default="config/tasks/kaspi_accounts.yaml",
        help="Kaspi accounts config path",
    )
    export_parser.add_argument(
        "--links-base-xlsx",
        default=DEFAULT_LINKS_BASE_XLSX,
        help="SKU link base workbook path",
    )
    export_parser.add_argument(
        "--scrape-catalog-xlsx",
        default=DEFAULT_SCRAPE_CATALOG_XLSX,
        help="Scrape catalog workbook path",
    )
    export_parser.add_argument(
        "--scrape-pricewars-dir",
        default=DEFAULT_SCRAPE_PRICEWARS_DIR,
        help="Scrape price-wars workbook directory",
    )
    export_parser.add_argument(
        "--sales-window-days",
        type=int,
        default=90,
        help="Rolling sales window in days for control scope",
    )
    export_parser.add_argument(
        "--refresh-repricer",
        action="store_true",
        help="Refresh current Repricer snapshot before unified merge",
    )
    export_parser.add_argument(
        "--include-all-rows",
        action="store_true",
        help="Include sales-off rows for repricer-items export",
    )
    export_parser.add_argument("--headless", action="store_true", help="Run headless")
    export_parser.add_argument("--headed", action="store_true", help="Run headful")

    args = parser.parse_args(argv)

    if args.env_file:
        load_dotenv(args.env_file)
    else:
        default_env = Path(".env")
        if default_env.exists():
            load_dotenv(default_env)

    _setup_logging(args.verbose, args.quiet)

    if args.command == "validate-config":
        try:
            task_id = detect_task_id(args.config)
            if task_id == "repricer_dumping_enable":
                load_dumping_config(args.config)
            elif task_id == "repricer_min_price_sync":
                load_min_price_sync_config(args.config)
            else:
                load_config(args.config)
        except ConfigError as exc:
            logging.error(str(exc))
            return 2
        logging.info("Config OK")
        return 0

    if args.command == "auth":
        token = os.environ.get(args.token_env)
        if not token:
            logging.error(f"Missing env var: {args.token_env}")
            return 3
        headless = args.headless
        if args.headed:
            headless = False
        generate_storage_state(
            base_url=args.base_url,
            token=token,
            output_path=args.out,
            profile_dir=args.profile_dir,
            headless=headless,
        )
        logging.info(f"storage_state.json written to {args.out}")
        return 0

    if args.command == "run":
        try:
            if args.task == "repricer-competitors":
                config = load_config(args.config)
            elif args.task == "repricer-dumping-enable":
                config = load_dumping_config(args.config)
            else:
                config = load_min_price_sync_config(args.config)
        except ConfigError as exc:
            logging.error(str(exc))
            return 2

        store_ids: list[int] = []
        if args.store:
            store_ids.extend(args.store)
        if args.stores:
            store_ids.extend([int(x.strip()) for x in args.stores.split(",") if x.strip()])

        if args.task == "repricer-competitors":
            if args.api:
                result = run_repricer_competitors_api(
                    config=config,
                    dry_run=args.dry_run,
                    confirm=args.confirm,
                    resume=args.resume,
                    account_name=args.account,
                    store_ids=store_ids or None,
                    checkpoint_path=args.checkpoint,
                    artifacts_dir=args.artifacts,
                    slowmo_ms=args.slowmo_ms,
                    timeout_ms=args.timeout_ms,
                    headless=args.headless,
                    headed=args.headed,
                    profile_dir=args.profile_dir,
                    storage_state=args.storage_state,
                    api_verify=args.api_verify,
                )
            else:
                result = run_repricer_competitors(
                    config=config,
                    dry_run=args.dry_run,
                    confirm=args.confirm,
                    resume=args.resume,
                    account_name=args.account,
                    store_ids=store_ids or None,
                    checkpoint_path=args.checkpoint,
                    artifacts_dir=args.artifacts,
                    slowmo_ms=args.slowmo_ms,
                    timeout_ms=args.timeout_ms,
                    modal_timeout_ms=args.modal_timeout_ms,
                    headless=args.headless,
                    headed=args.headed,
                    profile_dir=args.profile_dir,
                    storage_state=args.storage_state,
                    upload_after=args.upload_after,
                )
        elif args.task == "repricer-dumping-enable":
            result = run_repricer_dumping_enable_api(
                config=config,
                dry_run=args.dry_run,
                confirm=args.confirm,
                resume=args.resume,
                account_name=args.account,
                store_ids=store_ids or None,
                checkpoint_path=args.checkpoint,
                artifacts_dir=args.artifacts,
                slowmo_ms=args.slowmo_ms,
                timeout_ms=args.timeout_ms,
                headless=args.headless,
                headed=args.headed,
                profile_dir=args.profile_dir,
                storage_state=args.storage_state,
                verify=args.verify or args.api_verify,
            )
        else:
            result = run_repricer_min_price_sync_api(
                config=config,
                dry_run=args.dry_run,
                confirm=args.confirm,
                resume=args.resume,
                checkpoint_path=args.checkpoint,
                artifacts_dir=args.artifacts,
                slowmo_ms=args.slowmo_ms,
                timeout_ms=args.timeout_ms,
                headless=args.headless,
                headed=args.headed,
                profile_dir=args.profile_dir,
                storage_state=args.storage_state,
                verify=args.verify or args.api_verify,
            )

        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            if args.task == "repricer-competitors":
                logging.info(
                    "Summary: products=%s modals=%s checked=%s errors=%s",
                    result["products_visited"],
                    result["modals_opened"],
                    result["checkboxes_changed"],
                    result["errors"],
                )
            elif args.task == "repricer-dumping-enable":
                logging.info(
                    "Summary: products=%s dumping_enabled=%s errors=%s",
                    result["products_visited"],
                    result["dumping_enabled"],
                    result["errors"],
                )
            else:
                logging.info(
                    "Summary: products=%s min_price_updates=%s errors=%s",
                    result["products_visited"],
                    result["min_price_updates"],
                    result["errors"],
            )
        return 0 if result["errors"] == 0 else 4

    if args.command == "export":
        headless = True
        if args.headed:
            headless = False
        if args.headless:
            headless = True
        if args.task == "repricer-items":
            summary = export_repricer_items_to_sqlite(
                config_path=args.config,
                output_path=args.out,
                headless=headless,
                include_all_rows=args.include_all_rows,
            )
            if args.json:
                print(json.dumps(summary, ensure_ascii=False, indent=2))
            else:
                logging.info(
                    "Summary: stores=%s scanned=%s inserted=%s total_links=%s",
                    len(summary.get("stores", {})),
                    summary.get("total_rows_scanned"),
                    summary.get("total_items"),
                    summary.get("total_links"),
                )
            return 0
        if args.task == "repricer-unified-truth":
            summary = export_repricer_unified_truth(
                config_path=args.config,
                db_out_path=args.db_out,
                xlsx_out_path=args.xlsx_out,
                markdown_out_path=args.md_out,
                external_truth_dir=args.external_truth_dir,
                kaspi_accounts_path=args.kaspi_accounts,
                legacy_snapshot_path=args.legacy_snapshot,
                links_base_xlsx_path=args.links_base_xlsx,
                scrape_catalog_xlsx_path=args.scrape_catalog_xlsx,
                scrape_pricewars_dir=args.scrape_pricewars_dir,
                sales_window_days=args.sales_window_days,
                refresh_repricer=args.refresh_repricer,
                headless=headless,
            )
            if args.json:
                print(json.dumps(summary, ensure_ascii=False, indent=2))
            else:
                logging.info(
                    "Summary: history_rows=%s latest_rows=%s control_scope_rows=%s control_ready_rows=%s db=%s xlsx=%s md=%s",
                    summary.get("history_rows"),
                    summary.get("latest_rows"),
                    summary.get("sold_90d_total_rows"),
                    summary.get("sold_90d_control_ready_rows"),
                    summary.get("db_path"),
                    summary.get("xlsx_path"),
                    summary.get("markdown_path"),
                )
            return 0
        if args.task == "repricer-unified-report":
            summary = export_repricer_unified_report(db_path=args.db_out)
            if args.json:
                print(json.dumps(summary, ensure_ascii=False, indent=2))
            else:
                logging.info(
                    "Summary: history_rows=%s latest_rows=%s bridge_rows=%s unresolved_latest=%s",
                    summary.get("history_rows"),
                    summary.get("latest_rows"),
                    summary.get("bridge_rows"),
                    summary.get("unresolved_latest_rows"),
                )
            return 0
        logging.error("Unknown export task")
        return 2

    return 2
