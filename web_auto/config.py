from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

import yaml


@dataclass
class Account:
    name: str
    base_url: str
    token_env: str
    stores: list[int]


@dataclass
class RunConfig:
    dry_run: bool = False
    require_confirm: bool = True
    max_retries: int = 3
    modal_timeout_ms: int = 8000
    page_timeout_ms: int = 240000
    slowmo_ms: int = 150
    headless: bool = False
    browser_profile_dir: str | None = None
    storage_state_path: str | None = None
    upload_after: bool = False
    checkpoint_path: str = "data/checkpoints/repricer_competitors.json"
    artifacts_dir: str = "runs/repricer_competitors"


@dataclass
class TaskConfig:
    task_id: str
    accounts: list[Account]
    targets: list[str]
    contains_targets: list[str]
    run: RunConfig


@dataclass
class DumpingConfig:
    task_id: str
    accounts: list[Account]
    run: RunConfig


@dataclass
class MinPriceSyncConfig:
    task_id: str
    accounts: list[Account]
    source_account: str
    source_store_id: int
    fallback_source_account: str | None
    fallback_source_store_id: int | None
    target_account: str
    target_store_ids: list[int]
    run: RunConfig


class ConfigError(ValueError):
    pass


def _require(value: Any, message: str) -> Any:
    if value is None:
        raise ConfigError(message)
    return value


def _as_list(value: Any, message: str) -> list[Any]:
    if value is None:
        raise ConfigError(message)
    if not isinstance(value, list):
        raise ConfigError(message)
    return value


def _load_accounts(raw: dict[str, Any]) -> list[Account]:
    accounts_raw = _as_list(raw.get("accounts"), "accounts must be a list")
    accounts: list[Account] = []
    for item in accounts_raw:
        if not isinstance(item, dict):
            raise ConfigError("each account must be a mapping")
        name = _require(item.get("name"), "account.name is required")
        base_url = _require(item.get("base_url"), f"account.base_url is required for {name}")
        token_env = _require(item.get("token_env"), f"account.token_env is required for {name}")
        stores = _as_list(item.get("stores"), f"account.stores must be a list for {name}")
        stores_int = [int(s) for s in stores]
        accounts.append(Account(name=name, base_url=base_url, token_env=token_env, stores=stores_int))
    return accounts


def _load_run(raw: dict[str, Any]) -> RunConfig:
    run_raw = raw.get("run", {}) or {}
    if not isinstance(run_raw, dict):
        raise ConfigError("run must be a mapping")
    return RunConfig(
        dry_run=bool(run_raw.get("dry_run", False)),
        require_confirm=bool(run_raw.get("require_confirm", True)),
        max_retries=int(run_raw.get("max_retries", 3)),
        modal_timeout_ms=int(run_raw.get("modal_timeout_ms", 8000)),
        page_timeout_ms=int(run_raw.get("page_timeout_ms", 240000)),
        slowmo_ms=int(run_raw.get("slowmo_ms", 150)),
        headless=bool(run_raw.get("headless", False)),
        browser_profile_dir=run_raw.get("browser_profile_dir"),
        storage_state_path=run_raw.get("storage_state_path"),
        upload_after=bool(run_raw.get("upload_after", False)),
        checkpoint_path=str(run_raw.get("checkpoint_path", "data/checkpoints/repricer_competitors.json")),
        artifacts_dir=str(run_raw.get("artifacts_dir", "runs/repricer_competitors")),
    )


def load_config(path: str | Path) -> TaskConfig:
    path = Path(path)
    if not path.exists():
        raise ConfigError(f"Config file not found: {path}")
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ConfigError("Config must be a YAML mapping")

    task_id = raw.get("task_id", "repricer_competitors")

    accounts = _load_accounts(raw)

    targets_raw = raw.get("targets", {})
    if not isinstance(targets_raw, dict):
        raise ConfigError("targets must be a mapping")
    competitor_names = _as_list(targets_raw.get("competitor_names"), "targets.competitor_names must be a list")
    contains_names = targets_raw.get("contains_names", []) or []
    if not isinstance(contains_names, list):
        raise ConfigError("targets.contains_names must be a list if provided")

    run = _load_run(raw)

    return TaskConfig(
        task_id=task_id,
        accounts=accounts,
        targets=[str(v) for v in competitor_names],
        contains_targets=[str(v) for v in contains_names],
        run=run,
    )


def load_dumping_config(path: str | Path) -> DumpingConfig:
    path = Path(path)
    if not path.exists():
        raise ConfigError(f"Config file not found: {path}")
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ConfigError("Config must be a YAML mapping")

    task_id = raw.get("task_id", "repricer_dumping_enable")
    accounts = _load_accounts(raw)
    run = _load_run(raw)

    return DumpingConfig(
        task_id=task_id,
        accounts=accounts,
        run=run,
    )


def load_min_price_sync_config(path: str | Path) -> MinPriceSyncConfig:
    path = Path(path)
    if not path.exists():
        raise ConfigError(f"Config file not found: {path}")
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ConfigError("Config must be a YAML mapping")

    task_id = raw.get("task_id", "repricer_min_price_sync")
    accounts = _load_accounts(raw)
    run = _load_run(raw)

    source_raw = raw.get("source") or {}
    if not isinstance(source_raw, dict):
        raise ConfigError("source must be a mapping")
    source_account = _require(source_raw.get("account"), "source.account is required")
    source_store_id = int(_require(source_raw.get("store_id"), "source.store_id is required"))

    target_raw = raw.get("target") or {}
    if not isinstance(target_raw, dict):
        raise ConfigError("target must be a mapping")
    target_account = _require(target_raw.get("account"), "target.account is required")
    target_stores = _as_list(target_raw.get("stores"), "target.stores must be a list")
    target_store_ids = [int(s) for s in target_stores]

    return MinPriceSyncConfig(
        task_id=task_id,
        accounts=accounts,
        source_account=str(source_account),
        source_store_id=source_store_id,
        fallback_source_account=str(source_raw.get("fallback_account"))
        if source_raw.get("fallback_account")
        else None,
        fallback_source_store_id=int(source_raw.get("fallback_store_id"))
        if source_raw.get("fallback_store_id") is not None
        else None,
        target_account=str(target_account),
        target_store_ids=target_store_ids,
        run=run,
    )


def detect_task_id(path: str | Path) -> str | None:
    path = Path(path)
    if not path.exists():
        return None
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        return None
    task_id = raw.get("task_id")
    if task_id is None:
        return None
    return str(task_id)
