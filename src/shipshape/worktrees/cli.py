from __future__ import annotations

import argparse
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from shipshape.ui import confirm, require

NEW_WORKTREE = "+ new worktree"
DELETE_KEY = "ctrl-d"
CHECKOUT = "checkout"
CREATE = "create"


@dataclass(frozen=True)
class Worktree:
    path: Path
    branch: str | None

    @property
    def label(self) -> str:
        branch = self.branch or "detached"
        return f"{self.path}\t{branch}"


def get_repo_root(cwd: Path) -> Path | None:
    result = subprocess.run(
        [
            "git",
            "-C",
            str(cwd),
            "rev-parse",
            "--path-format=absolute",
            "--git-common-dir",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode:
        return None
    return Path(result.stdout.strip()).parent


def get_worktrees(repo: Path) -> list[Worktree]:
    result = subprocess.run(
        ["git", "-C", str(repo), "worktree", "list", "--porcelain"],
        capture_output=True,
        text=True,
        check=True,
    )
    worktrees = []
    for record in result.stdout.strip().split("\n\n"):
        fields = dict(
            line.partition(" ")[::2] for line in record.splitlines() if " " in line
        )
        if path := fields.get("worktree"):
            branch_ref = fields.get("branch")
            branch = branch_ref.removeprefix("refs/heads/") if branch_ref else None
            worktrees.append(Worktree(Path(path), branch))
    return worktrees


def discover_worktrees(base: Path) -> list[Worktree]:
    worktrees: dict[Path, Worktree] = {}
    for repo in base.iterdir() if base.is_dir() else ():
        if not (repo / ".git").exists():
            continue
        for worktree in get_worktrees(repo):
            worktrees[worktree.path] = worktree
    return list(worktrees.values())


def select(
    worktrees: list[Worktree], *, repo_name: str | None
) -> tuple[str | None, Worktree | None]:
    rows = [worktree.label for worktree in worktrees]
    if repo_name:
        rows.append(NEW_WORKTREE)
    prompt = f"wt:{repo_name}> " if repo_name else "wt> "
    result = subprocess.run(
        [
            "fzf",
            "--height=40%",
            "--layout=reverse",
            f"--prompt={prompt}",
            "--delimiter=\t",
            "--with-nth=1,2",
            "--expect=ctrl-d",
            "--header=ENTER checkout · CTRL-D delete",
        ],
        input="\n".join(rows),
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode in (1, 130):
        return None, None
    result.check_returncode()
    lines = result.stdout.splitlines()
    key = lines[0] or None
    selected = lines[1] if len(lines) > 1 else ""
    if selected == NEW_WORKTREE:
        return CREATE, None
    path = Path(selected.split("\t", 1)[0])
    action = DELETE_KEY if key == DELETE_KEY else CHECKOUT
    return action, next(worktree for worktree in worktrees if worktree.path == path)


def create_worktree(repo: Path, existing: set[Path]) -> Path | None:
    print(
        "branch name (e.g. fix/MEN-1234-short-desc): ",
        end="",
        file=sys.stderr,
        flush=True,
    )
    branch = sys.stdin.readline().strip()
    if not branch:
        print("wt: aborted", file=sys.stderr)
        return None
    setup = repo / "scripts" / "setup_worktree.sh"
    if not os.access(setup, os.X_OK):
        print(f"wt: no scripts/setup_worktree.sh in {repo.name}", file=sys.stderr)
        return None
    subprocess.run([str(setup), branch], cwd=repo, stdout=sys.stderr, check=True)
    created = {worktree.path for worktree in get_worktrees(repo)} - existing
    if len(created) != 1:
        print(
            "wt: setup completed, but the new worktree could not be identified",
            file=sys.stderr,
        )
        return None
    return created.pop()


def has_uncommitted_changes(worktree: Worktree) -> bool:
    result = subprocess.run(
        ["git", "-C", str(worktree.path), "status", "--porcelain"],
        capture_output=True,
        text=True,
        check=True,
    )
    return bool(result.stdout.strip())


def remove_worktree(repo: Path, worktree: Worktree, cwd: Path) -> bool:
    if worktree.path == repo:
        print("wt: the main worktree cannot be deleted", file=sys.stderr)
        return False
    if cwd.is_relative_to(worktree.path):
        print(
            "wt: cannot delete the worktree your shell is currently in", file=sys.stderr
        )
        return False
    description = f"Delete worktree {worktree.path} ({worktree.branch or 'detached'})?"
    if not confirm(description):
        print("Cancelled. Nothing changed.", file=sys.stderr)
        return False
    command = ["git", "-C", str(repo), "worktree", "remove", str(worktree.path)]
    result = subprocess.run(command, check=False)
    if result.returncode == 0:
        return True
    if not has_uncommitted_changes(worktree):
        return False
    force_description = (
        f"Force delete worktree {worktree.path} ({worktree.branch or 'detached'})? "
        "Modified or untracked files will be permanently lost."
    )
    if not confirm(force_description):
        print("Cancelled. Nothing changed.", file=sys.stderr)
        return False
    subprocess.run([*command[:-1], "--force", str(worktree.path)], check=True)
    return True


def shell_init() -> str:
    return """wt() {
  if (( $# )); then
    command wt "$@"
    return
  fi
  local destination
  destination="$(command wt)" || return
  [[ -n "$destination" ]] && builtin cd -- "$destination"
}"""


def run(cwd: Path, base: Path) -> int:
    repo = get_repo_root(cwd)
    while True:
        worktrees = get_worktrees(repo) if repo else discover_worktrees(base)
        action, selected = select(worktrees, repo_name=repo.name if repo else None)
        if action == DELETE_KEY:
            if selected is None:
                return 0
            selected_repo = repo or get_repo_root(selected.path)
            if selected_repo is None or not remove_worktree(
                selected_repo, selected, cwd
            ):
                return 0
            continue
        if action == CHECKOUT and selected is not None:
            print(selected.path)
            return 0
        if repo is not None and action == CREATE:
            created = create_worktree(repo, {worktree.path for worktree in worktrees})
            if created is not None:
                print(created)
        return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Pick, create, or delete a git worktree"
    )
    parser.add_argument("--init", choices=["zsh"], help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.init:
        print(shell_init())
        return 0
    require("fzf", "gum")
    try:
        return run(
            Path.cwd().resolve(),
            Path(os.environ.get("WT_BASE", Path.home() / "code")).expanduser(),
        )
    except subprocess.CalledProcessError as error:
        return error.returncode or 1


if __name__ == "__main__":
    raise SystemExit(main())
