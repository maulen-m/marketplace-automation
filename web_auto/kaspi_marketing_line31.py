from __future__ import annotations

import csv
import json
import shutil
from datetime import date, datetime
from pathlib import Path
from typing import Any, Mapping, Sequence

import yaml
from playwright.sync_api import sync_playwright

from .kaspi_marketing import MarketingCredentials, login_kaspi_marketing, resolve_marketing_credentials, run_kaspi_marketing_fetch
from .kaspi_merchant_common import merchant_browser_launch_kwargs


LINE31_SCHEMA_VERSION = "web_auto.kaspi_marketing_line31.v1"
DEFAULT_SCOPE_CONFIG = Path("config/tasks/line31_marketing_scope.yaml")
DEFAULT_SCOPE_RUN_ROOT = Path("runs/kaspi_marketing_line31_scope")
DEFAULT_FETCH_RUN_ROOT = Path("runs/kaspi_marketing_line31_fetch")
DEFAULT_ANALYSIS_RUN_ROOT = Path("runs/kaspi_marketing_line31_analysis")
DEFAULT_RECOMMEND_RUN_ROOT = Path("runs/kaspi_marketing_line31_recommendations")


class LINE31MarketingError(ValueError):
    pass


def _timestamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def _today() -> str:
    return date.today().isoformat()


def _load_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise LINE31MarketingError(f"scope_config_not_found:{path}")
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise LINE31MarketingError("scope config must be a mapping")
    return raw


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")


def _write_yaml(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(payload, allow_unicode=True, sort_keys=False), encoding="utf-8")


def _write_csv(path: Path, rows: Sequence[Mapping[str, Any]], fieldnames: Sequence[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(fieldnames), extrasaction="ignore", lineterminator="\n")
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field, "") for field in fieldnames})


def _read_csv(path: Path | None) -> list[dict[str, str]]:
    if path is None:
        return []
    if not path.exists():
        raise LINE31MarketingError(f"csv_not_found:{path}")
    with path.open("r", newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        return [{str(k): str(v or "") for k, v in row.items() if k is not None} for row in reader]


def _first(row: Mapping[str, Any], *keys: str) -> str:
    for key in keys:
        value = row.get(key)
        if value not in (None, ""):
            return str(value).strip()
    return ""


def _norm(value: Any) -> str:
    return str(value or "").strip().lower()


def _number(value: Any) -> float | None:
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if not text:
        return None
    text = text.replace("\u00a0", " ").replace("₸", "").replace("т", "").replace("%", "")
    text = text.replace(" ", "").replace(",", ".")
    if "/" in text:
        text = text.split("/", 1)[0]
    try:
        return float(text)
    except ValueError:
        return None


def _score(value: Any) -> float | None:
    return _number(value)


def _score_band(value: float | None) -> str:
    if value is None:
        return "unknown"
    if value < 5:
        return "low"
    if value < 7:
        return "medium"
    return "high"


def _as_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item or "").strip()]
    return [str(value).strip()] if str(value or "").strip() else []


def _campaign_id(row: Mapping[str, Any]) -> str:
    return _first(row, "campaign_id", "id")


def _campaign_name(row: Mapping[str, Any]) -> str:
    return _first(row, "campaign_name", "name", "title")


def _product_sku(row: Mapping[str, Any]) -> str:
    return _first(row, "product_sku", "sku_key", "sku", "json_sku")


def _merchant_sku(row: Mapping[str, Any]) -> str:
    return _first(row, "merchant_sku", "json_merchant_sku", "offerId")


def _product_name(row: Mapping[str, Any]) -> str:
    return _first(row, "product_name", "name", "title", "product")


def _product_bid(row: Mapping[str, Any]) -> str:
    return _first(row, "bid_kzt", "bid_cpc", "bid", "click_bid_kzt")


def _ad_score(row: Mapping[str, Any]) -> str:
    return _first(row, "ad_score", "advertisement_score", "score", "rating")


def _load_scope_config(path: Path) -> dict[str, Any]:
    raw = _load_yaml(path)
    schema = str(raw.get("schema_version") or "").strip()
    if schema != LINE31_SCHEMA_VERSION:
        raise LINE31MarketingError(f"unsupported_schema_version:{schema or '<missing>'}")
    return raw


def _seed_campaign_rows(config: Mapping[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for item in config.get("seed_campaigns") or []:
        if not isinstance(item, dict):
            continue
        cid = str(item.get("campaign_id") or "").strip()
        if not cid:
            continue
        rows.append(
            {
                "campaign_id": cid,
                "campaign_name": str(item.get("campaign_name") or cid).strip(),
                "color_label": str(item.get("color_label") or "").strip(),
                "product_sku": str(item.get("product_sku") or "").strip(),
                "merchant_sku": str(item.get("merchant_sku") or "").strip(),
                "product_name": str(item.get("product_name") or "").strip(),
                "configured_bid_kzt": str(item.get("bid_kzt") or item.get("configured_bid_kzt") or "").strip(),
                "daily_budget_kzt": str(item.get("daily_budget_kzt") or "").strip(),
                "ad_score": str(item.get("ad_score") or "").strip(),
                "include_reasons": "seed_campaign",
                "confidence": "seeded",
            }
        )
    return rows


def _merged_campaign_rows(
    *,
    config: Mapping[str, Any],
    campaign_rows: Sequence[Mapping[str, Any]],
    product_rows: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    discovery = config.get("discovery") if isinstance(config.get("discovery"), dict) else {}
    campaign_terms = _as_list((discovery or {}).get("campaign_name_terms") or ["LINE31"])
    product_terms = _as_list((discovery or {}).get("product_terms") or ["LINE31"])
    merchant_terms = _as_list((discovery or {}).get("merchant_sku_terms") or ["LINE31"])
    exclude_terms = [_norm(item) for item in _as_list((discovery or {}).get("exclude_terms"))]

    campaign_by_id = {_campaign_id(row): row for row in campaign_rows if _campaign_id(row)}
    products_by_campaign: dict[str, list[Mapping[str, Any]]] = {}
    for row in product_rows:
        cid = _campaign_id(row)
        if cid:
            products_by_campaign.setdefault(cid, []).append(row)

    out: dict[str, dict[str, Any]] = {}
    for seed in _seed_campaign_rows(config):
        out[seed["campaign_id"]] = seed

    for cid, campaign in campaign_by_id.items():
        products = products_by_campaign.get(cid) or [{}]
        for product in products:
            cname = _campaign_name(campaign)
            pname = _product_name(product)
            msku = _merchant_sku(product)
            haystack = _norm(" ".join([cid, cname, pname, msku, _product_sku(product)]))
            if any(term and term in haystack for term in exclude_terms):
                continue
            reasons: list[str] = []
            for term in campaign_terms:
                if _norm(term) and _norm(term) in _norm(cname):
                    reasons.append(f"campaign_name:{term}")
            for term in product_terms:
                if _norm(term) and _norm(term) in _norm(pname):
                    reasons.append(f"product:{term}")
            for term in merchant_terms:
                if _norm(term) and _norm(term) in _norm(msku):
                    reasons.append(f"merchant_sku:{term}")
            if not reasons:
                continue
            existing = out.get(cid, {})
            if existing:
                prior = _as_list(str(existing.get("include_reasons") or "").replace(";", ",").split(","))
                reasons = [*prior, *[item for item in reasons if item not in prior]]
            out[cid] = {
                "campaign_id": cid,
                "campaign_name": cname or str(existing.get("campaign_name") or cid),
                "color_label": str(existing.get("color_label") or "").strip(),
                "product_sku": _product_sku(product) or str(existing.get("product_sku") or ""),
                "merchant_sku": msku or str(existing.get("merchant_sku") or ""),
                "product_name": pname or str(existing.get("product_name") or ""),
                "configured_bid_kzt": _product_bid(product) or str(existing.get("configured_bid_kzt") or ""),
                "daily_budget_kzt": _first(campaign, "daily_budget_kzt", "daily_budget", "dailyBudget")
                or str(existing.get("daily_budget_kzt") or ""),
                "ad_score": _ad_score(product) or str(existing.get("ad_score") or ""),
                "include_reasons": ";".join(dict.fromkeys(reasons)),
                "confidence": "seeded_and_discovered" if existing else "dynamic",
            }
    return list(out.values())


def _promo_rows(config: Mapping[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for item in config.get("promos") or []:
        if not isinstance(item, dict):
            continue
        promo_id = str(item.get("promo_id") or item.get("promotion_id") or "").strip()
        if not promo_id:
            continue
        rows.append(
            {
                "promo_id": promo_id,
                "promo_name": str(item.get("name") or item.get("promo_name") or promo_id).strip(),
                "status": str(item.get("status") or "").strip(),
                "bonus_percent": item.get("bonus_percent", ""),
                "bonus_kzt": item.get("bonus_kzt", ""),
                "url": str(item.get("url") or "").strip(),
                "edit_url": str(item.get("edit_url") or "").strip(),
                "evidence_json": str(item.get("evidence_json") or "").strip(),
                "include_reasons": "seed_promo",
            }
        )
    return rows


def _render_scope_closeout(summary: Mapping[str, Any]) -> str:
    lines = [
        "# LINE31 Marketing Scope Resolution",
        "",
        f"Gate: {summary['gate']}",
        "",
        f"- status: `{summary['status']}`",
        f"- run_dir: `{summary['run_dir']}`",
        f"- store: `{summary['store']}`",
        f"- campaign_count: `{summary['campaign_count']}`",
        f"- promo_count: `{summary['promo_count']}`",
        f"- live_writes_executed: `{summary['live_writes_executed']}`",
        "",
    ]
    if summary.get("issues"):
        lines.extend(["## Issues", ""])
        lines.extend(f"- `{issue}`" for issue in summary.get("issues") or [])
        lines.append("")
    return "\n".join(lines)


def resolve_line31_scope(
    *,
    scope_config_path: Path = DEFAULT_SCOPE_CONFIG,
    campaign_csv: Path | None = None,
    product_csv: Path | None = None,
    run_root: Path = DEFAULT_SCOPE_RUN_ROOT,
    timestamp: str | None = None,
    out: Path | None = None,
) -> dict[str, Any]:
    config = _load_scope_config(scope_config_path)
    run_dir = run_root / f"{timestamp or _timestamp()}_line31_scope"
    run_dir.mkdir(parents=True, exist_ok=False)

    campaign_rows = _read_csv(campaign_csv)
    product_rows = _read_csv(product_csv)
    campaigns = _merged_campaign_rows(config=config, campaign_rows=campaign_rows, product_rows=product_rows)
    promos = _promo_rows(config)
    issues: list[str] = []
    if not campaigns:
        issues.append("no_line31_campaigns_resolved")

    resolved_scope = {
        "schema_version": LINE31_SCHEMA_VERSION,
        "generated_at_local": datetime.now().isoformat(timespec="seconds"),
        "store": str(config.get("store") or "ACMEWEAR"),
        "merchant_id": str(config.get("merchant_id") or ""),
        "store_code": str(config.get("store_code") or ""),
        "family": str(config.get("family") or "LINE31"),
        "timezone": str(config.get("timezone") or "Asia/Almaty"),
        "campaigns": campaigns,
        "promos": promos,
        "source_scope_config": str(scope_config_path),
        "source_campaign_csv": str(campaign_csv or ""),
        "source_product_csv": str(product_csv or ""),
        "live_writes_executed": False,
    }
    resolved_path = out or (run_dir / "line31_marketing_scope.resolved.yaml")
    _write_yaml(resolved_path, resolved_scope)

    campaign_fieldnames = [
        "campaign_id",
        "campaign_name",
        "color_label",
        "product_sku",
        "merchant_sku",
        "product_name",
        "configured_bid_kzt",
        "daily_budget_kzt",
        "ad_score",
        "include_reasons",
        "confidence",
    ]
    promo_fieldnames = [
        "promo_id",
        "promo_name",
        "status",
        "bonus_percent",
        "bonus_kzt",
        "url",
        "edit_url",
        "evidence_json",
        "include_reasons",
    ]
    _write_csv(run_dir / "line31_scope_campaigns.csv", campaigns, campaign_fieldnames)
    _write_csv(run_dir / "line31_scope_promos.csv", promos, promo_fieldnames)

    gate = "GREEN" if not issues else "YELLOW"
    summary = {
        "schema_version": LINE31_SCHEMA_VERSION,
        "status": "scope_ready" if gate == "GREEN" else "scope_incomplete",
        "gate": gate,
        "run_dir": str(run_dir),
        "store": resolved_scope["store"],
        "merchant_id": resolved_scope["merchant_id"],
        "store_code": resolved_scope["store_code"],
        "campaign_count": len(campaigns),
        "promo_count": len(promos),
        "campaign_ids": [row["campaign_id"] for row in campaigns],
        "promo_ids": [row["promo_id"] for row in promos],
        "campaigns": campaigns,
        "promos": promos,
        "resolved_scope_path": str(resolved_path),
        "live_writes_executed": False,
        "issues": issues,
    }
    _write_json(run_dir / "LINE31_SCOPE_SUMMARY.json", summary)
    (run_dir / "LINE31_SCOPE_CLOSEOUT.md").write_text(_render_scope_closeout(summary), encoding="utf-8")
    return summary


def _load_scope(path: Path) -> dict[str, Any]:
    raw = _load_yaml(path)
    if str(raw.get("schema_version") or "") != LINE31_SCHEMA_VERSION:
        raise LINE31MarketingError("scope must use LINE31 schema")
    if not raw.get("campaigns") and raw.get("seed_campaigns"):
        raw = {
            **raw,
            "campaigns": _seed_campaign_rows(raw),
            "promos": _promo_rows(raw),
        }
    return raw


def _build_campaign_fetch_argv(scope: Mapping[str, Any], campaign_ids: Sequence[str], target_date: str, run_dir: Path) -> list[str]:
    argv = [
        "./web-auto",
        "--json",
        "kaspi-marketing",
        "fetch-campaigns",
        "--store",
        str(scope.get("store") or "ACMEWEAR"),
    ]
    for campaign_id in campaign_ids:
        argv.extend(["--campaign-id", str(campaign_id)])
    argv.extend(["--date", target_date, "--run-dir", str(run_dir / "campaign_fetch"), "--headless"])
    merchant_id = str(scope.get("merchant_id") or "").strip()
    store_code = str(scope.get("store_code") or "").strip()
    if merchant_id:
        argv.extend(["--merchant-id", merchant_id])
    if store_code:
        argv.extend(["--store-code", store_code])
    return argv


def _promo_detail_url(promo: Mapping[str, Any], target_date: str) -> str:
    explicit = str(promo.get("url") or "").strip()
    if explicit:
        return explicit
    promo_id = str(promo.get("promo_id") or "").strip()
    if not promo_id:
        return ""
    return (
        f"https://marketing.kaspi.kz/bonuses/products/promotions/{promo_id}"
        f"?startDate={target_date}&endDate={target_date}"
    )


def _seller_bonus_rows_from_evidence(promo: Mapping[str, Any]) -> list[dict[str, Any]]:
    evidence_text = str(promo.get("evidence_json") or "").strip()
    evidence_path = Path(evidence_text) if evidence_text else None
    if evidence_path is None or not evidence_path.exists() or not evidence_path.is_file():
        return [
            {
                "promo_id": promo.get("promo_id", ""),
                "promo_name": promo.get("promo_name", ""),
                "product_title": "",
                "status": promo.get("status", ""),
                "ad_score": "",
                "price_kzt": "",
                "bonus_kzt": promo.get("bonus_kzt", ""),
                "bonus_percent": promo.get("bonus_percent", ""),
                "views": "",
                "clicks": "",
                "favorites": "",
                "carts": "",
                "orders": "",
                "revenue_kzt": "",
                "bonuses_paid_kzt": "",
                "evidence_json": evidence_text,
            }
        ]
    data = json.loads(evidence_path.read_text(encoding="utf-8"))
    line31 = data.get("line31_seller_bonus_promo") if isinstance(data, dict) else {}
    if not isinstance(line31, dict):
        return []
    metrics = line31.get("list_metrics_at_capture") if isinstance(line31.get("list_metrics_at_capture"), dict) else {}
    rows: list[dict[str, Any]] = []
    for product in line31.get("products") or []:
        if not isinstance(product, dict):
            continue
        rows.append(
            {
                "promo_id": line31.get("promotion_id_from_url") or promo.get("promo_id", ""),
                "promo_name": line31.get("name") or promo.get("promo_name", ""),
                "product_title": product.get("title", ""),
                "status": product.get("status") or line31.get("status") or "",
                "ad_score": product.get("ad_score", ""),
                "price_kzt": product.get("price_kzt", ""),
                "bonus_kzt": product.get("bonus_kzt", ""),
                "bonus_percent": product.get("bonus_percent", ""),
                "views": metrics.get("views", ""),
                "clicks": metrics.get("clicks", ""),
                "favorites": metrics.get("favorites", ""),
                "carts": metrics.get("carts", ""),
                "orders": metrics.get("orders", ""),
                "revenue_kzt": metrics.get("order_revenue_kzt", ""),
                "bonuses_paid_kzt": metrics.get("bonuses_paid_kzt", ""),
                "evidence_json": str(evidence_path),
            }
        )
    return rows


def _write_seller_bonus_snapshot(scope: Mapping[str, Any], path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for promo in scope.get("promos") or []:
        if isinstance(promo, dict):
            rows.extend(_seller_bonus_rows_from_evidence(promo))
    fields = [
        "promo_id",
        "promo_name",
        "product_title",
        "status",
        "ad_score",
        "price_kzt",
        "bonus_kzt",
        "bonus_percent",
        "views",
        "clicks",
        "favorites",
        "carts",
        "orders",
        "revenue_kzt",
        "bonuses_paid_kzt",
        "evidence_json",
    ]
    _write_csv(path, rows, fields)
    return rows


def _capture_seller_bonus_promos(
    *,
    creds: MarketingCredentials,
    scope: Mapping[str, Any],
    target_date: str,
    run_dir: Path,
    headless: bool,
) -> tuple[list[dict[str, Any]], Path]:
    raw_dir = run_dir / "seller_bonus_live"
    raw_dir.mkdir(parents=True, exist_ok=True)
    rows: list[dict[str, Any]] = []
    promos = [promo for promo in scope.get("promos") or [] if isinstance(promo, dict)]
    if promos:
        with sync_playwright() as p:
            browser = p.chromium.launch(**merchant_browser_launch_kwargs(headless=headless))
            context = browser.new_context(viewport={"width": 1440, "height": 1400})
            page = context.new_page()
            login_kaspi_marketing(page, login_value=creds.login, password_value=creds.password)
            for promo in promos:
                promo_id = str(promo.get("promo_id") or "").strip()
                url = _promo_detail_url(promo, target_date)
                if not promo_id or not url:
                    continue
                text_path = raw_dir / f"promo_{promo_id}_detail_text.txt"
                screenshot_path = raw_dir / f"promo_{promo_id}_detail.png"
                status = "captured"
                error = ""
                try:
                    page.goto(url, wait_until="domcontentloaded", timeout=60000)
                    page.wait_for_timeout(1500)
                    text_path.write_text(page.locator("body").inner_text(timeout=10000), encoding="utf-8")
                    page.screenshot(path=str(screenshot_path), full_page=True)
                except Exception as exc:
                    status = "failed"
                    error = str(exc)[:500]
                    text_path.write_text("", encoding="utf-8")
                rows.append(
                    {
                        "promo_id": promo_id,
                        "promo_name": promo.get("promo_name", ""),
                        "url": url,
                        "status": status,
                        "text_path": str(text_path),
                        "screenshot_path": str(screenshot_path) if screenshot_path.exists() else "",
                        "error": error,
                        "live_writes_executed": False,
                    }
                )
            context.close()
            browser.close()
    csv_path = run_dir / "line31_seller_bonus_live_capture.csv"
    _write_csv(
        csv_path,
        rows,
        ["promo_id", "promo_name", "url", "status", "text_path", "screenshot_path", "error", "live_writes_executed"],
    )
    return rows, csv_path


def _render_fetch_closeout(summary: Mapping[str, Any]) -> str:
    return "\n".join(
        [
            "# LINE31 Marketing Fetch Closeout",
            "",
            f"Gate: {summary['gate']}",
            "",
            f"- status: `{summary['status']}`",
            f"- run_dir: `{summary['run_dir']}`",
            f"- campaign_ids: `{','.join(summary.get('campaign_ids') or [])}`",
            f"- promo_ids: `{','.join(summary.get('promo_ids') or [])}`",
            f"- plan_only: `{summary.get('plan_only')}`",
            f"- live_writes_executed: `{summary['live_writes_executed']}`",
            "",
            "No live write is authorized by this fetcher.",
            "",
        ]
    )


def run_line31_fetch(
    *,
    scope_path: Path,
    target_date: str | None = None,
    closed_day: str | None = None,
    include_promos: bool = False,
    plan_only: bool = False,
    run_root: Path = DEFAULT_FETCH_RUN_ROOT,
    db_path: Path = Path("data/kaspi_marketing.sqlite"),
    env_file: Path | None = None,
    headless: bool = True,
    timestamp: str | None = None,
) -> dict[str, Any]:
    scope = _load_scope(scope_path)
    run_dir = run_root / f"{timestamp or _timestamp()}_line31_fetch"
    run_dir.mkdir(parents=True, exist_ok=False)
    resolved_date = str(target_date or _today())
    campaign_ids = [str(row.get("campaign_id") or "").strip() for row in scope.get("campaigns") or [] if isinstance(row, dict)]
    campaign_ids = [item for item in dict.fromkeys(campaign_ids) if item]
    promo_ids = [str(row.get("promo_id") or "").strip() for row in scope.get("promos") or [] if isinstance(row, dict)]
    promo_ids = [item for item in dict.fromkeys(promo_ids) if item]
    issues: list[str] = []
    if not campaign_ids:
        issues.append("no_campaign_ids_in_scope")

    seller_bonus_csv = run_dir / "line31_seller_bonus_snapshot.csv"
    seller_bonus_rows = _write_seller_bonus_snapshot(scope, seller_bonus_csv) if include_promos else []
    fetch_plan = {
        "schema_version": LINE31_SCHEMA_VERSION,
        "scope_path": str(scope_path),
        "target_date": resolved_date,
        "closed_day": str(closed_day or ""),
        "campaign_fetch_argv": _build_campaign_fetch_argv(scope, campaign_ids, resolved_date, run_dir),
        "include_promos": include_promos,
        "promo_ids": promo_ids,
        "promo_fetch_urls": [
            _promo_detail_url(promo, resolved_date)
            for promo in scope.get("promos") or []
            if isinstance(promo, dict) and _promo_detail_url(promo, resolved_date)
        ],
        "live_writes_executed": False,
    }
    fetch_plan_path = run_dir / "LINE31_FETCH_PLAN.json"
    _write_json(fetch_plan_path, fetch_plan)
    campaign_fetch_summary: dict[str, Any] | None = None
    promo_live_rows: list[dict[str, Any]] = []
    promo_live_csv = run_dir / "line31_seller_bonus_live_capture.csv"
    creds: MarketingCredentials | None = None
    if not plan_only and (campaign_ids or (include_promos and promo_ids)):
        creds = resolve_marketing_credentials(
            str(scope.get("store") or "ACMEWEAR"),
            env_file=env_file,
            merchant_id=str(scope.get("merchant_id") or "") or None,
            store_code=str(scope.get("store_code") or "") or None,
        )
    if not plan_only and campaign_ids and creds is not None:
        campaign_fetch_summary = run_kaspi_marketing_fetch(
            creds=creds,
            campaign_ids=campaign_ids,
            target_date=resolved_date,
            run_dir=run_dir / "campaign_fetch",
            db_path=db_path,
            headless=headless,
        )
    if not plan_only and include_promos and promo_ids and creds is not None:
        promo_live_rows, promo_live_csv = _capture_seller_bonus_promos(
            creds=creds,
            scope=scope,
            target_date=resolved_date,
            run_dir=run_dir,
            headless=headless,
        )

    gate = "GREEN" if not issues else "YELLOW"
    summary = {
        "schema_version": LINE31_SCHEMA_VERSION,
        "status": "planned" if plan_only else "success" if gate == "GREEN" else "warning",
        "gate": gate,
        "run_dir": str(run_dir),
        "store": str(scope.get("store") or "ACMEWEAR"),
        "target_date": resolved_date,
        "closed_day": str(closed_day or ""),
        "campaign_ids": campaign_ids,
        "promo_ids": promo_ids,
        "plan_only": plan_only,
        "include_promos": include_promos,
        "fetch_plan_path": str(fetch_plan_path),
        "seller_bonus_csv": str(seller_bonus_csv),
        "seller_bonus_rows": len(seller_bonus_rows),
        "seller_bonus_live_capture_csv": str(promo_live_csv),
        "seller_bonus_live_capture_rows": len(promo_live_rows),
        "campaign_fetch_summary": campaign_fetch_summary or {},
        "campaign_csv": str((campaign_fetch_summary or {}).get("campaign_csv") or ""),
        "product_csv": str((campaign_fetch_summary or {}).get("product_csv") or ""),
        "db_path": str(db_path),
        "live_writes_executed": False,
        "issues": issues,
    }
    summary_path = run_dir / "LINE31_FETCH_SUMMARY.json"
    _write_json(summary_path, summary)
    (run_dir / "LINE31_FETCH_CLOSEOUT.md").write_text(_render_fetch_closeout(summary), encoding="utf-8")
    current_dir = run_root / "current"
    current_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(summary_path, current_dir / "LINE31_FETCH_SUMMARY.json")
    return summary


def _promo_bonus_by_title(rows: Sequence[Mapping[str, Any]]) -> dict[str, float]:
    out: dict[str, float] = {}
    for row in rows:
        title = _norm(row.get("product_title"))
        if not title:
            continue
        paid = _number(row.get("bonuses_paid_kzt"))
        if paid is None:
            orders = _number(row.get("orders")) or 0
            bonus = _number(row.get("bonus_kzt")) or 0
            paid = orders * bonus
        out[title] = out.get(title, 0.0) + float(paid or 0)
    return out


def _matching_bonus(product_name: str, bonus_by_title: Mapping[str, float]) -> float:
    pname = _norm(product_name)
    if not pname:
        return 0.0
    for title, value in bonus_by_title.items():
        if title and (title in pname or pname in title):
            return float(value or 0)
    return 0.0


def _render_analysis_report(summary: Mapping[str, Any]) -> str:
    lines = [
        "# LINE31 Marketing Response Analysis",
        "",
        f"- generated_at_local: `{summary['generated_at_local']}`",
        f"- gate: `{summary['gate']}`",
        f"- analysis_rows: `{summary['analysis_row_count']}`",
        "",
        "This report preserves CPC ad spend and seller-bonus spend as separate source labels and does not claim deterministic color-level attribution.",
        "",
        "| campaign_id | campaign | score_band | bid | views | ad spend | bonus paid | combined cost | orders | views/KZT | spend/order |",
        "| --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for row in summary.get("analysis_rows") or []:
        lines.append(
            "| {campaign_id} | {campaign_name} | {score_band} | {configured_bid_kzt} | {views} | {ad_spend_kzt} | {seller_bonus_paid_kzt} | {combined_paid_growth_cost_kzt} | {orders} | {views_per_kzt} | {spend_per_order_kzt} |".format(
                **{key: row.get(key, "") for key in [
                    "campaign_id",
                    "campaign_name",
                    "score_band",
                    "configured_bid_kzt",
                    "views",
                    "ad_spend_kzt",
                    "seller_bonus_paid_kzt",
                    "combined_paid_growth_cost_kzt",
                    "orders",
                    "views_per_kzt",
                    "spend_per_order_kzt",
                ]}
            )
        )
    lines.append("")
    return "\n".join(lines)


def analyze_line31_snapshots(
    *,
    campaign_csv: Path,
    product_csv: Path,
    seller_bonus_csv: Path | None = None,
    run_root: Path = DEFAULT_ANALYSIS_RUN_ROOT,
    timestamp: str | None = None,
    emit_report: Path | None = None,
) -> dict[str, Any]:
    run_dir = run_root / f"{timestamp or _timestamp()}_line31_analysis"
    run_dir.mkdir(parents=True, exist_ok=False)
    _read_csv(campaign_csv)
    products = _read_csv(product_csv)
    promo_rows = _read_csv(seller_bonus_csv) if seller_bonus_csv else []
    bonus_by_title = _promo_bonus_by_title(promo_rows)
    analysis_rows: list[dict[str, Any]] = []
    for row in products:
        cost = _number(_first(row, "cost", "ad_spend_kzt", "spend")) or 0.0
        views = _number(_first(row, "views")) or 0.0
        orders = _number(_first(row, "orders_total", "transactions", "orders")) or 0.0
        product_name = _product_name(row)
        bonus_paid = _matching_bonus(product_name, bonus_by_title)
        combined = float(cost + bonus_paid)
        score = _score(_ad_score(row))
        analysis_rows.append(
            {
                "campaign_id": _campaign_id(row),
                "campaign_name": _campaign_name(row),
                "product_name": product_name,
                "configured_bid_kzt": _number(_product_bid(row)),
                "api_current_bid_kzt": _number(_product_bid(row)),
                "effective_bid_kzt_from_event_ledger": "",
                "ad_score": score,
                "score_band": _score_band(score),
                "views": int(views),
                "ad_spend_kzt": float(cost),
                "seller_bonus_paid_kzt": float(bonus_paid),
                "combined_paid_growth_cost_kzt": float(combined),
                "orders": int(orders),
                "revenue_kzt": _number(_first(row, "gmv", "revenue_kzt")) or 0.0,
                "views_per_kzt": round(views / combined, 4) if combined else "",
                "spend_per_order_kzt": round(combined / orders, 2) if orders else "",
            }
        )
    gate = "GREEN" if analysis_rows else "YELLOW"
    summary = {
        "schema_version": LINE31_SCHEMA_VERSION,
        "status": "success" if gate == "GREEN" else "warning",
        "gate": gate,
        "generated_at_local": datetime.now().isoformat(timespec="seconds"),
        "run_dir": str(run_dir),
        "campaign_csv": str(campaign_csv),
        "product_csv": str(product_csv),
        "seller_bonus_csv": str(seller_bonus_csv or ""),
        "analysis_row_count": len(analysis_rows),
        "analysis_rows": analysis_rows,
        "live_writes_executed": False,
    }
    report_path = emit_report or (run_dir / "LINE31_ANALYSIS_REPORT.md")
    summary["report_path"] = str(report_path)
    _write_csv(
        run_dir / "line31_analysis_rows.csv",
        analysis_rows,
        [
            "campaign_id",
            "campaign_name",
            "product_name",
            "configured_bid_kzt",
            "api_current_bid_kzt",
            "effective_bid_kzt_from_event_ledger",
            "ad_score",
            "score_band",
            "views",
            "ad_spend_kzt",
            "seller_bonus_paid_kzt",
            "combined_paid_growth_cost_kzt",
            "orders",
            "revenue_kzt",
            "views_per_kzt",
            "spend_per_order_kzt",
        ],
    )
    _write_json(run_dir / "LINE31_ANALYSIS_SUMMARY.json", summary)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(_render_analysis_report(summary), encoding="utf-8")
    return summary


def _render_recommendation_packet(summary: Mapping[str, Any]) -> str:
    lines = [
        "# LINE31 Marketing Recommendation Packet",
        "",
        f"Gate: {summary['gate']}",
        "",
        "OWNER_APPROVAL_REQUIRED_FOR_ANY_WRITE",
        "",
        f"- live_writes_executed: `{summary['live_writes_executed']}`",
        f"- observed_snapshot_count: `{summary['observed_snapshot_count']}`",
        f"- min_snapshots: `{summary['min_snapshots']}`",
        "",
        "| campaign_id | campaign | recommendation | reason |",
        "| --- | --- | --- | --- |",
    ]
    for row in summary.get("recommendations") or []:
        lines.append(f"| {row['campaign_id']} | {row['campaign_name']} | {row['recommendation']} | {row['reason']} |")
    lines.append("")
    return "\n".join(lines)


def run_line31_recommend(
    *,
    analysis_json: Path,
    min_snapshots: int = 4,
    observed_snapshot_count: int = 0,
    run_root: Path = DEFAULT_RECOMMEND_RUN_ROOT,
    timestamp: str | None = None,
) -> dict[str, Any]:
    if not analysis_json.exists():
        raise LINE31MarketingError(f"analysis_json_not_found:{analysis_json}")
    data = json.loads(analysis_json.read_text(encoding="utf-8"))
    rows = data.get("analysis_rows") if isinstance(data, dict) else []
    run_dir = run_root / f"{timestamp or _timestamp()}_line31_recommend"
    run_dir.mkdir(parents=True, exist_ok=False)
    recommendations: list[dict[str, Any]] = []
    enough = int(observed_snapshot_count or 0) >= int(min_snapshots)
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        if not enough:
            rec = "NEEDS_MORE_DATA"
            reason = f"observed snapshots {observed_snapshot_count} < required {min_snapshots}"
        elif str(row.get("score_band") or "") == "low" and (_number(row.get("spend_per_order_kzt")) or 0) >= 5000:
            rec = "REDUCE_BID_OR_PAUSE_CANDIDATE"
            reason = "low score with high combined spend per order"
        elif str(row.get("score_band") or "") == "high" and (_number(row.get("views_per_kzt")) or 0) >= 5:
            rec = "HOLD_OR_SCALE_CANDIDATE"
            reason = "high score with efficient observed views per KZT"
        else:
            rec = "HOLD"
            reason = "no strong rule fired"
        recommendations.append(
            {
                "campaign_id": str(row.get("campaign_id") or ""),
                "campaign_name": str(row.get("campaign_name") or ""),
                "recommendation": rec,
                "reason": reason,
            }
        )
    gate = "GREEN" if enough else "YELLOW"
    summary = {
        "schema_version": LINE31_SCHEMA_VERSION,
        "status": "success" if gate == "GREEN" else "warning",
        "gate": gate,
        "run_dir": str(run_dir),
        "analysis_json": str(analysis_json),
        "min_snapshots": int(min_snapshots),
        "observed_snapshot_count": int(observed_snapshot_count or 0),
        "recommendations": recommendations,
        "live_writes_executed": False,
    }
    packet_path = run_dir / "LINE31_RECOMMENDATION_PACKET.md"
    summary["packet_path"] = str(packet_path)
    _write_json(run_dir / "LINE31_RECOMMENDATION_SUMMARY.json", summary)
    packet_path.write_text(_render_recommendation_packet(summary), encoding="utf-8")
    return summary
