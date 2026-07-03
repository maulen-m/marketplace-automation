from __future__ import annotations

import argparse
import json
import logging
import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

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
from .kaspi_marketing import default_marketing_run_dir, resolve_marketing_credentials, run_kaspi_marketing_fetch
from .kaspi_marketing_directapi_controls import (
    DEFAULT_RUN_ROOT as DEFAULT_DIRECTAPI_CONTROL_RUN_ROOT,
    DirectAPIControlPlanError,
    run_directapi_control,
)
from .kaspi_marketing_directapi_pipeline import (
    DEFAULT_MONITORING_RUN_ROOT as DEFAULT_MARKETING_MONITORING_RUN_ROOT,
    DEFAULT_PIPELINE_RUN_ROOT as DEFAULT_DIRECTAPI_PIPELINE_RUN_ROOT,
    KaspiMarketingPipelineError,
    run_campaign_mapper,
    run_monitoring_packet,
)
from .kaspi_marketing_line31 import (
    DEFAULT_ANALYSIS_RUN_ROOT as DEFAULT_LINE31_ANALYSIS_RUN_ROOT,
    DEFAULT_FETCH_RUN_ROOT as DEFAULT_LINE31_FETCH_RUN_ROOT,
    DEFAULT_RECOMMEND_RUN_ROOT as DEFAULT_LINE31_RECOMMEND_RUN_ROOT,
    DEFAULT_SCOPE_CONFIG as DEFAULT_LINE31_SCOPE_CONFIG,
    DEFAULT_SCOPE_RUN_ROOT as DEFAULT_LINE31_SCOPE_RUN_ROOT,
    LINE31MarketingError,
    analyze_line31_snapshots,
    resolve_line31_scope,
    run_line31_fetch,
    run_line31_recommend,
)
from .marketing_experiments import (
    DEFAULT_AB_ROOT,
    DEFAULT_CHANGE_LOG,
    DEFAULT_EXPERIMENT_ROOT,
    DEFAULT_MARKETING_DB,
    DEFAULT_WATCH_ROOT,
    build_experiment_report,
    close_experiment,
    active_events,
    dedupe_campaign_ids,
    log_change_event,
    now_local_text,
    run_marketing_watch,
)
from .offer_flow_registry import (
    DEFAULT_OFFER_FLOW_CONFIG,
    OFFER_FLOW_ARTIFACT_STATES,
    OFFER_FLOW_GOALS,
    OfferFlow,
    OfferFlowRegistryError,
    filter_offer_flows,
    load_offer_flow_registry,
    render_offer_flow_registry_markdown,
    select_offer_flow,
)
from .offer_run import (
    DEFAULT_OFFER_RUN_ROOT,
    OfferRunError,
    OfferRunRequest,
    build_offer_run_plan,
    execute_offer_run_plan,
    write_offer_run_bundle,
)
from .acmewear_bundle_activation import (
    DEFAULT_CALENDAR_PATH as DEFAULT_ACMEWEAR_BUNDLE_CALENDAR_PATH,
    DEFAULT_CAPTURE_PATH as DEFAULT_ACMEWEAR_BUNDLE_CAPTURE_PATH,
    DEFAULT_REGISTRY_PATH as DEFAULT_ACMEWEAR_BUNDLE_REGISTRY_PATH,
    BundleActivationError,
    build_acmewear_bundle_activation_pack,
)
from .acmewear_express_selfpickup_sidecar import (
    DEFAULT_ON_DEMAND_LABEL_APPROVAL_POLICY,
    DEFAULT_ACMEWEAR_KASPI_TOKEN_ENV,
    DEFAULT_RUN_ROOT as DEFAULT_ACMEWEAR_EXPRESS_SIDECAR_RUN_ROOT,
    DEFAULT_TELEGRAM_ALERT_CHAT_ID_ENV,
    DEFAULT_TELEGRAM_BOT_TOKEN_ENV,
    DEFAULT_TELEGRAM_LABEL_LEDGER_CSV,
    DEFAULT_TELEGRAM_PRINT_CHAT_ID_ENV,
    TELEGRAM_ALERT_CHAT_ID_ENV_ALIASES,
    TELEGRAM_BOT_TOKEN_ENV_ALIASES,
    TELEGRAM_PRINT_CHAT_ID_ENV_ALIASES,
    WAYBILL_TELEGRAM_BOT_TOKEN_ENV,
    KaspiOrderFetchError,
    acmewear_kaspi_token_env_candidates,
    resolve_present_env_alias,
    resolve_present_acmewear_kaspi_token_env,
    run_express_assembly_review,
    run_sidecar_init,
    run_operator_queue_review,
    run_operational_readiness_review,
    run_pickup_completion_api_execute,
    run_pickup_completion_review,
    run_prepare_express_label,
    run_manual_sidecar_intake,
    run_sidecar_row_update,
    run_selfpickup_sample_scan,
    run_shift_packet_review,
    run_telegram_label_from_pdf,
    run_telegram_alerts_from_queue,
    run_sidecar_build_from_file,
    run_sidecar_fetch_once,
    run_sidecar_watch_loop,
)
from .scheduled_checkpoints import (
    DEFAULT_CONFIG_PATH as DEFAULT_SCHEDULED_CHECKPOINT_CONFIG_PATH,
    DEFAULT_RUN_ROOT as DEFAULT_SCHEDULED_CHECKPOINT_RUN_ROOT,
    ScheduledCheckpointError,
    due_jobs,
    get_job,
    load_schedule_config,
    run_due_jobs,
    run_job,
)
from .experiment_dashboard import (
    DEFAULT_DASHBOARD_CACHE_DIR,
    DEFAULT_DASHBOARD_PORT,
    build_experiment_dashboard_payload,
    build_sync_heartbeat,
    parse_gap_report,
    plan_gap_backfill,
    read_sync_heartbeat,
    serve_dashboard,
    sync_experiment_dashboard,
    write_dashboard_artifacts,
    write_sync_heartbeat,
)
from .delivery_promise import (
    DEFAULT_DELIVERY_PROMISE_ROOT,
    build_delivery_capture,
    fetch_delivery_promise_capture,
    record_delivery_capture,
    record_delivery_watch_failure,
)
from .kaspi_merchant_common import resolve_store_credentials
from .kaspi_pending_trash_dispute import (
    FIXED_DISPUTE_COMMENT,
    default_run_dir as default_pending_dispute_run_dir,
    resolve_pending_merchant_code,
    run_kaspi_pending_trash_dispute,
)
from .kaspi_pricelist_download import default_run_dir, run_kaspi_pricelist_download
from .kaspi_pricelist_ops import apply_intent, build_store_snapshot, emit_outputs, verify_uploaded_state
from .kaspi_pricelist_safe_patch import (
    SAFE_ACTIVE_CONFIRM_PHRASE,
    SafeActivePatchError,
    build_safe_active_patch,
    verify_safe_active_upload,
)
from .kaspi_pricelist_upload import (
    resolve_upload_file_paths,
    run_kaspi_pricelist_history_detail,
    run_kaspi_pricelist_upload,
)
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


ALMATY_TZ = ZoneInfo("Asia/Almaty")


def _setup_logging(verbose: bool, quiet: bool) -> None:
    level = logging.INFO
    if verbose:
        level = logging.DEBUG
    if quiet:
        level = logging.WARNING
    logging.basicConfig(level=level, format="%(message)s")


def _resolve_acmewear_express_token_from_env(token_env: str) -> tuple[str, str, tuple[str, ...]]:
    candidates = acmewear_kaspi_token_env_candidates(token_env)
    effective_env = resolve_present_acmewear_kaspi_token_env(os.environ, token_env)
    token = os.environ.get(effective_env, "") if effective_env else ""
    return token, effective_env, candidates


def _resolve_env_alias_from_env(
    requested_env: str,
    aliases: tuple[str, ...],
    default_env: str,
) -> tuple[str, str]:
    effective_env = resolve_present_env_alias(os.environ, requested_env, aliases, default_env)
    value = os.environ.get(effective_env, "") if effective_env else ""
    return value, effective_env


def _resolve_created_bounds_with_lookback(
    *,
    created_from: str,
    created_to: str,
    lookback_hours: int,
    now: datetime | None = None,
) -> tuple[str, str]:
    """Fill missing Kaspi order creation bounds from a dynamic local lookback."""
    if lookback_hours < 1:
        raise ValueError("lookback_hours must be >= 1")
    if created_from and created_to:
        return created_from, created_to
    now_value = now or datetime.now(ALMATY_TZ)
    if now_value.tzinfo is None:
        now_value = now_value.replace(tzinfo=ALMATY_TZ)
    resolved_to = created_to or now_value.isoformat(timespec="seconds")
    resolved_from = created_from or (now_value - timedelta(hours=lookback_hours)).isoformat(timespec="seconds")
    return resolved_from, resolved_to


def _add_global_flags(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--env-file", help="Path to .env file", default=None)
    parser.add_argument("-v", "--verbose", action="store_true", help="Verbose logging")
    parser.add_argument("-q", "--quiet", action="store_true", help="Quiet logging")
    parser.add_argument("--json", action="store_true", help="JSON output")
    parser.add_argument("--plain", action="store_true", help="Plain text output")
    parser.add_argument("--no-input", action="store_true", help="Disable prompts")


def _resolve_dashboard_relative_date(value: str | None) -> str:
    text = str(value or "").strip()
    if text.lower() in {"today", "now"}:
        return datetime.now().strftime("%Y-%m-%d")
    return text


def _resolve_watch_health_window(args: argparse.Namespace) -> tuple[str, str]:
    date_to = _resolve_dashboard_relative_date(getattr(args, "date_to", None) or "today")
    if not date_to:
        raise ValueError("--date-to resolved to an empty value")
    try:
        date_to_dt = datetime.strptime(date_to, "%Y-%m-%d")
    except ValueError as exc:
        raise ValueError("--date-to must be YYYY-MM-DD or today") from exc

    raw_date_from = getattr(args, "date_from", None)
    if raw_date_from:
        date_from = _resolve_dashboard_relative_date(raw_date_from)
        try:
            datetime.strptime(date_from, "%Y-%m-%d")
        except ValueError as exc:
            raise ValueError("--date-from must be YYYY-MM-DD or today") from exc
    else:
        days = int(getattr(args, "date_from_days", 90) or 90)
        if days < 1:
            raise ValueError("--date-from-days must be >= 1")
        date_from = (date_to_dt - timedelta(days=days)).strftime("%Y-%m-%d")
    return date_from, date_to


def _write_json_file(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


def _offer_flow_rows(flows: list[OfferFlow]) -> list[list[str]]:
    rows: list[list[str]] = []
    for flow in flows:
        rows.append(
            [
                flow.flow_id,
                flow.goal,
                flow.artifact_state,
                ",".join(flow.stores),
                flow.write_surface,
                "yes" if flow.active else "no",
            ]
        )
    return rows


def _render_offer_flow_table(flows: list[OfferFlow]) -> str:
    headers = ["flow_id", "goal", "artifact_state", "stores", "write_surface", "active"]
    rows = _offer_flow_rows(flows)
    widths = [len(header) for header in headers]
    for row in rows:
        for idx, cell in enumerate(row):
            widths[idx] = max(widths[idx], len(cell))
    table_rows = [
        "  ".join(header.ljust(widths[idx]) for idx, header in enumerate(headers)),
        "  ".join("-" * widths[idx] for idx in range(len(headers))),
    ]
    for row in rows:
        table_rows.append("  ".join(cell.ljust(widths[idx]) for idx, cell in enumerate(row)))
    return "\n".join(table_rows)


def _render_offer_flow_plain_lines(flows: list[OfferFlow]) -> str:
    return "\n".join(
        "|".join(
            [
                flow.flow_id,
                flow.goal,
                flow.artifact_state,
                ",".join(flow.stores),
                flow.write_surface,
                "active" if flow.active else "inactive",
            ]
        )
        for flow in flows
    )


def _render_offer_flow_detail(flow: OfferFlow, *, selection_reasons: list[str] | None = None) -> str:
    lines = [
        f"flow_id: {flow.flow_id}",
        f"title: {flow.title}",
        f"active: {'yes' if flow.active else 'no'}",
        f"goal: {flow.goal}",
        f"artifact_state: {flow.artifact_state}",
        f"stores: {', '.join(flow.stores)}",
        f"owner_surface: {flow.owner_surface}",
        f"write_surface: {flow.write_surface}",
        f"primary_entrypoint: {flow.primary_entrypoint}",
        f"dry_run_supported: {'yes' if flow.dry_run_supported else 'no'}",
        f"confirm_required: {'yes' if flow.confirm_required else 'no'}",
        f"verify_required: {'yes' if flow.verify_required else 'no'}",
        f"repricer_followup: {flow.repricer_followup}",
        f"summary: {flow.summary}",
    ]
    if selection_reasons:
        lines.append("selection_reasons:")
        lines.extend([f"  - {item}" for item in selection_reasons])
    if flow.command_examples:
        lines.append("command_examples:")
        lines.extend([f"  - {item}" for item in flow.command_examples])
    if flow.preflight_checks:
        lines.append("preflight_checks:")
        lines.extend([f"  - {item}" for item in flow.preflight_checks])
    if flow.success_checks:
        lines.append("success_checks:")
        lines.extend([f"  - {item}" for item in flow.success_checks])
    if flow.stoplines:
        lines.append("stoplines:")
        lines.extend([f"  - {item}" for item in flow.stoplines])
    if flow.fallback_flow_ids:
        lines.append("fallback_flow_ids:")
        lines.extend([f"  - {item}" for item in flow.fallback_flow_ids])
    if flow.docs_refs:
        lines.append("docs_refs:")
        lines.extend([f"  - {item}" for item in flow.docs_refs])
    if flow.skill_refs:
        lines.append("skill_refs:")
        lines.extend([f"  - {item}" for item in flow.skill_refs])
    if flow.artifact_roots:
        lines.append("artifact_roots:")
        lines.extend([f"  - {item}" for item in flow.artifact_roots])
    if flow.notes:
        lines.append("notes:")
        lines.extend([f"  - {item}" for item in flow.notes])
    return "\n".join(lines)


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
    run_parser.add_argument(
        "--competition-scope-map",
        help="Optional audit CSV that maps exact Repricer rows to resolved product scope and floor policy",
    )
    run_parser.add_argument(
        "--only-competitor-action",
        action="append",
        help="Limit Repricer competitor API changes to one or more actions, comma-separated or repeated",
    )
    run_parser.add_argument(
        "--only-competitor-reason",
        action="append",
        help="Limit Repricer competitor API changes to one or more reasons, comma-separated or repeated",
    )
    run_parser.add_argument(
        "--only-row-id",
        action="append",
        help="Limit Repricer competitor API changes to exact Repricer row id(s), comma-separated or repeated",
    )
    run_parser.add_argument(
        "--only-merchant-sku",
        action="append",
        help="Limit Repricer competitor API changes to exact merchant SKU(s), comma-separated or repeated",
    )
    run_parser.add_argument(
        "--only-competitor-mid",
        action="append",
        help="Limit Repricer competitor API changes to exact competitor merchant id(s), comma-separated or repeated",
    )

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

    pending_dispute_parser = subparsers.add_parser(
        "kaspi-pending-dispute",
        help="Replay Kaspi rejected-offer disputes for pending TRASH rows",
    )
    pending_dispute_parser.add_argument("--store", default="ACMEWEAR", help="Target store name")
    pending_dispute_parser.add_argument("--merchant-code", default="", help="Override merchant code")
    pending_dispute_parser.add_argument("--run-dir", help="Run directory for artifacts")
    pending_dispute_parser.add_argument("--comment", default=FIXED_DISPUTE_COMMENT, help="Dispute comment text")
    pending_dispute_parser.add_argument("--page-size", type=int, default=100, help="Trash list page size")
    pending_dispute_parser.add_argument("--verify-timeout-seconds", type=int, default=90, help="Post-submit verify timeout")
    pending_dispute_parser.add_argument("--verify-poll-seconds", type=int, default=5, help="Post-submit verify poll interval")
    pending_dispute_parser.add_argument("--confirm", action="store_true", help="Confirm write actions")
    pending_dispute_parser.add_argument("--headless", action="store_true", help="Run headless")
    pending_dispute_parser.add_argument("--headed", action="store_true", help="Run headful")
    pending_dispute_parser.add_argument("--skip-ui-verify", action="store_true", help="Skip final UI spot checks")

    offer_flow_parser = subparsers.add_parser("offer-flow", help="Inspect and select executable Kaspi offer flows")
    offer_flow_subparsers = offer_flow_parser.add_subparsers(dest="offer_flow_command", required=True)

    offer_flow_list = offer_flow_subparsers.add_parser("list", help="List configured offer flows")
    offer_flow_list.add_argument("--config", default=str(DEFAULT_OFFER_FLOW_CONFIG), help="Offer flow registry YAML path")
    offer_flow_list.add_argument("--goal", choices=OFFER_FLOW_GOALS, help="Filter by goal")
    offer_flow_list.add_argument("--artifact-state", choices=OFFER_FLOW_ARTIFACT_STATES, help="Filter by artifact state")
    offer_flow_list.add_argument("--store", help="Filter by store name")
    offer_flow_list.add_argument("--all", action="store_true", help="Include inactive flows")

    offer_flow_show = offer_flow_subparsers.add_parser("show", help="Show one configured offer flow")
    offer_flow_show.add_argument("flow_id", help="Offer flow id")
    offer_flow_show.add_argument("--config", default=str(DEFAULT_OFFER_FLOW_CONFIG), help="Offer flow registry YAML path")

    offer_flow_select = offer_flow_subparsers.add_parser("select", help="Select the best flow for a goal and artifact state")
    offer_flow_select.add_argument("--config", default=str(DEFAULT_OFFER_FLOW_CONFIG), help="Offer flow registry YAML path")
    offer_flow_select.add_argument("--goal", choices=OFFER_FLOW_GOALS, required=True, help="Desired workflow goal")
    offer_flow_select.add_argument(
        "--artifact-state",
        choices=OFFER_FLOW_ARTIFACT_STATES,
        required=True,
        help="Current artifact state",
    )
    offer_flow_select.add_argument("--store", help="Store name filter")
    offer_flow_select.add_argument("--all", action="store_true", help="Include inactive flows")

    offer_flow_validate = offer_flow_subparsers.add_parser("validate", help="Validate the offer flow registry")
    offer_flow_validate.add_argument("--config", default=str(DEFAULT_OFFER_FLOW_CONFIG), help="Offer flow registry YAML path")

    offer_flow_render = offer_flow_subparsers.add_parser("render-doc", help="Render a markdown doc from the offer flow registry")
    offer_flow_render.add_argument("--config", default=str(DEFAULT_OFFER_FLOW_CONFIG), help="Offer flow registry YAML path")
    offer_flow_render.add_argument("--out", help="Markdown output path")

    offer_run_parser = subparsers.add_parser("offer-run", help="Create a standardized run bundle from the offer-flow registry")
    offer_run_parser.add_argument("--config", default=str(DEFAULT_OFFER_FLOW_CONFIG), help="Offer flow registry YAML path")
    offer_run_parser.add_argument("--flow", dest="flow_id", help="Explicit flow id to use")
    offer_run_parser.add_argument("--goal", choices=OFFER_FLOW_GOALS, help="Select by workflow goal")
    offer_run_parser.add_argument("--artifact-state", choices=OFFER_FLOW_ARTIFACT_STATES, help="Select by current artifact state")
    offer_run_parser.add_argument("--store", help="Store name")
    offer_run_parser.add_argument("--zip-queue-path", help="Path to ordered ZIP queue descriptor or handoff-owned queue artifact")
    offer_run_parser.add_argument("--handoff-path", help="Path to a handoff doc or packet")
    offer_run_parser.add_argument("--workbook-path", help="Path to reviewed workbook for workbook-based flows")
    offer_run_parser.add_argument("--active-path", help="Path to ACTIVE workbook when needed")
    offer_run_parser.add_argument("--archive-path", help="Path to ARCHIVE workbook when needed")
    offer_run_parser.add_argument("--session-doc-path", help="Path to session memo or closeout doc")
    offer_run_parser.add_argument("--public-urls-path", help="Path to captured public URL truth")
    offer_run_parser.add_argument("--run-root", default=str(DEFAULT_OFFER_RUN_ROOT), help="Offer-run bundle root directory")
    offer_run_parser.add_argument("--note", default="", help="Operator note for the run bundle")
    offer_run_parser.add_argument("--allow-inactive", action="store_true", help="Allow inactive template flows")
    offer_run_parser.add_argument("--dry-run", action="store_true", help="Stage this run as a dry-run intent")
    offer_run_parser.add_argument("--confirm", action="store_true", help="Stage this run as a confirmed-write intent")
    offer_run_parser.add_argument("--verify", action="store_true", help="Require post-write verification in the staged run")
    offer_run_parser.add_argument("--dispatch", action="store_true", help="Execute the first supported live lane after staging the run bundle")
    offer_run_parser.add_argument(
        "--intent",
        help="Flow-specific intent; currently required for dispatched kaspi-pricelist-sync",
    )
    offer_run_parser.add_argument("--sku", help="Optional merchant SKU filter for supported flows")
    offer_run_parser.add_argument("--sku-key", help="Optional sku_key filter for supported flows")
    offer_run_parser.add_argument("--group-url", help="Optional resolved group URL filter for supported flows")
    offer_run_parser.add_argument("--target-price", help="Optional target price for supported flows")
    offer_run_parser.add_argument("--offers-book", default="exports/offers_book.xlsx", help="Offers book path for supported flows")
    offer_run_parser.add_argument("--truth-xlsx", default="exports/repricer_unified_truth.xlsx", help="Unified truth workbook path for supported flows")
    offer_run_parser.add_argument("--timeout-seconds", type=int, default=900, help="Upload or history timeout for supported flows")
    offer_run_parser.add_argument(
        "--processing-grace-seconds",
        type=int,
        default=900,
        help="Extra merchant processing grace for supported flows",
    )
    offer_run_parser.add_argument("--headless", action="store_true", help="Dispatch supported flows in headless mode")
    offer_run_parser.add_argument("--headed", action="store_true", help="Dispatch supported flows in headed mode")

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
    upload_parser.add_argument(
        "--confirm-raw-pricelist-upload",
        action="store_true",
        help="Required for legacy raw workbook upload; prefer safe-active-patch for price/stock changes",
    )
    upload_parser.add_argument("--headless", action="store_true", help="Run headless")
    upload_parser.add_argument("--headed", action="store_true", help="Run headful")

    history_detail_parser = pricelist_subparsers.add_parser(
        "history-detail",
        help="Read one Kaspi pricelist upload history detail page without mutating merchant state",
    )
    history_detail_parser.add_argument("--store", default="STORE-B", help="Target store name")
    history_detail_parser.add_argument("--run-dir", help="Run directory for artifacts")
    history_detail_parser.add_argument("--detail-ref", required=True, help="History detail id, href, or full detail URL")
    history_detail_parser.add_argument(
        "--detail-filter",
        choices=["all", "unrecognized", "restricted", "errors", "warnings"],
        default="",
        help="Optional read-only result filter to click before saving the detail page",
    )
    history_detail_parser.add_argument(
        "--download-result-excel",
        action="store_true",
        help="Download the read-only row-level result workbook from the history detail page",
    )
    history_detail_parser.add_argument("--headless", action="store_true", help="Run headless")
    history_detail_parser.add_argument("--headed", action="store_true", help="Run headful")

    safe_patch_parser = pricelist_subparsers.add_parser(
        "safe-active-patch",
        help="Build a full ACTIVE-state pricelist patch with full-sale-surface preservation guards",
    )
    safe_patch_parser.add_argument("--store", default="STORE-B", help="Target store name")
    safe_patch_parser.add_argument("--run-dir", help="Run directory for artifacts")
    safe_patch_parser.add_argument("--active-path", help="Existing ACTIVE workbook path; downloads fresh if omitted with archive-path")
    safe_patch_parser.add_argument("--archive-path", help="Existing ARCHIVE workbook path; downloads fresh if omitted with active-path")
    safe_patch_parser.add_argument("--updates-csv", required=True, help="CSV with SKU and authorized price/PP/preorder values")
    safe_patch_parser.add_argument("--expected-active-before", type=int, help="Fail unless source ACTIVE row count matches")
    safe_patch_parser.add_argument("--expected-active-after", type=int, help="Fail unless output ACTIVE row count matches")
    safe_patch_parser.add_argument("--allow-activate-from-archive", action="store_true", help="Allow target rows to be appended from ARCHIVE into full ACTIVE output")
    safe_patch_parser.add_argument("--restriction-ledger", help="Optional launchability ledger; blocks rows classified as platform-restricted/risk")
    safe_patch_parser.add_argument("--restriction-probe-approval", help="Optional owner approval JSON allowing exact restricted SKUs as deliberate restriction probes")
    safe_patch_parser.add_argument("--apply", action="store_true", help="Upload the generated full ACTIVE workbook after all guards pass")
    safe_patch_parser.add_argument("--confirm", help=f"Required phrase for --apply: {SAFE_ACTIVE_CONFIRM_PHRASE}")
    safe_patch_parser.add_argument("--verify-after-upload", action="store_true", help="Redownload ACTIVE/ARCHIVE and verify the full ACTIVE state after upload")
    safe_patch_parser.add_argument("--timeout-seconds", type=int, default=900, help="History poll timeout per file")
    safe_patch_parser.add_argument("--processing-grace-seconds", type=int, default=900, help="Extra history poll grace after merchant processing starts")
    safe_patch_parser.add_argument("--headless", action="store_true", help="Run headless")
    safe_patch_parser.add_argument("--headed", action="store_true", help="Run headful")

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
            help="Intent: inspect-group-status, turn-on-in-stock, turn-off-oos, repair-suspicious-off, repair-suspicious-on, repair-active-stock-units, set-upload-prices",
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
            sub.add_argument(
                "--confirm-legacy-archive-active-upload",
                action="store_true",
                help="Required for legacy archive-plus-active upload; safe-active-patch is the preferred live lane",
            )

    acmewear_bundles_parser = subparsers.add_parser("acmewear-bundles", help="ACMEWEAR child-bundle launch helpers")
    acmewear_bundles_subparsers = acmewear_bundles_parser.add_subparsers(dest="acmewear_bundles_command", required=True)
    acmewear_activation_build = acmewear_bundles_subparsers.add_parser(
        "activation-build",
        help="Build a review-only ST bundle price/stock activation workbook pack",
    )
    acmewear_activation_build.add_argument("--active-path", required=True, help="Fresh or explicit ACMEWEAR ACTIVE workbook")
    acmewear_activation_build.add_argument("--archive-path", required=True, help="Fresh or explicit ACMEWEAR ARCHIVE workbook")
    acmewear_activation_build.add_argument("--run-dir", help="Run directory for review artifacts")
    acmewear_activation_build.add_argument(
        "--calendar-path",
        default=str(DEFAULT_ACMEWEAR_BUNDLE_CALENDAR_PATH),
        help="Bundle experiment calendar YAML",
    )
    acmewear_activation_build.add_argument(
        "--registry-path",
        default=str(DEFAULT_ACMEWEAR_BUNDLE_REGISTRY_PATH),
        help="Bundle registry YAML",
    )
    acmewear_activation_build.add_argument(
        "--capture-path",
        default=str(DEFAULT_ACMEWEAR_BUNDLE_CAPTURE_PATH),
        help="Publication capture CSV",
    )
    acmewear_activation_build.add_argument(
        "--stock-warehouse",
        default="PP1",
        help="Warehouse column receiving launch stock caps; default PP1",
    )
    acmewear_activation_build.add_argument(
        "--image-moderation-cleared",
        action="store_true",
        help="Mark image moderation as live-verified in the review summary",
    )

    acmewear_express_parser = subparsers.add_parser(
        "acmewear-express-sidecar",
        help="ACMEWEAR PP2 Express/self-pickup sidecar helpers",
    )
    acmewear_express_subparsers = acmewear_express_parser.add_subparsers(
        dest="acmewear_express_command",
        required=True,
    )
    acmewear_express_init = acmewear_express_subparsers.add_parser(
        "init-sidecar",
        help="Create or validate the repo-local ACMEWEAR PP2 Express/self-pickup sidecar CSV",
    )
    acmewear_express_init.add_argument(
        "--sidecar-csv",
        default="data/acmewear_express_selfpickup_sidecar.csv",
        help="Repo-local sidecar CSV to create or validate",
    )
    acmewear_express_init.add_argument("--run-dir", help="Run directory for init review artifacts")
    acmewear_express_init.add_argument(
        "--schema-csv",
        default="",
        help="Optional path to write a generated schema copy; stable schema is config/schemas/acmewear_express_selfpickup_sidecar_v1.csv",
    )
    acmewear_express_build = acmewear_express_subparsers.add_parser(
        "build",
        help="Build repo-local sidecar rows and Telegram alert queue from redacted order JSON/JSONL",
    )
    acmewear_express_build.add_argument("--input-json", required=True, help="Redacted/minimized order JSON or JSONL input")
    acmewear_express_build.add_argument("--run-dir", help="Run directory for sidecar review artifacts")
    acmewear_express_build.add_argument("--sidecar-csv", help="Optional repo-local sidecar CSV to merge with")
    acmewear_express_build.add_argument(
        "--update-sidecar",
        action="store_true",
        help="Persist merged rows into --sidecar-csv; otherwise review-only artifacts are written",
    )
    acmewear_express_build.add_argument(
        "--persist-actionable-only",
        action="store_true",
        help="With --update-sidecar, persist only high-confidence Express/self-pickup rows; review artifacts still include all rows",
    )
    acmewear_express_build.add_argument("--detected-at", default="", help="Fixed detected_at timestamp for reproducible runs")
    acmewear_express_build.add_argument("--local-ref-prefix", default="order", help="Local reference prefix for generated rows")
    acmewear_express_manual = acmewear_express_subparsers.add_parser(
        "manual-intake",
        help="Manually add/review one Express or PP2 self-pickup row in the local sidecar without external writes",
    )
    acmewear_express_manual.add_argument("--delivery-kind", required=True, choices=["EXPRESS_DELIVERY", "SELLER_SELF_PICKUP"])
    acmewear_express_manual.add_argument("--local-ref", required=True, help="Stable non-PII local reference for the order")
    acmewear_express_manual.add_argument("--order-hash", default="", help="sha256:... order hash; raw order ids are rejected")
    acmewear_express_manual.add_argument(
        "--order-ref-env",
        default="",
        help="Optional env var containing raw order ref to hash in-memory only; raw value is never persisted",
    )
    acmewear_express_manual.add_argument("--run-dir", help="Run directory for manual-intake artifacts")
    acmewear_express_manual.add_argument("--sidecar-csv", help="Repo-local sidecar CSV to merge with")
    acmewear_express_manual.add_argument(
        "--update-sidecar",
        action="store_true",
        help="Persist merged rows into --sidecar-csv; otherwise review-only artifacts are written",
    )
    acmewear_express_manual.add_argument("--detected-at", default="", help="Fixed detected_at timestamp for reproducible runs")
    acmewear_express_manual.add_argument("--created-at", default="", help="Order creation timestamp when known")
    acmewear_express_manual.add_argument("--delivery-slot-label", default="", help="Express slot label, for example '12:00 - 14:00'")
    acmewear_express_manual.add_argument("--courier-planning-at", default="", help="Express courier planning timestamp when known")
    acmewear_express_manual.add_argument("--sku-key", default="", help="Canonical SKU key when known")
    acmewear_express_manual.add_argument("--merchant-article", default="", help="Kaspi merchant article when known")
    acmewear_express_manual.add_argument("--ordered-size", default="", help="Ordered platform/display size")
    acmewear_express_manual.add_argument("--height-cm", default="", help="Customer height, if already confirmed")
    acmewear_express_manual.add_argument("--weight-kg", default="", help="Customer weight, if already confirmed")
    acmewear_express_manual.add_argument("--my-size", default="", help="Resolved MY_SIZE from normal size rules")
    acmewear_express_manual.add_argument("--owner-size-override", default="", help="Owner-approved final size override")
    acmewear_express_manual.add_argument("--size-source", default="", help="Size source note, for example 'owner_telegram_call'")
    acmewear_express_manual.add_argument("--waybill-present", action="store_true", help="Already-present waybill was observed")
    acmewear_express_manual.add_argument("--cropped-label-path", default="", help="Repo-local cropped label path, if already produced")
    acmewear_express_manual.add_argument("--cropped-label-sha256", default="", help="sha256:... hash of cropped label PDF")
    acmewear_express_manual.add_argument("--telegram-alert-status", default="", help="Optional existing alert status to carry forward")
    acmewear_express_manual.add_argument("--telegram-label-status", default="", help="Optional existing label status to carry forward")
    acmewear_express_manual.add_argument("--printed-status", default="", help="Optional existing print status to carry forward")
    acmewear_express_manual.add_argument("--close-status", default="OPEN", help="OPEN, CLOSED, CANCELLED, or MANUAL_ONLY")
    acmewear_express_manual.add_argument(
        "--build-shift-packet",
        action="store_true",
        help="After repo-local sidecar update, build a warehouse shift packet from the durable sidecar",
    )
    acmewear_express_manual.add_argument(
        "--shift-run-root",
        default="runs/acmewear_express_selfpickup_sidecar",
        help="Run root used by the generated shift-packet readiness matrix",
    )
    acmewear_express_update = acmewear_express_subparsers.add_parser(
        "update-row",
        help="Patch operator-known facts onto one existing local sidecar row without losing API evidence",
    )
    acmewear_express_update.add_argument("--sidecar-csv", required=True, help="Repo-local sidecar CSV to update")
    acmewear_express_update.add_argument("--run-dir", help="Run directory for update artifacts")
    acmewear_express_update.add_argument("--order-hash", default="", help="sha256:... order hash selector")
    acmewear_express_update.add_argument("--local-ref", default="", help="Local non-PII row selector")
    acmewear_express_update.add_argument("--sidecar-idempotency-key", default="", help="Exact sidecar idempotency key selector")
    acmewear_express_update.add_argument("--ordered-size", default="", help="Ordered/display size to record when confirmed")
    acmewear_express_update.add_argument("--height-cm", default="", help="Customer height, if already confirmed")
    acmewear_express_update.add_argument("--weight-kg", default="", help="Customer weight, if already confirmed")
    acmewear_express_update.add_argument("--my-size", default="", help="Resolved MY_SIZE from normal size rules")
    acmewear_express_update.add_argument("--owner-size-override", default="", help="Owner-approved final size override")
    acmewear_express_update.add_argument("--size-source", default="", help="Size source note, for example 'owner_call'")
    acmewear_express_update.add_argument("--cropped-label-path", default="", help="Repo-local cropped label path")
    acmewear_express_update.add_argument("--cropped-label-sha256", default="", help="sha256:... hash of cropped label PDF")
    acmewear_express_update.add_argument("--telegram-alert-status", default="", help="Alert status to record")
    acmewear_express_update.add_argument("--telegram-label-status", default="", help="Label-send status to record")
    acmewear_express_update.add_argument("--printed-status", default="", help="Print status to record")
    acmewear_express_update.add_argument("--close-status", default="", help="OPEN, CLOSED, CANCELLED, or MANUAL_ONLY")
    acmewear_express_update.add_argument(
        "--build-shift-packet",
        action="store_true",
        help="After repo-local row update, build a warehouse shift packet from the durable sidecar",
    )
    acmewear_express_update.add_argument(
        "--shift-run-root",
        default="runs/acmewear_express_selfpickup_sidecar",
        help="Run root used by the generated shift-packet readiness matrix",
    )
    acmewear_express_fetch = acmewear_express_subparsers.add_parser(
        "fetch-once",
        help="GET-only Kaspi order-list fetch, minimize to sidecar artifacts, and never persist raw payloads",
    )
    acmewear_express_fetch.add_argument(
        "--token-env",
        default=DEFAULT_ACMEWEAR_KASPI_TOKEN_ENV,
        help="Environment variable containing the ACMEWEAR Kaspi API token; current default accepts legacy ACMEWEAR_KASPI_API_TOKEN as an alias",
    )
    acmewear_express_fetch.add_argument("--run-dir", help="Run directory for sidecar review artifacts")
    acmewear_express_fetch.add_argument("--sidecar-csv", help="Optional repo-local sidecar CSV to merge with")
    acmewear_express_fetch.add_argument(
        "--update-sidecar",
        action="store_true",
        help="Persist merged rows into --sidecar-csv; otherwise review-only artifacts are written",
    )
    acmewear_express_fetch.add_argument(
        "--persist-actionable-only",
        action="store_true",
        help="With --update-sidecar, persist only high-confidence Express/self-pickup rows; review artifacts still include all rows",
    )
    acmewear_express_fetch.add_argument("--detected-at", default="", help="Fixed detected_at timestamp for reproducible runs")
    acmewear_express_fetch.add_argument("--local-ref-prefix", default="api", help="Local reference prefix for generated rows")
    acmewear_express_fetch.add_argument("--state", action="append", dest="states", help="Order state filter; repeatable")
    acmewear_express_fetch.add_argument("--status", action="append", dest="statuses", help="Order status filter; repeatable")
    acmewear_express_fetch.add_argument("--delivery-type", action="append", dest="delivery_types", help="Delivery type filter; repeatable")
    acmewear_express_fetch.add_argument("--created-from", default="", help="Creation date lower bound; ISO datetime/date or epoch ms")
    acmewear_express_fetch.add_argument("--created-to", default="", help="Creation date upper bound; ISO datetime/date or epoch ms")
    acmewear_express_fetch.add_argument(
        "--lookback-hours",
        type=int,
        default=None,
        help="Optional dynamic lookback used only when one or both created bounds are omitted",
    )
    acmewear_express_fetch.add_argument("--page-size", type=int, default=100, help="Orders per page, max 100")
    acmewear_express_fetch.add_argument("--max-pages", type=int, default=5, help="Maximum pages per filter combination")
    acmewear_express_fetch.add_argument("--timeout-seconds", type=int, default=20, help="HTTP timeout in seconds")
    acmewear_express_watch = acmewear_express_subparsers.add_parser(
        "watch",
        help="Repeat GET-only order-list fetches and write a repo-local sidecar heartbeat",
    )
    acmewear_express_watch.add_argument(
        "--token-env",
        default=DEFAULT_ACMEWEAR_KASPI_TOKEN_ENV,
        help="Environment variable containing the ACMEWEAR Kaspi API token; current default accepts legacy ACMEWEAR_KASPI_API_TOKEN as an alias",
    )
    acmewear_express_watch.add_argument("--run-dir", help="Run directory for watch artifacts")
    acmewear_express_watch.add_argument("--sidecar-csv", help="Optional repo-local sidecar CSV to merge with")
    acmewear_express_watch.add_argument(
        "--update-sidecar",
        action="store_true",
        help="Persist merged rows into --sidecar-csv; otherwise review-only artifacts are written",
    )
    acmewear_express_watch.add_argument(
        "--persist-actionable-only",
        action="store_true",
        help="With --update-sidecar, persist only high-confidence Express/self-pickup rows; review artifacts still include all rows",
    )
    acmewear_express_watch.add_argument("--detected-at", default="", help="Fixed detected_at timestamp for reproducible runs")
    acmewear_express_watch.add_argument("--local-ref-prefix", default="watch", help="Local reference prefix for generated rows")
    acmewear_express_watch.add_argument("--state", action="append", dest="states", help="Order state filter; repeatable")
    acmewear_express_watch.add_argument("--status", action="append", dest="statuses", help="Order status filter; repeatable")
    acmewear_express_watch.add_argument("--delivery-type", action="append", dest="delivery_types", help="Delivery type filter; repeatable")
    acmewear_express_watch.add_argument("--created-from", default="", help="Creation date lower bound; ISO datetime/date or epoch ms")
    acmewear_express_watch.add_argument("--created-to", default="", help="Creation date upper bound; ISO datetime/date or epoch ms")
    acmewear_express_watch.add_argument("--lookback-hours", type=int, default=24, help="Dynamic lookback when created bounds are omitted")
    acmewear_express_watch.add_argument("--cycles", type=int, default=1, help="Number of fetch cycles to run")
    acmewear_express_watch.add_argument("--interval-seconds", type=int, default=0, help="Delay between cycles")
    acmewear_express_watch.add_argument("--page-size", type=int, default=100, help="Orders per page, max 100")
    acmewear_express_watch.add_argument("--max-pages", type=int, default=5, help="Maximum pages per filter combination")
    acmewear_express_watch.add_argument("--timeout-seconds", type=int, default=20, help="HTTP timeout in seconds")
    acmewear_express_selfpickup_scan = acmewear_express_subparsers.add_parser(
        "selfpickup-scan",
        help="Read a minimized sidecar CSV and prove whether any true PP2 seller self-pickup sample exists",
    )
    acmewear_express_selfpickup_scan.add_argument(
        "--input-csv",
        required=True,
        help="Minimized incoming_sidecar_rows.csv or durable sidecar CSV",
    )
    acmewear_express_selfpickup_scan.add_argument("--run-dir", help="Run directory for self-pickup sample scan artifacts")
    acmewear_express_telegram = acmewear_express_subparsers.add_parser(
        "telegram-alert",
        help="Dry-run or explicitly send Telegram alerts from an ACMEWEAR Express alert queue",
    )
    acmewear_express_telegram.add_argument("--alert-queue-csv", required=True, help="telegram_alert_queue_review.csv path")
    acmewear_express_telegram.add_argument("--ledger-csv", required=True, help="Persistent Telegram alert ledger CSV path")
    acmewear_express_telegram.add_argument("--run-dir", help="Run directory for Telegram alert artifacts")
    acmewear_express_telegram.add_argument(
        "--send",
        action="store_true",
        help="Actually call Telegram sendMessage; default is dry-run artifact generation only",
    )
    acmewear_express_telegram.add_argument(
        "--bot-token-env",
        default=DEFAULT_TELEGRAM_BOT_TOKEN_ENV,
        help="Env var containing Telegram bot token for --send; default also accepts generic AB Telegram token aliases",
    )
    acmewear_express_telegram.add_argument(
        "--chat-id-env",
        default=DEFAULT_TELEGRAM_ALERT_CHAT_ID_ENV,
        help="Env var containing Telegram chat id for --send; default also accepts generic AB Telegram chat aliases",
    )
    acmewear_express_telegram.add_argument("--chat-config-ref", default="ACMEWEAR_EXPRESS_TELEGRAM_CHAT")
    acmewear_express_telegram.add_argument("--max-alerts", type=int, default=0, help="Optional maximum rows to process")
    acmewear_express_telegram.add_argument("--timeout-seconds", type=int, default=20, help="HTTP timeout in seconds")
    acmewear_express_prepare_label = acmewear_express_subparsers.add_parser(
        "prepare-label",
        help="Fast path: fetch one active ACMEWEAR Express waybill, crop 75x120mm label, and optionally send to Telegram",
    )
    acmewear_express_prepare_label.add_argument(
        "--order-code",
        default="",
        help="Optional raw Kaspi order code; used in memory only and never written to artifacts",
    )
    acmewear_express_prepare_label.add_argument(
        "--order-hash",
        default="",
        help="Optional sha256:... order hash selector for the current active queue",
    )
    acmewear_express_prepare_label.add_argument(
        "--owner-size",
        default="",
        help="Optional owner-selected final size to include in the Telegram caption, for example 2XL",
    )
    acmewear_express_prepare_label.add_argument(
        "--require-size",
        action="store_true",
        help="Block label preparation unless owner size or MY_SIZE is already present; default keeps size optional",
    )
    acmewear_express_prepare_label.add_argument(
        "--send-telegram",
        action="store_true",
        help="Send the cropped label to the standing-approved AB waybill Telegram group; default is dry-run",
    )
    acmewear_express_prepare_label.add_argument(
        "--force-resend",
        action="store_true",
        help="Allow a new Telegram send even if this order hash already has a SENT label ledger row",
    )
    acmewear_express_prepare_label.add_argument(
        "--standing-approval",
        default=str(DEFAULT_ON_DEMAND_LABEL_APPROVAL_POLICY),
        help="Persistent owner approval policy JSON for this on-demand label lane",
    )
    acmewear_express_prepare_label.add_argument(
        "--ledger-csv",
        default=str(DEFAULT_TELEGRAM_LABEL_LEDGER_CSV),
        help="Persistent ignored Telegram label ledger CSV",
    )
    acmewear_express_prepare_label.add_argument(
        "--token-env",
        default=DEFAULT_ACMEWEAR_KASPI_TOKEN_ENV,
        help="Env var containing ACMEWEAR Kaspi API token; aliases are accepted",
    )
    acmewear_express_prepare_label.add_argument(
        "--bot-token-env",
        default=WAYBILL_TELEGRAM_BOT_TOKEN_ENV,
        help="Env var containing Telegram waybill bot token for --send-telegram",
    )
    acmewear_express_prepare_label.add_argument(
        "--chat-id-env",
        default=DEFAULT_TELEGRAM_PRINT_CHAT_ID_ENV,
        help="Env var containing Telegram waybill chat id for --send-telegram",
    )
    acmewear_express_prepare_label.add_argument("--chat-config-ref", default="ACMEWEAR_EXPRESS_TELEGRAM_PRINT_CHAT")
    acmewear_express_prepare_label.add_argument("--run-dir", help="Run directory for prepare-label artifacts")
    acmewear_express_prepare_label.add_argument("--state", action="append", dest="states", help="Order state filter; repeatable")
    acmewear_express_prepare_label.add_argument("--created-from", default="", help="Creation date lower bound; ISO datetime/date or epoch ms")
    acmewear_express_prepare_label.add_argument("--created-to", default="", help="Creation date upper bound; ISO datetime/date or epoch ms")
    acmewear_express_prepare_label.add_argument("--lookback-hours", type=int, default=96, help="Dynamic lookback for current active queue")
    acmewear_express_prepare_label.add_argument("--page-size", type=int, default=100, help="Orders per page, max 100")
    acmewear_express_prepare_label.add_argument("--max-pages", type=int, default=5, help="Maximum pages per state")
    acmewear_express_prepare_label.add_argument("--timeout-seconds", type=int, default=20, help="HTTP timeout in seconds")
    acmewear_express_label = acmewear_express_subparsers.add_parser(
        "telegram-label",
        help="Dry-run or explicitly send an already-cropped PP2 Express product-label PDF to Telegram",
    )
    acmewear_express_label.add_argument("--cropped-label-pdf", required=True, help="Already-cropped 75x120mm product-label PDF")
    acmewear_express_label.add_argument("--order-hash", required=True, help="sha256:... order hash; raw order ids are rejected")
    acmewear_express_label.add_argument("--local-ref", required=True, help="Local non-PII reference for this label send")
    acmewear_express_label.add_argument("--ledger-csv", required=True, help="Persistent Telegram send ledger CSV path")
    acmewear_express_label.add_argument("--run-dir", help="Run directory for Telegram label artifacts")
    acmewear_express_label.add_argument(
        "--send",
        action="store_true",
        help="Actually call Telegram sendDocument; default is dry-run artifact generation only",
    )
    acmewear_express_label.add_argument(
        "--bot-token-env",
        default=WAYBILL_TELEGRAM_BOT_TOKEN_ENV,
        help="Env var containing Telegram bot token for label --send; defaults to the AB waybill Telegram bot and still accepts configured aliases",
    )
    acmewear_express_label.add_argument(
        "--chat-id-env",
        default=DEFAULT_TELEGRAM_PRINT_CHAT_ID_ENV,
        help="Env var containing Telegram print-chat id for --send; default also accepts generic AB waybill chat aliases",
    )
    acmewear_express_label.add_argument("--chat-config-ref", default="ACMEWEAR_EXPRESS_TELEGRAM_PRINT_CHAT")
    acmewear_express_label.add_argument("--sidecar-idempotency-key", default="", help="Optional sidecar row key for ledger join")
    acmewear_express_label.add_argument("--caption", default="ACMEWEAR PP2 product label")
    acmewear_express_label.add_argument("--timeout-seconds", type=int, default=20, help="HTTP timeout in seconds")
    acmewear_express_pickup = acmewear_express_subparsers.add_parser(
        "pickup-completion-review",
        help="Review whether one PP2 seller self-pickup sidecar row is ready for manual/API pickup completion",
    )
    acmewear_express_pickup.add_argument("--sidecar-csv", required=True, help="Repo-local sidecar CSV")
    acmewear_express_pickup.add_argument("--run-dir", help="Run directory for pickup completion review artifacts")
    acmewear_express_pickup.add_argument("--order-hash", default="", help="sha256:... order hash selector")
    acmewear_express_pickup.add_argument("--local-ref", default="", help="Local non-PII row selector")
    acmewear_express_pickup.add_argument("--sidecar-idempotency-key", default="", help="Exact sidecar idempotency key selector")
    acmewear_express_pickup.add_argument(
        "--security-code-env",
        default="ACMEWEAR_PICKUP_SECURITY_CODE",
        help="Env var containing the pickup security code; raw value is hashed and never persisted",
    )
    acmewear_express_pickup.add_argument(
        "--security-code-sha256",
        default="",
        help="Precomputed sha256:... pickup security code hash; use instead of env when available",
    )
    acmewear_express_pickup.add_argument(
        "--customer-arrived",
        action="store_true",
        help="Operator confirms customer is physically present at PP2",
    )
    acmewear_express_pickup.add_argument(
        "--manual-handoff-confirmed",
        action="store_true",
        help="Operator confirms the sized product was handed to customer",
    )
    acmewear_express_pickup.add_argument("--ledger-csv", help="Optional repo-local pickup completion review ledger")
    acmewear_express_pickup.add_argument("--write-ledger", action="store_true", help="Persist the review row to --ledger-csv")
    acmewear_express_pickup.add_argument(
        "--build-api-plan",
        action="store_true",
        help="Also write a redacted Kaspi pickup-completion POST request plan; never executes it",
    )
    acmewear_express_pickup.add_argument(
        "--owner-approval-ref",
        default="",
        help="Exact owner approval reference required before a future live API pilot",
    )
    acmewear_express_pickup.add_argument(
        "--order-id-env",
        default="ACMEWEAR_PICKUP_ORDER_ID",
        help="Env var containing raw Kaspi order id for future live API pilot; raw value is never persisted",
    )
    acmewear_express_pickup.add_argument(
        "--order-code-env",
        default="ACMEWEAR_PICKUP_ORDER_CODE",
        help="Env var containing raw Kaspi order code for future live API pilot; raw value is never persisted",
    )
    acmewear_express_pickup_execute = acmewear_express_subparsers.add_parser(
        "pickup-completion-execute",
        help="Dry-run or explicitly execute one owner-approved Kaspi seller-pickup completion API step from a reviewed plan",
    )
    acmewear_express_pickup_execute.add_argument("--api-plan", required=True, help="Reviewed pickup_completion_api_request_plan.json")
    acmewear_express_pickup_execute.add_argument(
        "--step",
        required=True,
        choices=("send_customer_code", "complete_with_security_code"),
        help="Two-step Kaspi seller-pickup completion action to prepare or execute",
    )
    acmewear_express_pickup_execute.add_argument("--run-dir", help="Run directory for execution artifacts")
    acmewear_express_pickup_execute.add_argument(
        "--owner-approval-ref",
        default="",
        help="Exact non-secret owner approval reference; must match the reviewed API plan",
    )
    acmewear_express_pickup_execute.add_argument(
        "--token-env",
        default=DEFAULT_ACMEWEAR_KASPI_TOKEN_ENV,
        help="Env var containing the ACMEWEAR Kaspi API token; aliases include KASPI_TOKEN_ACMEWEAR and ACMEWEAR_KASPI_API_TOKEN",
    )
    acmewear_express_pickup_execute.add_argument(
        "--order-id-env",
        default="ACMEWEAR_PICKUP_ORDER_ID",
        help="Env var containing raw Kaspi order id; raw value is never persisted",
    )
    acmewear_express_pickup_execute.add_argument(
        "--order-code-env",
        default="ACMEWEAR_PICKUP_ORDER_CODE",
        help="Env var containing raw Kaspi order code; raw value is never persisted",
    )
    acmewear_express_pickup_execute.add_argument(
        "--security-code-env",
        default="ACMEWEAR_PICKUP_SECURITY_CODE",
        help="Env var containing raw customer pickup security code for complete_with_security_code; raw value is never persisted",
    )
    acmewear_express_pickup_execute.add_argument(
        "--execute",
        action="store_true",
        help="Actually POST to Kaspi; default is dry-run/no network",
    )
    acmewear_express_pickup_execute.add_argument("--timeout-seconds", type=int, default=20, help="HTTP timeout in seconds")
    acmewear_express_assembly = acmewear_express_subparsers.add_parser(
        "express-assembly-review",
        help="Review whether one PP2 Express row is ready for manual/API assembly without mutating Kaspi",
    )
    acmewear_express_assembly.add_argument("--sidecar-csv", required=True, help="Repo-local sidecar CSV")
    acmewear_express_assembly.add_argument("--run-dir", help="Run directory for Express assembly review artifacts")
    acmewear_express_assembly.add_argument("--order-hash", default="", help="sha256:... order hash selector")
    acmewear_express_assembly.add_argument("--local-ref", default="", help="Local non-PII row selector")
    acmewear_express_assembly.add_argument("--sidecar-idempotency-key", default="", help="Exact sidecar idempotency key selector")
    acmewear_express_assembly.add_argument(
        "--label-printed",
        action="store_true",
        help="Operator confirms the 75x120 product label is printed/attached",
    )
    acmewear_express_assembly.add_argument(
        "--operator-physically-ready",
        action="store_true",
        help="Operator confirms they are physically at PP2 and ready for courier pickup flow",
    )
    acmewear_express_assembly.add_argument(
        "--owner-assembly-approval-ref",
        default="",
        help="Non-secret approval/event reference for this review; required for READY status",
    )
    acmewear_express_assembly.add_argument("--ledger-csv", help="Optional repo-local Express assembly review ledger")
    acmewear_express_assembly.add_argument("--write-ledger", action="store_true", help="Persist the review row to --ledger-csv")
    acmewear_express_operator = acmewear_express_subparsers.add_parser(
        "operator-queue",
        help="Build a manual operator action queue from local PP2 Express/self-pickup sidecar rows",
    )
    acmewear_express_operator.add_argument("--sidecar-csv", required=True, help="Repo-local sidecar CSV")
    acmewear_express_operator.add_argument("--run-dir", help="Run directory for operator queue artifacts")
    acmewear_express_shift = acmewear_express_subparsers.add_parser(
        "shift-packet",
        help="Build one warehouse shift packet combining operator queue and automation readiness",
    )
    acmewear_express_shift.add_argument(
        "--sidecar-csv",
        default="data/acmewear_express_selfpickup_sidecar.csv",
        help="Repo-local sidecar CSV to summarize",
    )
    acmewear_express_shift.add_argument("--run-dir", help="Run directory for shift packet artifacts")
    acmewear_express_shift.add_argument(
        "--run-root",
        default=str(DEFAULT_ACMEWEAR_EXPRESS_SIDECAR_RUN_ROOT),
        help="Root to discover latest Agent 1-5 closeouts",
    )
    acmewear_express_shift.add_argument(
        "--token-env",
        default=DEFAULT_ACMEWEAR_KASPI_TOKEN_ENV,
        help="Env var expected to contain the ACMEWEAR Kaspi API token; current default accepts legacy ACMEWEAR_KASPI_API_TOKEN as an alias; value is never persisted",
    )
    acmewear_express_shift.add_argument(
        "--telegram-bot-token-env",
        default=DEFAULT_TELEGRAM_BOT_TOKEN_ENV,
        help="Env var expected to contain Telegram bot token; default also accepts generic AB Telegram token aliases; value is never persisted",
    )
    acmewear_express_shift.add_argument(
        "--telegram-alert-chat-id-env",
        default=DEFAULT_TELEGRAM_ALERT_CHAT_ID_ENV,
        help="Env var expected to contain Telegram alert chat id; default also accepts generic AB Telegram chat aliases; value is never persisted",
    )
    acmewear_express_shift.add_argument(
        "--telegram-print-chat-id-env",
        default=DEFAULT_TELEGRAM_PRINT_CHAT_ID_ENV,
        help="Env var expected to contain Telegram print chat id; default also accepts generic AB waybill chat aliases; value is never persisted",
    )
    acmewear_express_readiness = acmewear_express_subparsers.add_parser(
        "readiness",
        help="Build a local go/no-go matrix for ACMEWEAR PP2 Express/self-pickup automation gates",
    )
    acmewear_express_readiness.add_argument("--run-dir", help="Run directory for readiness artifacts")
    acmewear_express_readiness.add_argument(
        "--run-root",
        default=str(DEFAULT_ACMEWEAR_EXPRESS_SIDECAR_RUN_ROOT),
        help="Root to discover latest Agent 1-5 closeouts",
    )
    acmewear_express_readiness.add_argument(
        "--sidecar-csv",
        default="data/acmewear_express_selfpickup_sidecar.csv",
        help="Repo-local sidecar CSV to summarize",
    )
    acmewear_express_readiness.add_argument("--agent1-closeout", help="Explicit Agent 1 live fetch/watch closeout")
    acmewear_express_readiness.add_argument("--agent2-closeout", help="Explicit Agent 2 AB/Google handoff closeout")
    acmewear_express_readiness.add_argument("--agent3-closeout", help="Explicit Agent 3 Telegram config closeout")
    acmewear_express_readiness.add_argument("--agent4-closeout", help="Explicit Agent 4 pickup completion closeout")
    acmewear_express_readiness.add_argument("--agent5-closeout", help="Explicit Agent 5 go/no-go closeout")
    acmewear_express_readiness.add_argument(
        "--token-env",
        default=DEFAULT_ACMEWEAR_KASPI_TOKEN_ENV,
        help="Env var expected to contain the ACMEWEAR Kaspi API token; current default accepts legacy ACMEWEAR_KASPI_API_TOKEN as an alias; value is never persisted",
    )
    acmewear_express_readiness.add_argument(
        "--telegram-bot-token-env",
        default=DEFAULT_TELEGRAM_BOT_TOKEN_ENV,
        help="Env var expected to contain Telegram bot token; default also accepts generic AB Telegram token aliases; value is never persisted",
    )
    acmewear_express_readiness.add_argument(
        "--telegram-alert-chat-id-env",
        default=DEFAULT_TELEGRAM_ALERT_CHAT_ID_ENV,
        help="Env var expected to contain Telegram alert chat id; default also accepts generic AB Telegram chat aliases; value is never persisted",
    )
    acmewear_express_readiness.add_argument(
        "--telegram-print-chat-id-env",
        default=DEFAULT_TELEGRAM_PRINT_CHAT_ID_ENV,
        help="Env var expected to contain Telegram print chat id; default also accepts generic AB waybill chat aliases; value is never persisted",
    )

    marketing_parser = subparsers.add_parser("kaspi-marketing", help="Kaspi marketing campaign operations")
    marketing_subparsers = marketing_parser.add_subparsers(dest="marketing_command", required=True)

    marketing_fetch = marketing_subparsers.add_parser(
        "fetch-campaigns",
        help="Fetch targeted Kaspi marketing campaigns into local artifacts and SQLite",
    )
    marketing_fetch.add_argument("--store", default="ACMEWEAR", help="Target store name")
    marketing_fetch.add_argument("--campaign-id", action="append", dest="campaign_id_list", help="Campaign id (repeatable)")
    marketing_fetch.add_argument("--campaign-ids", help="Comma-separated campaign ids")
    marketing_fetch.add_argument("--date", help="Target date YYYY-MM-DD")
    marketing_fetch.add_argument("--merchant-id", help="Override marketing merchant id")
    marketing_fetch.add_argument("--store-code", help="Override marketing store code")
    marketing_fetch.add_argument("--run-dir", help="Run directory for artifacts")
    marketing_fetch.add_argument("--db-path", default="data/kaspi_marketing.sqlite", help="Local SQLite path")
    marketing_fetch.add_argument("--headless", action="store_true", help="Run headless")
    marketing_fetch.add_argument("--headed", action="store_true", help="Run headful")

    marketing_line31_scope = marketing_subparsers.add_parser(
        "line31-scope",
        help="Resolve the dynamic read-only LINE31 campaign and seller-bonus promo scope",
    )
    marketing_line31_scope.add_argument("--scope-config", default=str(DEFAULT_LINE31_SCOPE_CONFIG), help="LINE31 scope seed config")
    marketing_line31_scope.add_argument("--campaign-csv", help="Optional campaign CSV evidence for dynamic discovery")
    marketing_line31_scope.add_argument("--product-csv", help="Optional product-row CSV evidence for dynamic discovery")
    marketing_line31_scope.add_argument("--out", help="Optional resolved scope YAML output path")
    marketing_line31_scope.add_argument("--run-root", default=str(DEFAULT_LINE31_SCOPE_RUN_ROOT), help="Run root for scope artifacts")
    marketing_line31_scope.add_argument("--timestamp", help="Override artifact timestamp")

    marketing_line31_fetch = marketing_subparsers.add_parser(
        "line31-fetch",
        help="Fetch all scoped LINE31 campaign data and seller-bonus promo evidence read-only",
    )
    marketing_line31_fetch.add_argument("--scope", default=str(DEFAULT_LINE31_SCOPE_CONFIG), help="Resolved or seed LINE31 scope YAML")
    marketing_line31_fetch.add_argument("--date", help="Target same-day campaign metric date")
    marketing_line31_fetch.add_argument("--closed-day", help="Latest closed-day context date")
    marketing_line31_fetch.add_argument("--include-promos", action="store_true", help="Write seller-bonus promo snapshot rows")
    marketing_line31_fetch.add_argument("--plan-only", action="store_true", help="Build fetch plan without browser/network fetch")
    marketing_line31_fetch.add_argument("--run-root", default=str(DEFAULT_LINE31_FETCH_RUN_ROOT), help="Run root for LINE31 fetch artifacts")
    marketing_line31_fetch.add_argument("--db-path", default="data/kaspi_marketing.sqlite", help="Local SQLite path")
    marketing_line31_fetch.add_argument("--headless", action="store_true", help="Run headless")
    marketing_line31_fetch.add_argument("--headed", action="store_true", help="Run headful")
    marketing_line31_fetch.add_argument("--timestamp", help="Override artifact timestamp")

    marketing_line31_analyze = marketing_subparsers.add_parser(
        "line31-analyze",
        help="Analyze LINE31 BID, score, spend, views, and seller-bonus economics",
    )
    marketing_line31_analyze.add_argument("--campaign-csv", help="Campaign CSV evidence")
    marketing_line31_analyze.add_argument("--product-csv", help="Product-row CSV evidence")
    marketing_line31_analyze.add_argument("--seller-bonus-csv", help="Seller-bonus promo snapshot CSV")
    marketing_line31_analyze.add_argument("--fetch-summary", help="LINE31_FETCH_SUMMARY.json to resolve CSV inputs from latest fetch")
    marketing_line31_analyze.add_argument("--since", help="Optional analysis window start label")
    marketing_line31_analyze.add_argument("--until", help="Optional analysis window end label")
    marketing_line31_analyze.add_argument("--score-bands", action="store_true", help="Include score-band output")
    marketing_line31_analyze.add_argument("--emit-report", help="Markdown report output path")
    marketing_line31_analyze.add_argument("--plan-only", action="store_true", help="Validate command shape without reading inputs")
    marketing_line31_analyze.add_argument("--run-root", default=str(DEFAULT_LINE31_ANALYSIS_RUN_ROOT), help="Run root for LINE31 analysis artifacts")
    marketing_line31_analyze.add_argument("--timestamp", help="Override artifact timestamp")

    marketing_line31_recommend = marketing_subparsers.add_parser(
        "line31-recommend",
        help="Build owner-reviewable LINE31 recommendations without live writes",
    )
    marketing_line31_recommend.add_argument("--analysis-json", required=True, help="LINE31 analysis summary JSON")
    marketing_line31_recommend.add_argument("--min-snapshots", type=int, default=4, help="Minimum snapshots before action recommendations")
    marketing_line31_recommend.add_argument("--observed-snapshot-count", type=int, default=0, help="Observed LINE31 snapshot count")
    marketing_line31_recommend.add_argument("--no-writes", action="store_true", help="Declare recommendation-only mode")
    marketing_line31_recommend.add_argument("--plan-only", action="store_true", help="Validate command shape without reading inputs")
    marketing_line31_recommend.add_argument("--run-root", default=str(DEFAULT_LINE31_RECOMMEND_RUN_ROOT), help="Run root for LINE31 recommendation artifacts")
    marketing_line31_recommend.add_argument("--timestamp", help="Override artifact timestamp")

    marketing_directapi = marketing_subparsers.add_parser(
        "directapi-control",
        help="Resolve a dry-run-first DirectAPI BID/budget control plan",
    )
    marketing_directapi.add_argument("--plan-file", required=True, help="YAML/JSON control plan path")
    marketing_directapi.add_argument("--dry-run", action="store_true", help="Resolve and write artifacts without live writes")
    marketing_directapi.add_argument("--confirm", action="store_true", help="Confirmed live apply mode for supported exact operations")
    marketing_directapi.add_argument("--headless", action="store_true", help="Run browser-backed live apply headlessly")
    marketing_directapi.add_argument(
        "--run-root",
        default=str(DEFAULT_DIRECTAPI_CONTROL_RUN_ROOT),
        help="Run root for timestamped DirectAPI control artifacts",
    )
    marketing_directapi.add_argument("--timestamp", help="Override artifact timestamp for deterministic reruns")

    marketing_mapper = marketing_subparsers.add_parser(
        "directapi-map",
        help="Build a read-only campaign mapper and inert DirectAPI action-plan draft from CSV evidence",
    )
    marketing_mapper.add_argument("--store", default="ACMEWEAR", help="Target store name")
    marketing_mapper.add_argument("--merchant-id", required=True, help="Expected Kaspi Marketing merchant id")
    marketing_mapper.add_argument("--store-code", required=True, help="Expected seller store code")
    marketing_mapper.add_argument("--date", required=True, help="Target evidence date YYYY-MM-DD")
    marketing_mapper.add_argument("--campaign-csv", required=True, help="Read-only campaign CSV evidence")
    marketing_mapper.add_argument("--product-csv", required=True, help="Read-only campaign product CSV evidence")
    marketing_mapper.add_argument("--campaign-id", action="append", dest="mapper_campaign_ids", help="Target campaign id")
    marketing_mapper.add_argument("--campaign-ids", help="Comma-separated target campaign ids")
    marketing_mapper.add_argument("--target-contains", action="append", default=[], help="Case-insensitive target text filter")
    marketing_mapper.add_argument("--product-sku", action="append", default=[], help="Target product SKU")
    marketing_mapper.add_argument("--merchant-sku", action="append", default=[], help="Target merchant SKU")
    marketing_mapper.add_argument(
        "--operation",
        action="append",
        dest="mapper_operations",
        default=[],
        choices=["product_bid", "campaign_budget", "product_status", "campaign_resume"],
        help="Optional inert action draft operation; repeat for multi-step plans",
    )
    marketing_mapper.add_argument(
        "--operations",
        help="Comma-separated inert action draft operations, in execution order",
    )
    marketing_mapper.add_argument("--expected-current-bid", help="Old-value gate for product_bid")
    marketing_mapper.add_argument("--new-bid", help="Target bid for product_bid")
    marketing_mapper.add_argument("--expected-current-budget", help="Old-value gate for campaign_budget")
    marketing_mapper.add_argument("--new-daily-budget", help="Target daily budget for campaign_budget")
    marketing_mapper.add_argument("--expected-current-product-status", help="Old-value gate for product_status")
    marketing_mapper.add_argument("--target-product-status", help="Target product status for product_status")
    marketing_mapper.add_argument("--expected-current-campaign-state", help="Old-value gate for campaign_resume")
    marketing_mapper.add_argument("--owner-approval-text", default="", help="Optional owner phrase capture; does not authorize live writes")
    marketing_mapper.add_argument(
        "--run-root",
        default=str(DEFAULT_DIRECTAPI_PIPELINE_RUN_ROOT),
        help="Run root for timestamped mapper artifacts",
    )
    marketing_mapper.add_argument("--timestamp", help="Override artifact timestamp for deterministic reruns")

    marketing_monitor = marketing_subparsers.add_parser(
        "monitor-recommendation",
        help="Build a read-only post-change monitoring and recommendation packet",
    )
    marketing_monitor.add_argument("--store", default="ACMEWEAR", help="Target store name")
    marketing_monitor.add_argument("--merchant-id", required=True, help="Expected Kaspi Marketing merchant id")
    marketing_monitor.add_argument("--store-code", required=True, help="Expected seller store code")
    marketing_monitor.add_argument("--campaign-id", action="append", dest="monitor_campaign_ids", help="Campaign id")
    marketing_monitor.add_argument("--campaign-ids", help="Comma-separated campaign ids")
    marketing_monitor.add_argument("--change-timestamp", required=True, help="Change timestamp/date anchor")
    marketing_monitor.add_argument("--campaign-csv", required=True, help="Read-only campaign CSV evidence")
    marketing_monitor.add_argument("--product-csv", required=True, help="Read-only campaign product CSV evidence")
    marketing_monitor.add_argument("--window", action="append", default=[], help="Monitoring window/date label; repeatable")
    marketing_monitor.add_argument("--baseline-campaign-csv", help="Optional baseline campaign CSV")
    marketing_monitor.add_argument("--baseline-product-csv", help="Optional baseline product CSV")
    marketing_monitor.add_argument(
        "--run-root",
        default=str(DEFAULT_MARKETING_MONITORING_RUN_ROOT),
        help="Run root for timestamped monitoring artifacts",
    )
    marketing_monitor.add_argument("--timestamp", help="Override artifact timestamp for deterministic reruns")

    marketing_log = marketing_subparsers.add_parser("log-change", help="Log a timestamped marketing experiment change")
    marketing_log.add_argument("--store", default="ACMEWEAR", help="Target store name")
    marketing_log.add_argument("--store-code", default="ACMEWEAR", help="Order DB store_code scope")
    marketing_log.add_argument("--product-scope", default="", help="Human product scope label")
    marketing_log.add_argument("--sku-key", default="", help="Internal sku_key scope")
    marketing_log.add_argument("--campaign-id", required=True, help="Campaign id")
    marketing_log.add_argument("--campaign-name", default="", help="Campaign name")
    marketing_log.add_argument("--category", default="", help="Category label, e.g. ST or TRM")
    marketing_log.add_argument("--change-type", required=True, help="bid_cpc, price, budget, campaign_state, product_state")
    marketing_log.add_argument("--metric-name", required=True, help="Metric name, e.g. bid")
    marketing_log.add_argument("--old-value", default="", help="Previous value")
    marketing_log.add_argument("--new-value", required=True, help="New value")
    marketing_log.add_argument("--currency", default="KZT", help="Currency")
    marketing_log.add_argument("--effective-at", required=True, help="Exact effective timestamp")
    marketing_log.add_argument("--reason", required=True, help="Reason for the test/change")
    marketing_log.add_argument("--expected-duration-hours", default="", help="Expected duration in hours")
    marketing_log.add_argument("--operator-note", default="", help="Operator note")
    marketing_log.add_argument("--source-url", default="", help="Kaspi Marketing or decision source URL")
    marketing_log.add_argument("--status", default="active", help="planned, active, closed, rolled_back")
    marketing_log.add_argument("--ledger-path", default=str(DEFAULT_CHANGE_LOG), help="Append-only CSV ledger path")
    marketing_log.add_argument("--db-path", default=str(DEFAULT_MARKETING_DB), help="Local SQLite path")
    marketing_log.add_argument("--run-root", default=str(DEFAULT_EXPERIMENT_ROOT), help="Experiment run root")

    marketing_report = marketing_subparsers.add_parser("build-experiment-report", help="Build a period report for a logged experiment")
    marketing_report.add_argument("--event-id", required=True, help="Experiment event id")
    marketing_report.add_argument("--db-path", default=str(DEFAULT_MARKETING_DB), help="Local SQLite path")
    marketing_report.add_argument("--ledger-path", default=str(DEFAULT_CHANGE_LOG), help="Append-only CSV ledger path")
    marketing_report.add_argument("--ab-root", default=str(DEFAULT_AB_ROOT), help="Autonomous Business root, read-only")
    marketing_report.add_argument("--run-dir", help="Override output run directory")
    marketing_report.add_argument("--cutoff-at", help="Explicit period cutoff timestamp")

    marketing_watch = marketing_subparsers.add_parser("watch", help="Fetch active experiment campaigns and write watcher heartbeat")
    marketing_watch.add_argument("--store", default="ACMEWEAR", help="Target store name")
    marketing_watch.add_argument("--merchant-id", help="Override marketing merchant id")
    marketing_watch.add_argument("--store-code", help="Override marketing store code for API credentials")
    marketing_watch.add_argument("--date", help="Target campaign metric date YYYY-MM-DD")
    marketing_watch.add_argument("--db-path", default=str(DEFAULT_MARKETING_DB), help="Local SQLite path")
    marketing_watch.add_argument("--ledger-path", default=str(DEFAULT_CHANGE_LOG), help="Append-only CSV ledger path")
    marketing_watch.add_argument("--watch-root", default=str(DEFAULT_WATCH_ROOT), help="Watcher artifact root")
    marketing_watch.add_argument("--active-only", action="store_true", help="Only watch active events")
    marketing_watch.add_argument("--stale-minutes", type=int, default=75, help="Red heartbeat threshold in minutes")
    marketing_watch.add_argument("--headless", action="store_true", help="Run headless")
    marketing_watch.add_argument("--headed", action="store_true", help="Run headful")

    marketing_close = marketing_subparsers.add_parser("close-experiment", help="Close an experiment and rebuild its final report")
    marketing_close.add_argument("--event-id", required=True, help="Experiment event id")
    marketing_close.add_argument("--db-path", default=str(DEFAULT_MARKETING_DB), help="Local SQLite path")
    marketing_close.add_argument("--ledger-path", default=str(DEFAULT_CHANGE_LOG), help="Append-only CSV ledger path")
    marketing_close.add_argument("--ab-root", default=str(DEFAULT_AB_ROOT), help="Autonomous Business root, read-only")
    marketing_close.add_argument("--close-at", help="Explicit close timestamp")
    marketing_close.add_argument("--run-dir", help="Override output run directory")

    # -- experiment-dashboard -------------------------------------------------
    dashboard_parser = subparsers.add_parser(
        "experiment-dashboard",
        help="Experiment dashboard: sync, build, and serve",
    )
    dashboard_subparsers = dashboard_parser.add_subparsers(dest="dashboard_command", required=True)

    dash_sync = dashboard_subparsers.add_parser("sync", help="Refresh data sources for the dashboard")
    dash_sync.add_argument("--sku-key", required=True, help="Target sku_key")
    dash_sync.add_argument("--store", default="ACMEWEAR", help="Target store name")
    dash_sync.add_argument("--campaign-id", action="append", dest="campaign_id_list", help="Campaign id (repeatable)")
    dash_sync.add_argument("--date-from", required=True, help="Inclusive start date YYYY-MM-DD")
    dash_sync.add_argument("--date-to", required=True, help="Inclusive end date YYYY-MM-DD")
    dash_sync.add_argument("--db-path", default=str(DEFAULT_MARKETING_DB), help="Local SQLite path")
    dash_sync.add_argument("--ab-root", default=str(DEFAULT_AB_ROOT), help="Autonomous Business root, read-only")
    dash_sync.add_argument("--cache-dir", default=str(DEFAULT_DASHBOARD_CACHE_DIR), help="Dashboard cache directory")
    dash_sync.add_argument("--headless", action="store_true", help="Run headless")
    dash_sync.add_argument("--headed", action="store_true", help="Run headful")
    dash_sync.add_argument("--offline", action="store_true", help="Skip live fetch, rebuild from cache only")
    dash_sync.add_argument("--dry-run", action="store_true", help="Print planned actions without writing")
    dash_sync.add_argument("--allow-stale", action="store_true", help="Return success even if live fetch fails")
    dash_sync.add_argument("--json", action="store_true", help="JSON output")
    dash_sync.add_argument("--plain", action="store_true", help="Plain text output")

    dash_build = dashboard_subparsers.add_parser("build", help="Build dashboard artifacts from current data")
    dash_build.add_argument("--sku-key", required=True, help="Target sku_key")
    dash_build.add_argument("--store", default="ACMEWEAR", help="Target store name")
    dash_build.add_argument("--campaign-id", action="append", dest="campaign_id_list", help="Campaign id (repeatable)")
    dash_build.add_argument("--date-from", required=True, help="Inclusive start date YYYY-MM-DD")
    dash_build.add_argument("--date-to", required=True, help="Inclusive end date YYYY-MM-DD")
    dash_build.add_argument("--db-path", default=str(DEFAULT_MARKETING_DB), help="Local SQLite path")
    dash_build.add_argument("--ab-root", default=str(DEFAULT_AB_ROOT), help="Autonomous Business root, read-only")
    dash_build.add_argument("--cache-dir", default=str(DEFAULT_DASHBOARD_CACHE_DIR), help="Dashboard cache directory")
    dash_build.add_argument("--output-dir", help="Override output directory")
    dash_build.add_argument("--daily-history-days", type=int, default=90, help="Daily demand chart history window")
    dash_build.add_argument("--json", action="store_true", help="JSON output")
    dash_build.add_argument("--plain", action="store_true", help="Plain text output")

    dash_refresh = dashboard_subparsers.add_parser("refresh", help="Sync data, build dashboard, write heartbeat")
    dash_refresh.add_argument("--sku-key", required=True, help="Target sku_key")
    dash_refresh.add_argument("--store", default="ACMEWEAR", help="Target store name")
    dash_refresh.add_argument("--campaign-id", action="append", dest="campaign_id_list", help="Campaign id (repeatable)")
    dash_refresh.add_argument("--date-from", required=True, help="Inclusive start date YYYY-MM-DD")
    dash_refresh.add_argument("--date-to", required=True, help="Inclusive end date YYYY-MM-DD")
    dash_refresh.add_argument("--db-path", default=str(DEFAULT_MARKETING_DB), help="Local SQLite path")
    dash_refresh.add_argument("--ab-root", default=str(DEFAULT_AB_ROOT), help="Autonomous Business root, read-only")
    dash_refresh.add_argument("--cache-dir", default=str(DEFAULT_DASHBOARD_CACHE_DIR), help="Dashboard cache directory")
    dash_refresh.add_argument("--headless", action="store_true", help="Run headless")
    dash_refresh.add_argument("--headed", action="store_true", help="Run headful")
    dash_refresh.add_argument("--offline", action="store_true", help="Skip live fetch")
    dash_refresh.add_argument("--allow-stale", action="store_true", help="Continue even if sources are stale")
    dash_refresh.add_argument("--daily-history-days", type=int, default=90, help="Daily demand chart history window")
    dash_refresh.add_argument("--json", action="store_true", help="JSON output")

    dash_watch_health = dashboard_subparsers.add_parser(
        "watch-health",
        help="Run one scheduler-safe refresh cycle and write dashboard heartbeat",
    )
    dash_watch_health.add_argument("--sku-key", required=True, help="Target sku_key")
    dash_watch_health.add_argument("--store", default="ACMEWEAR", help="Target store name")
    dash_watch_health.add_argument("--campaign-id", action="append", dest="campaign_id_list", help="Campaign id (repeatable)")
    dash_watch_health.add_argument("--date-from", help="Inclusive dashboard start date YYYY-MM-DD; defaults from --date-from-days")
    dash_watch_health.add_argument("--date-from-days", type=int, default=90, help="Dashboard lookback when --date-from is omitted")
    dash_watch_health.add_argument("--date-to", default="today", help="Inclusive dashboard end date YYYY-MM-DD or today")
    dash_watch_health.add_argument("--sync-date", help="Marketing metric date to fetch; defaults to resolved --date-to")
    dash_watch_health.add_argument("--db-path", default=str(DEFAULT_MARKETING_DB), help="Local SQLite path")
    dash_watch_health.add_argument("--ab-root", default=str(DEFAULT_AB_ROOT), help="Autonomous Business root, read-only")
    dash_watch_health.add_argument("--cache-dir", default=str(DEFAULT_DASHBOARD_CACHE_DIR), help="Dashboard cache directory")
    dash_watch_health.add_argument("--headless", action="store_true", help="Run headless")
    dash_watch_health.add_argument("--headed", action="store_true", help="Run headful")
    dash_watch_health.add_argument("--offline", action="store_true", help="Skip live marketing fetch")
    dash_watch_health.add_argument("--allow-stale", action="store_true", help="Return success even if sources are stale/failing")
    dash_watch_health.add_argument("--daily-history-days", type=int, default=90, help="Daily demand chart history window")
    dash_watch_health.add_argument(
        "--delivery-mode",
        choices=["skip", "manual", "public-page"],
        default="skip",
        help="Optional delivery-promise capture mode before dashboard rebuild",
    )
    dash_watch_health.add_argument("--offer-url", default="", help="Public Kaspi offer URL for delivery-promise checks")
    dash_watch_health.add_argument("--merchant-id", default="30137883", help="Kaspi merchant id for delivery-promise checks")
    dash_watch_health.add_argument("--city", default="Astana", help="Displayed city context")
    dash_watch_health.add_argument("--city-id", default="710000000", help="Kaspi city id")
    dash_watch_health.add_argument("--expected-days", type=int, default=1, help="Normal delivery promise baseline in days")
    dash_watch_health.add_argument("--expected-date", help="Explicit baseline delivery date YYYY-MM-DD")
    dash_watch_health.add_argument("--displayed-date", help="Displayed delivery date YYYY-MM-DD for --delivery-mode manual")
    dash_watch_health.add_argument("--delivery-watch-root", default=str(DEFAULT_DELIVERY_PROMISE_ROOT), help="Delivery watcher artifact root")
    dash_watch_health.add_argument("--json", action="store_true", help="JSON output")

    dash_heartbeat = dashboard_subparsers.add_parser("heartbeat", help="Show sync freshness status")
    dash_heartbeat.add_argument("--cache-dir", default=str(DEFAULT_DASHBOARD_CACHE_DIR), help="Dashboard cache directory")
    dash_heartbeat.add_argument("--json", action="store_true", help="JSON output")

    dash_backfill = dashboard_subparsers.add_parser("backfill-gaps", help="Backfill missing marketing campaign/date rows from a gap report")
    dash_backfill.add_argument("--gap-report", required=True, help="Path to marketing_gap_report.csv or .json")
    dash_backfill.add_argument("--store", default="ACMEWEAR", help="Kaspi Marketing credential scope")
    dash_backfill.add_argument("--db-path", default=str(DEFAULT_MARKETING_DB), help="Local marketing SQLite cache to update")
    dash_backfill.add_argument("--campaign-id", action="append", dest="campaign_id_list", help="Optional campaign id filter (repeatable)")
    dash_backfill.add_argument("--date-from", help="Optional inclusive start date filter YYYY-MM-DD")
    dash_backfill.add_argument("--date-to", help="Optional inclusive end date filter YYYY-MM-DD")
    dash_backfill.add_argument("--price-policy", action="append", dest="price_policy_list", help="Optional price policy filter (repeatable)")
    dash_backfill.add_argument("--limit", type=int, help="Safety cap for number of campaign/date fetches")
    dash_backfill.add_argument("--dry-run", action="store_true", help="Print planned rows only; do not fetch")
    dash_backfill.add_argument("--allow-stale", action="store_true", help="Return success if some fetches fail")
    dash_backfill.add_argument("--rebuild-dashboard", action="store_true", help="Rebuild dashboard after successful fetches")
    dash_backfill.add_argument("--sku-key", help="Required with --rebuild-dashboard")
    dash_backfill.add_argument("--dashboard-date-from", help="Dashboard rebuild window start YYYY-MM-DD; required with --rebuild-dashboard")
    dash_backfill.add_argument("--dashboard-date-to", help="Dashboard rebuild window end YYYY-MM-DD; required with --rebuild-dashboard")
    dash_backfill.add_argument("--output-dir", help="Existing dashboard folder to overwrite in place (with --rebuild-dashboard)")
    dash_backfill.add_argument("--ab-root", default=str(DEFAULT_AB_ROOT), help="Autonomous Business root, read-only")
    dash_backfill.add_argument("--cache-dir", default=str(DEFAULT_DASHBOARD_CACHE_DIR), help="Dashboard cache directory")
    dash_backfill.add_argument("--daily-history-days", type=int, default=90, help="Passed to dashboard build")
    dash_backfill.add_argument("--headless", action="store_true", help="Run headless")
    dash_backfill.add_argument("--headed", action="store_true", help="Run headful")
    dash_backfill.add_argument("--json", action="store_true", help="JSON output")

    dash_serve = dashboard_subparsers.add_parser("serve", help="Serve dashboard locally")
    dash_serve.add_argument("--port", type=int, default=DEFAULT_DASHBOARD_PORT, help="Server port")
    dash_serve.add_argument("--host", default="127.0.0.1", help="Server host")
    dash_serve.add_argument("--cache-dir", default=str(DEFAULT_DASHBOARD_CACHE_DIR), help="Dashboard cache directory")
    dash_serve.add_argument("--run-dir", help="Exact run folder to serve")
    dash_serve.add_argument("--open", action="store_true", help="Open browser after starting")

    delivery_parser = subparsers.add_parser("delivery-health", help="Track public Kaspi delivery-promise health")
    delivery_subparsers = delivery_parser.add_subparsers(dest="delivery_command", required=True)
    delivery_record = delivery_subparsers.add_parser("record", help="Record a manual or scraped delivery promise capture")
    delivery_record.add_argument("--store", required=True, help="Store code/name, e.g. ACMEWEAR")
    delivery_record.add_argument("--city", default="Astana", help="Displayed city context")
    delivery_record.add_argument("--city-id", default="710000000", help="Kaspi city id")
    delivery_record.add_argument("--sku-key", required=True, help="Internal sku_key")
    delivery_record.add_argument("--offer-url", default="", help="Public Kaspi offer URL used for observation")
    delivery_record.add_argument("--displayed-date", required=True, help="Displayed delivery date YYYY-MM-DD")
    delivery_record.add_argument("--expected-date", required=True, help="Normal baseline delivery date YYYY-MM-DD")
    delivery_record.add_argument("--source", default="operator_observation", help="Capture source")
    delivery_record.add_argument("--note", default="", help="Operator note")
    delivery_record.add_argument("--observed-at", help="Observation timestamp, defaults to now")
    delivery_record.add_argument("--watch-root", default=str(DEFAULT_DELIVERY_PROMISE_ROOT), help="Delivery watcher artifact root")
    delivery_record.add_argument("--json", action="store_true", help="JSON output")

    delivery_watch = delivery_subparsers.add_parser("watch", help="Fetch public Kaspi delivery promise and write heartbeat")
    delivery_watch.add_argument("--store", default="ACMEWEAR", help="Store code/name")
    delivery_watch.add_argument("--merchant-id", default="30137883", help="Kaspi merchant id/store code to track")
    delivery_watch.add_argument("--city", default="Astana", help="Displayed city context")
    delivery_watch.add_argument("--city-id", default="710000000", help="Kaspi city id")
    delivery_watch.add_argument("--sku-key", required=True, help="Internal sku_key")
    delivery_watch.add_argument("--offer-url", required=True, help="Public Kaspi offer URL used for observation")
    delivery_watch.add_argument("--expected-days", type=int, default=1, help="Normal delivery promise baseline in days")
    delivery_watch.add_argument("--expected-date", help="Explicit baseline delivery date YYYY-MM-DD")
    delivery_watch.add_argument("--source", default="kaspi_offer_view_api", help="Capture source")
    delivery_watch.add_argument("--note", default="", help="Operator note")
    delivery_watch.add_argument("--watch-root", default=str(DEFAULT_DELIVERY_PROMISE_ROOT), help="Delivery watcher artifact root")
    delivery_watch.add_argument("--json", action="store_true", help="JSON output")

    scheduled_parser = subparsers.add_parser(
        "scheduled-checkpoint",
        help="Run repo-local scheduled checkpoint jobs with no production writes",
    )
    scheduled_subparsers = scheduled_parser.add_subparsers(dest="scheduled_command", required=True)

    scheduled_validate = scheduled_subparsers.add_parser("validate", help="Validate a scheduled checkpoint config")
    scheduled_validate.add_argument(
        "--config",
        default=str(DEFAULT_SCHEDULED_CHECKPOINT_CONFIG_PATH),
        help="Scheduled checkpoint YAML path",
    )

    scheduled_list = scheduled_subparsers.add_parser("list", help="List configured checkpoint jobs")
    scheduled_list.add_argument(
        "--config",
        default=str(DEFAULT_SCHEDULED_CHECKPOINT_CONFIG_PATH),
        help="Scheduled checkpoint YAML path",
    )
    scheduled_list.add_argument("--due", action="store_true", help="Only show jobs due at --now")
    scheduled_list.add_argument("--now", help="Override current local time for due checks")
    scheduled_list.add_argument(
        "--run-root",
        default=str(DEFAULT_SCHEDULED_CHECKPOINT_RUN_ROOT),
        help="Run root for due-job state",
    )

    scheduled_run = scheduled_subparsers.add_parser("run", help="Run one checkpoint job or all currently due jobs")
    scheduled_run.add_argument(
        "--config",
        default=str(DEFAULT_SCHEDULED_CHECKPOINT_CONFIG_PATH),
        help="Scheduled checkpoint YAML path",
    )
    scheduled_run.add_argument("--job", help="Exact job_id to run")
    scheduled_run.add_argument("--due", action="store_true", help="Run due jobs instead of one explicit --job")
    scheduled_run.add_argument(
        "--mode",
        choices=["plan", "dry-live", "live-readonly"],
        default="plan",
        help="plan writes only manifests; dry-live executes dry_live_argv; live-readonly executes read-only argv",
    )
    scheduled_run.add_argument("--force", action="store_true", help="Ignore completed-job state for due jobs")
    scheduled_run.add_argument("--allow-stale", action="store_true", help="Allow stale/failing source steps to close YELLOW")
    scheduled_run.add_argument("--now", help="Override current local time for due checks")
    scheduled_run.add_argument(
        "--run-root",
        default=str(DEFAULT_SCHEDULED_CHECKPOINT_RUN_ROOT),
        help="Run root for artifacts and due-job state",
    )
    scheduled_run.add_argument("--timestamp", help="Override artifact timestamp for explicit --job runs")
    scheduled_run.add_argument("--max-jobs", type=int, help="Maximum due jobs to run")

    args = parser.parse_args(argv)

    if args.env_file:
        load_dotenv(args.env_file)
    else:
        default_env = Path(".env")
        if default_env.exists():
            load_dotenv(default_env)

    _setup_logging(args.verbose, args.quiet)

    if args.command == "scheduled-checkpoint":
        try:
            schedule_config = load_schedule_config(args.config)
            timezone_name = str(schedule_config.get("timezone") or "Asia/Almaty")
            now_dt = None
            if getattr(args, "now", None):
                now_text = str(args.now).replace("T", " ")
                now_dt = datetime.fromisoformat(now_text)
                if now_dt.tzinfo is None:
                    now_dt = now_dt.replace(tzinfo=ZoneInfo(timezone_name))
        except (ScheduledCheckpointError, ValueError) as exc:
            logging.error(str(exc))
            return 2

        if args.scheduled_command == "validate":
            payload = {
                "status": "ok",
                "schema_version": schedule_config.get("schema_version"),
                "timezone": schedule_config.get("timezone"),
                "job_count": len(schedule_config.get("jobs") or []),
                "production_write_action_authorized": False,
            }
            if args.json:
                print(json.dumps(payload, ensure_ascii=False, indent=2))
            else:
                logging.info(
                    "Scheduled checkpoint config OK: jobs=%s timezone=%s",
                    payload["job_count"],
                    payload["timezone"],
                )
            return 0

        if args.scheduled_command == "list":
            selected_jobs = due_jobs(
                schedule_config,
                now=now_dt,
                run_root=Path(args.run_root),
            ) if args.due else list(schedule_config.get("jobs") or [])
            payload = {
                "status": "success",
                "due_only": bool(args.due),
                "job_count": len(selected_jobs),
                "jobs": [
                    {
                        "job_id": job.get("job_id"),
                        "title": job.get("title"),
                        "scheduled_at": job.get("scheduled_at"),
                        "command_count": len(job.get("commands") or []),
                        "owner_approval_required_for_live_writes": job.get(
                            "owner_approval_required_for_live_writes",
                            True,
                        ),
                    }
                    for job in selected_jobs
                ],
            }
            if args.json:
                print(json.dumps(payload, ensure_ascii=False, indent=2))
            else:
                for job in payload["jobs"]:
                    print(
                        f"{job['job_id']}  {job['scheduled_at']}  "
                        f"commands={job['command_count']}  {job['title']}"
                    )
            return 0

        if args.scheduled_command == "run":
            try:
                if bool(args.job) == bool(args.due):
                    raise ScheduledCheckpointError("choose exactly one of --job or --due")
                if args.job:
                    job = get_job(schedule_config, args.job)
                    summary = run_job(
                        schedule_config,
                        job,
                        mode=args.mode,
                        run_root=Path(args.run_root),
                        timestamp=args.timestamp,
                        allow_stale=bool(args.allow_stale),
                        mark_done=not bool(args.force),
                    )
                else:
                    summary = run_due_jobs(
                        schedule_config,
                        mode=args.mode,
                        run_root=Path(args.run_root),
                        now=now_dt,
                        force=bool(args.force),
                        allow_stale=bool(args.allow_stale),
                        max_jobs=args.max_jobs,
                    )
            except (ScheduledCheckpointError, FileExistsError) as exc:
                logging.error(str(exc))
                return 2
            if args.json:
                print(json.dumps(summary, ensure_ascii=False, indent=2, default=str))
            else:
                logging.info(
                    "Scheduled checkpoint: status=%s gate=%s jobs=%s run_dir=%s",
                    summary.get("status"),
                    summary.get("gate"),
                    summary.get("jobs_run", 1),
                    summary.get("run_dir", ""),
                )
            return 0 if summary.get("gate") in {"GREEN", "YELLOW"} else 4

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

    if args.command == "offer-flow":
        try:
            registry = load_offer_flow_registry(args.config)
        except OfferFlowRegistryError as exc:
            logging.error(str(exc))
            return 2

        if args.offer_flow_command == "validate":
            payload = {
                "status": "ok",
                "registry_version": registry.registry_version,
                "doc_output_path": registry.doc_output_path,
                "flow_count": len(registry.flows),
                "active_flow_count": len([flow for flow in registry.flows if flow.active]),
            }
            if args.json:
                print(json.dumps(payload, ensure_ascii=False, indent=2))
            elif args.plain:
                print(
                    "|".join(
                        [
                            "ok",
                            str(payload["registry_version"]),
                            payload["doc_output_path"],
                            str(payload["flow_count"]),
                            str(payload["active_flow_count"]),
                        ]
                    )
                )
            else:
                print(
                    f"Offer flow registry OK: version={payload['registry_version']} "
                    f"flows={payload['flow_count']} active={payload['active_flow_count']} "
                    f"doc_output_path={payload['doc_output_path']}"
                )
            return 0

        if args.offer_flow_command == "list":
            flows = filter_offer_flows(
                registry,
                goal=args.goal,
                artifact_state=args.artifact_state,
                store=args.store,
                include_inactive=args.all,
            )
            if args.json:
                print(json.dumps({"flows": [flow.to_dict() for flow in flows]}, ensure_ascii=False, indent=2))
            elif args.plain:
                print(_render_offer_flow_plain_lines(flows))
            else:
                print(_render_offer_flow_table(flows))
            return 0

        if args.offer_flow_command == "show":
            try:
                flow = registry.get_flow(args.flow_id)
            except OfferFlowRegistryError as exc:
                logging.error(str(exc))
                return 2
            if args.json:
                print(json.dumps(flow.to_dict(), ensure_ascii=False, indent=2))
            else:
                print(_render_offer_flow_detail(flow))
            return 0

        if args.offer_flow_command == "select":
            selection = select_offer_flow(
                registry,
                goal=args.goal,
                artifact_state=args.artifact_state,
                store=args.store,
                include_inactive=args.all,
            )
            if selection is None:
                payload = {
                    "status": "no_match",
                    "goal": args.goal,
                    "artifact_state": args.artifact_state,
                    "store": args.store,
                }
                if args.json:
                    print(json.dumps(payload, ensure_ascii=False, indent=2))
                elif args.plain:
                    print(
                        "|".join(
                            [
                                "no_match",
                                args.goal,
                                args.artifact_state,
                                str(args.store or ""),
                            ]
                        )
                    )
                else:
                    print(
                        f"No offer flow matched goal={args.goal} artifact_state={args.artifact_state}"
                        + (f" store={args.store}" if args.store else "")
                    )
                return 4
            if args.json:
                print(json.dumps(selection.to_dict(), ensure_ascii=False, indent=2))
            elif args.plain:
                print(
                    "|".join(
                        [
                            selection.flow.flow_id,
                            selection.flow.goal,
                            selection.flow.artifact_state,
                            ",".join(selection.flow.stores),
                            str(selection.score),
                        ]
                    )
                )
            else:
                print(_render_offer_flow_detail(selection.flow, selection_reasons=selection.reasons))
            return 0

        if args.offer_flow_command == "render-doc":
            rendered = render_offer_flow_registry_markdown(registry)
            output_path = Path(args.out) if args.out else Path(registry.doc_output_path)
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text(rendered, encoding="utf-8")
            payload = {
                "status": "success",
                "output_path": str(output_path),
                "flow_count": len(registry.flows),
            }
            if args.json:
                print(json.dumps(payload, ensure_ascii=False, indent=2))
            elif args.plain:
                print("|".join(["success", str(output_path), str(len(registry.flows))]))
            else:
                print(f"Rendered offer flow doc: {output_path} ({len(registry.flows)} flows)")
            return 0

        logging.error("Unknown offer-flow command")
        return 2

    if args.command == "offer-run":
        try:
            registry = load_offer_flow_registry(args.config)
            request = OfferRunRequest(
                flow_id=args.flow_id,
                goal=args.goal,
                artifact_state=args.artifact_state,
                store=args.store,
                confirm=args.confirm,
                dry_run=args.dry_run,
                verify=args.verify,
                include_inactive=args.allow_inactive,
                zip_queue_path=args.zip_queue_path,
                handoff_path=args.handoff_path,
                workbook_path=args.workbook_path,
                active_path=args.active_path,
                archive_path=args.archive_path,
                session_doc_path=args.session_doc_path,
                public_urls_path=args.public_urls_path,
                intent=args.intent or "",
                sku=args.sku or "",
                sku_key=args.sku_key or "",
                group_url=args.group_url or "",
                target_price=args.target_price or "",
                offers_book_path=args.offers_book,
                truth_xlsx_path=args.truth_xlsx,
                dispatch=args.dispatch,
                headless=args.headless,
                headed=args.headed,
                timeout_seconds=args.timeout_seconds,
                processing_grace_seconds=args.processing_grace_seconds,
                note=args.note,
                run_root=args.run_root,
            )
            plan = build_offer_run_plan(registry, request)
            summary = write_offer_run_bundle(plan)
            payload = {
                "status": summary["status"],
                "flow_id": plan.flow.flow_id,
                "mode": plan.mode,
                "run_dir": plan.run_dir,
                "selected_by": plan.selected_by,
                "required_inputs": plan.required_inputs,
                "warnings": plan.warnings,
                "next_commands": plan.next_commands,
            }
            if args.dispatch:
                dispatch_result = execute_offer_run_plan(
                    plan,
                    env_file=args.env_file,
                )
                payload["dispatch_result"] = dispatch_result
                payload["status"] = dispatch_result.get("status", payload["status"])
        except (OfferFlowRegistryError, OfferRunError, FileExistsError) as exc:
            logging.error(str(exc))
            return 2

        if args.json:
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        elif args.plain:
            print(
                "|".join(
                    [
                        payload["status"],
                        payload["flow_id"],
                        payload["mode"],
                        payload["run_dir"],
                    ]
                )
            )
        else:
            verb = "executed" if args.dispatch else "planned"
            print(f"Offer run {verb}: flow={payload['flow_id']} mode={payload['mode']} run_dir={payload['run_dir']}")
            if payload["warnings"]:
                print("Warnings:")
                for item in payload["warnings"]:
                    print(f"- {item}")
        return 0 if payload["status"] in {"planned", "success"} else 4

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
                    competition_scope_map_path=args.competition_scope_map,
                    only_competitor_actions=args.only_competitor_action,
                    only_competitor_reasons=args.only_competitor_reason,
                    only_row_ids=args.only_row_id,
                    only_merchant_skus=args.only_merchant_sku,
                    only_competitor_mids=args.only_competitor_mid,
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

    if args.command == "kaspi-marketing":
        headless = True
        if getattr(args, "headed", False):
            headless = False
        if getattr(args, "headless", False):
            headless = True

        env_path = Path(args.env_file) if args.env_file else Path(".env")

        if args.marketing_command == "line31-scope":
            try:
                summary = resolve_line31_scope(
                    scope_config_path=Path(args.scope_config),
                    campaign_csv=Path(args.campaign_csv) if args.campaign_csv else None,
                    product_csv=Path(args.product_csv) if args.product_csv else None,
                    run_root=Path(args.run_root),
                    timestamp=args.timestamp,
                    out=Path(args.out) if args.out else None,
                )
            except (LINE31MarketingError, FileExistsError) as exc:
                logging.error(str(exc))
                return 2
            if args.json:
                print(json.dumps(summary, ensure_ascii=False, indent=2))
            else:
                logging.info(
                    "LINE31 scope: status=%s gate=%s campaigns=%s promos=%s run_dir=%s",
                    summary.get("status"),
                    summary.get("gate"),
                    summary.get("campaign_count"),
                    summary.get("promo_count"),
                    summary.get("run_dir"),
                )
            return 0 if summary.get("gate") in {"GREEN", "YELLOW"} else 4

        if args.marketing_command == "line31-fetch":
            try:
                summary = run_line31_fetch(
                    scope_path=Path(args.scope),
                    target_date=args.date,
                    closed_day=args.closed_day,
                    include_promos=bool(args.include_promos),
                    plan_only=bool(args.plan_only),
                    run_root=Path(args.run_root),
                    db_path=Path(args.db_path),
                    env_file=env_path,
                    headless=headless,
                    timestamp=args.timestamp,
                )
            except (LINE31MarketingError, ValueError, FileExistsError) as exc:
                logging.error(str(exc))
                return 2
            if args.json:
                print(json.dumps(summary, ensure_ascii=False, indent=2))
            else:
                logging.info(
                    "LINE31 fetch: status=%s gate=%s campaigns=%s promos=%s run_dir=%s",
                    summary.get("status"),
                    summary.get("gate"),
                    len(summary.get("campaign_ids") or []),
                    len(summary.get("promo_ids") or []),
                    summary.get("run_dir"),
                )
            return 0 if summary.get("gate") in {"GREEN", "YELLOW"} else 4

        if args.marketing_command == "line31-analyze":
            if args.plan_only:
                summary = {
                    "schema_version": "web_auto.kaspi_marketing_line31.v1",
                    "status": "planned",
                    "gate": "GREEN",
                    "live_writes_executed": False,
                    "command": "line31-analyze",
                }
                if args.json:
                    print(json.dumps(summary, ensure_ascii=False, indent=2))
                else:
                    logging.info("LINE31 analyze plan only")
                return 0
            campaign_csv = Path(args.campaign_csv) if args.campaign_csv else None
            product_csv = Path(args.product_csv) if args.product_csv else None
            seller_bonus_csv = Path(args.seller_bonus_csv) if args.seller_bonus_csv else None
            if args.fetch_summary:
                try:
                    fetch_summary = json.loads(Path(args.fetch_summary).read_text(encoding="utf-8"))
                except Exception as exc:
                    logging.error("could not read fetch summary: %s", exc)
                    return 2
                if campaign_csv is None and str(fetch_summary.get("campaign_csv") or "").strip():
                    campaign_csv = Path(str(fetch_summary.get("campaign_csv")))
                if product_csv is None and str(fetch_summary.get("product_csv") or "").strip():
                    product_csv = Path(str(fetch_summary.get("product_csv")))
                if seller_bonus_csv is None and str(fetch_summary.get("seller_bonus_csv") or "").strip():
                    seller_bonus_csv = Path(str(fetch_summary.get("seller_bonus_csv")))
            if campaign_csv is None or product_csv is None:
                logging.error("line31-analyze requires --campaign-csv and --product-csv or --fetch-summary")
                return 2
            try:
                summary = analyze_line31_snapshots(
                    campaign_csv=campaign_csv,
                    product_csv=product_csv,
                    seller_bonus_csv=seller_bonus_csv,
                    run_root=Path(args.run_root),
                    timestamp=args.timestamp,
                    emit_report=Path(args.emit_report) if args.emit_report else None,
                )
            except (LINE31MarketingError, FileExistsError) as exc:
                logging.error(str(exc))
                return 2
            if args.json:
                print(json.dumps(summary, ensure_ascii=False, indent=2))
            else:
                logging.info(
                    "LINE31 analysis: status=%s gate=%s rows=%s report=%s",
                    summary.get("status"),
                    summary.get("gate"),
                    summary.get("analysis_row_count"),
                    summary.get("report_path"),
                )
            return 0 if summary.get("gate") in {"GREEN", "YELLOW"} else 4

        if args.marketing_command == "line31-recommend":
            if args.plan_only:
                summary = {
                    "schema_version": "web_auto.kaspi_marketing_line31.v1",
                    "status": "planned",
                    "gate": "GREEN",
                    "live_writes_executed": False,
                    "command": "line31-recommend",
                }
                if args.json:
                    print(json.dumps(summary, ensure_ascii=False, indent=2))
                else:
                    logging.info("LINE31 recommend plan only")
                return 0
            try:
                summary = run_line31_recommend(
                    analysis_json=Path(args.analysis_json),
                    min_snapshots=args.min_snapshots,
                    observed_snapshot_count=args.observed_snapshot_count,
                    run_root=Path(args.run_root),
                    timestamp=args.timestamp,
                )
            except (LINE31MarketingError, FileExistsError) as exc:
                logging.error(str(exc))
                return 2
            if args.json:
                print(json.dumps(summary, ensure_ascii=False, indent=2))
            else:
                logging.info(
                    "LINE31 recommendations: status=%s gate=%s recommendations=%s packet=%s",
                    summary.get("status"),
                    summary.get("gate"),
                    len(summary.get("recommendations") or []),
                    summary.get("packet_path"),
                )
            return 0 if summary.get("gate") in {"GREEN", "YELLOW"} else 4

        if args.marketing_command == "directapi-control":
            try:
                summary = run_directapi_control(
                    plan_file=Path(args.plan_file),
                    dry_run=bool(args.dry_run),
                    confirm=bool(args.confirm),
                    run_root=Path(args.run_root),
                    timestamp=args.timestamp,
                    env=os.environ,
                    env_file=Path(args.env_file) if args.env_file else None,
                    headless=bool(args.headless),
                )
            except (DirectAPIControlPlanError, FileExistsError) as exc:
                logging.error(str(exc))
                return 2
            if args.json:
                print(json.dumps(summary, ensure_ascii=False, indent=2))
            else:
                logging.info(
                    "Kaspi Marketing DirectAPI control: status=%s gate=%s actions=%s run_dir=%s",
                    summary.get("status"),
                    summary.get("gate"),
                    summary.get("action_count"),
                    summary.get("run_dir"),
                )
            return 0 if summary.get("gate") in {"GREEN", "YELLOW"} else 4

        if args.marketing_command == "directapi-map":
            campaign_ids: list[str] = []
            for item in getattr(args, "mapper_campaign_ids", []) or []:
                text = str(item or "").strip()
                if text:
                    campaign_ids.append(text)
            for item in str(getattr(args, "campaign_ids", "") or "").split(","):
                text = str(item or "").strip()
                if text:
                    campaign_ids.append(text)
            operations: list[str] = []
            for item in getattr(args, "mapper_operations", []) or []:
                text = str(item or "").strip()
                if text:
                    operations.append(text)
            for item in str(getattr(args, "operations", "") or "").split(","):
                text = str(item or "").strip()
                if text:
                    operations.append(text)
            try:
                summary = run_campaign_mapper(
                    store=args.store,
                    merchant_id=args.merchant_id,
                    store_code=args.store_code,
                    target_date=args.date,
                    campaign_csv=Path(args.campaign_csv),
                    product_csv=Path(args.product_csv),
                    campaign_ids=campaign_ids,
                    target_contains=args.target_contains or [],
                    product_skus=args.product_sku or [],
                    merchant_skus=args.merchant_sku or [],
                    operations=operations,
                    expected_current_bid=args.expected_current_bid,
                    new_bid=args.new_bid,
                    expected_current_budget=args.expected_current_budget,
                    new_daily_budget=args.new_daily_budget,
                    expected_current_product_status=args.expected_current_product_status,
                    target_product_status=args.target_product_status,
                    expected_current_campaign_state=args.expected_current_campaign_state,
                    owner_approval_text=args.owner_approval_text,
                    run_root=Path(args.run_root),
                    timestamp=args.timestamp,
                )
            except (KaspiMarketingPipelineError, FileExistsError) as exc:
                logging.error(str(exc))
                return 2
            if args.json:
                print(json.dumps(summary, ensure_ascii=False, indent=2))
            else:
                logging.info(
                    "Kaspi Marketing mapper: status=%s gate=%s candidates=%s run_dir=%s",
                    summary.get("status"),
                    summary.get("gate"),
                    summary.get("candidate_rows"),
                    summary.get("run_dir"),
                )
            return 0 if summary.get("gate") in {"GREEN", "YELLOW"} else 4

        if args.marketing_command == "monitor-recommendation":
            campaign_ids: list[str] = []
            for item in getattr(args, "monitor_campaign_ids", []) or []:
                text = str(item or "").strip()
                if text:
                    campaign_ids.append(text)
            for item in str(getattr(args, "campaign_ids", "") or "").split(","):
                text = str(item or "").strip()
                if text:
                    campaign_ids.append(text)
            try:
                summary = run_monitoring_packet(
                    store=args.store,
                    merchant_id=args.merchant_id,
                    store_code=args.store_code,
                    campaign_ids=campaign_ids,
                    change_timestamp=args.change_timestamp,
                    campaign_csv=Path(args.campaign_csv),
                    product_csv=Path(args.product_csv),
                    windows=args.window or [],
                    baseline_campaign_csv=Path(args.baseline_campaign_csv) if args.baseline_campaign_csv else None,
                    baseline_product_csv=Path(args.baseline_product_csv) if args.baseline_product_csv else None,
                    run_root=Path(args.run_root),
                    timestamp=args.timestamp,
                )
            except (KaspiMarketingPipelineError, FileExistsError) as exc:
                logging.error(str(exc))
                return 2
            if args.json:
                print(json.dumps(summary, ensure_ascii=False, indent=2))
            else:
                logging.info(
                    "Kaspi Marketing monitoring: status=%s gate=%s campaigns=%s run_dir=%s",
                    summary.get("status"),
                    summary.get("gate"),
                    ",".join(summary.get("campaign_ids", [])),
                    summary.get("run_dir"),
                )
            return 0 if summary.get("gate") in {"GREEN", "YELLOW"} else 4

        if args.marketing_command == "log-change":
            summary = log_change_event(
                db_path=Path(args.db_path),
                ledger_path=Path(args.ledger_path),
                event={
                    "effective_at": args.effective_at,
                    "store_name": args.store,
                    "store_code": args.store_code,
                    "product_scope": args.product_scope,
                    "sku_key": args.sku_key,
                    "campaign_id": args.campaign_id,
                    "campaign_name": args.campaign_name,
                    "category": args.category,
                    "change_type": args.change_type,
                    "metric_name": args.metric_name,
                    "old_value": args.old_value,
                    "new_value": args.new_value,
                    "currency": args.currency,
                    "reason": args.reason,
                    "expected_duration_hours": args.expected_duration_hours,
                    "operator_note": args.operator_note,
                    "source_url": args.source_url,
                    "status": args.status,
                },
                run_root=Path(args.run_root),
            )
            if args.json:
                print(json.dumps(summary, ensure_ascii=False, indent=2))
            else:
                logging.info("Logged marketing change: event_id=%s run_dir=%s", summary.get("event_id"), summary.get("run_dir"))
            return 0 if summary.get("status") == "success" else 4

        if args.marketing_command == "build-experiment-report":
            summary = build_experiment_report(
                event_id=args.event_id,
                marketing_db=Path(args.db_path),
                ledger_path=Path(args.ledger_path),
                ab_root=Path(args.ab_root),
                run_dir=Path(args.run_dir) if args.run_dir else None,
                cutoff_at=args.cutoff_at,
            )
            if args.json:
                print(json.dumps(summary, ensure_ascii=False, indent=2))
            else:
                logging.info("Built marketing experiment report: event_id=%s run_dir=%s", args.event_id, summary.get("run_dir"))
            return 0 if summary.get("status") in {"success", "partial"} else 4

        if args.marketing_command == "close-experiment":
            summary = close_experiment(
                event_id=args.event_id,
                db_path=Path(args.db_path),
                ledger_path=Path(args.ledger_path),
                close_at=args.close_at,
                ab_root=Path(args.ab_root),
                run_dir=Path(args.run_dir) if args.run_dir else None,
            )
            if args.json:
                print(json.dumps(summary, ensure_ascii=False, indent=2))
            else:
                logging.info("Closed marketing experiment: event_id=%s session_doc=%s", args.event_id, summary.get("session_doc"))
            return 0 if summary.get("status") in {"success", "partial"} else 4

        if args.marketing_command == "watch":
            watched_events = active_events(
                db_path=Path(args.db_path),
                ledger_path=Path(args.ledger_path),
                active_only=bool(args.active_only),
            )
            watched_campaign_ids = dedupe_campaign_ids(watched_events)
            creds = None
            if watched_campaign_ids:
                creds = resolve_marketing_credentials(
                    getattr(args, "store", "ACMEWEAR"),
                    env_file=env_path if env_path.exists() else None,
                    merchant_id=getattr(args, "merchant_id", None),
                    store_code=getattr(args, "store_code", None),
                )
            summary = run_marketing_watch(
                creds=creds,
                db_path=Path(args.db_path),
                ledger_path=Path(args.ledger_path),
                watch_root=Path(args.watch_root),
                active_only=bool(args.active_only),
                target_date=args.date,
                headless=headless,
                stale_minutes=args.stale_minutes,
            )
            if args.json:
                print(json.dumps(summary, ensure_ascii=False, indent=2))
            else:
                logging.info(
                    "Kaspi marketing watch: status=%s heartbeat=%s campaigns=%s run_dir=%s",
                    summary.get("status"),
                    summary.get("heartbeat_status"),
                    summary.get("campaign_ids"),
                    summary.get("run_dir"),
                )
            return 0 if summary.get("status") in {"success", "no_active_events"} else 4

        creds = resolve_marketing_credentials(
            getattr(args, "store", "ACMEWEAR"),
            env_file=env_path if env_path.exists() else None,
            merchant_id=getattr(args, "merchant_id", None),
            store_code=getattr(args, "store_code", None),
        )

        campaign_ids: list[str] = []
        for item in getattr(args, "campaign_id_list", []) or []:
            text = str(item or "").strip()
            if text:
                campaign_ids.append(text)
        for item in str(getattr(args, "campaign_ids", "") or "").split(","):
            text = str(item or "").strip()
            if text:
                campaign_ids.append(text)
        deduped_campaign_ids: list[str] = []
        for item in campaign_ids:
            if item not in deduped_campaign_ids:
                deduped_campaign_ids.append(item)
        if not deduped_campaign_ids:
            logging.error("At least one --campaign-id or --campaign-ids value is required")
            return 2

        run_dir = Path(args.run_dir) if args.run_dir else default_marketing_run_dir(creds.store_name)
        summary = run_kaspi_marketing_fetch(
            creds=creds,
            campaign_ids=deduped_campaign_ids,
            target_date=args.date,
            run_dir=run_dir,
            db_path=Path(args.db_path),
            headless=headless,
        )
        if args.json:
            print(json.dumps(summary, ensure_ascii=False, indent=2))
        else:
            logging.info(
                "Kaspi marketing fetch: store=%s campaigns=%s product_rows=%s run_dir=%s",
                summary.get("store_name"),
                summary.get("campaign_count"),
                summary.get("product_rows"),
                summary.get("run_dir"),
            )
        return 0 if summary.get("status") == "success" else 4

    if args.command == "experiment-dashboard":
        if args.dashboard_command == "serve":
            try:
                serve_dashboard(
                    cache_dir=Path(args.cache_dir),
                    run_dir=Path(args.run_dir) if args.run_dir else None,
                    port=args.port,
                    host=args.host,
                    open_browser=getattr(args, "open", False),
                )
            except FileNotFoundError as exc:
                logging.error(str(exc))
                return 4
            return 0

        campaign_ids: list[str] = []
        for item in getattr(args, "campaign_id_list", []) or []:
            text = str(item or "").strip()
            if text:
                campaign_ids.append(text)

        if args.dashboard_command == "sync":
            headless = True
            if getattr(args, "headed", False):
                headless = False
            if getattr(args, "headless", False):
                headless = True
            env_path = Path(args.env_file) if args.env_file else Path(".env")
            creds = None
            credential_error = None
            if not getattr(args, "offline", False) and not getattr(args, "dry_run", False) and campaign_ids:
                try:
                    creds = resolve_marketing_credentials(
                        getattr(args, "store", "ACMEWEAR"),
                        env_file=env_path if env_path.exists() else None,
                    )
                except Exception as exc:
                    credential_error = str(exc)
                    logging.warning("Could not resolve marketing credentials: %s", exc)
            summary = sync_experiment_dashboard(
                sku_key=args.sku_key,
                store=args.store,
                campaign_ids=campaign_ids,
                date_from=args.date_from,
                date_to=args.date_to,
                marketing_db=Path(args.db_path),
                ab_root=Path(args.ab_root),
                cache_dir=Path(args.cache_dir),
                headless=headless,
                offline=getattr(args, "offline", False),
                dry_run=getattr(args, "dry_run", False),
                allow_stale=getattr(args, "allow_stale", False),
                creds=creds,
                credential_error=credential_error,
            )
            if args.json:
                print(json.dumps(summary, ensure_ascii=False, indent=2))
            else:
                logging.info(
                    "Dashboard sync: status=%s fetch=%s cache_dir=%s",
                    summary.get("status"),
                    summary.get("fetch_status"),
                    summary.get("cache_dir"),
                )
            if summary.get("status") == "fetch_failed":
                return 3
            return 0

        if args.dashboard_command == "build":
            payload = build_experiment_dashboard_payload(
                sku_key=args.sku_key,
                store=args.store,
                campaign_ids=campaign_ids,
                date_from=args.date_from,
                date_to=args.date_to,
                marketing_db=Path(args.db_path),
                ab_root=Path(args.ab_root),
                cache_dir=Path(args.cache_dir),
                daily_history_days=args.daily_history_days,
            )
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            output_dir = Path(args.output_dir) if args.output_dir else Path(args.cache_dir) / timestamp
            artifacts = write_dashboard_artifacts(payload, output_dir)
            build_summary = {
                "status": "success",
                "generated_at": payload.get("generated_at"),
                "output_dir": str(output_dir),
                "scenarios": len(payload.get("scenario_observations", [])),
                "price_groups": len(payload.get("price_group_summaries", [])),
                "anomalies": len(payload.get("anomaly_observations", [])),
                "artifacts": artifacts,
            }
            if args.json:
                print(json.dumps(build_summary, ensure_ascii=False, indent=2))
            elif getattr(args, "plain", False):
                for pg in payload.get("price_group_summaries", []):
                    profit_after = pg.get("unit_profit_after_ads")
                    profit_text = "—" if profit_after is None else str(profit_after)
                    print(
                        f"{pg['price_label']:16s}  units={pg['total_units']:4d}  "
                        f"profit/u={profit_text:>8s}  "
                        f"gate={pg['decision_gate']}"
                    )
            else:
                logging.info(
                    "Dashboard build: price_groups=%s anomalies=%s output_dir=%s",
                    len(payload.get("price_group_summaries", [])),
                    len(payload.get("anomaly_observations", [])),
                    output_dir,
                )
            return 0

        if args.dashboard_command == "refresh":
            headless = True
            if getattr(args, "headed", False):
                headless = False
            if getattr(args, "headless", False):
                headless = True
            env_path = Path(args.env_file) if args.env_file else Path(".env")
            creds = None
            credential_error = None
            if not getattr(args, "offline", False) and campaign_ids:
                try:
                    creds = resolve_marketing_credentials(
                        getattr(args, "store", "ACMEWEAR"),
                        env_file=env_path if env_path.exists() else None,
                    )
                except Exception as exc:
                    credential_error = str(exc)
                    logging.warning("Could not resolve marketing credentials: %s", exc)
            # 1) Sync marketing data
            sync_result = sync_experiment_dashboard(
                sku_key=args.sku_key,
                store=args.store,
                campaign_ids=campaign_ids,
                date_from=args.date_from,
                date_to=args.date_to,
                marketing_db=Path(args.db_path),
                ab_root=Path(args.ab_root),
                cache_dir=Path(args.cache_dir),
                headless=headless,
                offline=getattr(args, "offline", False),
                allow_stale=getattr(args, "allow_stale", False),
                creds=creds,
                credential_error=credential_error,
            )
            if sync_result.get("status") == "fetch_failed" and not getattr(args, "allow_stale", False):
                if args.json:
                    print(json.dumps(sync_result, ensure_ascii=False, indent=2))
                return 3
            # 2) Build dashboard
            payload = build_experiment_dashboard_payload(
                sku_key=args.sku_key,
                store=args.store,
                campaign_ids=campaign_ids,
                date_from=args.date_from,
                date_to=args.date_to,
                marketing_db=Path(args.db_path),
                ab_root=Path(args.ab_root),
                cache_dir=Path(args.cache_dir),
                daily_history_days=args.daily_history_days,
            )
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            output_dir = Path(args.cache_dir) / timestamp
            payload["source_heartbeat"] = build_sync_heartbeat(
                payload.get("source_status", []),
                latest_dashboard_dir=str(output_dir),
            )
            artifacts = write_dashboard_artifacts(payload, output_dir)
            # 3) Write heartbeat
            hb = write_sync_heartbeat(
                Path(args.cache_dir),
                payload.get("source_status", []),
                latest_dashboard_dir=str(output_dir),
            )
            result = {
                "status": "success",
                "generated_at": payload.get("generated_at"),
                "output_dir": str(output_dir),
                "price_groups": len(payload.get("price_group_summaries", [])),
                "anomalies": len(payload.get("anomaly_observations", [])),
                "heartbeat": hb,
                "artifacts": artifacts,
            }
            if args.json:
                print(json.dumps(result, ensure_ascii=False, indent=2))
            else:
                logging.info(
                    "Dashboard refresh: status=%s groups=%s output=%s heartbeat=%s",
                    result["status"],
                    result["price_groups"],
                    result["output_dir"],
                    hb.get("overall_status"),
                )
            return 0

        if args.dashboard_command == "watch-health":
            try:
                date_from, date_to = _resolve_watch_health_window(args)
                sync_date = _resolve_dashboard_relative_date(getattr(args, "sync_date", None) or date_to)
                if sync_date:
                    datetime.strptime(sync_date, "%Y-%m-%d")
            except ValueError as exc:
                logging.error(str(exc))
                return 2

            headless = True
            if getattr(args, "headed", False):
                headless = False
            if getattr(args, "headless", False):
                headless = True

            cache_dir = Path(args.cache_dir)
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            output_dir = cache_dir / timestamp
            watch_dir = cache_dir / "watch_health" / timestamp
            warnings: list[str] = []

            delivery_result: dict[str, Any] = {"status": "skipped"}
            delivery_mode = getattr(args, "delivery_mode", "skip")
            delivery_root = Path(args.delivery_watch_root)
            if delivery_mode == "manual":
                if not getattr(args, "displayed_date", None) or not getattr(args, "expected_date", None):
                    logging.error("--displayed-date and --expected-date are required with --delivery-mode manual")
                    return 2
                capture = build_delivery_capture(
                    store=args.store,
                    city=args.city,
                    city_id=args.city_id,
                    sku_key=args.sku_key,
                    offer_url=args.offer_url,
                    displayed_date=args.displayed_date,
                    expected_date=args.expected_date,
                    source="manual_watch_health",
                )
                delivery_result = record_delivery_capture(capture, delivery_root)
            elif delivery_mode == "public-page":
                if not getattr(args, "offer_url", ""):
                    delivery_result = record_delivery_watch_failure(
                        error="--offer-url is required with --delivery-mode public-page",
                        root=delivery_root,
                        store=args.store,
                        city=args.city,
                        city_id=args.city_id,
                        sku_key=args.sku_key,
                        offer_url=args.offer_url,
                    )
                    warnings.append("delivery_fetch_error:missing_offer_url")
                else:
                    try:
                        capture = fetch_delivery_promise_capture(
                            store=args.store,
                            merchant_id=args.merchant_id,
                            city=args.city,
                            city_id=args.city_id,
                            sku_key=args.sku_key,
                            offer_url=args.offer_url,
                            expected_days=args.expected_days,
                            expected_date=args.expected_date,
                        )
                        delivery_result = record_delivery_capture(capture, delivery_root)
                    except Exception as exc:
                        delivery_result = record_delivery_watch_failure(
                            error=str(exc),
                            root=delivery_root,
                            store=args.store,
                            city=args.city,
                            city_id=args.city_id,
                            sku_key=args.sku_key,
                            offer_url=args.offer_url,
                        )
                        warnings.append(f"delivery_fetch_error:{exc}")

            env_path = Path(args.env_file) if args.env_file else Path(".env")
            creds = None
            credential_error = None
            if not getattr(args, "offline", False) and campaign_ids:
                try:
                    creds = resolve_marketing_credentials(
                        getattr(args, "store", "ACMEWEAR"),
                        env_file=env_path if env_path.exists() else None,
                    )
                except Exception as exc:
                    credential_error = str(exc)
                    warnings.append(f"marketing_credentials_error:{exc}")
                    logging.warning("Could not resolve marketing credentials: %s", exc)

            sync_result = sync_experiment_dashboard(
                sku_key=args.sku_key,
                store=args.store,
                campaign_ids=campaign_ids,
                date_from=sync_date,
                date_to=sync_date,
                marketing_db=Path(args.db_path),
                ab_root=Path(args.ab_root),
                cache_dir=cache_dir,
                headless=headless,
                offline=getattr(args, "offline", False),
                allow_stale=getattr(args, "allow_stale", False),
                creds=creds,
                credential_error=credential_error,
            )
            warnings.extend(sync_result.get("warnings", []) or [])
            if sync_result.get("status") == "fetch_failed" and not getattr(args, "allow_stale", False):
                failure_source_status = sync_result.get("source_status", []) or [
                    {
                        "source_name": "marketing_db",
                        "freshness_status": "FETCH_FAILED",
                        "required": True,
                        "failure_step": "marketing_fetch",
                        "failure_message": "; ".join(warnings) or "marketing fetch failed",
                    }
                ]
                heartbeat = write_sync_heartbeat(cache_dir, failure_source_status, latest_dashboard_dir=None)
                result = {
                    "status": "fetch_failed",
                    "generated_at": now_local_text(),
                    "date_from": date_from,
                    "date_to": date_to,
                    "sync_date": sync_date,
                    "sync": sync_result,
                    "delivery": delivery_result,
                    "heartbeat": heartbeat,
                    "source_status": failure_source_status,
                    "warnings": warnings,
                }
                _write_json_file(watch_dir / "watch_health_summary.json", result)
                _write_json_file(cache_dir / "watch_health" / "latest_heartbeat.json", result)
                if args.json:
                    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
                return 3

            try:
                payload = build_experiment_dashboard_payload(
                    sku_key=args.sku_key,
                    store=args.store,
                    campaign_ids=campaign_ids,
                    date_from=date_from,
                    date_to=date_to,
                    marketing_db=Path(args.db_path),
                    ab_root=Path(args.ab_root),
                    cache_dir=cache_dir,
                    daily_history_days=args.daily_history_days,
                )
                payload["source_heartbeat"] = build_sync_heartbeat(
                    payload.get("source_status", []),
                    latest_dashboard_dir=str(output_dir),
                )
                artifacts = write_dashboard_artifacts(payload, output_dir)
                heartbeat = write_sync_heartbeat(
                    cache_dir,
                    payload.get("source_status", []),
                    latest_dashboard_dir=str(output_dir),
                )
            except Exception as exc:
                result = {
                    "status": "build_failed",
                    "generated_at": now_local_text(),
                    "date_from": date_from,
                    "date_to": date_to,
                    "sync_date": sync_date,
                    "sync": sync_result,
                    "delivery": delivery_result,
                    "error": str(exc),
                    "warnings": warnings,
                }
                _write_json_file(watch_dir / "watch_health_summary.json", result)
                _write_json_file(cache_dir / "watch_health" / "latest_heartbeat.json", result)
                if args.json:
                    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
                else:
                    logging.error("Dashboard watch-health build failed: %s", exc)
                return 4

            overall = heartbeat.get("overall_status", "unknown")
            status = "success" if overall == "green" else "stale"
            result = {
                "status": status,
                "generated_at": now_local_text(),
                "date_from": date_from,
                "date_to": date_to,
                "sync_date": sync_date,
                "output_dir": str(output_dir),
                "sync": sync_result,
                "delivery": delivery_result,
                "heartbeat": heartbeat,
                "source_status": payload.get("source_status", []),
                "artifacts": artifacts,
                "warnings": warnings,
            }
            _write_json_file(watch_dir / "watch_health_summary.json", result)
            _write_json_file(cache_dir / "watch_health" / "latest_heartbeat.json", result)
            if args.json:
                print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
            else:
                logging.info(
                    "Dashboard watch-health: status=%s heartbeat=%s output=%s",
                    status,
                    overall,
                    output_dir,
                )
            if overall != "green" and not getattr(args, "allow_stale", False):
                return 1
            return 0

        if args.dashboard_command == "heartbeat":
            hb = read_sync_heartbeat(Path(args.cache_dir))
            if hb is None:
                if args.json:
                    print(json.dumps({"status": "missing", "message": "No heartbeat file found"}, indent=2))
                else:
                    logging.warning("No heartbeat file found in %s", args.cache_dir)
                return 2
            if args.json:
                print(json.dumps(hb, ensure_ascii=False, indent=2))
            else:
                status = hb.get("overall_status", "unknown")
                last_sync = hb.get("last_sync_at", "unknown")
                logging.info("Heartbeat: %s (last sync: %s)", status, last_sync)
                for name, info in hb.get("sources", {}).items():
                    logging.info("  %s: %s", name, info.get("status", "?"))
            return 0 if hb.get("overall_status") == "green" else 1

        if args.dashboard_command == "backfill-gaps":
            gap_path = Path(args.gap_report)
            if not gap_path.exists():
                logging.error("Gap report not found: %s", gap_path)
                return 2
            try:
                gap_rows = parse_gap_report(gap_path)
            except Exception as exc:
                logging.error("Failed to parse gap report: %s", exc)
                return 2
            campaign_ids = getattr(args, "campaign_id_list", None) or []
            price_policies = getattr(args, "price_policy_list", None) or []
            plan = plan_gap_backfill(
                gap_rows,
                campaign_ids=campaign_ids or None,
                price_policies=price_policies or None,
                date_from=getattr(args, "date_from", None),
                date_to=getattr(args, "date_to", None),
                limit=getattr(args, "limit", None),
            )
            if not plan:
                result = {"status": "empty", "message": "No gaps match the given filters", "planned": 0}
                if args.json:
                    print(json.dumps(result, indent=2))
                else:
                    logging.info("No gaps match filters.")
                return 0

            # Group by date for batch display / fetch
            from collections import defaultdict
            by_date: dict[str, list[str]] = defaultdict(list)
            for row in plan:
                by_date[row["date"]].append(row["campaign_id"])

            if getattr(args, "dry_run", False):
                result = {
                    "status": "dry_run",
                    "planned_rows": len(plan),
                    "planned_dates": len(by_date),
                    "plan": [
                        {"date": d, "campaign_ids": cids}
                        for d, cids in sorted(by_date.items())
                    ],
                }
                if args.json:
                    print(json.dumps(result, ensure_ascii=False, indent=2))
                else:
                    logging.info("Dry-run: %d rows across %d dates", len(plan), len(by_date))
                    for d in sorted(by_date):
                        logging.info("  %s: campaigns %s", d, ", ".join(by_date[d]))
                return 0

            # Live fetch
            if getattr(args, "rebuild_dashboard", False):
                if not getattr(args, "sku_key", None):
                    logging.error("--sku-key is required with --rebuild-dashboard")
                    return 2
                if not getattr(args, "dashboard_date_from", None) or not getattr(args, "dashboard_date_to", None):
                    logging.error(
                        "--dashboard-date-from and --dashboard-date-to are required with --rebuild-dashboard; "
                        "--date-from/--date-to only filter which gaps to fetch"
                    )
                    return 2

            headless = True
            if getattr(args, "headed", False):
                headless = False
            if getattr(args, "headless", False):
                headless = True

            env_path = Path(args.env_file) if getattr(args, "env_file", None) else Path(".env")
            try:
                creds = resolve_marketing_credentials(args.store, env_file=env_path)
            except Exception as exc:
                logging.error("Could not resolve marketing credentials: %s", exc)
                return 3

            db_path = Path(args.db_path)
            backfill_ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            backfill_dir = Path(args.cache_dir) / "gap_backfills" / backfill_ts
            backfill_dir.mkdir(parents=True, exist_ok=True)

            fetched = []
            failed = []
            for date_str in sorted(by_date):
                date_campaigns = by_date[date_str]
                run_dir = backfill_dir / f"date={date_str}"
                try:
                    run_kaspi_marketing_fetch(
                        creds=creds,
                        campaign_ids=date_campaigns,
                        target_date=date_str,
                        run_dir=run_dir,
                        db_path=db_path,
                        headless=headless,
                    )
                    fetched.append({"date": date_str, "campaign_ids": date_campaigns, "status": "success", "run_dir": str(run_dir)})
                except Exception as exc:
                    logging.warning("Fetch failed for %s: %s", date_str, exc)
                    failed.append({"date": date_str, "campaign_ids": date_campaigns, "run_dir": str(run_dir), "error": str(exc)})

            if failed and not getattr(args, "allow_stale", False):
                result = {
                    "status": "partial_failure",
                    "fetched": len(fetched),
                    "failed": len(failed),
                    "fetched_rows": fetched,
                    "failed_rows": failed,
                    "backfill_dir": str(backfill_dir),
                }
                # Write summary even on failure
                with open(backfill_dir / "backfill_summary.json", "w") as f:
                    json.dump(result, f, indent=2, ensure_ascii=False)
                if args.json:
                    print(json.dumps(result, ensure_ascii=False, indent=2))
                else:
                    logging.error("Partial failure: %d fetched, %d failed", len(fetched), len(failed))
                return 4

            # Write backfill summary
            backfill_summary = {
                "status": "success" if not failed else "partial_success",
                "fetched": len(fetched),
                "failed": len(failed),
                "fetched_rows": fetched,
                "failed_rows": failed,
                "backfill_dir": str(backfill_dir),
            }
            with open(backfill_dir / "backfill_summary.json", "w") as f:
                json.dump(backfill_summary, f, indent=2, ensure_ascii=False)

            # Optional rebuild
            dashboard_result = None
            if getattr(args, "rebuild_dashboard", False):
                rebuild_campaigns = campaign_ids or [r["campaign_id"] for r in plan]
                rebuild_campaigns = list(dict.fromkeys(rebuild_campaigns))
                try:
                    payload = build_experiment_dashboard_payload(
                        sku_key=args.sku_key,
                        store=args.store,
                        campaign_ids=rebuild_campaigns,
                        date_from=args.dashboard_date_from,
                        date_to=args.dashboard_date_to,
                        marketing_db=db_path,
                        ab_root=Path(args.ab_root),
                        cache_dir=Path(args.cache_dir),
                        daily_history_days=args.daily_history_days,
                    )
                    output_dir = Path(args.output_dir) if getattr(args, "output_dir", None) else backfill_dir / "dashboard"
                    artifacts = write_dashboard_artifacts(payload, output_dir)
                    dashboard_result = {
                        "status": "success",
                        "output_dir": str(output_dir),
                        "artifacts": artifacts,
                        "price_groups": len(payload.get("price_group_summaries", [])),
                        "anomalies": len(payload.get("anomaly_observations", [])),
                    }
                except Exception as exc:
                    logging.error("Dashboard rebuild failed: %s", exc)
                    dashboard_result = {"status": "failed", "error": str(exc)}
                    if not getattr(args, "allow_stale", False):
                        backfill_summary["dashboard"] = dashboard_result
                        if args.json:
                            print(json.dumps(backfill_summary, ensure_ascii=False, indent=2))
                        return 5

            result = {**backfill_summary}
            if dashboard_result:
                result["dashboard"] = dashboard_result
            if args.json:
                print(json.dumps(result, ensure_ascii=False, indent=2))
            else:
                logging.info(
                    "Backfill complete: %d fetched, %d failed",
                    len(fetched), len(failed),
                )
                if dashboard_result:
                    logging.info("Dashboard rebuild: %s", dashboard_result.get("status"))
            return 0

        logging.error("Unknown dashboard command")
        return 2

    if args.command == "delivery-health":
        if args.delivery_command == "record":
            capture = build_delivery_capture(
                store=args.store,
                city=args.city,
                city_id=args.city_id,
                sku_key=args.sku_key,
                offer_url=args.offer_url,
                displayed_date=args.displayed_date,
                expected_date=args.expected_date,
                source=args.source,
                note=args.note,
                observed_at=args.observed_at,
            )
            summary = record_delivery_capture(capture, Path(args.watch_root))
            if args.json:
                print(json.dumps({**summary, "capture": capture}, ensure_ascii=False, indent=2))
            else:
                logging.info(
                    "Delivery health: status=%s delta_days=%s heartbeat=%s",
                    summary.get("status"),
                    summary.get("delta_days"),
                    summary.get("heartbeat_path"),
                )
            return 0
        if args.delivery_command == "watch":
            try:
                capture = fetch_delivery_promise_capture(
                    store=args.store,
                    merchant_id=args.merchant_id,
                    city=args.city,
                    city_id=args.city_id,
                    sku_key=args.sku_key,
                    offer_url=args.offer_url,
                    expected_days=args.expected_days,
                    expected_date=args.expected_date,
                    source=args.source,
                    note=args.note,
                )
                summary = record_delivery_capture(capture, Path(args.watch_root))
                exit_code = 0
            except Exception as exc:
                capture = {}
                summary = record_delivery_watch_failure(
                    error=str(exc),
                    root=Path(args.watch_root),
                    store=args.store,
                    city=args.city,
                    city_id=args.city_id,
                    sku_key=args.sku_key,
                    offer_url=args.offer_url,
                )
                exit_code = 4
            if args.json:
                print(json.dumps({**summary, "capture": capture}, ensure_ascii=False, indent=2))
            else:
                logging.info(
                    "Delivery watch: status=%s delta_days=%s heartbeat=%s",
                    summary.get("status"),
                    summary.get("delta_days"),
                    summary.get("heartbeat_path"),
                )
            return exit_code
        logging.error("Unknown delivery-health command")
        return 2

    if args.command == "kaspi-pending-dispute":
        headless = True
        if getattr(args, "headed", False):
            headless = False
        if getattr(args, "headless", False):
            headless = True

        env_path = Path(args.env_file) if args.env_file else Path("~/Docs/Autonomous_business/.env")
        creds = resolve_store_credentials(getattr(args, "store", "ACMEWEAR"), env_path)
        merchant_code = resolve_pending_merchant_code(creds["store_name"], args.merchant_code)
        run_dir = Path(args.run_dir) if args.run_dir else default_pending_dispute_run_dir(creds["store_name"])
        summary = run_kaspi_pending_trash_dispute(
            store_name=creds["store_name"],
            email=creds["email"],
            password=creds["password"],
            merchant_code=merchant_code,
            run_dir=run_dir,
            comment=args.comment,
            confirm=args.confirm,
            headless=headless,
            page_size=args.page_size,
            verify_timeout_seconds=args.verify_timeout_seconds,
            verify_poll_seconds=args.verify_poll_seconds,
            ui_verify=not args.skip_ui_verify,
        )
        if args.json:
            print(json.dumps(summary, ensure_ascii=False, indent=2))
        else:
            logging.info(
                "Kaspi pending dispute: store=%s status=%s actionable=%s skipped=%s run_dir=%s",
                summary.get("store_name"),
                summary.get("status"),
                summary.get("preflight", {}).get("actionable_count"),
                summary.get("preflight", {}).get("known_skip_count"),
                summary.get("run_dir"),
            )
        return 0 if summary.get("status") in {"success", "dry_run", "nothing_to_do"} else 4

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
            if not args.confirm_raw_pricelist_upload:
                payload = {
                    "status": "blocked",
                    "error": "raw_pricelist_upload_requires_--confirm-raw-pricelist-upload",
                    "preferred_command": "kaspi-pricelist safe-active-patch",
                    "reason": "Kaspi pricelist upload is a full sale-surface state operation, not a row patch.",
                }
                if args.json:
                    print(json.dumps(payload, ensure_ascii=False, indent=2))
                else:
                    logging.error(payload["error"])
                return 2
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

        if args.pricelist_command == "history-detail":
            run_dir = Path(args.run_dir) if args.run_dir else default_run_dir(creds["store_name"]) / "history_detail"
            summary = run_kaspi_pricelist_history_detail(
                store_name=creds["store_name"],
                email=creds["email"],
                password=creds["password"],
                detail_ref=args.detail_ref,
                run_dir=run_dir,
                headless=headless,
                detail_filter=args.detail_filter,
                download_result_excel=args.download_result_excel,
            )
            if args.json:
                print(json.dumps(summary, ensure_ascii=False, indent=2))
            else:
                logging.info(
                    "Kaspi pricelist history detail: store=%s status=%s run_dir=%s",
                    summary.get("store_name"),
                    summary.get("status"),
                    summary.get("run_dir"),
                )
            return 0 if summary.get("status") == "success" else 4

        if args.pricelist_command == "safe-active-patch":
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
                    run_dir=run_dir / "download",
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

            try:
                build_summary = build_safe_active_patch(
                    active_path=active_path,
                    archive_path=archive_path,
                    updates_path=Path(args.updates_csv),
                    output_dir=run_dir / "safe_active_patch",
                    store_name=creds["store_name"],
                    expected_active_before=args.expected_active_before,
                    expected_active_after=args.expected_active_after,
                    allow_activate_from_archive=args.allow_activate_from_archive,
                    restriction_ledger_path=Path(args.restriction_ledger) if args.restriction_ledger else None,
                    restriction_probe_approval_path=Path(args.restriction_probe_approval) if args.restriction_probe_approval else None,
                )
            except SafeActivePatchError as exc:
                payload = {"status": "blocked", "error": str(exc), "run_dir": str(run_dir)}
                if args.json:
                    print(json.dumps(payload, ensure_ascii=False, indent=2))
                else:
                    logging.error(str(exc))
                return 4
            summary: dict[str, Any] = {
                "store_name": creds["store_name"],
                "run_dir": str(run_dir),
                "download": download_summary,
                "build": build_summary,
                "status": build_summary.get("status"),
            }
            if args.apply:
                if args.confirm != SAFE_ACTIVE_CONFIRM_PHRASE:
                    summary["status"] = "blocked"
                    summary["apply"] = {
                        "status": "skipped",
                        "reason": f"--confirm must equal {SAFE_ACTIVE_CONFIRM_PHRASE}",
                    }
                elif build_summary.get("status") != "ready":
                    summary["status"] = "blocked"
                    summary["apply"] = {"status": "skipped", "reason": "safe_patch_build_not_ready"}
                else:
                    upload_summary = run_kaspi_pricelist_upload(
                        store_name=creds["store_name"],
                        email=creds["email"],
                        password=creds["password"],
                        file_paths=[Path(build_summary["active_output"])],
                        run_dir=run_dir / "upload",
                        headless=headless,
                        timeout_seconds=args.timeout_seconds,
                        processing_grace_seconds=args.processing_grace_seconds,
                    )
                    summary["apply"] = upload_summary
                    summary["status"] = "success" if upload_summary.get("status") == "success" else "failed"
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
                            verify_summary = verify_safe_active_upload(
                                intended_active_path=Path(build_summary["active_output"]),
                                redownloaded_active_path=after_paths["ACTIVE"],
                                updates_path=Path(args.updates_csv),
                            )
                            summary["verify"] = verify_summary
                            if verify_summary.get("status") != "ok":
                                summary["status"] = "failed"
                        else:
                            summary["status"] = "failed"
            _write_json_file(run_dir / "safe_active_patch_command_summary.json", summary)
            if args.apply:
                _write_json_file(run_dir / "safe_active_patch_apply_summary.json", summary)
            if args.json:
                print(json.dumps(summary, ensure_ascii=False, indent=2))
            else:
                logging.info(
                    "Kaspi safe-active-patch: store=%s status=%s active_before=%s active_after=%s run_dir=%s",
                    creds["store_name"],
                    summary.get("status"),
                    build_summary.get("active_before_count"),
                    build_summary.get("active_after_count"),
                    run_dir,
                )
            return 0 if summary.get("status") in {"ready", "success"} else 4

        if args.pricelist_command in {"inspect", "build", "sync"}:
            run_dir = Path(args.run_dir) if args.run_dir else default_run_dir(creds["store_name"])
            if args.pricelist_command == "sync" and args.upload and not args.confirm_legacy_archive_active_upload:
                payload = {
                    "status": "blocked",
                    "store_name": creds["store_name"],
                    "run_dir": str(run_dir),
                    "upload": {
                        "status": "skipped",
                        "reason": "legacy_archive_active_upload_requires_--confirm-legacy-archive-active-upload",
                        "preferred_command": "kaspi-pricelist safe-active-patch",
                    },
                }
                if args.json:
                    print(json.dumps(payload, ensure_ascii=False, indent=2))
                else:
                    logging.error(payload["upload"]["reason"])
                return 2
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
                if not args.confirm_legacy_archive_active_upload:
                    summary["upload"] = {
                        "status": "skipped",
                        "reason": "legacy_archive_active_upload_requires_--confirm-legacy-archive-active-upload",
                        "preferred_command": "kaspi-pricelist safe-active-patch",
                    }
                    if args.json:
                        print(json.dumps(summary, ensure_ascii=False, indent=2))
                    else:
                        logging.error(summary["upload"]["reason"])
                    return 2
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

    if args.command == "acmewear-bundles":
        if args.acmewear_bundles_command == "activation-build":
            run_dir = Path(args.run_dir) if args.run_dir else Path("runs/acmewear_bundle_activation_build") / datetime.now().strftime("%Y%m%d_%H%M%S")
            try:
                summary = build_acmewear_bundle_activation_pack(
                    active_path=Path(args.active_path),
                    archive_path=Path(args.archive_path),
                    calendar_path=Path(args.calendar_path),
                    registry_path=Path(args.registry_path),
                    capture_path=Path(args.capture_path),
                    output_dir=run_dir,
                    image_moderation_cleared=bool(args.image_moderation_cleared),
                    stock_warehouse=str(args.stock_warehouse or "PP1"),
                )
            except BundleActivationError as exc:
                payload = {"status": "blocked", "error": str(exc), "run_dir": str(run_dir)}
                if args.json:
                    print(json.dumps(payload, ensure_ascii=False, indent=2))
                else:
                    logging.error("ACMEWEAR bundle activation build blocked: %s", exc)
                return 4
            if args.json:
                print(json.dumps(summary, ensure_ascii=False, indent=2))
            else:
                logging.info(
                    "ACMEWEAR bundle activation build: status=%s rows_to_activate=%s run_dir=%s",
                    summary.get("status"),
                    summary.get("rows_to_activate"),
                    run_dir,
                )
            return 0

        logging.error("Unknown acmewear-bundles command")
        return 2

    if args.command == "acmewear-express-sidecar":
        if args.acmewear_express_command == "init-sidecar":
            run_dir = (
                Path(args.run_dir)
                if args.run_dir
                else DEFAULT_ACMEWEAR_EXPRESS_SIDECAR_RUN_ROOT
                / f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_sidecar_init"
            )
            try:
                summary = run_sidecar_init(
                    sidecar_csv=Path(args.sidecar_csv),
                    run_dir=run_dir,
                    schema_csv=Path(args.schema_csv) if args.schema_csv else None,
                )
            except (OSError, ValueError, RuntimeError) as exc:
                payload = {
                    "status": "blocked",
                    "error": str(exc),
                    "run_dir": str(run_dir),
                    "external_writes": {
                        "kaspi_order_mutation": False,
                        "telegram_send": False,
                        "print_job": False,
                        "autonomous_business_write": False,
                    },
                }
                if args.json:
                    print(json.dumps(payload, ensure_ascii=False, indent=2))
                else:
                    logging.error("ACMEWEAR Express sidecar init blocked: %s", exc)
                return 4
            if args.json:
                print(json.dumps(summary, ensure_ascii=False, indent=2))
            else:
                logging.info(
                    "ACMEWEAR Express sidecar init: action=%s path=%s",
                    summary.get("action"),
                    summary.get("sidecar_csv"),
                )
            return 0

        if args.acmewear_express_command == "build":
            if args.update_sidecar and not args.sidecar_csv:
                payload = {
                    "status": "blocked",
                    "error": "--update-sidecar requires --sidecar-csv",
                    "external_writes": {
                        "kaspi_order_mutation": False,
                        "telegram_send": False,
                        "print_job": False,
                        "autonomous_business_write": False,
                    },
                }
                if args.json:
                    print(json.dumps(payload, ensure_ascii=False, indent=2))
                else:
                    logging.error(payload["error"])
                return 2
            run_dir = (
                Path(args.run_dir)
                if args.run_dir
                else DEFAULT_ACMEWEAR_EXPRESS_SIDECAR_RUN_ROOT / datetime.now().strftime("%Y%m%d_%H%M%S")
            )
            try:
                summary = run_sidecar_build_from_file(
                    input_path=Path(args.input_json),
                    run_dir=run_dir,
                    detected_at=args.detected_at or None,
                    sidecar_csv=Path(args.sidecar_csv) if args.sidecar_csv else None,
                    update_sidecar=bool(args.update_sidecar),
                    persist_actionable_only=bool(args.persist_actionable_only),
                    local_ref_prefix=str(args.local_ref_prefix or "order"),
                )
            except (OSError, ValueError, json.JSONDecodeError) as exc:
                payload = {
                    "status": "blocked",
                    "error": str(exc),
                    "run_dir": str(run_dir),
                    "external_writes": {
                        "kaspi_order_mutation": False,
                        "telegram_send": False,
                        "print_job": False,
                        "autonomous_business_write": False,
                    },
                }
                if args.json:
                    print(json.dumps(payload, ensure_ascii=False, indent=2))
                else:
                    logging.error("ACMEWEAR Express sidecar build blocked: %s", exc)
                return 4
            if args.json:
                print(json.dumps(summary, ensure_ascii=False, indent=2))
            else:
                logging.info(
                    "ACMEWEAR Express sidecar build: mode=%s incoming=%s alerts=%s run_dir=%s",
                    summary.get("mode"),
                    summary.get("incoming_sidecar_rows"),
                    summary.get("alert_queue_rows"),
                    run_dir,
                )
            return 0

        if args.acmewear_express_command == "manual-intake":
            if args.update_sidecar and not args.sidecar_csv:
                payload = {
                    "status": "blocked",
                    "error": "--update-sidecar requires --sidecar-csv",
                    "external_writes": {
                        "kaspi_order_mutation": False,
                        "telegram_send": False,
                        "print_job": False,
                        "autonomous_business_write": False,
                    },
                }
                if args.json:
                    print(json.dumps(payload, ensure_ascii=False, indent=2))
                else:
                    logging.error(payload["error"])
                return 2
            run_dir = (
                Path(args.run_dir)
                if args.run_dir
                else DEFAULT_ACMEWEAR_EXPRESS_SIDECAR_RUN_ROOT
                / f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_manual_intake"
            )
            order_ref_text = ""
            if args.order_ref_env:
                order_ref_text = os.environ.get(str(args.order_ref_env), "")
                if not order_ref_text:
                    payload = {
                        "status": "blocked",
                        "error": f"Missing order ref env var: {args.order_ref_env}",
                        "run_dir": str(run_dir),
                        "external_writes": {
                            "kaspi_order_mutation": False,
                            "telegram_send": False,
                            "print_job": False,
                            "autonomous_business_write": False,
                        },
                    }
                    if args.json:
                        print(json.dumps(payload, ensure_ascii=False, indent=2))
                    else:
                        logging.error(payload["error"])
                    return 2
            try:
                summary = run_manual_sidecar_intake(
                    run_dir=run_dir,
                    sidecar_csv=Path(args.sidecar_csv) if args.sidecar_csv else None,
                    update_sidecar=bool(args.update_sidecar),
                    delivery_kind=str(args.delivery_kind),
                    local_ref=str(args.local_ref),
                    order_hash=str(args.order_hash or ""),
                    order_ref_text=order_ref_text,
                    detected_at=args.detected_at or None,
                    created_at=str(args.created_at or ""),
                    delivery_slot_label=str(args.delivery_slot_label or ""),
                    courier_planning_at=str(args.courier_planning_at or ""),
                    sku_key=str(args.sku_key or ""),
                    merchant_article=str(args.merchant_article or ""),
                    ordered_size=str(args.ordered_size or ""),
                    height_cm=str(args.height_cm or ""),
                    weight_kg=str(args.weight_kg or ""),
                    my_size=str(args.my_size or ""),
                    owner_size_override=str(args.owner_size_override or ""),
                    size_source=str(args.size_source or ""),
                    waybill_present=bool(args.waybill_present),
                    cropped_label_path=str(args.cropped_label_path or ""),
                    cropped_label_sha256=str(args.cropped_label_sha256 or ""),
                    telegram_alert_status=str(args.telegram_alert_status or ""),
                    telegram_label_status=str(args.telegram_label_status or ""),
                    printed_status=str(args.printed_status or ""),
                    close_status=str(args.close_status or "OPEN"),
                    build_shift_packet=bool(args.build_shift_packet),
                    shift_run_root=Path(args.shift_run_root),
                    env=os.environ,
                )
            except (OSError, ValueError, RuntimeError) as exc:
                payload = {
                    "status": "blocked",
                    "error": str(exc),
                    "run_dir": str(run_dir),
                    "external_writes": {
                        "kaspi_order_mutation": False,
                        "telegram_send": False,
                        "print_job": False,
                        "autonomous_business_write": False,
                    },
                }
                if args.json:
                    print(json.dumps(payload, ensure_ascii=False, indent=2))
                else:
                    logging.error("ACMEWEAR Express manual intake blocked: %s", exc)
                return 2
            if args.json:
                print(json.dumps(summary, ensure_ascii=False, indent=2))
            else:
                logging.info(
                    "ACMEWEAR Express manual intake: mode=%s local_ref=%s operator_rows=%s run_dir=%s",
                    summary.get("mode"),
                    summary.get("local_ref"),
                    summary.get("operator_queue_rows"),
                    run_dir,
                )
            return 0

        if args.acmewear_express_command == "update-row":
            run_dir = (
                Path(args.run_dir)
                if args.run_dir
                else DEFAULT_ACMEWEAR_EXPRESS_SIDECAR_RUN_ROOT
                / f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_sidecar_row_update"
            )
            try:
                summary = run_sidecar_row_update(
                    sidecar_csv=Path(args.sidecar_csv),
                    run_dir=run_dir,
                    order_hash=str(args.order_hash or ""),
                    local_ref=str(args.local_ref or ""),
                    sidecar_idempotency_key=str(args.sidecar_idempotency_key or ""),
                    ordered_size=str(args.ordered_size or ""),
                    height_cm=str(args.height_cm or ""),
                    weight_kg=str(args.weight_kg or ""),
                    my_size=str(args.my_size or ""),
                    owner_size_override=str(args.owner_size_override or ""),
                    size_source=str(args.size_source or ""),
                    cropped_label_path=str(args.cropped_label_path or ""),
                    cropped_label_sha256=str(args.cropped_label_sha256 or ""),
                    telegram_alert_status=str(args.telegram_alert_status or ""),
                    telegram_label_status=str(args.telegram_label_status or ""),
                    printed_status=str(args.printed_status or ""),
                    close_status=str(args.close_status or ""),
                    build_shift_packet=bool(args.build_shift_packet),
                    shift_run_root=Path(args.shift_run_root),
                    env=os.environ,
                )
            except (OSError, ValueError, RuntimeError) as exc:
                payload = {
                    "status": "blocked",
                    "error": str(exc),
                    "run_dir": str(run_dir),
                    "external_writes": {
                        "kaspi_order_mutation": False,
                        "telegram_send": False,
                        "print_job": False,
                        "autonomous_business_write": False,
                    },
                }
                if args.json:
                    print(json.dumps(payload, ensure_ascii=False, indent=2))
                else:
                    logging.error("ACMEWEAR Express sidecar row update blocked: %s", exc)
                return 2
            if args.json:
                print(json.dumps(summary, ensure_ascii=False, indent=2))
            else:
                logging.info(
                    "ACMEWEAR Express sidecar row update: local_ref=%s state=%s blockers=%s run_dir=%s",
                    summary.get("local_ref"),
                    summary.get("sidecar_state"),
                    summary.get("blockers"),
                    run_dir,
                )
            return 0

        if args.acmewear_express_command == "fetch-once":
            if args.update_sidecar and not args.sidecar_csv:
                payload = {
                    "status": "blocked",
                    "error": "--update-sidecar requires --sidecar-csv",
                    "external_writes": {
                        "kaspi_order_mutation": False,
                        "telegram_send": False,
                        "print_job": False,
                        "autonomous_business_write": False,
                    },
                }
                if args.json:
                    print(json.dumps(payload, ensure_ascii=False, indent=2))
                else:
                    logging.error(payload["error"])
                return 2
            token_env = str(args.token_env or DEFAULT_ACMEWEAR_KASPI_TOKEN_ENV)
            token, effective_token_env, token_env_candidates = _resolve_acmewear_express_token_from_env(token_env)
            run_dir = (
                Path(args.run_dir)
                if args.run_dir
                else DEFAULT_ACMEWEAR_EXPRESS_SIDECAR_RUN_ROOT / f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_fetch_once"
            )
            if not token:
                payload = {
                    "status": "blocked",
                    "error": f"Missing token env var; checked: {', '.join(token_env_candidates)}",
                    "requested_token_env": token_env,
                    "effective_token_env": effective_token_env,
                    "run_dir": str(run_dir),
                    "external_writes": {
                        "kaspi_order_mutation": False,
                        "telegram_send": False,
                        "print_job": False,
                        "autonomous_business_write": False,
                    },
                }
                if args.json:
                    print(json.dumps(payload, ensure_ascii=False, indent=2))
                else:
                    logging.error(payload["error"])
                return 2
            created_from = str(args.created_from or "")
            created_to = str(args.created_to or "")
            if args.lookback_hours is not None:
                try:
                    created_from, created_to = _resolve_created_bounds_with_lookback(
                        created_from=created_from,
                        created_to=created_to,
                        lookback_hours=int(args.lookback_hours),
                    )
                except ValueError as exc:
                    payload = {
                        "status": "blocked",
                        "error": str(exc),
                        "run_dir": str(run_dir),
                        "external_writes": {
                            "kaspi_order_mutation": False,
                            "telegram_send": False,
                            "print_job": False,
                            "autonomous_business_write": False,
                        },
                    }
                    if args.json:
                        print(json.dumps(payload, ensure_ascii=False, indent=2))
                    else:
                        logging.error(payload["error"])
                    return 2
            try:
                summary = run_sidecar_fetch_once(
                    token=token,
                    run_dir=run_dir,
                    detected_at=args.detected_at or None,
                    sidecar_csv=Path(args.sidecar_csv) if args.sidecar_csv else None,
                    update_sidecar=bool(args.update_sidecar),
                    persist_actionable_only=bool(args.persist_actionable_only),
                    local_ref_prefix=str(args.local_ref_prefix or "api"),
                    states=args.states or None,
                    statuses=args.statuses or None,
                    delivery_types=args.delivery_types or None,
                    created_from=created_from,
                    created_to=created_to,
                    page_size=int(args.page_size),
                    max_pages=int(args.max_pages),
                    timeout_seconds=int(args.timeout_seconds),
                )
            except KaspiOrderFetchError as exc:
                payload = {
                    "status": "blocked",
                    "error": str(exc),
                    "run_dir": str(run_dir),
                    "external_writes": {
                        "kaspi_order_mutation": False,
                        "telegram_send": False,
                        "print_job": False,
                        "autonomous_business_write": False,
                    },
                }
                if args.json:
                    print(json.dumps(payload, ensure_ascii=False, indent=2))
                else:
                    logging.error("ACMEWEAR Express sidecar fetch blocked: %s", exc)
                return 4
            if args.json:
                print(json.dumps(summary, ensure_ascii=False, indent=2))
            else:
                logging.info(
                    "ACMEWEAR Express sidecar fetch: mode=%s source_rows=%s alerts=%s run_dir=%s",
                    summary.get("mode"),
                    summary.get("source_rows"),
                    summary.get("alert_queue_rows"),
                    run_dir,
                )
            return 0

        if args.acmewear_express_command == "watch":
            if args.update_sidecar and not args.sidecar_csv:
                payload = {
                    "status": "blocked",
                    "error": "--update-sidecar requires --sidecar-csv",
                    "external_writes": {
                        "kaspi_order_mutation": False,
                        "telegram_send": False,
                        "print_job": False,
                        "autonomous_business_write": False,
                    },
                }
                if args.json:
                    print(json.dumps(payload, ensure_ascii=False, indent=2))
                else:
                    logging.error(payload["error"])
                return 2
            token_env = str(args.token_env or DEFAULT_ACMEWEAR_KASPI_TOKEN_ENV)
            token, effective_token_env, token_env_candidates = _resolve_acmewear_express_token_from_env(token_env)
            run_dir = (
                Path(args.run_dir)
                if args.run_dir
                else DEFAULT_ACMEWEAR_EXPRESS_SIDECAR_RUN_ROOT / f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_watch"
            )
            if not token:
                run_dir.mkdir(parents=True, exist_ok=True)
                payload = {
                    "schema_version": "acmewear_express_selfpickup_watch.v1",
                    "status": "blocked",
                    "error": f"Missing token env var; checked: {', '.join(token_env_candidates)}",
                    "requested_token_env": token_env,
                    "effective_token_env": effective_token_env,
                    "run_dir": str(run_dir),
                    "external_writes": {
                        "kaspi_order_mutation": False,
                        "telegram_send": False,
                        "print_job": False,
                        "autonomous_business_write": False,
                    },
                }
                heartbeat_path = run_dir / "watch_heartbeat.json"
                heartbeat_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
                payload["heartbeat_path"] = str(heartbeat_path)
                if args.json:
                    print(json.dumps(payload, ensure_ascii=False, indent=2))
                else:
                    logging.error(payload["error"])
                return 2
            try:
                summary = run_sidecar_watch_loop(
                    token=token,
                    run_dir=run_dir,
                    detected_at=args.detected_at or None,
                    sidecar_csv=Path(args.sidecar_csv) if args.sidecar_csv else None,
                    update_sidecar=bool(args.update_sidecar),
                    persist_actionable_only=bool(args.persist_actionable_only),
                    local_ref_prefix=str(args.local_ref_prefix or "watch"),
                    states=args.states or None,
                    statuses=args.statuses or None,
                    delivery_types=args.delivery_types or None,
                    created_from=str(args.created_from or ""),
                    created_to=str(args.created_to or ""),
                    lookback_hours=int(args.lookback_hours),
                    cycles=int(args.cycles),
                    interval_seconds=int(args.interval_seconds),
                    page_size=int(args.page_size),
                    max_pages=int(args.max_pages),
                    timeout_seconds=int(args.timeout_seconds),
                )
            except KaspiOrderFetchError as exc:
                payload = {
                    "status": "blocked",
                    "error": str(exc),
                    "run_dir": str(run_dir),
                    "heartbeat_path": str(run_dir / "watch_heartbeat.json"),
                    "external_writes": {
                        "kaspi_order_mutation": False,
                        "telegram_send": False,
                        "print_job": False,
                        "autonomous_business_write": False,
                    },
                }
                if args.json:
                    print(json.dumps(payload, ensure_ascii=False, indent=2))
                else:
                    logging.error("ACMEWEAR Express sidecar watch blocked: %s", exc)
                return 4
            if args.json:
                print(json.dumps(summary, ensure_ascii=False, indent=2))
            else:
                logging.info(
                    "ACMEWEAR Express sidecar watch: cycles=%s heartbeat=%s run_dir=%s",
                    summary.get("cycles_completed"),
                    summary.get("heartbeat_path"),
                    run_dir,
                )
            return 0

        if args.acmewear_express_command == "selfpickup-scan":
            run_dir = (
                Path(args.run_dir)
                if args.run_dir
                else DEFAULT_ACMEWEAR_EXPRESS_SIDECAR_RUN_ROOT
                / f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_selfpickup_sample_scan"
            )
            try:
                summary = run_selfpickup_sample_scan(
                    input_csv=Path(args.input_csv),
                    run_dir=run_dir,
                )
            except (OSError, ValueError, RuntimeError) as exc:
                payload = {
                    "status": "blocked",
                    "error": str(exc),
                    "run_dir": str(run_dir),
                    "external_writes": {
                        "kaspi_order_mutation": False,
                        "telegram_send": False,
                        "print_job": False,
                        "autonomous_business_write": False,
                    },
                }
                if args.json:
                    print(json.dumps(payload, ensure_ascii=False, indent=2))
                else:
                    logging.error("ACMEWEAR self-pickup sample scan blocked: %s", exc)
                return 4
            if args.json:
                print(json.dumps(summary, ensure_ascii=False, indent=2))
            else:
                logging.info(
                    "ACMEWEAR self-pickup sample scan: gate=%s proven=%s report=%s",
                    summary.get("gate"),
                    summary.get("proven_seller_selfpickup_rows"),
                    summary.get("selfpickup_sample_scan_report_path"),
                )
            return 0

        if args.acmewear_express_command == "telegram-alert":
            run_dir = (
                Path(args.run_dir)
                if args.run_dir
                else DEFAULT_ACMEWEAR_EXPRESS_SIDECAR_RUN_ROOT
                / f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_telegram_alert"
            )
            bot_token_env = str(args.bot_token_env or DEFAULT_TELEGRAM_BOT_TOKEN_ENV)
            chat_id_env = str(args.chat_id_env or DEFAULT_TELEGRAM_ALERT_CHAT_ID_ENV)
            bot_token, effective_bot_token_env = _resolve_env_alias_from_env(
                bot_token_env,
                TELEGRAM_BOT_TOKEN_ENV_ALIASES,
                DEFAULT_TELEGRAM_BOT_TOKEN_ENV,
            )
            chat_id, effective_chat_id_env = _resolve_env_alias_from_env(
                chat_id_env,
                TELEGRAM_ALERT_CHAT_ID_ENV_ALIASES,
                DEFAULT_TELEGRAM_ALERT_CHAT_ID_ENV,
            )
            try:
                summary = run_telegram_alerts_from_queue(
                    alert_queue_csv=Path(args.alert_queue_csv),
                    ledger_csv=Path(args.ledger_csv),
                    run_dir=run_dir,
                    send=bool(args.send),
                    bot_token=bot_token,
                    chat_id=chat_id,
                    chat_config_ref=str(args.chat_config_ref or f"{effective_bot_token_env}+{effective_chat_id_env}"),
                    timeout_seconds=int(args.timeout_seconds),
                    max_alerts=int(args.max_alerts) if int(args.max_alerts or 0) > 0 else None,
                )
            except (OSError, ValueError, RuntimeError) as exc:
                payload = {
                    "status": "blocked",
                    "error": str(exc),
                    "run_dir": str(run_dir),
                    "external_writes": {
                        "kaspi_order_mutation": False,
                        "telegram_send": False,
                        "print_job": False,
                        "autonomous_business_write": False,
                    },
                }
                if args.json:
                    print(json.dumps(payload, ensure_ascii=False, indent=2))
                else:
                    logging.error("ACMEWEAR Express Telegram alert blocked: %s", exc)
                return 2 if "requires bot token and chat id" in str(exc) else 4
            if args.json:
                print(json.dumps(summary, ensure_ascii=False, indent=2))
            else:
                logging.info(
                    "ACMEWEAR Express Telegram alert: mode=%s sent=%s skipped=%s run_dir=%s",
                    summary.get("send_mode"),
                    summary.get("sent_rows"),
                    summary.get("skipped_duplicate_rows"),
                    run_dir,
                )
            return 0

        if args.acmewear_express_command == "prepare-label":
            run_dir = (
                Path(args.run_dir)
                if args.run_dir
                else DEFAULT_ACMEWEAR_EXPRESS_SIDECAR_RUN_ROOT
                / f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_prepare_label"
            )
            token_env = str(args.token_env or DEFAULT_ACMEWEAR_KASPI_TOKEN_ENV)
            token, effective_token_env, token_env_candidates = _resolve_acmewear_express_token_from_env(token_env)
            bot_token_env = str(args.bot_token_env or WAYBILL_TELEGRAM_BOT_TOKEN_ENV)
            chat_id_env = str(args.chat_id_env or DEFAULT_TELEGRAM_PRINT_CHAT_ID_ENV)
            bot_token, effective_bot_token_env = _resolve_env_alias_from_env(
                bot_token_env,
                TELEGRAM_BOT_TOKEN_ENV_ALIASES,
                DEFAULT_TELEGRAM_BOT_TOKEN_ENV,
            )
            chat_id, effective_chat_id_env = _resolve_env_alias_from_env(
                chat_id_env,
                TELEGRAM_PRINT_CHAT_ID_ENV_ALIASES,
                DEFAULT_TELEGRAM_PRINT_CHAT_ID_ENV,
            )
            if not token:
                payload = {
                    "schema_version": "acmewear_express_prepare_label.v1",
                    "status": "blocked",
                    "gate": "YELLOW_KASPI_TOKEN_MISSING",
                    "error": f"Missing token env var; checked: {', '.join(token_env_candidates)}",
                    "requested_token_env": token_env,
                    "effective_token_env": effective_token_env,
                    "run_dir": str(run_dir),
                    "external_writes": {
                        "kaspi_order_mutation": False,
                        "telegram_send": False,
                        "print_job": False,
                        "autonomous_business_write": False,
                    },
                }
                run_dir.mkdir(parents=True, exist_ok=True)
                summary_path = run_dir / "prepare_label_summary.json"
                summary_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
                payload["summary_path"] = str(summary_path)
                if args.json:
                    print(json.dumps(payload, ensure_ascii=False, indent=2))
                else:
                    logging.error(payload["error"])
                return 2
            try:
                summary = run_prepare_express_label(
                    token=token,
                    run_dir=run_dir,
                    send_telegram=bool(args.send_telegram),
                    bot_token=bot_token,
                    chat_id=chat_id,
                    chat_config_ref=str(args.chat_config_ref or f"{effective_bot_token_env}+{effective_chat_id_env}"),
                    order_code=str(args.order_code or ""),
                    order_hash=str(args.order_hash or ""),
                    owner_size_override=str(args.owner_size or ""),
                    size_optional=not bool(args.require_size),
                    force_resend=bool(args.force_resend),
                    approval_policy_path=Path(args.standing_approval),
                    ledger_csv=Path(args.ledger_csv),
                    lookback_hours=int(args.lookback_hours),
                    states=args.states or None,
                    created_from=str(args.created_from or ""),
                    created_to=str(args.created_to or ""),
                    page_size=int(args.page_size),
                    max_pages=int(args.max_pages),
                    timeout_seconds=int(args.timeout_seconds),
                    repo_root=Path.cwd(),
                )
            except (OSError, ValueError, RuntimeError, KaspiOrderFetchError) as exc:
                payload = {
                    "schema_version": "acmewear_express_prepare_label.v1",
                    "status": "blocked",
                    "gate": "YELLOW_PREPARE_LABEL_EXCEPTION",
                    "error": str(exc),
                    "run_dir": str(run_dir),
                    "external_writes": {
                        "kaspi_order_mutation": False,
                        "telegram_send": False,
                        "print_job": False,
                        "autonomous_business_write": False,
                    },
                }
                if args.json:
                    print(json.dumps(payload, ensure_ascii=False, indent=2))
                else:
                    logging.error("ACMEWEAR Express prepare-label blocked: %s", exc)
                return 4
            if args.json:
                print(json.dumps(summary, ensure_ascii=False, indent=2))
            else:
                logging.info(
                    "ACMEWEAR Express prepare-label: gate=%s order=%s telegram=%s run_dir=%s",
                    summary.get("gate"),
                    summary.get("order_hash_prefix"),
                    summary.get("telegram_message_id") or summary.get("telegram_send_summary", {}).get("send_mode"),
                    run_dir,
                )
            return 0 if str(summary.get("gate", "")).startswith("GREEN") else 4

        if args.acmewear_express_command == "telegram-label":
            run_dir = (
                Path(args.run_dir)
                if args.run_dir
                else DEFAULT_ACMEWEAR_EXPRESS_SIDECAR_RUN_ROOT
                / f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_telegram_label"
            )
            bot_token_env = str(args.bot_token_env or WAYBILL_TELEGRAM_BOT_TOKEN_ENV)
            chat_id_env = str(args.chat_id_env or DEFAULT_TELEGRAM_PRINT_CHAT_ID_ENV)
            bot_token, effective_bot_token_env = _resolve_env_alias_from_env(
                bot_token_env,
                TELEGRAM_BOT_TOKEN_ENV_ALIASES,
                DEFAULT_TELEGRAM_BOT_TOKEN_ENV,
            )
            chat_id, effective_chat_id_env = _resolve_env_alias_from_env(
                chat_id_env,
                TELEGRAM_PRINT_CHAT_ID_ENV_ALIASES,
                DEFAULT_TELEGRAM_PRINT_CHAT_ID_ENV,
            )
            try:
                summary = run_telegram_label_from_pdf(
                    cropped_label_pdf=Path(args.cropped_label_pdf),
                    order_hash=str(args.order_hash or ""),
                    local_ref=str(args.local_ref or ""),
                    ledger_csv=Path(args.ledger_csv),
                    run_dir=run_dir,
                    send=bool(args.send),
                    bot_token=bot_token,
                    chat_id=chat_id,
                    chat_config_ref=str(args.chat_config_ref or f"{effective_bot_token_env}+{effective_chat_id_env}"),
                    sidecar_idempotency_key=str(args.sidecar_idempotency_key or ""),
                    caption=str(args.caption or "ACMEWEAR PP2 product label"),
                    timeout_seconds=int(args.timeout_seconds),
                )
            except (OSError, ValueError, RuntimeError) as exc:
                payload = {
                    "status": "blocked",
                    "error": str(exc),
                    "run_dir": str(run_dir),
                    "external_writes": {
                        "kaspi_order_mutation": False,
                        "telegram_send": False,
                        "print_job": False,
                        "autonomous_business_write": False,
                    },
                }
                if args.json:
                    print(json.dumps(payload, ensure_ascii=False, indent=2))
                else:
                    logging.error("ACMEWEAR Express Telegram label blocked: %s", exc)
                return 2 if "requires bot token and chat id" in str(exc) else 4
            if args.json:
                print(json.dumps(summary, ensure_ascii=False, indent=2))
            else:
                logging.info(
                    "ACMEWEAR Express Telegram label: mode=%s sent=%s skipped=%s run_dir=%s",
                    summary.get("send_mode"),
                    summary.get("sent_rows"),
                    summary.get("skipped_duplicate_rows"),
                    run_dir,
                )
            return 0

        if args.acmewear_express_command == "pickup-completion-review":
            run_dir = (
                Path(args.run_dir)
                if args.run_dir
                else DEFAULT_ACMEWEAR_EXPRESS_SIDECAR_RUN_ROOT
                / f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_pickup_completion_review"
            )
            security_code_env = str(args.security_code_env or "ACMEWEAR_PICKUP_SECURITY_CODE")
            security_code = os.environ.get(security_code_env, "")
            try:
                summary = run_pickup_completion_review(
                    sidecar_csv=Path(args.sidecar_csv),
                    run_dir=run_dir,
                    order_hash=str(args.order_hash or ""),
                    local_ref=str(args.local_ref or ""),
                    sidecar_idempotency_key=str(args.sidecar_idempotency_key or ""),
                    security_code=security_code,
                    security_code_sha256=str(args.security_code_sha256 or ""),
                    customer_arrived=bool(args.customer_arrived),
                    manual_handoff_confirmed=bool(args.manual_handoff_confirmed),
                    ledger_csv=Path(args.ledger_csv) if args.ledger_csv else None,
                    write_ledger=bool(args.write_ledger),
                    build_api_plan=bool(args.build_api_plan),
                    owner_approval_ref=str(args.owner_approval_ref or ""),
                    order_id_env=str(args.order_id_env or "ACMEWEAR_PICKUP_ORDER_ID"),
                    order_code_env=str(args.order_code_env or "ACMEWEAR_PICKUP_ORDER_CODE"),
                    security_code_env=security_code_env,
                    env=os.environ,
                )
            except (OSError, ValueError, RuntimeError) as exc:
                payload = {
                    "status": "blocked",
                    "error": str(exc),
                    "run_dir": str(run_dir),
                    "external_writes": {
                        "kaspi_order_mutation": False,
                        "telegram_send": False,
                        "print_job": False,
                        "autonomous_business_write": False,
                    },
                }
                if args.json:
                    print(json.dumps(payload, ensure_ascii=False, indent=2))
                else:
                    logging.error("ACMEWEAR Express pickup completion review blocked: %s", exc)
                return 2 if "requires --order-hash" in str(exc) or "--write-ledger requires" in str(exc) else 4
            if args.json:
                print(json.dumps(summary, ensure_ascii=False, indent=2))
            else:
                logging.info(
                    "ACMEWEAR pickup completion review: status=%s blockers=%s run_dir=%s",
                    summary.get("review_status"),
                    summary.get("blockers"),
                    run_dir,
                )
            return 0

        if args.acmewear_express_command == "pickup-completion-execute":
            run_dir = (
                Path(args.run_dir)
                if args.run_dir
                else DEFAULT_ACMEWEAR_EXPRESS_SIDECAR_RUN_ROOT
                / f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_pickup_completion_api_execute"
            )
            try:
                summary = run_pickup_completion_api_execute(
                    api_plan_path=Path(args.api_plan),
                    run_dir=run_dir,
                    step=str(args.step or ""),
                    owner_approval_ref=str(args.owner_approval_ref or ""),
                    token_env=str(args.token_env or DEFAULT_ACMEWEAR_KASPI_TOKEN_ENV),
                    order_id_env=str(args.order_id_env or "ACMEWEAR_PICKUP_ORDER_ID"),
                    order_code_env=str(args.order_code_env or "ACMEWEAR_PICKUP_ORDER_CODE"),
                    security_code_env=str(args.security_code_env or "ACMEWEAR_PICKUP_SECURITY_CODE"),
                    execute=bool(args.execute),
                    timeout_seconds=int(args.timeout_seconds),
                    env=os.environ,
                )
            except (OSError, ValueError, RuntimeError) as exc:
                payload = {
                    "status": "blocked",
                    "error": str(exc),
                    "run_dir": str(run_dir),
                    "external_writes": {
                        "kaspi_order_mutation": False,
                        "telegram_send": False,
                        "print_job": False,
                        "autonomous_business_write": False,
                    },
                }
                if args.json:
                    print(json.dumps(payload, ensure_ascii=False, indent=2))
                else:
                    logging.error("ACMEWEAR pickup completion API execution blocked: %s", exc)
                return 4
            if args.json:
                print(json.dumps(summary, ensure_ascii=False, indent=2))
            else:
                logging.info(
                    "ACMEWEAR pickup completion API execution: status=%s step=%s dry_run=%s run_dir=%s",
                    summary.get("status"),
                    summary.get("step"),
                    summary.get("dry_run"),
                    run_dir,
                )
            return 0 if summary.get("status") not in {"BLOCKED", "LIVE_EXECUTED_HTTP_ERROR", "LIVE_EXECUTION_URL_ERROR"} else 4

        if args.acmewear_express_command == "express-assembly-review":
            run_dir = (
                Path(args.run_dir)
                if args.run_dir
                else DEFAULT_ACMEWEAR_EXPRESS_SIDECAR_RUN_ROOT
                / f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_express_assembly_review"
            )
            try:
                summary = run_express_assembly_review(
                    sidecar_csv=Path(args.sidecar_csv),
                    run_dir=run_dir,
                    order_hash=str(args.order_hash or ""),
                    local_ref=str(args.local_ref or ""),
                    sidecar_idempotency_key=str(args.sidecar_idempotency_key or ""),
                    label_printed=bool(args.label_printed),
                    operator_physically_ready=bool(args.operator_physically_ready),
                    owner_assembly_approval_ref=str(args.owner_assembly_approval_ref or ""),
                    ledger_csv=Path(args.ledger_csv) if args.ledger_csv else None,
                    write_ledger=bool(args.write_ledger),
                )
            except (OSError, ValueError, RuntimeError) as exc:
                payload = {
                    "status": "blocked",
                    "error": str(exc),
                    "run_dir": str(run_dir),
                    "external_writes": {
                        "kaspi_order_mutation": False,
                        "telegram_send": False,
                        "print_job": False,
                        "autonomous_business_write": False,
                    },
                }
                if args.json:
                    print(json.dumps(payload, ensure_ascii=False, indent=2))
                else:
                    logging.error("ACMEWEAR Express assembly review blocked: %s", exc)
                return 2 if "requires --order-hash" in str(exc) or "--write-ledger requires" in str(exc) else 4
            if args.json:
                print(json.dumps(summary, ensure_ascii=False, indent=2))
            else:
                logging.info(
                    "ACMEWEAR Express assembly review: status=%s blockers=%s run_dir=%s",
                    summary.get("review_status"),
                    summary.get("blockers"),
                    run_dir,
                )
            return 0

        if args.acmewear_express_command == "operator-queue":
            run_dir = (
                Path(args.run_dir)
                if args.run_dir
                else DEFAULT_ACMEWEAR_EXPRESS_SIDECAR_RUN_ROOT
                / f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_operator_queue"
            )
            try:
                summary = run_operator_queue_review(
                    sidecar_csv=Path(args.sidecar_csv),
                    run_dir=run_dir,
                )
            except (OSError, ValueError, RuntimeError) as exc:
                payload = {
                    "status": "blocked",
                    "error": str(exc),
                    "run_dir": str(run_dir),
                    "external_writes": {
                        "kaspi_order_mutation": False,
                        "telegram_send": False,
                        "print_job": False,
                        "autonomous_business_write": False,
                    },
                }
                if args.json:
                    print(json.dumps(payload, ensure_ascii=False, indent=2))
                else:
                    logging.error("ACMEWEAR Express operator queue blocked: %s", exc)
                return 4
            if args.json:
                print(json.dumps(summary, ensure_ascii=False, indent=2))
            else:
                logging.info(
                    "ACMEWEAR Express operator queue: rows=%s report=%s",
                    summary.get("operator_queue_rows"),
                    summary.get("operator_queue_report_path"),
                )
            return 0

        if args.acmewear_express_command == "shift-packet":
            run_dir = (
                Path(args.run_dir)
                if args.run_dir
                else DEFAULT_ACMEWEAR_EXPRESS_SIDECAR_RUN_ROOT
                / f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_shift_packet"
            )
            try:
                summary = run_shift_packet_review(
                    sidecar_csv=Path(args.sidecar_csv),
                    run_dir=run_dir,
                    run_root=Path(args.run_root),
                    env=os.environ,
                    token_env=str(args.token_env or DEFAULT_ACMEWEAR_KASPI_TOKEN_ENV),
                    telegram_bot_token_env=str(args.telegram_bot_token_env or DEFAULT_TELEGRAM_BOT_TOKEN_ENV),
                    telegram_alert_chat_id_env=str(args.telegram_alert_chat_id_env or DEFAULT_TELEGRAM_ALERT_CHAT_ID_ENV),
                    telegram_print_chat_id_env=str(args.telegram_print_chat_id_env or DEFAULT_TELEGRAM_PRINT_CHAT_ID_ENV),
                )
            except (OSError, ValueError, RuntimeError) as exc:
                payload = {
                    "status": "blocked",
                    "error": str(exc),
                    "run_dir": str(run_dir),
                    "external_writes": {
                        "kaspi_order_mutation": False,
                        "telegram_send": False,
                        "print_job": False,
                        "autonomous_business_write": False,
                    },
                }
                if args.json:
                    print(json.dumps(payload, ensure_ascii=False, indent=2))
                else:
                    logging.error("ACMEWEAR Express shift packet blocked: %s", exc)
                return 4
            if args.json:
                print(json.dumps(summary, ensure_ascii=False, indent=2))
            else:
                logging.info(
                    "ACMEWEAR Express shift packet: gate=%s queue=%s report=%s",
                    summary.get("gate"),
                    summary.get("operator_queue_rows"),
                    summary.get("shift_packet_report_path"),
                )
            return 0

        if args.acmewear_express_command == "readiness":
            run_dir = (
                Path(args.run_dir)
                if args.run_dir
                else DEFAULT_ACMEWEAR_EXPRESS_SIDECAR_RUN_ROOT
                / f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_readiness"
            )
            summary = run_operational_readiness_review(
                run_dir=run_dir,
                run_root=Path(args.run_root),
                sidecar_csv=Path(args.sidecar_csv) if args.sidecar_csv else None,
                agent1_closeout=Path(args.agent1_closeout) if args.agent1_closeout else None,
                agent2_closeout=Path(args.agent2_closeout) if args.agent2_closeout else None,
                agent3_closeout=Path(args.agent3_closeout) if args.agent3_closeout else None,
                agent4_closeout=Path(args.agent4_closeout) if args.agent4_closeout else None,
                agent5_closeout=Path(args.agent5_closeout) if args.agent5_closeout else None,
                token_env=str(args.token_env or DEFAULT_ACMEWEAR_KASPI_TOKEN_ENV),
                telegram_bot_token_env=str(args.telegram_bot_token_env or DEFAULT_TELEGRAM_BOT_TOKEN_ENV),
                telegram_alert_chat_id_env=str(args.telegram_alert_chat_id_env or DEFAULT_TELEGRAM_ALERT_CHAT_ID_ENV),
                telegram_print_chat_id_env=str(args.telegram_print_chat_id_env or DEFAULT_TELEGRAM_PRINT_CHAT_ID_ENV),
                env=os.environ,
            )
            if args.json:
                print(json.dumps(summary, ensure_ascii=False, indent=2))
            else:
                logging.info(
                    "ACMEWEAR Express readiness: gate=%s matrix=%s report=%s",
                    summary.get("gate"),
                    summary.get("readiness_matrix_path"),
                    summary.get("readiness_report_path"),
                )
            return 0

        logging.error("Unknown acmewear-express-sidecar command")
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


if __name__ == "__main__":
    raise SystemExit(main())
