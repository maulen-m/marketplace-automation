from __future__ import annotations

import json
import os
import shlex
import subprocess
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo

import yaml


DEFAULT_CONFIG_PATH = Path("config/schedules/line61_line51_checkpoints.yaml")
DEFAULT_RUN_ROOT = Path("runs/scheduled_checkpoints")
SCHEMA_VERSION = "web_auto.scheduled_checkpoints.v1"

SAFE_MODES = {"plan", "dry-live", "live-readonly"}
SUCCESS_STATUSES = {"success", "ok", "dry_run", "planned", "no_active_events", "nothing_to_do"}
WARNING_STATUSES = {"stale", "partial", "partial_success", "completed_with_warnings"}
PRODUCTION_WRITE_TOKENS = {
    "--confirm",
    "--upload",
    "--upload-after",
    "--verify-after-upload",
    "--dispatch",
    "--confirm-production-write",
}
PRODUCTION_WRITE_WORDS = {
    "upload",
    "close-experiment",
}


class ScheduledCheckpointError(ValueError):
    pass


@dataclass(frozen=True)
class CommandResult:
    command_id: str
    status: str
    gate: str
    argv: list[str]
    returncode: int | None = None
    stdout_path: str = ""
    stderr_path: str = ""
    parsed_status: str = ""
    parsed_gate: str = ""
    parsed_decision: str = ""
    error: str = ""


def repo_root_from(path: Path | None = None) -> Path:
    start = (path or Path.cwd()).resolve()
    for candidate in [start, *start.parents]:
        if (candidate / "AGENTS.md").exists() and (candidate / "web_auto").exists():
            return candidate
    raise ScheduledCheckpointError(f"could not locate Web_automation repo from {start}")


def _safe_slug(value: str) -> str:
    out = []
    for ch in str(value or "").strip().lower():
        if ch.isalnum():
            out.append(ch)
        elif ch in {"-", "_", "."}:
            out.append("_")
    return "".join(out).strip("_") or "checkpoint"


def _load_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise ScheduledCheckpointError(f"config not found: {path}")
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ScheduledCheckpointError("schedule config must be a mapping")
    return raw


def _parse_dt(value: str, timezone_name: str) -> datetime:
    text = str(value or "").strip().replace("T", " ")
    if not text:
        raise ScheduledCheckpointError("scheduled_at is required")
    if len(text) == 10:
        text = f"{text} 00:00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=ZoneInfo(timezone_name))
    return parsed


def _now(timezone_name: str) -> datetime:
    return datetime.now(ZoneInfo(timezone_name)).replace(microsecond=0)


def _as_argv(value: Any, *, field_name: str) -> list[str]:
    if isinstance(value, str):
        return shlex.split(value)
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return list(value)
    raise ScheduledCheckpointError(f"{field_name} must be a shell string or list of strings")


def _looks_like_production_write(argv: Iterable[str]) -> list[str]:
    findings: list[str] = []
    tokens = [str(item) for item in argv]
    for token in tokens:
        if token in PRODUCTION_WRITE_TOKENS:
            findings.append(token)
    for idx, token in enumerate(tokens):
        lowered = token.strip().lower()
        previous = tokens[idx - 1].strip().lower() if idx > 0 else ""
        if lowered in PRODUCTION_WRITE_WORDS and previous in {"kaspi-pricelist", "offer-run"}:
            findings.append(f"{previous} {lowered}")
    return findings


def _validate_command(job_id: str, command: dict[str, Any]) -> None:
    command_id = str(command.get("id") or "").strip()
    if not command_id:
        raise ScheduledCheckpointError(f"job {job_id}: command id is required")
    argv = _as_argv(command.get("argv") or [], field_name=f"{job_id}.{command_id}.argv")
    if not argv:
        raise ScheduledCheckpointError(f"job {job_id}: command {command_id} argv is empty")
    dry_live_argv = command.get("dry_live_argv")
    if dry_live_argv is not None:
        _as_argv(dry_live_argv, field_name=f"{job_id}.{command_id}.dry_live_argv")
    if not bool(command.get("read_only", False)):
        raise ScheduledCheckpointError(f"job {job_id}: command {command_id} must declare read_only: true")
    for label, raw_argv in (("argv", argv), ("dry_live_argv", dry_live_argv)):
        if raw_argv is None:
            continue
        actual_argv = _as_argv(raw_argv, field_name=f"{job_id}.{command_id}.{label}")
        findings = _looks_like_production_write(actual_argv)
        if findings:
            joined = ", ".join(findings)
            raise ScheduledCheckpointError(f"job {job_id}: command {command_id} has forbidden write token(s): {joined}")


def load_schedule_config(path: str | Path = DEFAULT_CONFIG_PATH) -> dict[str, Any]:
    config_path = Path(path)
    raw = _load_yaml(config_path)
    schema = str(raw.get("schema_version") or "").strip()
    if schema != SCHEMA_VERSION:
        raise ScheduledCheckpointError(f"unsupported schema_version: {schema or '<missing>'}")
    timezone_name = str(raw.get("timezone") or "Asia/Almaty").strip()
    try:
        ZoneInfo(timezone_name)
    except Exception as exc:
        raise ScheduledCheckpointError(f"invalid timezone: {timezone_name}") from exc
    jobs = raw.get("jobs")
    if not isinstance(jobs, list) or not jobs:
        raise ScheduledCheckpointError("jobs must be a non-empty list")
    seen_jobs: set[str] = set()
    for job in jobs:
        if not isinstance(job, dict):
            raise ScheduledCheckpointError("each job must be a mapping")
        job_id = str(job.get("job_id") or "").strip()
        if not job_id:
            raise ScheduledCheckpointError("job_id is required")
        if job_id in seen_jobs:
            raise ScheduledCheckpointError(f"duplicate job_id: {job_id}")
        seen_jobs.add(job_id)
        _parse_dt(str(job.get("scheduled_at") or ""), timezone_name)
        if not bool(job.get("owner_approval_required_for_live_writes", True)):
            raise ScheduledCheckpointError(f"job {job_id}: live writes must require owner approval")
        commands = job.get("commands")
        if not isinstance(commands, list) or not commands:
            raise ScheduledCheckpointError(f"job {job_id}: commands must be a non-empty list")
        seen_commands: set[str] = set()
        for command in commands:
            if not isinstance(command, dict):
                raise ScheduledCheckpointError(f"job {job_id}: each command must be a mapping")
            command_id = str(command.get("id") or "").strip()
            if command_id in seen_commands:
                raise ScheduledCheckpointError(f"job {job_id}: duplicate command id: {command_id}")
            seen_commands.add(command_id)
            _validate_command(job_id, command)
    return raw


def _state_path(run_root: Path) -> Path:
    return run_root / "state" / "completed_jobs.json"


def load_completed_jobs(run_root: Path) -> dict[str, Any]:
    path = _state_path(run_root)
    if not path.exists():
        return {"completed_jobs": {}}
    return json.loads(path.read_text(encoding="utf-8"))


def _business_gate_summary(summary: dict[str, Any]) -> dict[str, Any]:
    parsed_gates: list[str] = []
    parsed_decisions: list[str] = []
    for command in summary.get("commands") or []:
        if not isinstance(command, dict):
            continue
        parsed_gate = str(command.get("parsed_gate") or "").strip().upper()
        parsed_decision = str(command.get("parsed_decision") or "").strip()
        if parsed_gate:
            parsed_gates.append(parsed_gate)
        if parsed_decision:
            parsed_decisions.append(parsed_decision)
    gate_rank = {"RED": 3, "YELLOW": 2, "GREEN": 1}
    business_gate = ""
    if parsed_gates:
        business_gate = max(parsed_gates, key=lambda item: gate_rank.get(item, 0))
    return {
        "business_gate": business_gate,
        "business_gates": parsed_gates,
        "business_decisions": parsed_decisions,
    }


def save_completed_job(run_root: Path, job_id: str, summary: dict[str, Any]) -> None:
    path = _state_path(run_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    state = load_completed_jobs(run_root)
    completed = dict(state.get("completed_jobs") or {})
    business_summary = _business_gate_summary(summary)
    completed[job_id] = {
        "completed_at": summary.get("generated_at"),
        "run_dir": summary.get("run_dir"),
        "gate": summary.get("gate"),
        "mode": summary.get("mode"),
        **business_summary,
    }
    path.write_text(json.dumps({"completed_jobs": completed}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def due_jobs(
    config: dict[str, Any],
    *,
    now: datetime | None = None,
    run_root: Path | None = None,
    force: bool = False,
) -> list[dict[str, Any]]:
    timezone_name = str(config.get("timezone") or "Asia/Almaty")
    now_dt = now or _now(timezone_name)
    root = Path(run_root or config.get("run_root") or DEFAULT_RUN_ROOT)
    completed = set((load_completed_jobs(root).get("completed_jobs") or {}).keys())
    selected: list[dict[str, Any]] = []
    for job in config.get("jobs") or []:
        job_id = str(job.get("job_id") or "")
        scheduled_at = _parse_dt(str(job.get("scheduled_at") or ""), timezone_name)
        max_lag = int(job.get("max_lag_minutes", 2880))
        if not force and job_id in completed:
            continue
        if scheduled_at <= now_dt <= scheduled_at + timedelta(minutes=max_lag):
            selected.append(job)
    return selected


def get_job(config: dict[str, Any], job_id: str) -> dict[str, Any]:
    for job in config.get("jobs") or []:
        if str(job.get("job_id") or "") == job_id:
            return job
    raise ScheduledCheckpointError(f"job not found: {job_id}")


def _json_from_stdout(text: str) -> dict[str, Any] | None:
    stripped = text.strip()
    if not stripped:
        return None
    try:
        return json.loads(stripped)
    except Exception:
        pass
    start = stripped.find("{")
    end = stripped.rfind("}")
    if start >= 0 and end > start:
        try:
            return json.loads(stripped[start : end + 1])
        except Exception:
            return None
    return None


def _select_argv(command: dict[str, Any], mode: str) -> tuple[list[str], str]:
    if mode == "dry-live" and command.get("dry_live_argv") is not None:
        return _as_argv(command["dry_live_argv"], field_name=f"{command.get('id')}.dry_live_argv"), "dry_live_argv"
    return _as_argv(command.get("argv") or [], field_name=f"{command.get('id')}.argv"), "argv"


def _run_command(
    *,
    command: dict[str, Any],
    mode: str,
    repo_root: Path,
    run_dir: Path,
    job_id: str,
    command_dir: Path,
    allow_stale: bool,
) -> CommandResult:
    command_id = str(command.get("id") or "command")
    argv, argv_source = _select_argv(command, mode)
    allow_modes = set(command.get("allow_modes") or ["dry-live", "live-readonly"])
    if mode == "plan":
        return CommandResult(command_id=command_id, status="planned", gate="GREEN", argv=argv)
    if mode not in allow_modes:
        return CommandResult(command_id=command_id, status="skipped", gate="GREEN", argv=argv)
    findings = _looks_like_production_write(argv)
    if findings:
        return CommandResult(
            command_id=command_id,
            status="blocked",
            gate="RED",
            argv=argv,
            error=f"refused forbidden production write token(s): {', '.join(findings)}",
        )

    stdout_path = command_dir / f"{command_id}.stdout.txt"
    stderr_path = command_dir / f"{command_id}.stderr.txt"
    timeout_seconds = int(command.get("timeout_seconds") or 900)
    env = os.environ.copy()
    env["WEB_AUTO_SCHEDULED_CHECKPOINT_MODE"] = mode
    env["WEB_AUTO_SCHEDULED_CHECKPOINT_ARGV_SOURCE"] = argv_source
    env["WEB_AUTO_SCHEDULED_CHECKPOINT_REPO_ROOT"] = str(repo_root)
    env["WEB_AUTO_SCHEDULED_CHECKPOINT_RUN_DIR"] = str(run_dir)
    env["WEB_AUTO_SCHEDULED_CHECKPOINT_COMMAND_DIR"] = str(command_dir)
    env["WEB_AUTO_SCHEDULED_CHECKPOINT_JOB_ID"] = job_id
    env["WEB_AUTO_SCHEDULED_CHECKPOINT_COMMAND_ID"] = command_id
    try:
        proc = subprocess.run(
            argv,
            cwd=repo_root,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout_seconds,
            check=False,
            env=env,
        )
    except subprocess.TimeoutExpired as exc:
        stdout_path.write_text(exc.stdout or "", encoding="utf-8")
        stderr_path.write_text(exc.stderr or f"timeout after {timeout_seconds}s", encoding="utf-8")
        return CommandResult(
            command_id=command_id,
            status="timeout",
            gate="YELLOW" if allow_stale else "RED",
            argv=argv,
            returncode=None,
            stdout_path=str(stdout_path),
            stderr_path=str(stderr_path),
            error=f"timeout after {timeout_seconds}s",
        )

    stdout_path.write_text(proc.stdout or "", encoding="utf-8")
    stderr_path.write_text(proc.stderr or "", encoding="utf-8")
    parsed = _json_from_stdout(proc.stdout or "")
    parsed_status = str((parsed or {}).get("status") or "").strip()
    parsed_gate = str((parsed or {}).get("gate") or "").strip().upper()
    parsed_decision = str((parsed or {}).get("decision") or "").strip()
    status = "success"
    gate = "GREEN"
    error = ""
    if proc.returncode != 0:
        status = "failed"
        gate = "YELLOW" if allow_stale else "RED"
        error = f"returncode={proc.returncode}"
    elif parsed_status:
        lowered = parsed_status.lower()
        if lowered in WARNING_STATUSES:
            status = "warning"
            gate = "YELLOW"
        elif lowered not in SUCCESS_STATUSES:
            status = "warning"
            gate = "YELLOW"
    return CommandResult(
        command_id=command_id,
        status=status,
        gate=gate,
        argv=argv,
        returncode=proc.returncode,
        stdout_path=str(stdout_path),
        stderr_path=str(stderr_path),
        parsed_status=parsed_status,
        parsed_gate=parsed_gate,
        parsed_decision=parsed_decision,
        error=error,
    )


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")


def _write_command_results(path: Path, results: list[CommandResult]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for result in results:
            handle.write(json.dumps(result.__dict__, ensure_ascii=False, default=str) + "\n")


def _overall_gate(results: list[CommandResult]) -> str:
    gates = {result.gate for result in results}
    if "RED" in gates:
        return "RED"
    if "YELLOW" in gates:
        return "YELLOW"
    return "GREEN"


def _render_closeout(summary: dict[str, Any], results: list[CommandResult]) -> str:
    lines = [
        f"# Scheduled Checkpoint Closeout: {summary['job_id']}",
        "",
        f"- generated_at: {summary['generated_at']}",
        f"- scheduled_at: {summary['scheduled_at']}",
        f"- mode: {summary['mode']}",
        f"- run_dir: {summary['run_dir']}",
        f"- owner_approval_required_for_live_writes: {summary['owner_approval_required_for_live_writes']}",
        f"- production_write_action_executed: {summary['production_write_action_executed']}",
        "",
        "## Commands",
        "",
        "| command_id | status | gate | returncode | parsed_status | parsed_gate | parsed_decision |",
        "| --- | --- | --- | ---: | --- | --- | --- |",
    ]
    for result in results:
        returncode = "" if result.returncode is None else str(result.returncode)
        lines.append(
            f"| {result.command_id} | {result.status} | {result.gate} | {returncode} | {result.parsed_status} | {result.parsed_gate} | {result.parsed_decision} |"
        )
    lines.extend(
        [
            "",
            "No production write action was executed. Any live price, bid, budget, campaign, stock, scheduler, Repricer, API, or database change still requires explicit owner authorization.",
            "",
            f"Gate: {summary['gate']}",
            "",
        ]
    )
    return "\n".join(lines)


def run_job(
    config: dict[str, Any],
    job: dict[str, Any],
    *,
    mode: str,
    repo_root: Path | None = None,
    run_root: Path | None = None,
    timestamp: str | None = None,
    allow_stale: bool = False,
    mark_done: bool = True,
) -> dict[str, Any]:
    if mode not in SAFE_MODES:
        raise ScheduledCheckpointError(f"unsupported mode: {mode}")
    root = repo_root or repo_root_from()
    timezone_name = str(config.get("timezone") or "Asia/Almaty")
    generated_at = _now(timezone_name)
    ts = timestamp or generated_at.strftime("%Y%m%d_%H%M%S")
    job_id = str(job.get("job_id") or "")
    scheduled_at = _parse_dt(str(job.get("scheduled_at") or ""), timezone_name)
    output_root = Path(run_root or config.get("run_root") or DEFAULT_RUN_ROOT)
    run_dir = output_root / job_id / ts
    commands_dir = run_dir / "commands"
    commands_dir.mkdir(parents=True, exist_ok=False)

    results = [
        _run_command(
            command=command,
            mode=mode,
            repo_root=root,
            run_dir=run_dir,
            job_id=job_id,
            command_dir=commands_dir,
            allow_stale=allow_stale,
        )
        for command in job.get("commands") or []
    ]
    gate = _overall_gate(results)
    summary = {
        "schema_version": SCHEMA_VERSION,
        "status": "success" if gate == "GREEN" else "warning" if gate == "YELLOW" else "failed",
        "gate": gate,
        "job_id": job_id,
        "job_title": str(job.get("title") or ""),
        "mode": mode,
        "generated_at": generated_at.isoformat(),
        "scheduled_at": scheduled_at.isoformat(),
        "run_dir": str(run_dir),
        "owner_approval_required_for_live_writes": bool(job.get("owner_approval_required_for_live_writes", True)),
        "production_write_action_executed": False,
        "command_count": len(results),
        "commands": [result.__dict__ for result in results],
        "next_owner_action": str(job.get("next_owner_action") or "review checkpoint closeout"),
    }
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "job": job,
        "mode": mode,
        "repo_root": str(root),
        "run_dir": str(run_dir),
        "generated_at": summary["generated_at"],
    }
    _write_json(run_dir / "RUN_MANIFEST.json", manifest)
    _write_json(run_dir / "CHECKPOINT_SUMMARY.json", summary)
    _write_command_results(run_dir / "COMMAND_RESULTS.jsonl", results)
    (run_dir / "CHECKPOINT_CLOSEOUT.md").write_text(_render_closeout(summary, results), encoding="utf-8")
    if mark_done and mode != "plan" and (gate == "GREEN" or (gate == "YELLOW" and allow_stale)):
        save_completed_job(output_root, job_id, summary)
    return summary


def run_due_jobs(
    config: dict[str, Any],
    *,
    mode: str,
    repo_root: Path | None = None,
    run_root: Path | None = None,
    now: datetime | None = None,
    force: bool = False,
    allow_stale: bool = False,
    max_jobs: int | None = None,
) -> dict[str, Any]:
    selected = due_jobs(config, now=now, run_root=run_root, force=force)
    if max_jobs is not None:
        selected = selected[:max_jobs]
    results = [
        run_job(
            config,
            job,
            mode=mode,
            repo_root=repo_root,
            run_root=run_root,
            allow_stale=allow_stale,
        )
        for job in selected
    ]
    gate = _overall_gate(
        [
            CommandResult(
                command_id=item["job_id"],
                status=item["status"],
                gate=item["gate"],
                argv=[],
            )
            for item in results
        ]
    ) if results else "GREEN"
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "success" if results else "no_due_jobs",
        "gate": gate,
        "jobs_run": len(results),
        "job_ids": [item["job_id"] for item in results],
        "runs": results,
        "production_write_action_executed": False,
    }
