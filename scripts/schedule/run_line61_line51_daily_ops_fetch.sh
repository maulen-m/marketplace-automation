#!/usr/bin/env bash
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
PYTHON_BIN="${PYTHON_BIN:-$REPO_ROOT/.venv/bin/python}"
if [[ ! -x "$PYTHON_BIN" ]]; then
  PYTHON_BIN="python3"
fi

ENV_FILE="${WEB_AUTOMATION_ENV_FILE:-$HOME/Docs/Autonomous_business/.env}"
AB_DB="${AUTONOMOUS_BUSINESS_DB:-$HOME/Docs/Autonomous_business/db/app.db}"
MARKETING_DB="${LINE61_LINE51_MARKETING_DB:-$REPO_ROOT/data/kaspi_marketing.sqlite}"
RUN_ROOT_REL="runs/line61_line51_daily_ops_fetch"
RUN_ROOT="$REPO_ROOT/$RUN_ROOT_REL"
LOOKBACK_DAYS="${LINE61_LINE51_DAILY_LOOKBACK_DAYS:-45}"
TIMESTAMP="${LINE61_LINE51_DAILY_FETCH_TIMESTAMP:-$(date +%Y%m%d_%H%M%S)}"
TODAY="${LINE61_LINE51_DAILY_FETCH_DATE:-$(date +%F)}"
RUN_DIR="$RUN_ROOT/$TIMESTAMP"
LOG_DIR="$RUN_DIR/logs"
EXPORT_DIR="$RUN_DIR/exports"
STEP_STATUS="$RUN_DIR/step_status.csv"
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

run_sqlite_export() {
  local step="$1"
  local db_path="$2"
  local out_path="$3"
  local sql="$4"
  local step_dir="$LOG_DIR/$step"
  mkdir -p "$step_dir" "$(dirname "$out_path")"
  printf '%s\n' "$sql" > "$step_dir/query.sql"
  if [[ ! -f "$db_path" ]]; then
    printf 'missing db: %s\n' "$db_path" > "$step_dir/stderr.txt"
    append_status "$step" "failed" "2" "" "$step_dir/stderr.txt"
    mark_yellow
    return 0
  fi
  sqlite3 -readonly -header -csv "$db_path" "$sql" > "$out_path" 2> "$step_dir/stderr.txt"
  local rc=$?
  if [[ "$rc" -eq 0 ]]; then
    append_status "$step" "success" "$rc" "$out_path" "$step_dir/stderr.txt"
  else
    append_status "$step" "failed" "$rc" "$out_path" "$step_dir/stderr.txt"
    mark_yellow
  fi
  return 0
}

printf 'generated_at,step,status,returncode,stdout_path,stderr_path\n' > "$STEP_STATUS"

run_step marketing_fetch \
  "$PYTHON_BIN" -m web_auto --env-file "$ENV_FILE" --json kaspi-marketing fetch-campaigns \
  --store ACMEWEAR \
  --merchant-id 759051 \
  --store-code 30137883 \
  --campaign-ids 2545773,2629982,2380614,2626530,2690256 \
  --date "$TODAY" \
  --headless \
  --run-dir "$RUN_ROOT_REL/$TIMESTAMP/marketing_fetch_$TODAY" \
  --db-path data/kaspi_marketing.sqlite

run_step line61_watch_health \
  "$PYTHON_BIN" -m web_auto --env-file "$ENV_FILE" --json experiment-dashboard watch-health \
  --sku-key CL_NEW-CLO2_MEN_SUIT-61_BLACK \
  --store ACMEWEAR \
  --campaign-id 2545773 \
  --campaign-id 2629982 \
  --date-from-days "$LOOKBACK_DAYS" \
  --date-to today \
  --headless \
  --allow-stale

run_step line51_watch_health \
  "$PYTHON_BIN" -m web_auto --env-file "$ENV_FILE" --json experiment-dashboard watch-health \
  --sku-key CL_OC_MEN_LINE51_WHITE \
  --store ACMEWEAR \
  --campaign-id 2380614 \
  --campaign-id 2626530 \
  --campaign-id 2690256 \
  --date-from-days "$LOOKBACK_DAYS" \
  --date-to today \
  --headless \
  --allow-stale

run_step pricelist_download \
  "$PYTHON_BIN" -m web_auto --env-file "$ENV_FILE" --json kaspi-pricelist download \
  --store ACMEWEAR \
  --run-dir "$RUN_ROOT_REL/$TIMESTAMP/pricelist_download" \
  --headless

run_sqlite_export marketing_campaign_daily_current "$MARKETING_DB" "$EXPORT_DIR/marketing_campaign_daily_current.csv" "
SELECT date, merchant_id, store_code, campaign_id, campaign_name, state, daily_budget, default_bid,
       views, clicks, favorites, carts, ctr, gmv, transactions, cost, crr, report_state,
       record_timestamp, ingested_at
FROM campaign_daily_current
WHERE campaign_id IN ('2545773','2629982','2380614','2626530','2690256')
ORDER BY date, campaign_id;
"

run_sqlite_export marketing_product_daily_current "$MARKETING_DB" "$EXPORT_DIR/marketing_product_daily_current.csv" "
SELECT date, merchant_id, store_code, campaign_id, campaign_name, sku_key, product_name, product_status,
       bid_cpc, avg_cpc, views, clicks, favorites, carts, ctr, gmv, orders_total, orders_direct,
       orders_assisted, conversion_order, cost, acos_share, json_sku, json_merchant_sku,
       ingested_at, bid_cpc_source
FROM campaign_product_daily_current
WHERE campaign_id IN ('2545773','2629982','2380614','2626530','2690256')
ORDER BY date, campaign_id, json_merchant_sku;
"

run_sqlite_export marketing_recent_summary "$MARKETING_DB" "$EXPORT_DIR/marketing_recent_summary_${START_DATE}_${TODAY}.csv" "
WITH rows AS (
  SELECT CASE WHEN campaign_id IN ('2545773','2629982') THEN 'LINE61'
              WHEN campaign_id IN ('2380614','2626530','2690256') THEN 'LINE51'
         END AS family,
         campaign_id, campaign_name, date, bid_cpc, views, clicks, carts, orders_total, gmv, cost
  FROM campaign_product_daily_current
  WHERE campaign_id IN ('2545773','2629982','2380614','2626530','2690256')
    AND date BETWEEN '$START_DATE' AND '$TODAY'
)
SELECT family, campaign_id, campaign_name, MIN(date) AS min_date, MAX(date) AS max_date, COUNT(*) AS rows,
       ROUND(AVG(bid_cpc), 2) AS avg_bid, SUM(views) AS views, SUM(clicks) AS clicks, SUM(carts) AS carts,
       SUM(orders_total) AS marketing_orders, ROUND(SUM(gmv), 0) AS marketing_gmv, ROUND(SUM(cost), 0) AS ad_cost,
       ROUND(CASE WHEN SUM(clicks)>0 THEN SUM(cost)*1.0/SUM(clicks) END, 2) AS avg_cpc,
       ROUND(CASE WHEN SUM(orders_total)>0 THEN SUM(cost)*1.0/SUM(orders_total) END, 0) AS cost_per_marketing_order
FROM rows
GROUP BY family, campaign_id, campaign_name
ORDER BY family, campaign_id;
"

run_sqlite_export ab_api_orders_recent "$AB_DB" "$EXPORT_DIR/ab_api_orders_recent_${START_DATE}_${TODAY}.csv" "
SELECT substr(created_at, 1, 10) AS order_date, order_id, store_code, sku_key, sku_id, my_size, quantity,
       unit_price_kzt, kaspi_status, internal_status, delivery_mode, payment_mode, source,
       created_at, updated_at, imported_at
FROM fact_orders_kaspi
WHERE store_code='ACMEWEAR'
  AND sku_key IN ('CL_NEW-CLO2_MEN_SUIT-61_BLACK','CL_OC_MEN_LINE51_WHITE')
  AND substr(created_at, 1, 10) BETWEEN '$START_DATE' AND '$TODAY'
ORDER BY created_at, sku_key, order_id;
"

run_sqlite_export ab_api_orders_daily_summary "$AB_DB" "$EXPORT_DIR/ab_api_orders_daily_summary_${START_DATE}_${TODAY}.csv" "
SELECT substr(created_at, 1, 10) AS order_date, sku_key, kaspi_status,
       COUNT(DISTINCT order_id) AS orders, SUM(quantity) AS units,
       ROUND(SUM(unit_price_kzt * quantity), 0) AS gross
FROM fact_orders_kaspi
WHERE store_code='ACMEWEAR'
  AND sku_key IN ('CL_NEW-CLO2_MEN_SUIT-61_BLACK','CL_OC_MEN_LINE51_WHITE')
  AND substr(created_at, 1, 10) BETWEEN '$START_DATE' AND '$TODAY'
GROUP BY order_date, sku_key, kaspi_status
ORDER BY order_date, sku_key, kaspi_status;
"

run_sqlite_export ab_buyout_sales_daily_summary "$AB_DB" "$EXPORT_DIR/ab_buyout_sales_daily_summary_${START_DATE}_${TODAY}.csv" "
SELECT order_date, sku_key, status, return_flag, COUNT(DISTINCT order_id) AS orders, SUM(quantity) AS units,
       ROUND(SUM(sell_price_kzt * quantity), 0) AS gross,
       ROUND(SUM(net_rev), 0) AS net_rev,
       ROUND(SUM(cogs), 0) AS cogs,
       ROUND(SUM(profit), 0) AS profit
FROM sales_fact_v2
WHERE store_code='ACMEWEAR'
  AND sku_key IN ('CL_NEW-CLO2_MEN_SUIT-61_BLACK','CL_OC_MEN_LINE51_WHITE')
  AND order_date BETWEEN '$START_DATE' AND '$TODAY'
GROUP BY order_date, sku_key, status, return_flag
ORDER BY order_date, sku_key, status, return_flag;
"

run_sqlite_export source_freshness "$AB_DB" "$EXPORT_DIR/ab_source_freshness.csv" "
SELECT 'fact_orders_kaspi' AS source_table, COUNT(*) AS rows, MAX(created_at) AS max_event_at
FROM fact_orders_kaspi
UNION ALL
SELECT 'sales_fact_v2' AS source_table, COUNT(*) AS rows, MAX(order_date) AS max_event_at
FROM sales_fact_v2;
"

"$PYTHON_BIN" - "$RUN_DIR" "$STEP_STATUS" "$MARKETING_DB" "$AB_DB" "$START_DATE" "$TODAY" <<'PY'
from __future__ import annotations

import csv
import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

run_dir = Path(sys.argv[1])
step_status = Path(sys.argv[2])
marketing_db = Path(sys.argv[3])
ab_db = Path(sys.argv[4])
start_date = sys.argv[5]
today = sys.argv[6]
export_dir = run_dir / "exports"

def write_csv(path: Path, rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields: list[str] = []
    seen: set[str] = set()
    for row in rows:
        for key in row:
            if key not in seen:
                fields.append(key)
                seen.add(key)
    with path.open("w", newline="", encoding="utf-8") as handle:
        if fields:
            writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(rows)

def sqlite_rows(path: Path, sql: str) -> list[dict[str, object]]:
    if not path.exists():
        return []
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(row) for row in conn.execute(sql)]
    finally:
        conn.close()

def latest_pricelist_paths() -> tuple[Path | None, Path | None]:
    candidates = list((run_dir / "pricelist_download").rglob("*.xlsx"))
    active = sorted([p for p in candidates if "ACTIVE" in p.name.upper()])
    archive = sorted([p for p in candidates if "ARCHIVE" in p.name.upper()])
    return (active[-1] if active else None, archive[-1] if archive else None)

def normalize_headers(values: list[object]) -> list[str]:
    out: list[str] = []
    counts: dict[str, int] = {}
    for idx, value in enumerate(values, start=1):
        base = str(value or "").strip() or f"col_{idx}"
        base = base.replace("\n", " ").strip()
        count = counts.get(base, 0) + 1
        counts[base] = count
        out.append(base if count == 1 else f"{base}_{count}")
    return out

def export_offer_rows() -> tuple[str, int]:
    active, archive = latest_pricelist_paths()
    rows: list[dict[str, object]] = []
    prefixes = ("CL_NEW-CLO2_MEN_SUIT-61_BLACK", "CL_OC_MEN_LINE51_WHITE")
    try:
        from openpyxl import load_workbook
    except Exception as exc:
        return (f"openpyxl_unavailable:{exc}", 0)
    for state, path in (("ACTIVE", active), ("ARCHIVE", archive)):
        if path is None:
            continue
        wb = load_workbook(path, read_only=True, data_only=True)
        ws = wb.active
        iterator = ws.iter_rows(values_only=True)
        try:
            headers = normalize_headers(list(next(iterator)))
        except StopIteration:
            continue
        for values in iterator:
            text = " ".join("" if v is None else str(v) for v in values)
            if not any(prefix in text for prefix in prefixes):
                continue
            row = {"source_state": state, "source_workbook": str(path)}
            for key, value in zip(headers, values):
                row[key] = value
            rows.append(row)
    write_csv(export_dir / "merchant_pricelist_offer_rows_line61_line51.csv", rows)
    summary: dict[tuple[str, str], int] = {}
    for row in rows:
        text = " ".join("" if v is None else str(v) for v in row.values())
        family = "LINE61" if "CL_NEW-CLO2_MEN_SUIT-61_BLACK" in text else "LINE51"
        key = (str(row.get("source_state") or ""), family)
        summary[key] = summary.get(key, 0) + 1
    write_csv(
        export_dir / "merchant_pricelist_offer_summary.csv",
        [
            {"source_state": state, "family": family, "rows": count}
            for (state, family), count in sorted(summary.items())
        ],
    )
    return ("success", len(rows))

offer_status, offer_rows = export_offer_rows()
marketing_latest = sqlite_rows(
    marketing_db,
    """
    WITH ranked AS (
      SELECT *, ROW_NUMBER() OVER (PARTITION BY campaign_id ORDER BY date DESC, ingested_at DESC) AS rn
      FROM campaign_product_daily_current
      WHERE campaign_id IN ('2545773','2629982','2380614','2626530','2690256')
    )
    SELECT date, campaign_id, campaign_name, json_sku, json_merchant_sku, product_status, bid_cpc,
           views, clicks, carts, orders_total, gmv, cost, ingested_at
    FROM ranked WHERE rn=1 ORDER BY campaign_id
    """,
)
ab_latest = sqlite_rows(
    ab_db,
    """
    SELECT 'fact_orders_kaspi' AS source_table, COUNT(*) AS rows, MAX(created_at) AS max_event_at
    FROM fact_orders_kaspi
    UNION ALL
    SELECT 'sales_fact_v2' AS source_table, COUNT(*) AS rows, MAX(order_date) AS max_event_at
    FROM sales_fact_v2
    """,
)

steps = []
if step_status.exists():
    with step_status.open(newline="", encoding="utf-8") as handle:
        steps = list(csv.DictReader(handle))
gate = "GREEN"
if any(row.get("status") != "success" for row in steps):
    gate = "YELLOW"
if offer_status != "success":
    gate = "YELLOW"

summary = {
    "status": "success" if gate == "GREEN" else "partial",
    "gate": gate,
    "generated_at": datetime.now().isoformat(timespec="seconds"),
    "run_dir": str(run_dir),
    "date": today,
    "lookback_start": start_date,
    "offer_export_status": offer_status,
    "offer_rows": offer_rows,
    "marketing_latest_rows": len(marketing_latest),
    "ab_source_rows": ab_latest,
}
(run_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

def md_table(rows: list[dict[str, object]], columns: list[str]) -> str:
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
    for row in rows:
        lines.append("| " + " | ".join(str(row.get(column, "")).ljust(widths[column]) for column in columns) + " |")
    return "\n".join(lines)

closeout = [
    "# Line61/LINE51 Daily Ops Fetch Closeout",
    "",
    f"Gate: {gate}",
    "",
    f"- generated_at: `{summary['generated_at']}`",
    f"- run_dir: `{run_dir}`",
    f"- date: `{today}`",
    f"- lookback_start: `{start_date}`",
    f"- mode: read-only",
    "",
    "## Latest Marketing Product Rows",
    "",
    md_table(marketing_latest, ["campaign_id", "campaign_name", "json_merchant_sku", "product_status", "bid_cpc", "views", "clicks", "orders_total", "cost", "ingested_at"]),
    "",
    "## Source Freshness",
    "",
    md_table(ab_latest, ["source_table", "rows", "max_event_at"]),
    "",
    "## Artifacts",
    "",
    "- `marketing_fetch_<date>/campaign_daily_current.csv`",
    "- `marketing_fetch_<date>/campaign_product_daily_current.csv`",
    "- `marketing_fetch_<date>/raw/`",
    "- `pricelist_download/`",
    "- `exports/marketing_campaign_daily_current.csv`",
    "- `exports/marketing_product_daily_current.csv`",
    "- `exports/marketing_recent_summary_<start>_<date>.csv`",
    "- `exports/merchant_pricelist_offer_rows_line61_line51.csv`",
    "- `exports/merchant_pricelist_offer_summary.csv`",
    "- `exports/ab_api_orders_recent_<start>_<date>.csv`",
    "- `exports/ab_api_orders_daily_summary_<start>_<date>.csv`",
    "- `exports/ab_buyout_sales_daily_summary_<start>_<date>.csv`",
    "",
    "## No-Write Confirmation",
    "",
    "No price, stock, listing, bid, budget, campaign state, product state, Meta, Repricer, CRM, scheduler, or Autonomous_business write is performed by this daily fetch.",
]
(run_dir / "DAILY_FETCH_CLOSEOUT.md").write_text("\n".join(closeout) + "\n", encoding="utf-8")

prompt = [
    "# Codex 5.5 High Review Prompt",
    "",
    "You are reviewing a read-only daily operations evidence packet for ACMEWEAR Line61 and LINE51.",
    "",
    "Objective: identify operational steering signals in price, listing/sellability, stock/offer rows, internal Kaspi Marketing campaigns, spend, BID, budget, views, clicks, carts, orders, API orders, and buyout/finalized sales evidence.",
    "",
    "Read first:",
    "",
    "1. `DAILY_FETCH_CLOSEOUT.md`",
    "2. `summary.json`",
    "3. `exports/marketing_recent_summary_<start>_<date>.csv`",
    "4. `exports/merchant_pricelist_offer_summary.csv`",
    "5. `exports/ab_api_orders_daily_summary_<start>_<date>.csv`",
    "6. `exports/ab_buyout_sales_daily_summary_<start>_<date>.csv`",
    "",
    "Rules: do not propose direct DB writes. Separate raw API order momentum from finalized/buyout economics. Flag stale or partial sources. Recommend operational steering options only.",
]
(run_dir / "CODEX_5_5_HIGH_REVIEW_PROMPT.md").write_text("\n".join(prompt) + "\n", encoding="utf-8")
PY

ln -sfn "$TIMESTAMP" "$RUN_ROOT/latest"

cat "$RUN_DIR/summary.json"
