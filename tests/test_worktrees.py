from __future__ import annotations

import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import call, patch

from shipshape.worktrees.cli import (
    CHECKOUT,
    CREATE,
    DELETE_KEY,
    Worktree,
    get_worktrees,
    has_uncommitted_changes,
    remove_worktree,
    run,
    select,
    shell_init,
)


class WorktreeTests(unittest.TestCase):
    def test_successful_deletion_refreshes_the_picker(self) -> None:
        repo = Path("/tmp/repo")
        deleted = Worktree(Path("/tmp/repo-worktrees/deleted"), "feat/deleted")
        remaining = Worktree(Path("/tmp/repo-worktrees/remaining"), "feat/remaining")
        output = StringIO()

        with (
            patch("shipshape.worktrees.cli.get_repo_root", return_value=repo),
            patch(
                "shipshape.worktrees.cli.get_worktrees",
                side_effect=[[deleted, remaining], [remaining]],
            ) as get_worktrees,
            patch(
                "shipshape.worktrees.cli.select",
                side_effect=[(DELETE_KEY, deleted), (CHECKOUT, remaining)],
            ),
            patch("shipshape.worktrees.cli.remove_worktree", return_value=True),
            redirect_stdout(output),
        ):
            status = run(repo, Path("/tmp"))

        self.assertEqual(status, 0)
        self.assertEqual(get_worktrees.call_count, 2)
        self.assertEqual(output.getvalue(), f"{remaining.path}\n")

    def test_select_distinguishes_checkout_create_and_delete(self) -> None:
        worktree = Worktree(Path("/tmp/repo-worktrees/feature"), "feat/example")
        outputs = [
            "\n/tmp/repo-worktrees/feature\tfeat/example\n",
            "ctrl-d\n/tmp/repo-worktrees/feature\tfeat/example\n",
            "\n+ new worktree\n",
        ]

        with patch("shipshape.worktrees.cli.subprocess.run") as run:
            run.side_effect = [
                subprocess.CompletedProcess([], 0, stdout=output, stderr="")
                for output in outputs
            ]
            self.assertEqual(select([worktree], repo_name="repo"), (CHECKOUT, worktree))
            self.assertEqual(
                select([worktree], repo_name="repo"), (DELETE_KEY, worktree)
            )
            self.assertEqual(select([worktree], repo_name="repo"), (CREATE, None))

    def test_get_worktrees_parses_branch_and_detached_records(self) -> None:
        output = """worktree /tmp/repo
HEAD abc123
branch refs/heads/main

worktree /tmp/repo worktrees/feature
HEAD def456
detached
"""
        completed = subprocess.CompletedProcess([], 0, stdout=output, stderr="")

        with patch("shipshape.worktrees.cli.subprocess.run", return_value=completed):
            worktrees = get_worktrees(Path("/tmp/repo"))

        self.assertEqual(
            worktrees,
            [
                Worktree(Path("/tmp/repo"), "main"),
                Worktree(Path("/tmp/repo worktrees/feature"), None),
            ],
        )

    def test_detects_uncommitted_changes(self) -> None:
        worktree = Worktree(Path("/tmp/repo-worktrees/feature"), "feat/example")
        completed = subprocess.CompletedProcess(
            [], 0, stdout="?? untracked.txt\n", stderr=""
        )

        with patch(
            "shipshape.worktrees.cli.subprocess.run", return_value=completed
        ) as run:
            changed = has_uncommitted_changes(worktree)

        self.assertTrue(changed)
        run.assert_called_once_with(
            ["git", "-C", str(worktree.path), "status", "--porcelain"],
            capture_output=True,
            text=True,
            check=True,
        )

    def test_remove_worktree_requires_confirmation(self) -> None:
        worktree = Worktree(Path("/tmp/repo-worktrees/feature"), "feat/example")

        with (
            patch("shipshape.worktrees.cli.confirm", return_value=False) as confirm,
            patch("shipshape.worktrees.cli.subprocess.run") as run,
        ):
            removed = remove_worktree(Path("/tmp/repo"), worktree, Path("/tmp/repo"))

        self.assertFalse(removed)
        confirm.assert_called_once_with(
            "Delete worktree /tmp/repo-worktrees/feature (feat/example)?"
        )
        run.assert_not_called()

    def test_remove_worktree_uses_git_after_confirmation(self) -> None:
        worktree = Worktree(Path("/tmp/repo-worktrees/feature"), "feat/example")

        completed = subprocess.CompletedProcess([], 0)
        with (
            patch("shipshape.worktrees.cli.confirm", return_value=True),
            patch(
                "shipshape.worktrees.cli.subprocess.run", return_value=completed
            ) as run,
        ):
            removed = remove_worktree(Path("/tmp/repo"), worktree, Path("/tmp/repo"))

        self.assertTrue(removed)
        run.assert_called_once_with(
            [
                "git",
                "-C",
                "/tmp/repo",
                "worktree",
                "remove",
                "/tmp/repo-worktrees/feature",
            ],
            check=False,
        )

    def test_remove_worktree_can_force_delete_local_changes(self) -> None:
        worktree = Worktree(Path("/tmp/repo-worktrees/feature"), "feat/example")

        failed = subprocess.CompletedProcess([], 128)
        forced = subprocess.CompletedProcess([], 0)
        with (
            patch("shipshape.worktrees.cli.has_uncommitted_changes", return_value=True),
            patch(
                "shipshape.worktrees.cli.confirm", side_effect=[True, True]
            ) as confirm,
            patch(
                "shipshape.worktrees.cli.subprocess.run", side_effect=[failed, forced]
            ) as run,
        ):
            removed = remove_worktree(Path("/tmp/repo"), worktree, Path("/tmp/repo"))

        self.assertTrue(removed)
        self.assertEqual(
            confirm.call_args_list,
            [
                call("Delete worktree /tmp/repo-worktrees/feature (feat/example)?"),
                call(
                    "Force delete worktree /tmp/repo-worktrees/feature (feat/example)? "
                    "Modified or untracked files will be permanently lost."
                ),
            ],
        )
        self.assertEqual(
            run.call_args_list,
            [
                call(
                    [
                        "git",
                        "-C",
                        "/tmp/repo",
                        "worktree",
                        "remove",
                        "/tmp/repo-worktrees/feature",
                    ],
                    check=False,
                ),
                call(
                    [
                        "git",
                        "-C",
                        "/tmp/repo",
                        "worktree",
                        "remove",
                        "--force",
                        "/tmp/repo-worktrees/feature",
                    ],
                    check=True,
                ),
            ],
        )

    def test_remove_worktree_protects_current_worktree(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            worktree = Worktree(path, "feat/example")
            with (
                patch("shipshape.worktrees.cli.confirm") as confirm,
                patch("shipshape.worktrees.cli.subprocess.run") as run,
            ):
                remove_worktree(Path("/tmp/repo"), worktree, path / "src")

        confirm.assert_not_called()
        run.assert_not_called()

    def test_shell_init_changes_directory_to_command_output(self) -> None:
        self.assertIn('command wt "$@"', shell_init())
        self.assertIn('destination="$(command wt)"', shell_init())
        self.assertIn('builtin cd -- "$destination"', shell_init())


if __name__ == "__main__":
    unittest.main()
