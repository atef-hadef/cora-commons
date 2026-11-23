"""Check a synthetic worker snapshot and optionally run configured restarts."""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path


STALE_AFTER = timedelta(minutes=10)
MAX_ARGUMENT_BYTES = 4 * 1024
MAX_COMMAND_BYTES = 16 * 1024
DEFAULT_CONFIG = Path(__file__).resolve().with_name("restart_commands.json")
HEARTBEAT_FORMAT = re.compile(
    r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2}(?:[.,]\d+)?)?"
    r"(?:Z|[+-](?:[01]\d|2[0-3])(?::?[0-5]\d)?)",
    re.ASCII,
)


def reject_json_constant(value: str) -> None:
    raise ValueError(f"invalid JSON constant {value}")


def read_json(path: Path) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8"), parse_constant=reject_json_constant)
    except ValueError as exc:
        raise ValueError(f"{path}: {exc}") from exc


def find_stuck(workers: object, checked_at: datetime) -> list[str]:
    if not isinstance(workers, list):
        raise ValueError("workers must be a JSON array")

    seen = set()
    stuck = []
    for index, worker in enumerate(workers):
        if not isinstance(worker, dict):
            raise ValueError(f"workers[{index}] must be an object")
        worker_id = worker.get("id")
        if not isinstance(worker_id, str) or not worker_id.strip():
            raise ValueError(f"workers[{index}] needs a nonempty string id")
        if worker_id in seen:
            raise ValueError(f"duplicate worker id: {worker_id}")
        seen.add(worker_id)

        state = worker.get("state")
        if not isinstance(state, str) or not state.strip():
            raise ValueError(f"{worker_id}: missing or invalid state")
        heartbeat = worker.get("last_heartbeat")
        if heartbeat is None and state == "crashed":
            stuck.append(worker_id)
            continue
        if not isinstance(heartbeat, str):
            raise ValueError(f"{worker_id}: missing or invalid last_heartbeat")
        if not HEARTBEAT_FORMAT.fullmatch(heartbeat):
            raise ValueError(f"{worker_id}: invalid last_heartbeat (expected a timestamp with a UTC offset)")
        try:
            last_seen = datetime.fromisoformat(heartbeat)
        except ValueError as exc:
            raise ValueError(f"{worker_id}: invalid last_heartbeat {heartbeat!r}") from exc
        if state == "crashed" or checked_at - last_seen > STALE_AFTER:
            stuck.append(worker_id)
    return stuck


def plan_actions(stuck: list[str], commands: object, apply: bool) -> list[dict]:
    if not isinstance(commands, dict):
        raise ValueError("restart configuration must be an object keyed by worker id")

    for worker_id, command in commands.items():
        if not isinstance(command, list) or not command:
            raise ValueError(f"{worker_id}: restart command must be a nonempty argument list")
        if not all(isinstance(arg, str) and "\x00" not in arg for arg in command):
            raise ValueError(f"{worker_id}: restart arguments must be strings without NUL bytes")
        command_bytes = 0
        try:
            for arg in command:
                argument_bytes = len(os.fsencode(arg))
                if argument_bytes > MAX_ARGUMENT_BYTES:
                    raise ValueError(f"{worker_id}: restart argument exceeds {MAX_ARGUMENT_BYTES} bytes")
                command_bytes += argument_bytes + 1  # Include the terminating NUL for each argument.
                if command_bytes > MAX_COMMAND_BYTES:
                    raise ValueError(f"{worker_id}: restart command exceeds {MAX_COMMAND_BYTES} bytes")
        except UnicodeEncodeError as exc:
            raise ValueError(f"{worker_id}: restart argument cannot be encoded for this host") from exc
        if not command[0].strip():
            raise ValueError(f"{worker_id}: missing restart executable")
        if apply and shutil.which(command[0]) is None:
            raise ValueError(f"{worker_id}: restart executable unavailable: {command[0]}")

    actions = []
    for worker_id in stuck:
        if worker_id not in commands:
            raise ValueError(f"{worker_id}: no configured restart command")
        actions.append({"id": worker_id, "command": commands[worker_id]})
    return actions


def write_report(path: Path, report: dict) -> None:
    # Replace in one step so readers always see a complete snapshot.
    with tempfile.TemporaryDirectory(dir=path.parent) as directory:
        snapshot = Path(directory) / path.name
        snapshot.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        snapshot.replace(path)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--workers", type=Path, required=True, help="worker snapshot JSON")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG, help="trusted restart map")
    parser.add_argument("--apply", action="store_true", help="execute planned restart commands")
    args = parser.parse_args()

    checked_at = datetime.now(timezone.utc)
    try:
        report_path = Path("heal.json")
        resolved_report = report_path.resolve()
        for option, path in (("--workers", args.workers), ("--config", args.config)):
            resolved_input = path.resolve()
            if resolved_input == resolved_report or (
                resolved_report.exists() and resolved_report.samefile(resolved_input)
            ):
                raise ValueError(f"{option} input and output heal.json refer to the same file: {path}")
        stuck = find_stuck(read_json(args.workers), checked_at)
        actions = plan_actions(stuck, read_json(args.config), args.apply)
        write_report(report_path, {
            "checked_at": checked_at.isoformat(),
            "stuck": stuck,
            "actions": actions,
        })
        mode = "Apply" if args.apply else "Dry-run"
        print(f"{mode} | Stuck workers ({len(stuck)}): {json.dumps(stuck)}", flush=True)
        if args.apply:
            for action in actions:
                try:
                    subprocess.run(action["command"], shell=False, check=True)
                except (OSError, subprocess.CalledProcessError) as exc:
                    raise ValueError(f"{action['id']}: restart failed: {exc}") from exc
    except (OSError, ValueError) as exc:
        print(f"heal: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
