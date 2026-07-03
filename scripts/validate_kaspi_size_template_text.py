#!/usr/bin/env python3
"""Validate the approved Kaspi size-request template text."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from dataclasses import dataclass


APPROVED_TEMPLATE = (
    "Добрый день, пожалуйста подскажите примерный Рост и Вес, подберем точный размер😊"
)
APPROVED_TEMPLATE_SHA256 = (
    "c68968319ac3d3d245df3d74722dad5b788c0cec5963c609fc362f723d069884"
)
APPROVED_TEMPLATE_CHAR_LENGTH = 80
APPROVED_TEMPLATE_UTF8_BYTES = 150

MOJIBAKE_OR_AWKWARD_MARKERS = (
    "\ufffd",
    "Ð",
    "Ñ",
    "Р’",
    "Р°",
    "Рµ",
    "????",
    "\\u",
    "&#",
    "&nbsp;",
)


@dataclass(frozen=True)
class TemplateValidation:
    ok: bool
    sha256: str
    char_length: int
    utf8_bytes: int
    errors: tuple[str, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "ok": self.ok,
            "sha256": self.sha256,
            "char_length": self.char_length,
            "utf8_bytes": self.utf8_bytes,
            "errors": list(self.errors),
        }


def template_sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def validate_template_text(text: str) -> TemplateValidation:
    errors: list[str] = []
    digest = template_sha256(text)
    char_length = len(text)
    utf8_bytes = len(text.encode("utf-8"))

    if text != APPROVED_TEMPLATE:
        errors.append("template_text_not_exact")
    if digest != APPROVED_TEMPLATE_SHA256:
        errors.append("template_sha256_mismatch")
    if char_length != APPROVED_TEMPLATE_CHAR_LENGTH:
        errors.append("template_char_length_mismatch")
    if utf8_bytes != APPROVED_TEMPLATE_UTF8_BYTES:
        errors.append("template_utf8_byte_length_mismatch")
    if any(ch in text for ch in ("\n", "\r", "\t")):
        errors.append("template_contains_control_whitespace")
    if "  " in text:
        errors.append("template_contains_double_space")
    for marker in MOJIBAKE_OR_AWKWARD_MARKERS:
        if marker in text:
            errors.append(f"template_contains_awkward_marker:{marker}")

    return TemplateValidation(
        ok=not errors,
        sha256=digest,
        char_length=char_length,
        utf8_bytes=utf8_bytes,
        errors=tuple(errors),
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--stdin", action="store_true", help="Read draft text from stdin.")
    source.add_argument("--text", help="Validate this exact draft text.")
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print JSON output instead of a compact key=value summary.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    text = sys.stdin.read() if args.stdin else args.text
    if text is None:
        raise RuntimeError("No text provided")
    result = validate_template_text(text)
    if args.json:
        print(json.dumps(result.as_dict(), ensure_ascii=False, sort_keys=True))
    else:
        print(f"ok={result.ok}")
        print(f"sha256={result.sha256}")
        print(f"char_length={result.char_length}")
        print(f"utf8_bytes={result.utf8_bytes}")
        if result.errors:
            print(f"errors={','.join(result.errors)}")
    return 0 if result.ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
