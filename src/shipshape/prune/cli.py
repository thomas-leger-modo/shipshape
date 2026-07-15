# ruff: noqa: T201
"""Interactively select and remove branches/worktrees proven safe by analyze.py."""

from __future__ import annotations

import json
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path

from shipshape.prune.analyze import get_analysis
from shipshape.ui import BOLD, DIM, GREEN, RED, RESET, YELLOW, confirm, get_preview_height, require, run_fzf


def get_candidates(analysis: dict) -> list[dict]:
    candidates = [{"kind": "worktree", **worktree} for worktree in analysis["safe_worktrees"]]
    candidates.extend({"kind": "branch", **branch} for branch in analysis["safe_branches"])
    return candidates


def collapse(path: str) -> str:
    worktree_path = Path(path)
    try:
        return f"~/{worktree_path.relative_to(Path.home())}"
    except ValueError:
        return str(worktree_path)


def format_candidate(candidate: dict) -> str:
    if candidate["kind"] == "worktree":
        action = "worktree + branch" if candidate["branch"] else "worktree"
        identity = candidate["branch"] or f"detached @ {candidate['head_sha'][:8]}"
        return f"{YELLOW}[{action}]{RESET} {collapse(candidate['path'])} {DIM}·{RESET} {identity}"
    return f"{GREEN}[branch]{RESET} {candidate['branch']}"


def format_preview(candidate: dict, trunk_sha: str) -> str:
    if candidate["kind"] == "worktree":
        branch = candidate["branch"] or "detached HEAD"
        action = "Remove this worktree and its local branch" if candidate["branch"] else "Remove this worktree"
        lines = [
            f"{BOLD}{collapse(candidate['path'])} · {branch}{RESET}",
            f"{DIM}{action}{RESET}",
            "",
            "Location",
            f"  {candidate['path']}",
        ]
    else:
        lines = [
            f"{BOLD}{candidate['branch']}{RESET}",
            f"{DIM}Remove this local branch{RESET}",
        ]
    summary = "\n".join(f"  {line.lstrip()}" for line in candidate["change_summary"].splitlines())
    lines.extend(
        [
            "",
            "Last commit",
            f"  {candidate['last_commit']}",
            "",
            "Files changed",
            summary or "  no file changes",
            "",
            "Why safe",
            f"  {GREEN}fully merged into trunk{RESET}",
            "",
            f"{DIM}Rechecked immediately before deletion · trunk {trunk_sha[:12]}{RESET}",
        ],
    )
    return "\n".join(lines)


def select_candidates(analysis: dict) -> list[dict]:
    candidates = get_candidates(analysis)
    if not candidates:
        return []
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json") as snapshot:
        json.dump(analysis, snapshot)
        snapshot.flush()
        preview_command = shlex.join([sys.executable, "-m", "shipshape.prune.cli", "preview", snapshot.name]) + " {1}"
        rows = [f"{index}\t{format_candidate(candidate)}" for index, candidate in enumerate(candidates)]
        selected_rows = run_fzf(
            rows,
            prompt="select> ",
            header=(
                f"{len(candidates)} safe · {len(analysis['skipped'])} unsafe hidden  |  "
                "SPACE select + next · CTRL-A all · CTRL-D clear · ENTER review"
            ),
            preview=preview_command,
            preview_height=get_preview_height(
                format_preview(candidate, analysis["trunk_sha"]) for candidate in candidates
            ),
        )
    return [candidates[int(row.split("\t", 1)[0])] for row in selected_rows]


def confirm_removal(selected: list[dict]) -> bool:
    worktree_count = sum(candidate["kind"] == "worktree" for candidate in selected)
    branch_count = sum(bool(candidate.get("branch")) for candidate in selected)
    return confirm(f"Remove {worktree_count} worktree(s) and {branch_count} branch(es)?")


def candidate_id(candidate: dict) -> tuple:
    if candidate["kind"] == "worktree":
        return ("worktree", candidate["path"], candidate["head_sha"])
    return ("branch", candidate["branch"], candidate["tip_sha"])


def get_stale_candidates(selected: list[dict], fresh_analysis: dict) -> list[dict]:
    fresh_ids = {candidate_id(candidate) for candidate in get_candidates(fresh_analysis)}
    return [candidate for candidate in selected if candidate_id(candidate) not in fresh_ids]


def remove_candidates(selected: list[dict]) -> int:
    failed = 0
    for candidate in selected:
        if candidate["kind"] == "worktree":
            result = subprocess.run(["git", "worktree", "remove", candidate["path"]], check=False)
            if result.returncode != 0:
                failed += 1
                continue
            if candidate["branch"]:
                result = subprocess.run(["git", "branch", "-d", candidate["branch"]], check=False)
        else:
            result = subprocess.run(["git", "branch", "-d", candidate["branch"]], check=False)
        failed += result.returncode != 0
    return failed


def preview(snapshot_path: str, candidate_index: str) -> None:
    analysis = json.loads(Path(snapshot_path).read_text())
    print(format_preview(get_candidates(analysis)[int(candidate_index)], analysis["trunk_sha"]))


def main() -> int:
    if len(sys.argv) == 4 and sys.argv[1] == "preview":
        preview(sys.argv[2], sys.argv[3])
        return 0
    require("fzf", "gum")

    print(f"{DIM}Checking branches and worktrees…{RESET}", end="", flush=True)
    analysis = get_analysis()
    print("\r\033[2K", end="", flush=True)
    if not get_candidates(analysis):
        print(f"{GREEN}Nothing to prune.{RESET} {DIM}{len(analysis['skipped'])} unsafe item(s) left untouched.{RESET}")
        return 0
    selected = select_candidates(analysis)
    if not selected:
        print(f"{DIM}Nothing selected.{RESET}")
        return 0
    if not confirm_removal(selected):
        print(f"{DIM}Cancelled. Nothing changed.{RESET}")
        return 0

    stale = get_stale_candidates(selected, get_analysis())
    if stale:
        print(f"{RED}Safety snapshot changed; nothing was removed. Run prune again.{RESET}")
        for candidate in stale:
            print(f"  {candidate.get('branch') or candidate.get('path')}")
        return 1

    failed = remove_candidates(selected)
    removed = len(selected) - failed
    colour = GREEN if failed == 0 else YELLOW
    print(f"{colour}Removed {removed}; failed {failed}.{RESET}")
    return int(failed > 0)


if __name__ == "__main__":
    raise SystemExit(main())
