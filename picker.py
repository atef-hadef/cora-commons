"""Task picker that keeps its state on a side branch in its own git worktree.

Each run reads a tasks file (read-only), works out the picker state, writes it
into a separate worktree checked out on --branch, and makes at most one commit
there, only when the state changed. The main checkout and its branch are never
written to.

Usage:
    python picker.py --repo <path> --branch picker/state [--worktree <path>] [--tasks <path>]
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any


STATE_FILE = "picker-state.json"
STATUSES = {"open", "done"}


class PickerError(Exception):
    """A condition that stops the run with a clear message and a non-zero exit."""


def run_git(args: list[str], cwd: Path, check: bool = True) -> subprocess.CompletedProcess[str]:
    """Run git with list arguments (never through a shell) and capture its text output."""
    result = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, encoding="utf-8")
    if check and result.returncode != 0:
        raise PickerError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result


def repo_root(repo: Path) -> Path:
    """Return the top folder of the main checkout, or refuse if --repo is not a git repository."""
    if not repo.is_dir():
        raise PickerError(f"not a git repository: {repo}")
    result = run_git(["rev-parse", "--show-toplevel"], repo, check=False)
    if result.returncode != 0:
        raise PickerError(f"not a git repository: {repo}")
    return Path(result.stdout.strip()).resolve()


def current_branch(repo: Path) -> str | None:
    """Return the branch checked out in the main checkout, or None for a detached HEAD."""
    result = run_git(["symbolic-ref", "--quiet", "--short", "HEAD"], repo, check=False)
    return result.stdout.strip() or None


def load_tasks(path: Path) -> list[dict[str, Any]]:
    """Read the tasks file without changing it, and refuse anything that is not a valid task list."""
    try:
        tasks = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PickerError(f"cannot read tasks file {path}: {exc}") from exc
    if not isinstance(tasks, list):
        raise PickerError(f"tasks file {path} must contain a JSON list")
    for index, task in enumerate(tasks):
        if (
            not isinstance(task, dict)
            or not isinstance(task.get("id"), str)
            or not isinstance(task.get("title"), str)
            or not isinstance(task.get("priority"), int)
            or isinstance(task.get("priority"), bool)
            or task.get("status") not in STATUSES
        ):
            raise PickerError(f"tasks[{index}] must have text id and title, integer priority and status open or done")
    return tasks


def pick_task(tasks: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Return the open task with the lowest priority number, ties broken by id; None if none is open."""
    open_tasks = [task for task in tasks if task["status"] == "open"]
    if not open_tasks:
        return None
    return min(open_tasks, key=lambda task: (task["priority"], task["id"]))


def build_state(tasks: list[dict[str, Any]]) -> dict[str, Any]:
    """Return the picker state. It depends only on the tasks, so the same tasks always give the same state."""
    return {
        "schema_version": "0.1",
        "picked": pick_task(tasks),
        "open_count": sum(1 for task in tasks if task["status"] == "open"),
    }


def write_state(path: Path, state: dict[str, Any]) -> None:
    """Write the state as sorted, indented JSON with LF line endings and one trailing newline."""
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(state, handle, sort_keys=True, indent=2)
        handle.write("\n")


def registered_worktrees(repo: Path) -> list[tuple[Path, str | None]]:
    """Return (folder, branch) for every worktree git knows about, from `git worktree list --porcelain`."""
    entries: list[tuple[Path, str | None]] = []
    folder: Path | None = None
    branch: str | None = None
    for line in run_git(["worktree", "list", "--porcelain"], repo).stdout.splitlines() + [""]:
        if line.startswith("worktree "):
            folder, branch = Path(line[len("worktree "):]).resolve(), None
        elif line.startswith("branch refs/heads/"):
            branch = line[len("branch refs/heads/"):]
        elif not line and folder is not None:
            entries.append((folder, branch))
            folder = None
    return entries


def ensure_worktree(repo: Path, branch: str, worktree: Path) -> Path:
    """Reuse the worktree for `branch`, or add one; never moves or checks out anything in the main checkout."""
    run_git(["worktree", "prune"], repo)
    for folder, checked_out in registered_worktrees(repo):
        if checked_out != branch:
            continue
        if folder == worktree:
            return worktree
        raise PickerError(f"branch {branch} is already checked out in {folder}")
    branch_exists = run_git(["rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"], repo, check=False).returncode == 0
    if branch_exists:
        run_git(["worktree", "add", str(worktree), branch], repo)
    else:
        main_sha = run_git(["rev-parse", "HEAD"], repo).stdout.strip()
        run_git(["worktree", "add", "-b", branch, str(worktree), main_sha], repo)
    return worktree


def commit_if_changed(worktree: Path) -> str | None:
    """Stage the worktree and make one commit if anything changed; return its short sha, or None."""
    run_git(["add", "-A"], worktree)
    diff = run_git(["diff", "--cached", "--quiet"], worktree, check=False)
    if diff.returncode == 0:
        return None
    if diff.returncode != 1:
        raise PickerError(f"git diff --cached failed: {diff.stderr.strip()}")
    run_git(
        ["-c", "user.name=cora-picker", "-c", "user.email=picker@example.invalid", "commit", "-m", "picker: update state"],
        worktree,
    )
    return run_git(["rev-parse", "--short", "HEAD"], worktree).stdout.strip()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Pick the next task and commit the picker state to a side branch.")
    parser.add_argument("--repo", type=Path, required=True, help="main checkout of the repository")
    parser.add_argument("--branch", required=True, help="side branch for the picker state, e.g. picker/state")
    parser.add_argument("--worktree", type=Path, help="worktree folder (default: <repo parent>/<repo name>-picker)")
    parser.add_argument("--tasks", type=Path, help="tasks file, read-only (default: <repo>/tasks.json)")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        repo = repo_root(args.repo.resolve())
        if args.branch == current_branch(repo):
            raise PickerError(f"refusing to use {args.branch}: it is checked out in the main checkout {repo}")
        worktree = (args.worktree or repo.parent / f"{repo.name}-picker").resolve()
        if worktree == repo or repo in worktree.parents:
            raise PickerError(f"refusing worktree {worktree}: it is inside the main checkout {repo}")
        tasks = (args.tasks or repo / "tasks.json").resolve()
        state = build_state(load_tasks(tasks))
        ensure_worktree(repo, args.branch, worktree)
        write_state(worktree / STATE_FILE, state)
        sha = commit_if_changed(worktree)
    except PickerError as exc:
        print(f"picker: {exc}", file=sys.stderr)
        return 2
    print("no changes" if sha is None else f"committed {sha}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
