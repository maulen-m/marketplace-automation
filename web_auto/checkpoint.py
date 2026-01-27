from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict


@dataclass
class Progress:
    page_index: int
    row_index: int


@dataclass
class Checkpoint:
    task_id: str
    progress: Dict[str, Progress]


def load_checkpoint(path: str | Path, task_id: str) -> Checkpoint:
    path = Path(path)
    if not path.exists():
        return Checkpoint(task_id=task_id, progress={})
    data = json.loads(path.read_text(encoding="utf-8"))
    progress: Dict[str, Progress] = {}
    for key, value in (data.get("progress") or {}).items():
        try:
            progress[key] = Progress(page_index=int(value["page_index"]), row_index=int(value["row_index"]))
        except Exception:
            continue
    return Checkpoint(task_id=data.get("task_id", task_id), progress=progress)


def save_checkpoint(path: str | Path, checkpoint: Checkpoint) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "task_id": checkpoint.task_id,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "progress": {k: {"page_index": v.page_index, "row_index": v.row_index} for k, v in checkpoint.progress.items()},
    }
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def progress_key(account_name: str, store_id: int) -> str:
    return f"{account_name}|{store_id}"
