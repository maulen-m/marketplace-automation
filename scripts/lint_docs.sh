#!/usr/bin/env bash
set -euo pipefail

ROOT="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
cd "$ROOT"

files=()
if (($# > 0)); then
  for path in "$@"; do
    case "$path" in
      *.md|*.markdown) files+=("$path") ;;
    esac
  done
else
  while IFS= read -r path; do
    files+=("$path")
  done < <(
    {
      git diff --name-only --diff-filter=ACMR HEAD -- '*.md' '*.markdown' 2>/dev/null || true
      git ls-files --others --exclude-standard -- '*.md' '*.markdown' 2>/dev/null || true
    } | awk '
      /(^|\/)(node_modules|\.venv|venv|runs|exports|data)\// { next }
      /^(Docs|docs|AGENTS\.md|README(\.md)?|CHANGELOG\.md|execution_plan\.md|\.claude\/)/ { print }
    ' | sort -u
  )
fi

if ((${#files[@]} == 0)); then
  echo "lint_docs: no Markdown files to check"
  exit 0
fi

python3 - "$ROOT" "${files[@]}" <<'PY'
from __future__ import annotations

import re
import sys
from pathlib import Path
from urllib.parse import unquote

root = Path(sys.argv[1])
paths = [Path(p) for p in sys.argv[2:]]
errors: list[str] = []
link_re = re.compile(r"(?<!!)\[[^\]]+\]\(([^)]+)\)")


def is_external_link(target: str) -> bool:
    lowered = target.lower()
    return lowered.startswith(
        (
            "http://",
            "https://",
            "mailto:",
            "tel:",
            "app://",
            "file:",
            "#",
        )
    )


for raw_path in paths:
    path = raw_path if raw_path.is_absolute() else root / raw_path
    display = raw_path.as_posix()
    if not path.exists():
        errors.append(f"{display}: file does not exist")
        continue
    if not path.is_file():
        errors.append(f"{display}: not a regular file")
        continue
    data = path.read_bytes()
    if b"\0" in data:
        errors.append(f"{display}: contains NUL byte")
    if data and not data.endswith(b"\n"):
        errors.append(f"{display}: missing final newline")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        errors.append(f"{display}: not valid UTF-8: {exc}")
        continue
    for line_no, line in enumerate(text.splitlines(), start=1):
        if line.rstrip(" \t") != line:
            errors.append(f"{display}:{line_no}: trailing whitespace")
    for match in link_re.finditer(text):
        target = match.group(1).strip()
        if not target or is_external_link(target):
            continue
        if target.startswith("<") and target.endswith(">"):
            target = target[1:-1]
        target = target.split(" ", 1)[0]
        target_path = unquote(target.split("#", 1)[0])
        if not target_path or target_path.startswith("/"):
            continue
        resolved = (path.parent / target_path).resolve()
        if not resolved.exists():
            line_no = text.count("\n", 0, match.start()) + 1
            errors.append(f"{display}:{line_no}: missing local link target {target}")

if errors:
    print("lint_docs: FAIL")
    for error in errors:
        print(error)
    raise SystemExit(1)

print(f"lint_docs: OK ({len(paths)} Markdown file(s))")
PY

if [[ "${LINT_DOCS_MARKDOWNLINT:-0}" == "1" ]]; then
  if command -v markdownlint >/dev/null 2>&1; then
    markdownlint "${files[@]}"
  else
    echo "lint_docs: markdownlint requested but not installed; skipped optional style pass"
  fi
fi
