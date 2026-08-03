# ruff: noqa: T201
"""Classify local branches and worktrees as safe-to-delete or unsafe, relative to the trunk.

A worktree is safe to remove iff it has NO uncommitted/untracked changes AND its branch (or HEAD,
if detached) is fully merged into the trunk. A standalone branch (not checked out anywhere) is
safe iff it is fully merged. The trunk branch is never a candidate.
"""

from __future__ import annotations

import subprocess


def git(*args: str) -> str:
    return subprocess.run(["git", *args], capture_output=True, text=True, check=True).stdout.strip()


def trunk_ref() -> str:
    result = subprocess.run(
        ["git", "symbolic-ref", "refs/remotes/origin/HEAD"],
        capture_output=True,
        text=True,
        check=False,
    )
    ref = result.stdout.strip()
    if result.returncode == 0 and ref:
        return ref.removeprefix("refs/remotes/")
    return "origin/main"


def worktrees() -> list[dict]:
    """Parse `git worktree list --porcelain` into {path, head, branch, prunable} records.

    `prunable` marks a worktree whose directory git can no longer find: the admin record survives
    the directory, so nothing inside it can be inspected. Recording each HEAD sha here means the
    checks below never have to run inside a worktree to learn what it points at.
    """
    records, current = [], {}
    for line in git("worktree", "list", "--porcelain").splitlines():
        if line.startswith("worktree "):
            if current:
                records.append(current)
            current = {"path": line[len("worktree ") :], "head": None, "branch": None, "prunable": False}
        elif line.startswith("HEAD "):
            current["head"] = line[len("HEAD ") :]
        elif line.startswith("branch "):
            current["branch"] = line[len("branch ") :].removeprefix("refs/heads/")
        elif line.startswith("prunable"):
            current["prunable"] = True
    if current:
        records.append(current)
    return records


def is_dirty(path: str) -> list[str]:
    # Don't strip: porcelain status codes are column-significant (" M" unstaged vs "M " staged).
    result = subprocess.run(
        ["git", "-C", path, "status", "--porcelain"],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.splitlines()


def is_merged(ref: str, trunk: str) -> bool:
    return (
        subprocess.run(
            ["git", "merge-base", "--is-ancestor", ref, trunk],
            capture_output=True,
            check=False,
        ).returncode
        == 0
    )


def change_summary(ref: str) -> str:
    return git("show", "--format=", "--stat", "--first-parent", ref)


def get_analysis() -> dict:
    """Return a safety snapshot after refreshing the trunk branch."""
    trunk = trunk_ref()
    remote, _, branch_name = trunk.partition("/")
    git("fetch", remote, branch_name)
    trunk_sha = git("rev-parse", trunk)

    trees = worktrees()
    main_path = trees[0]["path"] if trees else None
    wt_branches = {t["branch"] for t in trees if t["branch"]}

    safe_worktrees, safe_branches, skipped = [], [], []

    for t in trees:
        if t["path"] == main_path:
            continue
        # A prunable worktree has no directory left, so there is no working tree left to be dirty.
        reasons, dirty = [], [] if t["prunable"] else is_dirty(t["path"])
        if dirty:
            reasons.append("uncommitted/untracked changes")
        if not is_merged(t["head"], trunk):
            reasons.append("not merged into trunk")

        if reasons:
            skipped.append(
                {
                    "kind": "worktree",
                    "path": t["path"],
                    "branch": t["branch"],
                    "reasons": reasons,
                    "dirty": dirty,
                },
            )
        else:
            safe_worktrees.append(
                {
                    "path": t["path"],
                    "branch": t["branch"],
                    "head_sha": t["head"],
                    "prunable": t["prunable"],
                    "last_commit": git("log", "-1", "--format=%h %s", t["head"]),
                    "change_summary": change_summary(t["head"]),
                },
            )

    for branch in git("for-each-ref", "--format=%(refname:short)", "refs/heads").splitlines():
        if branch in ("", branch_name) or branch in wt_branches:
            continue
        if not is_merged(branch, trunk):
            skipped.append({"kind": "branch", "branch": branch, "reasons": ["not merged into trunk"]})
        else:
            safe_branches.append(
                {
                    "branch": branch,
                    "tip_sha": git("rev-parse", branch),
                    "last_commit": git("log", "-1", "--format=%h %s (%cr)", branch),
                    "change_summary": change_summary(branch),
                },
            )

    return {
        "trunk_sha": trunk_sha,
        "safe_branches": safe_branches,
        "safe_worktrees": safe_worktrees,
        "skipped": skipped,
    }
