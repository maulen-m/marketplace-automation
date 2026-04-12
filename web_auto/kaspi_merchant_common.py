from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Iterable

from playwright.sync_api import Page


LOGIN_URL = "https://idmc.shop.kaspi.kz/login"


STORE_CREDENTIAL_KEYS = {
    "UNIVERSAL": [("UNIVERSAL_KASPI_ACCOUNT_EMAIL", "UNIVERSAL_KASPI_ACCOUNT_PASSWORD")],
    "ACMEWEAR": [("ACMEWEAR_ACCOUNT_EMAIL", "ACMEWEAR_ACCOUNT_PASSWORD")],
    "STORE-B": [("STOREB_ACCOUNT_EMAIL", "STOREB_ACCOUNT_PASSWORD")],
}


def normalize_store_name(value: object) -> str:
    raw = str(value or "").strip().upper().replace("_", "-")
    raw = raw.replace(" ", "")
    if raw == "STOREB":
        return "STORE-B"
    return raw


def load_env_assignments(path: Path, keys: Iterable[str]) -> dict[str, str]:
    wanted = set(keys)
    out: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key not in wanted:
            continue
        out[key] = value.strip().strip('"').strip("'")
    return out


def resolve_store_credentials(store_name: str, env_file: Path) -> dict[str, str]:
    store = normalize_store_name(store_name)
    key_pairs = STORE_CREDENTIAL_KEYS.get(store)
    if not key_pairs:
        raise ValueError(f"unsupported store for merchant login: {store_name}")
    needed = [item for pair in key_pairs for item in pair]
    assignments = load_env_assignments(env_file, needed)
    for email_key, password_key in key_pairs:
        email = assignments.get(email_key, "").strip()
        password = assignments.get(password_key, "").strip()
        if email and password:
            return {
                "store_name": store,
                "email_key": email_key,
                "password_key": password_key,
                "email": email,
                "password": password,
            }
    raise ValueError(f"missing merchant credentials for {store}")


def merchant_browser_launch_kwargs(*, headless: bool) -> dict[str, Any]:
    return {
        "channel": "chrome",
        "headless": bool(headless),
        "slow_mo": 150,
    }


def login_kaspi_merchant(page: Page, *, email: str, password: str) -> None:
    page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=60000)
    page.get_by_text("Email", exact=True).click()
    page.locator('input[placeholder="Email"]').fill(email)
    page.get_by_text("Продолжить", exact=True).click()
    page.locator('input[type="password"]').fill(password)
    page.get_by_text("Продолжить", exact=True).click()
    page.wait_for_url(re.compile(r"https://kaspi\.kz/mc/#/?"), timeout=60000)
    page.wait_for_timeout(3000)
