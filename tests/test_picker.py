from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


PICKER = Path(__file__).resolve().parents[1] / "picker.py"
BRANCH = "picker/state"
IDENTITY = ["-c", "user.name=test", "-c", "user.email=test@example.invalid"]
TASKS = [
    {"id": "T-1", "title": "Fix login", "priority": 1, "status": "open"},
    {"id": "T-2", "title": "Write docs", "priority": 2, "status": "open"},
]


def git(cwd: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, encoding="utf-8", check=True)
    return result.stdout.strip()


class PickerTests(unittest.TestCase):
    """Run picker.py against a synthetic repository built in a temporary folder."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name).resolve()
        self.repo = root / "repo"
        self.worktree = root / "repo-picker"
        self.repo.mkdir()
        git(self.repo, "init", "-b", "main")
        git(self.repo, "config", "core.autocrlf", "false")
        self.write_tasks(TASKS)
        git(self.repo, "add", "tasks.json")
        git(self.repo, *IDENTITY, "commit", "-m", "add tasks")

    def write_tasks(self, tasks: list[dict[str, object]]) -> None:
        text = json.dumps(tasks, indent=2) + "\n"
        (self.repo / "tasks.json").write_text(text, encoding="utf-8", newline="\n")

    def run_picker(self, *extra: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(PICKER), "--repo", str(self.repo), "--branch", BRANCH, "--worktree", str(self.worktree), *extra],
            capture_output=True,
            text=True,
            encoding="utf-8",
        )

    def picker_commits(self) -> int:
        return int(git(self.repo, "rev-list", "--count", BRANCH))

    def picked_id(self) -> str | None:
        state = json.loads((self.worktree / "picker-state.json").read_text(encoding="utf-8"))
        return state["picked"]["id"] if state["picked"] else None

    def test_first_run_makes_exactly_one_commit_on_the_side_branch(self) -> None:
        result = self.run_picker()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(result.stdout.startswith("committed "))
        self.assertEqual(self.picker_commits(), 2)  # the "add tasks" commit plus one picker commit
        self.assertEqual(git(self.repo, "log", "-1", "--format=%s", BRANCH), "picker: update state")
        self.assertEqual(self.picked_id(), "T-1")

    def test_unchanged_second_run_makes_no_commit(self) -> None:
        self.run_picker()
        result = self.run_picker()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "no changes")
        self.assertEqual(self.picker_commits(), 2)

    def test_changed_tasks_make_exactly_one_new_commit(self) -> None:
        self.run_picker()
        self.write_tasks([{**TASKS[0], "status": "done"}, TASKS[1]])
        result = self.run_picker()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(result.stdout.startswith("committed "))
        self.assertEqual(self.picker_commits(), 3)
        self.assertEqual(self.picked_id(), "T-2")

    def test_main_head_and_main_checkout_unchanged_after_five_runs(self) -> None:
        main_before = git(self.repo, "rev-parse", "main")
        task_lists = [
            TASKS,
            TASKS,
            [{**TASKS[0], "status": "done"}, TASKS[1]],
            [{**TASKS[0], "status": "done"}, TASKS[1]],
            [{**TASKS[0], "status": "done"}, {**TASKS[1], "status": "done"}],
        ]
        outputs = []
        for tasks in task_lists:
            self.write_tasks(tasks)
            result = self.run_picker()
            self.assertEqual(result.returncode, 0, result.stderr)
            outputs.append(result.stdout.split()[0])
        self.assertEqual(outputs, ["committed", "no", "committed", "no", "committed"])
        self.assertEqual(git(self.repo, "rev-parse", "main"), main_before)
        self.assertEqual(git(self.repo, "symbolic-ref", "--short", "HEAD"), "main")
        self.assertEqual(git(self.repo, "status", "--porcelain"), "M tasks.json")

    def test_existing_worktree_is_reused_not_added_again(self) -> None:
        for _ in range(3):
            self.assertEqual(self.run_picker().returncode, 0)
        listed = [line for line in git(self.repo, "worktree", "list", "--porcelain").splitlines() if line.startswith("worktree ")]
        self.assertEqual([Path(line[len("worktree "):]).resolve() for line in listed], [self.repo, self.worktree])
        self.assertEqual(len(list((self.repo / ".git" / "worktrees").iterdir())), 1)

    def test_branch_checked_out_in_main_checkout_is_refused(self) -> None:
        commits_before = git(self.repo, "rev-list", "--count", "main")
        result = subprocess.run(
            [sys.executable, str(PICKER), "--repo", str(self.repo), "--branch", "main", "--worktree", str(self.worktree)],
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("refusing to use main", result.stderr)
        self.assertEqual(git(self.repo, "rev-list", "--count", "main"), commits_before)
        self.assertFalse(self.worktree.exists())
        self.assertEqual(git(self.repo, "status", "--porcelain"), "")

    def test_number_task_id_is_refused_before_any_branch_or_worktree(self) -> None:
        self.write_tasks([TASKS[0], {**TASKS[1], "id": 2, "priority": 1}])
        result = self.run_picker()
        self.assertEqual(result.returncode, 2)
        self.assertIn("tasks[1] must have text id", result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertFalse(self.worktree.exists())
        branch = subprocess.run(["git", "rev-parse", "--verify", "--quiet", f"refs/heads/{BRANCH}"], cwd=self.repo)
        self.assertNotEqual(branch.returncode, 0)


if __name__ == "__main__":
    unittest.main()
