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
from .kaspi_archive_export import run_kaspi_archive_sales_export
from .kaspi_hourly_snapshot import (
    collect_offer_universe,
    load_offer_rows_from_sqlite,
    prune_snapshot_retention,
    run_hourly_snapshot,
)
from .kaspi_merchant_common import resolve_store_credentials
from .kaspi_pricelist_download import default_run_dir, run_kaspi_pricelist_download
from .kaspi_pricelist_ops import apply_intent, build_store_snapshot, emit_outputs, verify_uploaded_state
from .kaspi_pricelist_upload import resolve_upload_file_paths, run_kaspi_pricelist_upload
from .kaspi_snapshot_config import SnapshotConfigError, load_kaspi_snapshot_config
from .kaspi_variant_refresh import run_daily_variant_refresh
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
        choices=["repricer-items", "repricer-unified-truth", "repricer-unified-report", "kaspi-archive-sales"],
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
    export_parser.add_argument("--dry-run", action="store_true", help="Dry-run mode")
    export_parser.add_argument("--confirm", action="store_true", help="Confirm write/export actions")
    export_parser.add_argument("--start-date", default="2024-06-06", help="Archive export start date (YYYY-MM-DD)")
    export_parser.add_argument("--end-date", default="2026-02-26", help="Archive export end date (YYYY-MM-DD)")
    export_parser.add_argument("--block-days", type=int, default=90, help="Archive export window size in days")
    export_parser.add_argument("--window-count", type=int, default=5, help="Number of Chrome windows/accounts to use")
    export_parser.add_argument(
        "--store-labels",
        default="Universal,Acmewear,store-d,Store-C,STORE-B",
        help="Comma-separated store labels in Chrome window order",
    )
    export_parser.add_argument("--downloads-dir", default="~/Downloads", help="Browser downloads directory")
    export_parser.add_argument(
        "--output-root",
        default="exports/kaspi_archive_sales",
        help="Run output root directory",
    )
    export_parser.add_argument(
        "--copy-dst",
        default="~/Documents/useful tables/Main crm spreadsheets/main tables/Purchase_orders/vibe_code_PO/Sales_archive/web_automation",
        help="Destination root to copy completed run folder",
    )
    export_parser.add_argument("--timeout-seconds", type=int, default=180, help="Per-block download wait timeout")
    export_parser.add_argument("--retries", type=int, default=2, help="Retries per store/date block")
    export_parser.add_argument("--headless", action="store_true", help="Run headless")
    export_parser.add_argument("--headed", action="store_true", help="Run headful")

    snapshot_parser = subparsers.add_parser("snapshot", help="Kaspi market snapshots")
    snapshot_parser.add_argument("task", choices=["hourly", "daily-variants", "prune-retention"], help="Snapshot task")
    snapshot_parser.add_argument(
        "--config",
        default="config/tasks/kaspi_hourly_snapshot.yaml",
        help="Snapshot config file path",
    )
    snapshot_parser.add_argument("--run-id", help="Override run id for hourly snapshot")
    snapshot_parser.add_argument("--run-date", help="Override run date (YYYY-MM-DD) for daily variant refresh")
    snapshot_parser.add_argument("--refresh-repricer", action="store_true", help="Force Repricer source refresh before run")
    snapshot_parser.add_argument("--no-refresh-repricer", action="store_true", help="Skip Repricer source refresh")
    snapshot_parser.add_argument("--city-id", help="Override Kaspi city id")
    snapshot_parser.add_argument("--limit", type=int, help="Override offer-view page limit")
    snapshot_parser.add_argument("--max-pages", type=int, help="Override max pages per offer")
    snapshot_parser.add_argument("--timeout-seconds", type=int, help="Override request timeout")
    snapshot_parser.add_argument("--hot-days", type=int, help="Override SQLite hot retention days")
    snapshot_parser.add_argument("--cold-days", type=int, help="Override Parquet cold retention days")
    snapshot_parser.add_argument("--headless", action="store_true", help="Run headless")
    snapshot_parser.add_argument("--headed", action="store_true", help="Run headful")

    pricelist_parser = subparsers.add_parser("kaspi-pricelist", help="Kaspi merchant pricelist operations")
    pricelist_subparsers = pricelist_parser.add_subparsers(dest="pricelist_command", required=True)

    download_parser = pricelist_subparsers.add_parser("download", help="Download current ACTIVE and ARCHIVE pricelists")
    download_parser.add_argument("--store", default="STORE-B", help="Target store name")
    download_parser.add_argument("--run-dir", help="Run directory for artifacts")
    download_parser.add_argument("--headless", action="store_true", help="Run headless")
    download_parser.add_argument("--headed", action="store_true", help="Run headful")

    upload_parser = pricelist_subparsers.add_parser("upload", help="Upload prepared pricelist workbook(s)")
    upload_parser.add_argument("--store", default="STORE-B", help="Target store name")
    upload_parser.add_argument("--run-dir", help="Run directory for artifacts")
    upload_parser.add_argument("--archive", help="ARCHIVE workbook path")
    upload_parser.add_argument("--active", help="ACTIVE workbook path")
    upload_parser.add_argument("--file", action="append", dest="files", help="Upload one or more workbook paths in explicit order")
    upload_parser.add_argument("--timeout-seconds", type=int, default=900, help="History poll timeout per file")
    upload_parser.add_argument("--processing-grace-seconds", type=int, default=900, help="Extra history poll grace after merchant processing starts")
    upload_parser.add_argument("--headless", action="store_true", help="Run headless")
    upload_parser.add_argument("--headed", action="store_true", help="Run headful")

    for sub_name in ("inspect", "build", "sync"):
        sub = pricelist_subparsers.add_parser(sub_name, help=f"{sub_name.capitalize()} merchant pricelists")
        sub.add_argument("--store", default="STORE-B", help="Target store name")
        sub.add_argument("--run-dir", help="Run directory for artifacts")
        sub.add_argument("--active-path", help="Existing ACTIVE workbook path")
        sub.add_argument("--archive-path", help="Existing ARCHIVE workbook path")
        sub.add_argument("--offers-book", default="exports/offers_book.xlsx", help="Offers book workbook path")
        sub.add_argument("--truth-xlsx", default="exports/repricer_unified_truth.xlsx", help="Repricer unified truth workbook path")
        sub.add_argument(
            "--intent",
            default="inspect-group-status",
            help="Intent: inspect-group-status, turn-on-in-stock, turn-off-oos, repair-suspicious-off, repair-suspicious-on, set-upload-prices",
        )
        sub.add_argument("--sku", help="Limit to a merchant SKU")
        sub.add_argument("--sku-key", help="Limit to an internal sku_key")
        sub.add_argument("--group-url", help="Limit to one resolved URL")
        sub.add_argument("--target-price", help="Explicit upload price for set-upload-prices")
        sub.add_argument("--timeout-seconds", type=int, default=900, help="History poll timeout per file when upload is requested")
        sub.add_argument("--processing-grace-seconds", type=int, default=900, help="Extra history poll grace after merchant processing starts")
        sub.add_argument("--headless", action="store_true", help="Run headless")
        sub.add_argument("--headed", action="store_true", help="Run headful")
        if sub_name == "sync":
            sub.add_argument("--upload", action="store_true", help="Upload generated ARCHIVE and ACTIVE workbooks")
            sub.add_argument("--verify-after-upload", action="store_true", help="Redownload and verify the selected rows after upload")

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
                    "Summary: products=%s min_price_updates=%s max_price_updates=%s current_price_updates=%s errors=%s",
                    result["products_visited"],
                    result["min_price_updates"],
                    result.get("max_price_updates", 0),
                    result.get("current_price_updates", 0),
                    result["errors"],
            )
        return 0 if result["errors"] == 0 else 4

    if args.command == "export":
        headless = True
        if args.headed:
            headless = False
        if args.headless:
            headless = True
        if args.task == "kaspi-archive-sales":
            summary = run_kaspi_archive_sales_export(
                start_date=args.start_date,
                end_date=args.end_date,
                block_days=args.block_days,
                window_count=args.window_count,
                store_labels=args.store_labels,
                downloads_dir=args.downloads_dir,
                output_root=args.output_root,
                copy_dst=args.copy_dst,
                timeout_seconds=args.timeout_seconds,
                retries=args.retries,
                dry_run=args.dry_run,
                confirm=args.confirm,
            )
            if args.json:
                print(json.dumps(summary, ensure_ascii=False, indent=2))
            else:
                logging.info(
                    "Archive sales export: status=%s expected=%s succeeded=%s failed=%s run_dir=%s copy_dir=%s",
                    summary.get("status"),
                    summary.get("exports_expected"),
                    summary.get("exports_succeeded"),
                    summary.get("exports_failed"),
                    summary.get("run_dir"),
                    summary.get("copy_dir"),
                )
            return 0 if summary.get("status") in {"success", "dry_run"} else 4
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

    if args.command == "kaspi-pricelist":
        headless = True
        if getattr(args, "headed", False):
            headless = False
        if getattr(args, "headless", False):
            headless = True

        env_path = Path(args.env_file) if args.env_file else Path("~/Docs/Autonomous_business/.env")
        creds = resolve_store_credentials(getattr(args, "store", "STORE-B"), env_path)

        if args.pricelist_command == "download":
            run_dir = Path(args.run_dir) if args.run_dir else default_run_dir(creds["store_name"])
            summary = run_kaspi_pricelist_download(
                store_name=creds["store_name"],
                email=creds["email"],
                password=creds["password"],
                run_dir=run_dir,
                headless=headless,
            )
            if args.json:
                print(json.dumps(summary, ensure_ascii=False, indent=2))
            else:
                logging.info(
                    "Kaspi pricelist download: store=%s status=%s run_dir=%s",
                    summary.get("store_name"),
                    summary.get("status"),
                    summary.get("run_dir"),
                )
            return 0 if summary.get("status") == "success" else 4

        if args.pricelist_command == "upload":
            run_dir = Path(args.run_dir) if args.run_dir else default_run_dir(creds["store_name"])
            try:
                file_paths = resolve_upload_file_paths(archive=args.archive, active=args.active, files=args.files)
            except ValueError as exc:
                if args.json:
                    print(json.dumps({"status": "invalid_args", "error": str(exc)}, ensure_ascii=False, indent=2))
                else:
                    logging.error(str(exc))
                return 2
            summary = run_kaspi_pricelist_upload(
                store_name=creds["store_name"],
                email=creds["email"],
                password=creds["password"],
                file_paths=file_paths,
                run_dir=run_dir,
                headless=headless,
                timeout_seconds=args.timeout_seconds,
                processing_grace_seconds=args.processing_grace_seconds,
            )
            if args.json:
                print(json.dumps(summary, ensure_ascii=False, indent=2))
            else:
                logging.info(
                    "Kaspi pricelist upload: store=%s status=%s run_dir=%s",
                    summary.get("store_name"),
                    summary.get("status"),
                    summary.get("run_dir"),
                )
            return 0 if summary.get("status") == "success" else 4

        if args.pricelist_command in {"inspect", "build", "sync"}:
            run_dir = Path(args.run_dir) if args.run_dir else default_run_dir(creds["store_name"])
            if args.active_path and args.archive_path:
                active_path = Path(args.active_path)
                archive_path = Path(args.archive_path)
                download_summary: dict[str, Any] = {"status": "skipped", "reason": "using_explicit_source_paths"}
            else:
                download_summary = run_kaspi_pricelist_download(
                    store_name=creds["store_name"],
                    email=creds["email"],
                    password=creds["password"],
                    run_dir=run_dir,
                    headless=headless,
                )
                if download_summary.get("status") != "success":
                    if args.json:
                        print(json.dumps(download_summary, ensure_ascii=False, indent=2))
                    else:
                        logging.error("Kaspi pricelist download failed: %s", download_summary.get("error", "unknown_error"))
                    return 4
                downloads_by_state = {row["sale_state"]: Path(row["saved_path"]) for row in download_summary.get("downloads", [])}
                active_path = downloads_by_state["ACTIVE"]
                archive_path = downloads_by_state["ARCHIVE"]

            snapshot = build_store_snapshot(
                active_path=active_path,
                archive_path=archive_path,
                store_name=creds["store_name"],
                offers_book_path=Path(args.offers_book),
                truth_xlsx_path=Path(args.truth_xlsx),
            )
            mutated_df = apply_intent(
                snapshot.snapshot_df,
                intent=args.intent,
                sku=args.sku or "",
                sku_key=args.sku_key or "",
                group_url=args.group_url or "",
                target_price=args.target_price or "",
            )
            build_summary = emit_outputs(snapshot, mutated_df=mutated_df, output_dir=run_dir / "outputs")
            summary: dict[str, Any] = {
                "store_name": creds["store_name"],
                "run_dir": str(run_dir),
                "download": download_summary,
                "build": build_summary,
            }
            if args.pricelist_command == "sync" and args.upload:
                upload_summary = run_kaspi_pricelist_upload(
                    store_name=creds["store_name"],
                    email=creds["email"],
                    password=creds["password"],
                    file_paths=[Path(build_summary["archive_output"]), Path(build_summary["active_output"])],
                    run_dir=run_dir / "upload",
                    headless=headless,
                    timeout_seconds=args.timeout_seconds,
                    processing_grace_seconds=args.processing_grace_seconds,
                )
                summary["upload"] = upload_summary
                if upload_summary.get("status") == "success" and args.verify_after_upload:
                    verify_download = run_kaspi_pricelist_download(
                        store_name=creds["store_name"],
                        email=creds["email"],
                        password=creds["password"],
                        run_dir=run_dir / "verify",
                        headless=headless,
                    )
                    summary["verify_download"] = verify_download
                    if verify_download.get("status") == "success":
                        after_paths = {row["sale_state"]: Path(row["saved_path"]) for row in verify_download.get("downloads", [])}
                        after_snapshot = build_store_snapshot(
                            active_path=after_paths["ACTIVE"],
                            archive_path=after_paths["ARCHIVE"],
                            store_name=creds["store_name"],
                            offers_book_path=Path(args.offers_book),
                            truth_xlsx_path=Path(args.truth_xlsx),
                        )
                        summary["verify"] = verify_uploaded_state(before_df=mutated_df, after_snapshot=after_snapshot)
            if args.json:
                print(json.dumps(summary, ensure_ascii=False, indent=2))
            else:
                logging.info(
                    "Kaspi pricelist %s: store=%s active=%s archive=%s review=%s",
                    args.pricelist_command,
                    creds["store_name"],
                    build_summary.get("active_rows"),
                    build_summary.get("archive_rows"),
                    build_summary.get("review_rows"),
                )
            if args.pricelist_command == "sync" and args.upload:
                if summary.get("upload", {}).get("status") != "success":
                    return 4
                if summary.get("verify", {}).get("mismatch_count", 0):
                    return 4
            return 0

        logging.error("Unknown kaspi-pricelist command")
        return 2

    if args.command == "snapshot":
        try:
            snapshot_cfg = load_kaspi_snapshot_config(args.config)
        except SnapshotConfigError as exc:
            logging.error(str(exc))
            return 2

        headless = True
        if args.headed:
            headless = False
        if args.headless:
            headless = True

        refresh_repricer = snapshot_cfg.refresh_repricer
        if args.refresh_repricer:
            refresh_repricer = True
        if args.no_refresh_repricer:
            refresh_repricer = False

        effective_city_id = str(args.city_id or snapshot_cfg.city_id)
        effective_limit = int(args.limit or snapshot_cfg.limit)
        effective_max_pages = int(args.max_pages or snapshot_cfg.max_pages)
        effective_timeout = int(args.timeout_seconds or snapshot_cfg.timeout_seconds)
        effective_hot_days = int(args.hot_days or snapshot_cfg.hot_days)
        effective_cold_days = int(args.cold_days or snapshot_cfg.cold_days)

        if args.task == "hourly":
            if refresh_repricer:
                export_repricer_items_to_sqlite(
                    config_path=snapshot_cfg.repricer_config_path,
                    output_path=snapshot_cfg.source_items_sqlite,
                    headless=headless,
                    include_all_rows=False,
                )
            summary = run_hourly_snapshot(
                source_items_sqlite=snapshot_cfg.source_items_sqlite,
                snapshot_sqlite=snapshot_cfg.snapshot_sqlite,
                parquet_root=snapshot_cfg.parquet_root,
                artifacts_root=snapshot_cfg.artifacts_root,
                store_ids=snapshot_cfg.stores,
                city_id=effective_city_id,
                run_id=args.run_id,
                max_pages=effective_max_pages,
                limit=effective_limit,
                timeout_seconds=effective_timeout,
                hot_days=effective_hot_days,
                cold_days=effective_cold_days,
            )
            if args.json:
                print(json.dumps(summary, ensure_ascii=False, indent=2))
            else:
                logging.info(
                    "Snapshot hourly: status=%s offers_total=%s succeeded=%s failed=%s rows=%s requests=%s",
                    summary.get("status"),
                    summary.get("offers_total"),
                    summary.get("offers_succeeded"),
                    summary.get("offers_failed"),
                    summary.get("rows_written"),
                    summary.get("api_requests"),
                )
            return 0 if summary.get("status") in {"success", "partial"} else 4

        if args.task == "daily-variants":
            offer_rows = load_offer_rows_from_sqlite(
                snapshot_cfg.source_items_sqlite,
                store_ids=snapshot_cfg.stores,
                on_sale_only=True,
            )
            offer_universe = collect_offer_universe(offer_rows)
            summary = run_daily_variant_refresh(
                sqlite_path=snapshot_cfg.snapshot_sqlite,
                offer_urls=sorted(offer_universe.keys()),
                run_date=args.run_date,
                timeout_seconds=effective_timeout,
            )
            if args.json:
                print(json.dumps(summary, ensure_ascii=False, indent=2))
            else:
                logging.info(
                    "Variant refresh: offers_total=%s succeeded=%s failed=%s members=%s",
                    summary.get("offers_total"),
                    summary.get("offers_succeeded"),
                    summary.get("offers_failed"),
                    summary.get("members_written"),
                )
            return 0 if int(summary.get("offers_failed", 0)) == 0 else 4

        if args.task == "prune-retention":
            summary = prune_snapshot_retention(
                sqlite_path=snapshot_cfg.snapshot_sqlite,
                parquet_root=snapshot_cfg.parquet_root,
                hot_days=effective_hot_days,
                cold_days=effective_cold_days,
            )
            if args.json:
                print(json.dumps(summary, ensure_ascii=False, indent=2))
            else:
                logging.info(
                    "Retention prune: sqlite_rows_deleted=%s parquet_dirs_deleted=%s",
                    summary.get("sqlite_rows_deleted"),
                    summary.get("parquet_dirs_deleted"),
                )
            return 0

    return 2
