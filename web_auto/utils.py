import os
import re
from typing import Iterable, Set


_WHITESPACE_RE = re.compile(r"\s+")


def normalize_name(value: str) -> str:
    if value is None:
        return ""
    value = value.strip().casefold()
    value = _WHITESPACE_RE.sub(" ", value)
    return value


def build_target_sets(names: Iterable[str], contains: Iterable[str] | None = None) -> tuple[Set[str], list[str]]:
    exact = {normalize_name(n) for n in names if n}
    contains_list = [normalize_name(n) for n in (contains or []) if n]
    return exact, contains_list


def ensure_env(var_name: str) -> str:
    value = os.environ.get(var_name)
    if not value:
        raise RuntimeError(f"Missing required environment variable: {var_name}")
    return value
