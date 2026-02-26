from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import urllib.error
import urllib.parse
import urllib.request
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo


DEFAULT_ASTANA_CITY_ID = "710000000"
DEFAULT_STORE_IDS = (30000001, 30000002)
DEFAULT_LIMIT = 50
DEFAULT_MAX_PAGES = 20
ASTANA_TZ = ZoneInfo("Asia/Almaty")

_KASPI_OFFER_CODE_RE = re.compile(r"-(\d{6,})(?:[/?#]|$)")
_DIGIT_SPACE_RE = re.compile(r"(?<=\d)[\s\u00A0](?=\d)")
_PRICE_NUMBER_RE = re.compile(r"-?\d+(?:[.,]\d+)?")


@dataclass(frozen=True)
class SnapshotFactRow:
    run_id: str
    scraped_at: str
    city_id: str
    canonical_offer_url: str
    offer_code: str
    store_id: int
    merchant_key: str
    merchant_id: int | None
    merchant_name: str
    competitor_price: float | None
    delivery_iso: str | None
    delivery_days_astana: int | None
    page_index: int


def canonicalize_offer_url(value: Any) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    split = urllib.parse.urlsplit(raw)
    host = (split.netloc or "").lower()
    path = (split.path or "").rstrip("/")
    if not host or not path:
        return ""
    return f"https://{host}{path}".lower()


def extract_offer_code_from_url(url: Any) -> str:
    text = str(url or "").strip()
    if not text:
        return ""
    match = _KASPI_OFFER_CODE_RE.search(text)
    return match.group(1) if match else ""


def build_hourly_run_id(now: datetime | None = None, *, tz_name: str = "Asia/Almaty") -> str:
    dt = now or datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    local = dt.astimezone(ZoneInfo(tz_name)).replace(minute=0, second=0, microsecond=0)
    return local.strftime("%Y%m%d_%H00")


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    text = str(value or "").strip().lower()
    return text in {"1", "true", "yes", "y", "on"}


def collect_offer_universe(rows: Iterable[dict[str, Any]]) -> dict[str, set[int]]:
    out: dict[str, set[int]] = {}
    for row in rows:
        link = canonicalize_offer_url(row.get("link") or row.get("resolved_url") or row.get("url"))
        if not link:
            continue
        try:
            store_id = int(row.get("store_id"))
        except Exception:
            continue
        out.setdefault(link, set()).add(store_id)
    return out


def _parse_price(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if not text:
        return None
    compact = text.replace("₸", "")
    compact = _DIGIT_SPACE_RE.sub("", compact)
    compact = compact.replace("\u00A0", "").replace(" ", "")
    match = _PRICE_NUMBER_RE.search(compact)
    if not match:
        return None
    num = match.group(0)
    if "," in num and "." in num:
        num = num.replace(",", "")
    elif "," in num:
        left, right = num.split(",", 1)
        if len(right) <= 2:
            num = f"{left}.{right}"
        else:
            num = f"{left}{right}"
    try:
        return float(num)
    except ValueError:
        return None


def delivery_days_to_astana(delivery_iso: Any, *, now_dt: datetime | None = None) -> int | None:
    text = str(delivery_iso or "").strip()
    if not text:
        return None
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    current = now_dt or datetime.now(ASTANA_TZ)
    if current.tzinfo is None:
        current = current.replace(tzinfo=ASTANA_TZ)
    delivery_date = dt.astimezone(ASTANA_TZ).date()
    today = current.astimezone(ASTANA_TZ).date()
    return max((delivery_date - today).days, 0)


def _merchant_key(merchant_id: int | None, merchant_name: str) -> str:
    if merchant_id is not None:
        return str(merchant_id)
    name = " ".join(str(merchant_name or "").strip().lower().split())
    return f"name:{name}" if name else "name:unknown"


def parse_offer_page_rows(
    *,
    payload: dict[str, Any],
    run_id: str,
    scraped_at_iso: str,
    city_id: str,
    canonical_offer_url: str,
    offer_code: str,
    store_ids: set[int],
    page_index: int,
) -> list[SnapshotFactRow]:
    offers = payload.get("offers") or []
    if not isinstance(offers, list):
        return []
    scraped_dt = datetime.fromisoformat(scraped_at_iso.replace("Z", "+00:00"))

    rows: list[SnapshotFactRow] = []
    for offer in offers:
        if not isinstance(offer, dict):
            continue
        merchant_raw = offer.get("merchantId")
        merchant_id: int | None
        try:
            merchant_id = int(merchant_raw) if merchant_raw is not None else None
        except Exception:
            merchant_id = None
        merchant_name = str(
            offer.get("merchantName")
            or offer.get("merchant")
            or offer.get("sellerName")
            or offer.get("shopName")
            or ""
        ).strip()
        competitor_price = _parse_price(offer.get("price") or offer.get("priceValue") or offer.get("cost"))
        delivery_iso = str(offer.get("delivery") or offer.get("deliveryDate") or "").strip() or None
        delivery_days = delivery_days_to_astana(delivery_iso, now_dt=scraped_dt)
        key = _merchant_key(merchant_id, merchant_name)

        for store_id in sorted(store_ids):
            rows.append(
                SnapshotFactRow(
                    run_id=run_id,
                    scraped_at=scraped_at_iso,
                    city_id=str(city_id),
                    canonical_offer_url=canonical_offer_url,
                    offer_code=offer_code,
                    store_id=int(store_id),
                    merchant_key=key,
                    merchant_id=merchant_id,
                    merchant_name=merchant_name,
                    competitor_price=competitor_price,
                    delivery_iso=delivery_iso,
                    delivery_days_astana=delivery_days,
                    page_index=page_index,
                )
            )
    return rows


def init_snapshot_db(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS run_manifest (
            run_id TEXT PRIMARY KEY,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            status TEXT NOT NULL,
            city_id TEXT NOT NULL,
            offers_total INTEGER NOT NULL DEFAULT 0,
            offers_succeeded INTEGER NOT NULL DEFAULT 0,
            offers_failed INTEGER NOT NULL DEFAULT 0,
            rows_written INTEGER NOT NULL DEFAULT 0,
            api_requests INTEGER NOT NULL DEFAULT 0,
            errors_json TEXT NOT NULL DEFAULT '[]'
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS dim_offer (
            canonical_offer_url TEXT PRIMARY KEY,
            offer_code TEXT,
            first_seen_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS dim_offer_variant_family (
            run_date TEXT NOT NULL,
            canonical_offer_url TEXT NOT NULL,
            variant_offer_url TEXT NOT NULL,
            product_code TEXT,
            size_label TEXT,
            source_payload_hash TEXT,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (run_date, canonical_offer_url, variant_offer_url, product_code)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS fact_offer_competitor_hourly (
            run_id TEXT NOT NULL,
            scraped_at TEXT NOT NULL,
            city_id TEXT NOT NULL,
            canonical_offer_url TEXT NOT NULL,
            offer_code TEXT,
            store_id INTEGER NOT NULL,
            merchant_key TEXT NOT NULL,
            merchant_id INTEGER,
            merchant_name TEXT,
            competitor_price REAL,
            delivery_iso TEXT,
            delivery_days_astana INTEGER,
            page_index INTEGER NOT NULL,
            PRIMARY KEY (run_id, canonical_offer_url, store_id, merchant_key)
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_fact_scraped_at ON fact_offer_competitor_hourly(scraped_at)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_fact_store_offer ON fact_offer_competitor_hourly(store_id, canonical_offer_url)"
    )


def upsert_competitor_facts(conn: sqlite3.Connection, rows: list[SnapshotFactRow]) -> int:
    if not rows:
        return 0
    payload = [
        (
            r.run_id,
            r.scraped_at,
            r.city_id,
            r.canonical_offer_url,
            r.offer_code,
            r.store_id,
            r.merchant_key,
            r.merchant_id,
            r.merchant_name,
            r.competitor_price,
            r.delivery_iso,
            r.delivery_days_astana,
            r.page_index,
        )
        for r in rows
    ]
    conn.executemany(
        """
        INSERT OR REPLACE INTO fact_offer_competitor_hourly (
            run_id, scraped_at, city_id, canonical_offer_url, offer_code, store_id,
            merchant_key, merchant_id, merchant_name, competitor_price,
            delivery_iso, delivery_days_astana, page_index
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        payload,
    )
    return len(payload)


def _upsert_dim_offer_rows(conn: sqlite3.Connection, offer_universe: dict[str, set[int]], seen_at_iso: str) -> int:
    rows = []
    for canonical_url in sorted(offer_universe.keys()):
        rows.append((canonical_url, extract_offer_code_from_url(canonical_url), seen_at_iso, seen_at_iso))
    if not rows:
        return 0
    conn.executemany(
        """
        INSERT INTO dim_offer (canonical_offer_url, offer_code, first_seen_at, last_seen_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(canonical_offer_url) DO UPDATE SET
            offer_code=excluded.offer_code,
            last_seen_at=excluded.last_seen_at
        """,
        rows,
    )
    return len(rows)


def load_offer_rows_from_sqlite(
    sqlite_path: str | Path,
    *,
    store_ids: Iterable[int] = DEFAULT_STORE_IDS,
    on_sale_only: bool = True,
) -> list[dict[str, Any]]:
    path = Path(sqlite_path)
    if not path.exists():
        return []

    store_ids_list = [int(v) for v in store_ids]
    placeholders = ",".join("?" for _ in store_ids_list) or "NULL"
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        table_ok = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='repricer_items'"
        ).fetchone()
        if not table_ok:
            return []
        sql = (
            "SELECT store_id, link, is_available, active FROM repricer_items "
            f"WHERE store_id IN ({placeholders}) AND link IS NOT NULL AND TRIM(link) <> ''"
        )
        rows = [dict(r) for r in conn.execute(sql, store_ids_list).fetchall()]
    finally:
        conn.close()

    if not on_sale_only:
        return rows

    out: list[dict[str, Any]] = []
    for row in rows:
        if _truthy(row.get("is_available")) or _truthy(row.get("active")):
            out.append(row)
    return out


def _kaspi_offer_headers(*, referer: str) -> dict[str, str]:
    return {
        "content-type": "application/json; charset=UTF-8",
        "origin": "https://kaspi.kz",
        "referer": referer,
        "accept": "application/json, text/plain, */*",
        "user-agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/122.0.0.0 Safari/537.36"
        ),
        "accept-language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
    }


def _post_json(url: str, payload: dict[str, Any], headers: dict[str, str], timeout_seconds: int) -> tuple[int, dict[str, Any]]:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url=url, method="POST", data=body, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout_seconds) as resp:
            status = int(getattr(resp, "status", 200))
            raw = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        status = int(exc.code)
        raw = exc.read().decode("utf-8", errors="replace") if exc.fp else ""
    if status != 200:
        return status, {}
    try:
        data = json.loads(raw)
    except Exception:
        data = {}
    return status, data if isinstance(data, dict) else {}


def fetch_offer_payload_pages(
    *,
    canonical_offer_url: str,
    offer_code: str,
    city_id: str,
    limit: int = DEFAULT_LIMIT,
    max_pages: int = DEFAULT_MAX_PAGES,
    timeout_seconds: int = 30,
) -> tuple[list[tuple[int, dict[str, Any]]], int]:
    referer = f"{canonical_offer_url}/?c={city_id}"
    headers = _kaspi_offer_headers(referer=referer)
    pages: list[tuple[int, dict[str, Any]]] = []
    requests_made = 0

    for page_idx in range(max(1, int(max_pages))):
        payload = {
            "cityId": str(city_id),
            "id": str(offer_code),
            "merchantUID": [],
            "limit": int(limit),
            "page": int(page_idx),
            "sortOption": "PRICE",
        }
        status, data = _post_json(
            f"https://kaspi.kz/yml/offer-view/offers/{offer_code}",
            payload,
            headers,
            timeout_seconds,
        )
        requests_made += 1
        if status != 200 or not data:
            break
        pages.append((page_idx, data))
        offers = data.get("offers") or []
        if not isinstance(offers, list) or len(offers) < int(limit):
            break
    return pages, requests_made


def _write_raw_payloads_parquet(
    *,
    raw_rows: list[dict[str, Any]],
    parquet_root: Path,
    run_dt: datetime,
    run_id: str,
) -> dict[str, Any]:
    partition_dir = parquet_root / f"dt={run_dt.strftime('%Y-%m-%d')}" / f"hour={run_dt.strftime('%H')}"
    partition_dir.mkdir(parents=True, exist_ok=True)
    out_path = partition_dir / f"{run_id}.parquet"

    if out_path.exists():
        out_path.unlink()

    if not raw_rows:
        return {
            "parquet_file": str(out_path),
            "raw_rows": 0,
            "parquet_written": False,
            "parquet_error": "no_rows",
        }

    try:
        import pyarrow as pa
        import pyarrow.parquet as pq
    except Exception:
        return {
            "parquet_file": str(out_path),
            "raw_rows": len(raw_rows),
            "parquet_written": False,
            "parquet_error": "pyarrow_missing",
        }

    table = pa.Table.from_pylist(raw_rows)
    pq.write_table(table, out_path, compression="zstd")
    return {
        "parquet_file": str(out_path),
        "raw_rows": len(raw_rows),
        "parquet_written": True,
        "parquet_error": "",
    }


def prune_snapshot_retention(
    *,
    sqlite_path: str | Path,
    parquet_root: str | Path,
    hot_days: int,
    cold_days: int,
    now_iso: str | None = None,
) -> dict[str, int]:
    now_dt = datetime.fromisoformat(now_iso.replace("Z", "+00:00")) if now_iso else datetime.now(ASTANA_TZ)
    if now_dt.tzinfo is None:
        now_dt = now_dt.replace(tzinfo=ASTANA_TZ)

    hot_cutoff = now_dt - timedelta(days=int(hot_days))
    cold_cutoff_date = (now_dt - timedelta(days=int(cold_days))).date()

    sqlite_rows_deleted = 0
    sqlite_path = Path(sqlite_path)
    if sqlite_path.exists():
        conn = sqlite3.connect(sqlite_path)
        conn.row_factory = sqlite3.Row
        try:
            init_snapshot_db(conn)
            rows = conn.execute("SELECT rowid, scraped_at FROM fact_offer_competitor_hourly").fetchall()
            to_delete: list[int] = []
            for row in rows:
                text = str(row["scraped_at"] or "").strip()
                if not text:
                    continue
                try:
                    row_dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
                except ValueError:
                    continue
                if row_dt.tzinfo is None:
                    row_dt = row_dt.replace(tzinfo=ASTANA_TZ)
                if row_dt < hot_cutoff:
                    to_delete.append(int(row["rowid"]))
            if to_delete:
                conn.executemany("DELETE FROM fact_offer_competitor_hourly WHERE rowid=?", [(rid,) for rid in to_delete])
                sqlite_rows_deleted = len(to_delete)
            conn.commit()
        finally:
            conn.close()

    parquet_dirs_deleted = 0
    parquet_root = Path(parquet_root)
    if parquet_root.exists():
        for dt_dir in parquet_root.glob("dt=*"):
            if not dt_dir.is_dir():
                continue
            raw_date = dt_dir.name.split("=", 1)[-1]
            try:
                date_obj = datetime.strptime(raw_date, "%Y-%m-%d").date()
            except ValueError:
                continue
            if date_obj < cold_cutoff_date:
                shutil.rmtree(dt_dir, ignore_errors=True)
                parquet_dirs_deleted += 1

    return {
        "sqlite_rows_deleted": sqlite_rows_deleted,
        "parquet_dirs_deleted": parquet_dirs_deleted,
    }


@contextmanager
def _single_run_lock(lock_path: Path):
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd: int | None = None
    try:
        fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, str(os.getpid()).encode("utf-8"))
        yield
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        try:
            lock_path.unlink()
        except FileNotFoundError:
            pass


def run_hourly_snapshot(
    *,
    source_items_sqlite: str | Path,
    snapshot_sqlite: str | Path,
    parquet_root: str | Path,
    artifacts_root: str | Path,
    store_ids: Iterable[int] = DEFAULT_STORE_IDS,
    city_id: str = DEFAULT_ASTANA_CITY_ID,
    run_id: str | None = None,
    max_pages: int = DEFAULT_MAX_PAGES,
    limit: int = DEFAULT_LIMIT,
    timeout_seconds: int = 30,
    hot_days: int = 90,
    cold_days: int = 365,
    now_utc: datetime | None = None,
) -> dict[str, Any]:
    run_start = now_utc or datetime.now(timezone.utc)
    if run_start.tzinfo is None:
        run_start = run_start.replace(tzinfo=timezone.utc)
    run_dt = run_start.astimezone(ASTANA_TZ).replace(minute=0, second=0, microsecond=0)
    effective_run_id = run_id or build_hourly_run_id(run_start)

    source_items_sqlite = Path(source_items_sqlite)
    snapshot_sqlite = Path(snapshot_sqlite)
    parquet_root = Path(parquet_root)
    artifacts_dir = Path(artifacts_root) / effective_run_id
    artifacts_dir.mkdir(parents=True, exist_ok=True)

    lock_path = snapshot_sqlite.parent / "hourly_snapshot.lock"

    summary: dict[str, Any] = {
        "run_id": effective_run_id,
        "city_id": str(city_id),
        "source_items_sqlite": str(source_items_sqlite),
        "snapshot_sqlite": str(snapshot_sqlite),
        "parquet_root": str(parquet_root),
        "offers_total": 0,
        "offers_succeeded": 0,
        "offers_failed": 0,
        "rows_written": 0,
        "api_requests": 0,
        "errors": [],
        "status": "running",
        "artifacts_dir": str(artifacts_dir),
    }

    try:
        with _single_run_lock(lock_path):
            source_rows = load_offer_rows_from_sqlite(source_items_sqlite, store_ids=store_ids, on_sale_only=True)
            offer_universe = collect_offer_universe(source_rows)
            summary["offers_total"] = len(offer_universe)

            snapshot_sqlite.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(snapshot_sqlite)
            try:
                init_snapshot_db(conn)
                conn.execute("DELETE FROM fact_offer_competitor_hourly WHERE run_id=?", (effective_run_id,))
                conn.execute(
                    """
                    INSERT INTO run_manifest (
                        run_id, started_at, status, city_id, offers_total, offers_succeeded, offers_failed,
                        rows_written, api_requests, errors_json
                    ) VALUES (?, ?, 'running', ?, ?, 0, 0, 0, 0, '[]')
                    ON CONFLICT(run_id) DO UPDATE SET
                        started_at=excluded.started_at,
                        status='running',
                        city_id=excluded.city_id,
                        offers_total=excluded.offers_total,
                        offers_succeeded=0,
                        offers_failed=0,
                        rows_written=0,
                        api_requests=0,
                        errors_json='[]'
                    """,
                    (effective_run_id, run_start.isoformat(), str(city_id), len(offer_universe)),
                )
                _upsert_dim_offer_rows(conn, offer_universe, run_start.isoformat())

                raw_rows: list[dict[str, Any]] = []
                for canonical_url, attached_store_ids in sorted(offer_universe.items()):
                    offer_code = extract_offer_code_from_url(canonical_url)
                    if not offer_code:
                        summary["offers_failed"] += 1
                        summary["errors"].append({"offer_url": canonical_url, "error": "missing_offer_code"})
                        continue
                    try:
                        pages, req_count = fetch_offer_payload_pages(
                            canonical_offer_url=canonical_url,
                            offer_code=offer_code,
                            city_id=str(city_id),
                            limit=limit,
                            max_pages=max_pages,
                            timeout_seconds=timeout_seconds,
                        )
                        summary["api_requests"] += req_count
                        if not pages:
                            summary["offers_failed"] += 1
                            summary["errors"].append({"offer_url": canonical_url, "error": "no_pages"})
                            continue

                        wrote_for_offer = 0
                        for page_idx, payload in pages:
                            scraped_at = datetime.now(ASTANA_TZ).isoformat()
                            raw_rows.append(
                                {
                                    "run_id": effective_run_id,
                                    "scraped_at": scraped_at,
                                    "city_id": str(city_id),
                                    "canonical_offer_url": canonical_url,
                                    "offer_code": offer_code,
                                    "page_index": int(page_idx),
                                    "store_ids_csv": ",".join(str(v) for v in sorted(attached_store_ids)),
                                    "payload_json": json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                                }
                            )
                            fact_rows = parse_offer_page_rows(
                                payload=payload,
                                run_id=effective_run_id,
                                scraped_at_iso=scraped_at,
                                city_id=str(city_id),
                                canonical_offer_url=canonical_url,
                                offer_code=offer_code,
                                store_ids=set(attached_store_ids),
                                page_index=page_idx,
                            )
                            wrote_for_offer += upsert_competitor_facts(conn, fact_rows)

                        summary["rows_written"] += wrote_for_offer
                        summary["offers_succeeded"] += 1
                    except Exception as exc:
                        summary["offers_failed"] += 1
                        summary["errors"].append({"offer_url": canonical_url, "error": f"{type(exc).__name__}: {exc}"})

                parquet_summary = _write_raw_payloads_parquet(
                    raw_rows=raw_rows,
                    parquet_root=parquet_root,
                    run_dt=run_dt,
                    run_id=effective_run_id,
                )
                summary.update(parquet_summary)

                retention_summary = prune_snapshot_retention(
                    sqlite_path=snapshot_sqlite,
                    parquet_root=parquet_root,
                    hot_days=hot_days,
                    cold_days=cold_days,
                    now_iso=datetime.now(ASTANA_TZ).isoformat(),
                )
                summary.update(retention_summary)

                status = "success" if summary["offers_failed"] == 0 else "partial"
                summary["status"] = status
                conn.execute(
                    """
                    UPDATE run_manifest
                    SET finished_at=?, status=?, offers_succeeded=?, offers_failed=?, rows_written=?, api_requests=?, errors_json=?
                    WHERE run_id=?
                    """,
                    (
                        datetime.now(ASTANA_TZ).isoformat(),
                        status,
                        int(summary["offers_succeeded"]),
                        int(summary["offers_failed"]),
                        int(summary["rows_written"]),
                        int(summary["api_requests"]),
                        json.dumps(summary["errors"], ensure_ascii=False),
                        effective_run_id,
                    ),
                )
                conn.commit()
            finally:
                conn.close()
    except (RuntimeError, FileExistsError):
        summary["status"] = "lock_held"
        summary["errors"].append({"error": "lock_held"})

    (artifacts_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    if summary["errors"]:
        with (artifacts_dir / "errors.jsonl").open("w", encoding="utf-8") as fh:
            for item in summary["errors"]:
                fh.write(json.dumps(item, ensure_ascii=False) + "\n")
    return summary
