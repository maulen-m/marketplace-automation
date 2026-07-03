#!/usr/bin/env bash
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
PYTHON_BIN="${PYTHON_BIN:-$REPO_ROOT/.venv/bin/python}"
if [[ ! -x "$PYTHON_BIN" ]]; then
  PYTHON_BIN="python3"
fi

REGISTRY_PATH="${ACMEWEAR_UNIVERSAL_MARKETING_REGISTRY:-$REPO_ROOT/config/tasks/acmewear_universal_marketing_registry.yaml}"
MARKET_EVENTS_PATH="${ACMEWEAR_UNIVERSAL_MARKET_EVENTS:-$REPO_ROOT/config/tasks/kaspi_market_events.yaml}"
ENV_FILE="${WEB_AUTOMATION_ENV_FILE:-$HOME/Docs/Autonomous_business/.env}"
AB_DB="${AUTONOMOUS_BUSINESS_DB:-$HOME/Docs/Autonomous_business/db/app.db}"
MARKETING_DB="${ACMEWEAR_UNIVERSAL_MARKETING_DB:-$REPO_ROOT/data/kaspi_marketing.sqlite}"
RUN_ROOT_REL="runs/acmewear_universal_marketing_ops_fetch"
RUN_ROOT="$REPO_ROOT/$RUN_ROOT_REL"
LOOKBACK_DAYS="${ACMEWEAR_UNIVERSAL_MARKETING_LOOKBACK_DAYS:-45}"
FETCH_LAG_DAYS="${ACMEWEAR_UNIVERSAL_MARKETING_FETCH_LAG_DAYS:-2}"
ALL_STORE_CODES="${ACMEWEAR_UNIVERSAL_MARKETING_ORDER_STORE_CODES:-ACMEWEAR,UNIVERSAL,STOREB,11KZ,MELVIS}"
TIMESTAMP="${ACMEWEAR_UNIVERSAL_MARKETING_TIMESTAMP:-$(date +%Y%m%d_%H%M%S)}"
TODAY="${ACMEWEAR_UNIVERSAL_MARKETING_DATE:-$(date +%F)}"
RUN_DIR="$RUN_ROOT/$TIMESTAMP"
LOG_DIR="$RUN_DIR/logs"
EXPORT_DIR="$RUN_DIR/exports"
STEP_STATUS="$RUN_DIR/step_status.csv"
SCOPE_JSON="$RUN_DIR/resolved_scope.json"
LIVE_INVENTORY_SOURCE="${ACMEWEAR_UNIVERSAL_MARKETING_LIVE_INVENTORY_SOURCE:-}"
LIVE_INVENTORY_JSON="$EXPORT_DIR/live_campaign_inventory.json"
GATE="GREEN"

mkdir -p "$LOG_DIR" "$EXPORT_DIR"
cd "$REPO_ROOT" || exit 2

START_DATE="$("$PYTHON_BIN" - "$LOOKBACK_DAYS" "$TODAY" <<'PY'
from __future__ import annotations

import sys
from datetime import date, timedelta

days = int(sys.argv[1])
anchor = date.fromisoformat(sys.argv[2])
print((anchor - timedelta(days=days)).isoformat())
PY
)"

FETCH_DATES_JSON="$("$PYTHON_BIN" - "$FETCH_LAG_DAYS" "$TODAY" <<'PY'
from __future__ import annotations

import json
import sys
from datetime import date, timedelta

days = int(sys.argv[1])
anchor = date.fromisoformat(sys.argv[2])
dates = [(anchor - timedelta(days=offset)).isoformat() for offset in range(days, -1, -1)]
print(json.dumps(dates, ensure_ascii=False))
PY
)"

append_status() {
  local step="$1"
  local status="$2"
  local rc="$3"
  local stdout_path="$4"
  local stderr_path="$5"
  printf '%s,%s,%s,%s,%s,%s\n' "$(date -Iseconds)" "$step" "$status" "$rc" "$stdout_path" "$stderr_path" >> "$STEP_STATUS"
}

mark_yellow() {
  GATE="YELLOW"
}

run_step() {
  local step="$1"
  shift
  local step_dir="$LOG_DIR/$step"
  mkdir -p "$step_dir"
  local stdout_path="$step_dir/stdout.txt"
  local stderr_path="$step_dir/stderr.txt"
  printf '%q ' "$@" > "$step_dir/command.txt"
  printf '\n' >> "$step_dir/command.txt"
  "$@" > "$stdout_path" 2> "$stderr_path"
  local rc=$?
  if [[ "$rc" -eq 0 ]]; then
    append_status "$step" "success" "$rc" "$stdout_path" "$stderr_path"
  else
    append_status "$step" "failed" "$rc" "$stdout_path" "$stderr_path"
    mark_yellow
  fi
  return 0
}

printf 'generated_at,step,status,returncode,stdout_path,stderr_path\n' > "$STEP_STATUS"

live_inventory_step_dir="$LOG_DIR/live_campaign_inventory"
mkdir -p "$live_inventory_step_dir"
"$PYTHON_BIN" - "$MARKETING_DB" "$LIVE_INVENTORY_SOURCE" "$LIVE_INVENTORY_JSON" > "$live_inventory_step_dir/stdout.txt" 2> "$live_inventory_step_dir/stderr.txt" <<'PY'
from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

from web_auto.universal_marketing_registry import (
    discover_active_campaign_rows_from_live_inventory,
    discover_active_campaign_rows_from_marketing_db,
)

marketing_db = Path(sys.argv[1])
inventory_source = sys.argv[2].strip()
inventory_path = Path(sys.argv[3])

source_type = "local_marketing_db_active_snapshot"
rows = []
if inventory_source:
    rows = discover_active_campaign_rows_from_live_inventory(inventory_source)
    source_type = "sanitized_live_inventory_source"
if not rows:
    rows = discover_active_campaign_rows_from_marketing_db(marketing_db)

payload = {
    "schema_version": "web_auto.acmewear_live_campaign_inventory.v1",
    "generated_at": datetime.now().isoformat(timespec="seconds"),
    "source_type": source_type,
    "external_behavior": "read_only",
    "store": "ACMEWEAR",
    "merchant_id": "759051",
    "store_code": "30137883",
    "campaign_count": len(rows),
    "campaigns": rows,
    "no_secret_material": True,
}
inventory_path.parent.mkdir(parents=True, exist_ok=True)
inventory_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
print(json.dumps({"campaign_count": len(rows), "source_type": source_type}, ensure_ascii=False))
PY
live_inventory_rc=$?
printf '%q ' "$PYTHON_BIN" - "$MARKETING_DB" "$LIVE_INVENTORY_SOURCE" "$LIVE_INVENTORY_JSON" > "$live_inventory_step_dir/command.txt"
printf '\n' >> "$live_inventory_step_dir/command.txt"
if [[ "$live_inventory_rc" -eq 0 ]]; then
  append_status "live_campaign_inventory" "success" "$live_inventory_rc" "$live_inventory_step_dir/stdout.txt" "$live_inventory_step_dir/stderr.txt"
else
  append_status "live_campaign_inventory" "failed" "$live_inventory_rc" "$live_inventory_step_dir/stdout.txt" "$live_inventory_step_dir/stderr.txt"
  mark_yellow
fi

scope_step_dir="$LOG_DIR/resolve_scope"
mkdir -p "$scope_step_dir"
"$PYTHON_BIN" - "$REGISTRY_PATH" "$MARKETING_DB" "$SCOPE_JSON" "$LIVE_INVENTORY_JSON" > "$scope_step_dir/stdout.txt" 2> "$scope_step_dir/stderr.txt" <<'PY'
from __future__ import annotations

import json
import sys
from pathlib import Path

from web_auto.universal_marketing_registry import resolve_universal_marketing_scope

registry_path = Path(sys.argv[1])
marketing_db = Path(sys.argv[2])
scope_path = Path(sys.argv[3])
live_inventory_path = Path(sys.argv[4])

scope = resolve_universal_marketing_scope(
    registry_path=registry_path,
    repo_root=Path.cwd(),
    marketing_db_path=marketing_db,
    include_active_discovery=True,
    live_inventory_path=live_inventory_path,
)
scope_path.parent.mkdir(parents=True, exist_ok=True)
scope_path.write_text(json.dumps(scope, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
print(json.dumps({"campaign_count": len(scope["campaign_ids"]), "sku_key_count": len(scope["sku_keys"])}, ensure_ascii=False))
PY
scope_rc=$?
printf '%q ' "$PYTHON_BIN" - "$REGISTRY_PATH" "$MARKETING_DB" "$SCOPE_JSON" "$LIVE_INVENTORY_JSON" > "$scope_step_dir/command.txt"
printf '\n' >> "$scope_step_dir/command.txt"
if [[ "$scope_rc" -eq 0 ]]; then
  append_status "resolve_scope" "success" "$scope_rc" "$scope_step_dir/stdout.txt" "$scope_step_dir/stderr.txt"
else
  append_status "resolve_scope" "failed" "$scope_rc" "$scope_step_dir/stdout.txt" "$scope_step_dir/stderr.txt"
  mark_yellow
fi

CAMPAIGN_IDS=""
if [[ -f "$SCOPE_JSON" ]]; then
  CAMPAIGN_IDS="$("$PYTHON_BIN" - "$SCOPE_JSON" <<'PY'
from __future__ import annotations

import json
import sys
from pathlib import Path

payload = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
print(",".join(str(item) for item in payload.get("campaign_ids", []) if str(item).strip()))
PY
)"
fi

if [[ -n "$CAMPAIGN_IDS" ]]; then
  FETCH_DATES="$("$PYTHON_BIN" - "$FETCH_DATES_JSON" <<'PY'
from __future__ import annotations

import json
import sys

print("\n".join(str(item) for item in json.loads(sys.argv[1])))
PY
)"
  for FETCH_DATE in $FETCH_DATES; do
    run_step "marketing_fetch_${FETCH_DATE}" \
      "$PYTHON_BIN" -m web_auto --env-file "$ENV_FILE" --json kaspi-marketing fetch-campaigns \
      --store ACMEWEAR \
      --merchant-id 759051 \
      --store-code 30137883 \
      --campaign-ids "$CAMPAIGN_IDS" \
      --date "$FETCH_DATE" \
      --headless \
      --run-dir "$RUN_ROOT_REL/$TIMESTAMP/marketing_fetch_$FETCH_DATE" \
      --db-path "$MARKETING_DB"
  done
else
  empty_step_dir="$LOG_DIR/marketing_fetch"
  mkdir -p "$empty_step_dir"
  printf 'missing resolved campaign ids\n' > "$empty_step_dir/stderr.txt"
  append_status "marketing_fetch" "failed" "2" "" "$empty_step_dir/stderr.txt"
  mark_yellow
fi

run_step pricelist_download \
  "$PYTHON_BIN" -m web_auto --env-file "$ENV_FILE" --json kaspi-pricelist download \
  --store ACMEWEAR \
  --run-dir "$RUN_ROOT_REL/$TIMESTAMP/pricelist_download" \
  --headless

"$PYTHON_BIN" - "$RUN_DIR" "$STEP_STATUS" "$SCOPE_JSON" "$MARKETING_DB" "$AB_DB" "$START_DATE" "$TODAY" "$GATE" "$MARKET_EVENTS_PATH" "$FETCH_DATES_JSON" "$ALL_STORE_CODES" <<'PY'
from __future__ import annotations

import csv
import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

run_dir = Path(sys.argv[1])
step_status = Path(sys.argv[2])
scope_path = Path(sys.argv[3])
marketing_db = Path(sys.argv[4])
ab_db = Path(sys.argv[5])
start_date = sys.argv[6]
today = sys.argv[7]
shell_gate = sys.argv[8]
market_events_path = Path(sys.argv[9])
fetch_dates = json.loads(sys.argv[10])
all_store_codes = [item.strip() for item in sys.argv[11].split(",") if item.strip()]
export_dir = run_dir / "exports"


def read_scope() -> dict[str, Any]:
    if not scope_path.exists():
        return {
            "store": {"name": "ACMEWEAR", "merchant_id": "759051", "store_code": "30137883"},
            "registry_rows": [],
            "registry_summary": {},
            "registry_campaign_ids": [],
            "discovered_active_campaign_rows": [],
            "discovered_active_campaign_ids": [],
            "discovered_active_new_campaign_ids": [],
            "live_inventory_campaign_rows": [],
            "live_inventory_campaign_ids": [],
            "live_inventory_new_campaign_ids": [],
            "campaign_ids": [],
            "sku_keys": [],
            "configured_product_skus": [],
        }
    return json.loads(scope_path.read_text(encoding="utf-8"))


scope = read_scope()
campaign_ids = [str(item).strip() for item in scope.get("campaign_ids", []) if str(item).strip()]
sku_keys = [str(item).strip() for item in scope.get("sku_keys", []) if str(item).strip()]
configured_product_skus = [
    str(item).strip() for item in scope.get("configured_product_skus", []) if str(item).strip()
]


def write_csv(path: Path, rows: list[dict[str, Any]], fieldnames: list[str] | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = list(fieldnames or [])
    seen = set(fields)
    for row in rows:
        for key in row:
            if key not in seen:
                fields.append(key)
                seen.add(key)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type IN ('table','view') AND name = ?",
        (name,),
    ).fetchone()
    return row is not None


def sqlite_rows(path: Path, sql: str, params: Iterable[Any] = ()) -> tuple[list[dict[str, Any]], str]:
    if not path.exists():
        return [], f"missing_db:{path}"
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(row) for row in conn.execute(sql, tuple(params))], "success"
    except Exception as exc:
        return [], f"sqlite_error:{exc}"
    finally:
        conn.close()


def relation_available(path: Path, table: str) -> bool:
    if not path.exists():
        return False
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return table_exists(conn, table)
    finally:
        conn.close()


def placeholders(values: list[str]) -> str:
    return ",".join("?" for _ in values) or "NULL"


def write_registry_exports() -> None:
    write_csv(
        export_dir / "registry_campaign_rows.csv",
        list(scope.get("registry_rows", [])),
        [
            "store",
            "merchant_id",
            "store_code",
            "campaign_id",
            "family",
            "sku_key",
            "tier",
            "inclusion_reason",
            "source_path",
            "source_section",
            "configured_product_sku",
        ],
    )
    write_csv(
        export_dir / "discovered_active_campaign_rows.csv",
        list(scope.get("discovered_active_campaign_rows", [])),
        [
            "campaign_id",
            "campaign_name",
            "state",
            "date",
            "merchant_id",
            "store_code",
            "daily_budget",
            "default_bid",
            "views",
            "clicks",
            "carts",
            "transactions",
            "cost",
            "gmv",
            "record_timestamp",
            "ingested_at",
            "discovery_reason",
        ],
    )
    write_csv(
        export_dir / "live_inventory_campaign_rows.csv",
        list(scope.get("live_inventory_campaign_rows", [])),
        [
            "campaign_id",
            "campaign_name",
            "state",
            "merchant_id",
            "store_code",
            "daily_budget",
            "default_bid",
            "source_path",
            "discovery_reason",
        ],
    )


write_registry_exports()


def export_market_events() -> tuple[list[dict[str, Any]], str]:
    if not market_events_path.exists():
        write_csv(export_dir / "market_event_overlay.csv", [])
        return [], f"missing_market_events:{market_events_path}"
    try:
        import yaml

        raw = yaml.safe_load(market_events_path.read_text(encoding="utf-8")) or {}
    except Exception as exc:
        write_csv(export_dir / "market_event_overlay.csv", [])
        return [], f"market_events_error:{exc}"
    rows: list[dict[str, Any]] = []
    for event in raw.get("events") or []:
        if not isinstance(event, dict):
            continue
        affected = [str(item) for item in event.get("affected_dates") or []]
        lag = [str(item) for item in event.get("attribution_lag_dates") or []]
        window_dates = set(affected + lag)
        if today not in window_dates and not any(start_date <= item <= today for item in window_dates):
            continue
        rows.append(
            {
                "event_id": event.get("event_id", ""),
                "name": event.get("name", ""),
                "timezone": event.get("timezone", ""),
                "start_date": event.get("start_date", ""),
                "end_date": event.get("end_date", ""),
                "affected_dates": ";".join(affected),
                "attribution_lag_dates": ";".join(lag),
                "distortion_risk": event.get("distortion_risk", ""),
                "reporting_labels": ";".join(str(item) for item in event.get("reporting_labels") or []),
                "analysis_policy": event.get("analysis_policy", ""),
            }
        )
    write_csv(export_dir / "market_event_overlay.csv", rows)
    return rows, "success"


def export_configured_promos() -> tuple[list[dict[str, Any]], str]:
    rows: list[dict[str, Any]] = []
    try:
        import yaml
    except Exception as exc:
        write_csv(export_dir / "configured_seller_bonus_promos.csv", [])
        return [], f"yaml_unavailable:{exc}"
    for registry_row in scope.get("registry_rows", []):
        source_path = str(registry_row.get("source_path") or "").split(";", 1)[0]
        if not source_path:
            continue
        path = Path.cwd() / source_path
        if not path.exists():
            continue
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except Exception:
            continue
        for promo in raw.get("promos") or []:
            if not isinstance(promo, dict):
                continue
            row = dict(promo)
            row["source_path"] = source_path
            row["cash_flow_overlay_note"] = "Kaspi internal marketing is a cash-flow overlay, not deterministic Meta attribution."
            rows.append(row)
    deduped: dict[str, dict[str, Any]] = {}
    for row in rows:
        promo_id = str(row.get("promo_id") or row.get("name") or "")
        if promo_id:
            deduped[promo_id] = row
    final_rows = list(deduped.values())
    write_csv(export_dir / "configured_seller_bonus_promos.csv", final_rows)
    return final_rows, "success"


market_event_rows, market_event_status = export_market_events()
configured_promo_rows, configured_promo_status = export_configured_promos()

marketing_statuses: dict[str, str] = {}
campaign_current: list[dict[str, Any]] = []
product_current: list[dict[str, Any]] = []
campaign_recent_summary: list[dict[str, Any]] = []
product_recent_summary: list[dict[str, Any]] = []

if campaign_ids and relation_available(marketing_db, "campaign_daily_current"):
    in_clause = placeholders(campaign_ids)
    campaign_current, marketing_statuses["campaign_current"] = sqlite_rows(
        marketing_db,
        f"""
        SELECT date, merchant_id, store_code, campaign_id, campaign_name, state, daily_budget, default_bid,
               views, clicks, favorites, carts, ctr, gmv, transactions, cost, crr, report_state,
               record_timestamp, ingested_at
        FROM campaign_daily_current
        WHERE merchant_id = '759051'
          AND store_code = '30137883'
          AND campaign_id IN ({in_clause})
        ORDER BY date, campaign_id
        """,
        campaign_ids,
    )
    campaign_recent_summary, marketing_statuses["campaign_recent_summary"] = sqlite_rows(
        marketing_db,
        f"""
        SELECT campaign_id, campaign_name, state, MIN(date) AS min_date, MAX(date) AS max_date,
               COUNT(*) AS rows, SUM(views) AS views, SUM(clicks) AS clicks, SUM(carts) AS carts,
               SUM(transactions) AS transactions, ROUND(SUM(gmv), 0) AS gmv,
               ROUND(SUM(cost), 0) AS cost, MAX(ingested_at) AS max_ingested_at
        FROM campaign_daily_current
        WHERE merchant_id = '759051'
          AND store_code = '30137883'
          AND campaign_id IN ({in_clause})
          AND date BETWEEN ? AND ?
        GROUP BY campaign_id, campaign_name, state
        ORDER BY campaign_id, state
        """,
        [*campaign_ids, start_date, today],
    )
else:
    marketing_statuses["campaign_current"] = "missing_campaign_daily_current_or_ids"
    marketing_statuses["campaign_recent_summary"] = "missing_campaign_daily_current_or_ids"

if campaign_ids and relation_available(marketing_db, "campaign_product_daily_current"):
    in_clause = placeholders(campaign_ids)
    product_current, marketing_statuses["product_current"] = sqlite_rows(
        marketing_db,
        f"""
        SELECT date, merchant_id, store_code, campaign_id, campaign_name, sku_key, product_name,
               product_status, bid_cpc, avg_cpc, views, clicks, favorites, carts, ctr, gmv,
               orders_total, orders_direct, orders_assisted, conversion_order, cost, acos_share,
               json_sku, json_merchant_sku, ingested_at, bid_cpc_source
        FROM campaign_product_daily_current
        WHERE merchant_id = '759051'
          AND store_code = '30137883'
          AND campaign_id IN ({in_clause})
        ORDER BY date, campaign_id, json_merchant_sku
        """,
        campaign_ids,
    )
    product_recent_summary, marketing_statuses["product_recent_summary"] = sqlite_rows(
        marketing_db,
        f"""
        SELECT campaign_id, campaign_name,
               COALESCE(NULLIF(json_merchant_sku, ''), NULLIF(sku_key, ''), NULLIF(json_sku, '')) AS product_key,
               MAX(product_name) AS product_name,
               MAX(product_status) AS latest_product_status,
               MIN(date) AS min_date,
               MAX(date) AS max_date,
               COUNT(*) AS rows,
               ROUND(AVG(bid_cpc), 2) AS avg_bid_cpc,
               ROUND(AVG(avg_cpc), 2) AS avg_actual_cpc,
               SUM(views) AS views,
               SUM(clicks) AS clicks,
               SUM(carts) AS carts,
               SUM(orders_total) AS marketing_orders,
               ROUND(SUM(gmv), 0) AS marketing_gmv,
               ROUND(SUM(cost), 0) AS ad_cost,
               MAX(ingested_at) AS max_ingested_at
        FROM campaign_product_daily_current
        WHERE merchant_id = '759051'
          AND store_code = '30137883'
          AND campaign_id IN ({in_clause})
          AND date BETWEEN ? AND ?
        GROUP BY campaign_id, campaign_name, product_key
        ORDER BY campaign_id, product_key
        """,
        [*campaign_ids, start_date, today],
    )
else:
    marketing_statuses["product_current"] = "missing_campaign_product_daily_current_or_ids"
    marketing_statuses["product_recent_summary"] = "missing_campaign_product_daily_current_or_ids"

write_csv(export_dir / "marketing_campaign_daily_current.csv", campaign_current)
write_csv(export_dir / "marketing_product_daily_current.csv", product_current)
write_csv(export_dir / f"marketing_campaign_recent_summary_{start_date}_{today}.csv", campaign_recent_summary)
write_csv(export_dir / f"marketing_product_recent_summary_{start_date}_{today}.csv", product_recent_summary)


def latest_pricelist_paths() -> tuple[Path | None, Path | None, str]:
    summary_path = run_dir / "pricelist_download" / "download_summary.json"
    active: Path | None = None
    archive: Path | None = None
    if summary_path.exists():
        try:
            payload = json.loads(summary_path.read_text(encoding="utf-8"))
            for item in payload.get("downloads", []):
                state = str(item.get("sale_state") or "").upper()
                saved = Path(str(item.get("saved_path") or ""))
                if state == "ACTIVE" and saved.exists():
                    active = saved
                if state == "ARCHIVE" and saved.exists():
                    archive = saved
        except Exception:
            pass
    if active is None or archive is None:
        candidates = list((run_dir / "pricelist_download").rglob("*.xlsx"))
        active_candidates = sorted(path for path in candidates if "ACTIVE" in path.name.upper())
        archive_candidates = sorted(path for path in candidates if "ARCHIVE" in path.name.upper())
        active = active or (active_candidates[-1] if active_candidates else None)
        archive = archive or (archive_candidates[-1] if archive_candidates else None)
    if active is None and archive is None:
        return None, None, "no_workbooks_found"
    if active is None:
        return None, archive, "missing_active_workbook"
    if archive is None:
        return active, None, "missing_archive_workbook"
    return active, archive, "success"


def normalize_headers(values: list[Any]) -> list[str]:
    out: list[str] = []
    counts: dict[str, int] = {}
    for idx, value in enumerate(values, start=1):
        base = str(value or "").replace("\n", " ").strip() or f"col_{idx}"
        count = counts.get(base, 0) + 1
        counts[base] = count
        out.append(base if count == 1 else f"{base}_{count}")
    return out


def clean_offer_token(value: str) -> str:
    token = value.strip()
    if not token:
        return ""
    upper = token.upper()
    if upper in {"ACMEWEAR", "LINE31", "LINE61", "LINE51"}:
        return ""
    if token in campaign_ids:
        return ""
    if len(token) < 5:
        return ""
    return token


def build_offer_tokens() -> list[str]:
    tokens: list[str] = []
    for value in [*sku_keys, *configured_product_skus]:
        token = clean_offer_token(value)
        if token and token not in tokens:
            tokens.append(token)
    for row in product_current:
        for field in ("sku_key", "json_sku", "json_merchant_sku"):
            token = clean_offer_token(str(row.get(field) or ""))
            if token and token not in tokens:
                tokens.append(token)
    return tokens


def export_offer_rows() -> tuple[str, int, list[str]]:
    offer_tokens = build_offer_tokens()
    active, archive, path_status = latest_pricelist_paths()
    if path_status != "success":
        write_csv(export_dir / "merchant_pricelist_offer_rows.csv", [])
        write_csv(export_dir / "merchant_pricelist_offer_summary.csv", [])
        return path_status, 0, offer_tokens
    try:
        from openpyxl import load_workbook
    except Exception as exc:
        write_csv(export_dir / "merchant_pricelist_offer_rows.csv", [])
        write_csv(export_dir / "merchant_pricelist_offer_summary.csv", [])
        return f"openpyxl_unavailable:{exc}", 0, offer_tokens

    rows: list[dict[str, Any]] = []
    lowered_tokens = [(token, token.lower()) for token in offer_tokens]
    for state, path in (("ACTIVE", active), ("ARCHIVE", archive)):
        if path is None:
            continue
        workbook = load_workbook(path, read_only=True, data_only=True)
        worksheet = workbook.active
        iterator = worksheet.iter_rows(values_only=True)
        try:
            headers = normalize_headers(list(next(iterator)))
        except StopIteration:
            continue
        for values in iterator:
            text = " ".join("" if value is None else str(value) for value in values)
            lowered = text.lower()
            matches = [token for token, lowered_token in lowered_tokens if lowered_token in lowered]
            if not matches:
                continue
            row = {
                "source_state": state,
                "source_workbook": str(path),
                "matched_tokens": ";".join(matches),
            }
            for key, value in zip(headers, values):
                row[key] = value
            rows.append(row)
    write_csv(export_dir / "merchant_pricelist_offer_rows.csv", rows)

    summary_counts: dict[tuple[str, str], int] = {}
    for row in rows:
        token = str(row.get("matched_tokens") or "").split(";", 1)[0]
        key = (str(row.get("source_state") or ""), token)
        summary_counts[key] = summary_counts.get(key, 0) + 1
    write_csv(
        export_dir / "merchant_pricelist_offer_summary.csv",
        [
            {"source_state": state, "matched_token": token, "rows": count}
            for (state, token), count in sorted(summary_counts.items())
        ],
        ["source_state", "matched_token", "rows"],
    )
    return "success", len(rows), offer_tokens


offer_status, offer_rows, offer_tokens = export_offer_rows()

ab_statuses: dict[str, str] = {}
ab_api_orders: list[dict[str, Any]] = []
ab_api_summary: list[dict[str, Any]] = []
ab_buyout_summary: list[dict[str, Any]] = []
ab_source_freshness: list[dict[str, Any]] = []
store_clause = placeholders(all_store_codes)

if sku_keys and relation_available(ab_db, "fact_orders_kaspi"):
    in_clause = placeholders(sku_keys)
    ab_api_orders, ab_statuses["api_orders"] = sqlite_rows(
        ab_db,
        f"""
        SELECT date(created_at) AS order_date, order_id, store_code, sku_key, sku_id, my_size,
               quantity, unit_price_kzt, kaspi_status, internal_status, delivery_mode,
               payment_mode, source, created_at, updated_at, imported_at
        FROM fact_orders_kaspi
        WHERE store_code IN ({store_clause})
          AND sku_key IN ({in_clause})
          AND date(created_at) BETWEEN ? AND ?
        ORDER BY created_at, sku_key, order_id
        """,
        [*all_store_codes, *sku_keys, start_date, today],
    )
    ab_api_summary, ab_statuses["api_order_summary"] = sqlite_rows(
        ab_db,
        f"""
        SELECT date(created_at) AS order_date, store_code, sku_key, kaspi_status, internal_status,
               COUNT(DISTINCT order_id) AS orders,
               SUM(COALESCE(quantity, 1)) AS units,
               ROUND(SUM(COALESCE(unit_price_kzt, 0) * COALESCE(quantity, 1)), 0) AS gross
        FROM fact_orders_kaspi
        WHERE store_code IN ({store_clause})
          AND sku_key IN ({in_clause})
          AND date(created_at) BETWEEN ? AND ?
        GROUP BY order_date, store_code, sku_key, kaspi_status, internal_status
        ORDER BY order_date, store_code, sku_key, kaspi_status, internal_status
        """,
        [*all_store_codes, *sku_keys, start_date, today],
    )
else:
    ab_statuses["api_orders"] = "missing_fact_orders_kaspi_or_sku_keys"
    ab_statuses["api_order_summary"] = "missing_fact_orders_kaspi_or_sku_keys"

if sku_keys and relation_available(ab_db, "sales_fact_v2"):
    in_clause = placeholders(sku_keys)
    ab_buyout_summary, ab_statuses["buyout_sales_summary"] = sqlite_rows(
        ab_db,
        f"""
        SELECT order_date, store_code, sku_key, status, return_flag,
               COUNT(DISTINCT order_id) AS orders,
               SUM(COALESCE(quantity, 1)) AS units,
               ROUND(SUM(COALESCE(sell_price_kzt, 0) * COALESCE(quantity, 1)), 0) AS gross,
               ROUND(SUM(COALESCE(net_rev, 0)), 0) AS net_rev,
               ROUND(SUM(COALESCE(cogs, 0)), 0) AS cogs,
               ROUND(SUM(COALESCE(profit, 0)), 0) AS profit
        FROM sales_fact_v2
        WHERE store_code IN ({store_clause})
          AND sku_key IN ({in_clause})
          AND date(order_date) BETWEEN ? AND ?
        GROUP BY order_date, store_code, sku_key, status, return_flag
        ORDER BY order_date, store_code, sku_key, status, return_flag
        """,
        [*all_store_codes, *sku_keys, start_date, today],
    )
else:
    ab_statuses["buyout_sales_summary"] = "missing_sales_fact_v2_or_sku_keys"

if relation_available(ab_db, "fact_orders_kaspi") and relation_available(ab_db, "sales_fact_v2"):
    ab_source_freshness, ab_statuses["source_freshness"] = sqlite_rows(
        ab_db,
        f"""
        SELECT 'fact_orders_kaspi' AS source_table, store_code, COUNT(*) AS rows, MAX(created_at) AS max_event_at
        FROM fact_orders_kaspi
        WHERE store_code IN ({store_clause})
        GROUP BY store_code
        UNION ALL
        SELECT 'sales_fact_v2' AS source_table, store_code, COUNT(*) AS rows, MAX(order_date) AS max_event_at
        FROM sales_fact_v2
        WHERE store_code IN ({store_clause})
        GROUP BY store_code
        """,
        all_store_codes + all_store_codes,
    )
else:
    ab_statuses["source_freshness"] = "missing_ab_tables"

write_csv(export_dir / f"ab_api_orders_recent_{start_date}_{today}.csv", ab_api_orders)
write_csv(export_dir / f"ab_api_orders_daily_summary_{start_date}_{today}.csv", ab_api_summary)
write_csv(export_dir / f"ab_buyout_sales_daily_summary_{start_date}_{today}.csv", ab_buyout_summary)
write_csv(export_dir / f"all_store_api_orders_recent_{start_date}_{today}.csv", ab_api_orders)
write_csv(export_dir / f"all_store_api_orders_daily_summary_{start_date}_{today}.csv", ab_api_summary)
write_csv(export_dir / f"all_store_buyout_sales_daily_summary_{start_date}_{today}.csv", ab_buyout_summary)
write_csv(export_dir / "ab_source_freshness.csv", ab_source_freshness)

steps: list[dict[str, str]] = []
if step_status.exists():
    with step_status.open(newline="", encoding="utf-8") as handle:
        steps = list(csv.DictReader(handle))

gate = shell_gate
if any(row.get("status") != "success" for row in steps):
    gate = "YELLOW"
if offer_status != "success":
    gate = "YELLOW"

latest_campaign_rows: list[dict[str, Any]] = []
seen_campaigns: set[str] = set()
for row in sorted(campaign_current, key=lambda item: (str(item.get("date") or ""), str(item.get("ingested_at") or "")), reverse=True):
    campaign_id = str(row.get("campaign_id") or "")
    if campaign_id in seen_campaigns:
        continue
    seen_campaigns.add(campaign_id)
    latest_campaign_rows.append(row)


def as_float(value: Any) -> float:
    try:
        return float(value or 0)
    except Exception:
        return 0.0


event_ids = [str(row.get("event_id") or "") for row in market_event_rows if str(row.get("event_id") or "")]
anomaly_signal_rows: list[dict[str, Any]] = []
for row in latest_campaign_rows:
    campaign_id = str(row.get("campaign_id") or "")
    daily_budget = as_float(row.get("daily_budget"))
    cost = as_float(row.get("cost"))
    cap_ratio = round(cost / daily_budget, 4) if daily_budget > 0 else 0
    signals: list[str] = []
    if cap_ratio >= 0.9:
        signals.append("budget_cap_pressure_90pct")
    elif cap_ratio >= 0.8:
        signals.append("budget_cap_pressure_80pct")
    if event_ids:
        signals.append("market_event_overlay")
    if signals:
        anomaly_signal_rows.append(
            {
                "campaign_id": campaign_id,
                "campaign_name": row.get("campaign_name", ""),
                "date": row.get("date", ""),
                "state": row.get("state", ""),
                "daily_budget": row.get("daily_budget", ""),
                "cost": row.get("cost", ""),
                "budget_cap_ratio": cap_ratio,
                "signals": ";".join(signals),
                "market_event_ids": ";".join(event_ids),
                "cash_flow_overlay_note": "Kaspi internal marketing is a cash-flow overlay, not deterministic Meta attribution.",
            }
        )
write_csv(export_dir / "anomaly_signal_summary.csv", anomaly_signal_rows)

summary = {
    "status": "success" if gate == "GREEN" else "partial",
    "gate": gate,
    "generated_at": datetime.now().isoformat(timespec="seconds"),
    "run_dir": str(run_dir),
    "date": today,
    "lookback_start": start_date,
    "mode": "read_only",
    "store": scope.get("store", {}),
    "registry_summary": scope.get("registry_summary", {}),
    "registry_campaign_count": len(scope.get("registry_campaign_ids", [])),
    "discovered_active_campaign_count": len(scope.get("discovered_active_campaign_ids", [])),
    "discovered_active_new_campaign_count": len(scope.get("discovered_active_new_campaign_ids", [])),
    "campaign_count": len(campaign_ids),
    "sku_key_count": len(sku_keys),
    "campaign_current_rows": len(campaign_current),
    "product_current_rows": len(product_current),
    "campaign_recent_summary_rows": len(campaign_recent_summary),
    "product_recent_summary_rows": len(product_recent_summary),
    "offer_export_status": offer_status,
    "offer_rows": offer_rows,
    "offer_token_count": len(offer_tokens),
    "api_order_rows": len(ab_api_orders),
    "api_order_summary_rows": len(ab_api_summary),
    "buyout_sales_summary_rows": len(ab_buyout_summary),
    "ab_source_freshness_rows": len(ab_source_freshness),
    "fetch_dates": fetch_dates,
    "all_store_codes": all_store_codes,
    "live_inventory_campaign_count": len(scope.get("live_inventory_campaign_ids", [])),
    "live_inventory_new_campaign_count": len(scope.get("live_inventory_new_campaign_ids", [])),
    "market_event_status": market_event_status,
    "market_event_ids": event_ids,
    "configured_seller_bonus_promo_status": configured_promo_status,
    "configured_seller_bonus_promo_rows": len(configured_promo_rows),
    "anomaly_signal_rows": len(anomaly_signal_rows),
    "marketing_export_statuses": marketing_statuses,
    "ab_export_statuses": ab_statuses,
    "steps": steps,
    "artifacts": {
        "scope": str(scope_path),
        "exports_dir": str(export_dir),
        "marketing_fetch_dir": str(run_dir / f"marketing_fetch_{today}"),
        "pricelist_download_dir": str(run_dir / "pricelist_download"),
    },
}
(run_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def md_table(rows: list[dict[str, Any]], columns: list[str]) -> str:
    if not rows:
        return "_No rows._"
    widths = {column: len(column) for column in columns}
    for row in rows:
        for column in columns:
            widths[column] = max(widths[column], len(str(row.get(column, ""))))
    lines = [
        "| " + " | ".join(column.ljust(widths[column]) for column in columns) + " |",
        "|-" + "-|-".join("-" * widths[column] for column in columns) + "-|",
    ]
    for row in rows[:30]:
        lines.append("| " + " | ".join(str(row.get(column, "")).ljust(widths[column]) for column in columns) + " |")
    if len(rows) > 30:
        lines.append(f"| ... {len(rows) - 30} more rows |" + " |" * (len(columns) - 1))
    return "\n".join(lines)


closeout = [
    "# ACMEWEAR Universal Marketing Ops Fetch Closeout",
    "",
    f"Gate: {gate}",
    "",
    f"- generated_at: `{summary['generated_at']}`",
    f"- run_dir: `{run_dir}`",
    f"- date: `{today}`",
    f"- lookback_start: `{start_date}`",
    f"- mode: read-only",
    f"- registry_campaign_count: `{summary['registry_campaign_count']}`",
    f"- discovered_active_campaign_count: `{summary['discovered_active_campaign_count']}`",
    f"- live_inventory_campaign_count: `{summary['live_inventory_campaign_count']}`",
    f"- campaign_count: `{summary['campaign_count']}`",
    f"- product_current_rows: `{summary['product_current_rows']}`",
    f"- offer_rows: `{summary['offer_rows']}`",
    f"- api_order_summary_rows: `{summary['api_order_summary_rows']}`",
    f"- market_event_ids: `{';'.join(summary['market_event_ids'])}`",
    f"- anomaly_signal_rows: `{summary['anomaly_signal_rows']}`",
    f"- cash_flow_overlay_note: `Kaspi internal marketing is a cash-flow overlay, not deterministic Meta attribution.`",
    "",
    "## Latest Campaign Rows",
    "",
    md_table(latest_campaign_rows, ["campaign_id", "campaign_name", "state", "date", "views", "clicks", "transactions", "cost", "ingested_at"]),
    "",
    "## Source Freshness",
    "",
    md_table(ab_source_freshness, ["source_table", "store_code", "rows", "max_event_at"]),
    "",
    "## Artifacts",
    "",
    "- `resolved_scope.json`",
    f"- `marketing_fetch_{today}/campaign_daily_current.csv`",
    f"- `marketing_fetch_{today}/campaign_product_daily_current.csv`",
    f"- `marketing_fetch_{today}/raw/`",
    "- `pricelist_download/`",
    "- `exports/registry_campaign_rows.csv`",
    "- `exports/discovered_active_campaign_rows.csv`",
    "- `exports/live_campaign_inventory.json`",
    "- `exports/live_inventory_campaign_rows.csv`",
    "- `exports/market_event_overlay.csv`",
    "- `exports/configured_seller_bonus_promos.csv`",
    "- `exports/anomaly_signal_summary.csv`",
    "- `exports/marketing_campaign_daily_current.csv`",
    "- `exports/marketing_product_daily_current.csv`",
    f"- `exports/marketing_campaign_recent_summary_{start_date}_{today}.csv`",
    f"- `exports/marketing_product_recent_summary_{start_date}_{today}.csv`",
    "- `exports/merchant_pricelist_offer_rows.csv`",
    "- `exports/merchant_pricelist_offer_summary.csv`",
    f"- `exports/ab_api_orders_recent_{start_date}_{today}.csv`",
    f"- `exports/ab_api_orders_daily_summary_{start_date}_{today}.csv`",
    f"- `exports/ab_buyout_sales_daily_summary_{start_date}_{today}.csv`",
    f"- `exports/all_store_api_orders_recent_{start_date}_{today}.csv`",
    f"- `exports/all_store_api_orders_daily_summary_{start_date}_{today}.csv`",
    f"- `exports/all_store_buyout_sales_daily_summary_{start_date}_{today}.csv`",
    "- `exports/ab_source_freshness.csv`",
    "",
    "## No-Write Confirmation",
    "",
    "This collector fetches and reports only. It does not change price, stock, listing state, BID, budget, campaign state, product state, Meta, Repricer, CRM, scheduler state, or Autonomous_business. Kaspi internal marketing is a cash-flow overlay, not deterministic Meta attribution.",
]
(run_dir / "UNIVERSAL_MARKETING_OPS_CLOSEOUT.md").write_text("\n".join(closeout) + "\n", encoding="utf-8")

prompt = [
    "# Codex 5.5 High Review Prompt",
    "",
    "You are reviewing a read-only universal ACMEWEAR Kaspi Marketing evidence packet.",
    "",
    "Objective: inspect all registry campaigns plus discovered active campaign rows, all-store order momentum, seller-bonus context, and market-event overlays without proposing direct writes.",
    "",
    "Read first:",
    "",
    "1. `UNIVERSAL_MARKETING_OPS_CLOSEOUT.md`",
    "2. `summary.json`",
    "3. `resolved_scope.json`",
    "4. `exports/registry_campaign_rows.csv`",
    "5. `exports/live_inventory_campaign_rows.csv`",
    f"6. `exports/marketing_campaign_recent_summary_{start_date}_{today}.csv`",
    f"7. `exports/marketing_product_recent_summary_{start_date}_{today}.csv`",
    "8. `exports/market_event_overlay.csv`",
    "9. `exports/configured_seller_bonus_promos.csv`",
    "10. `exports/anomaly_signal_summary.csv`",
    f"11. `exports/all_store_api_orders_daily_summary_{start_date}_{today}.csv`",
    f"12. `exports/all_store_buyout_sales_daily_summary_{start_date}_{today}.csv`",
    "",
    "Rules: do not recommend automatic DB writes or live Kaspi changes. Separate raw API order momentum from finalized buyout economics. Flag stale or partial sources. Keep any suggested write as an owner-reviewable, separately approved action. Kaspi internal marketing is a cash-flow overlay, not deterministic Meta attribution.",
]
(run_dir / "CODEX_5_5_HIGH_REVIEW_PROMPT.md").write_text("\n".join(prompt) + "\n", encoding="utf-8")
PY

ln -sfn "$TIMESTAMP" "$RUN_ROOT/latest"

cat "$RUN_DIR/summary.json"
