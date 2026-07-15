# ruff: noqa: T201
"""Shared terminal-UI helpers: ANSI colours, fzf selection, gum confirmation."""

from __future__ import annotations

import shutil
import subprocess
import sys
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable

GREEN = "\033[32m"
YELLOW = "\033[33m"
RED = "\033[31m"
CYAN = "\033[36m"
DIM = "\033[2m"
BOLD = "\033[1m"
RESET = "\033[0m"

_INSTALL_HINTS = {"fzf": "brew install fzf", "gum": "brew install gum", "gh": "https://cli.github.com"}


def require(*tools: str, gh_auth: bool = False) -> None:
    """Exit with hints if any required CLI tool is missing (or gh is unauthenticated)."""
    missing = [f"{tool} ({_INSTALL_HINTS.get(tool, tool)})" for tool in tools if shutil.which(tool) is None]
    if gh_auth and shutil.which("gh") and subprocess.run(["gh", "auth", "status"], capture_output=True, check=False).returncode:
        missing.append("GitHub authentication (run: gh auth login)")
    if not missing:
        return
    print(f"{RED}Missing requirements:{RESET}", file=sys.stderr)
    for requirement in missing:
        print(f"  • {requirement}", file=sys.stderr)
    sys.exit(1)


def get_preview_height(previews: Iterable[str]) -> int:
    tallest_preview_height = max(preview.count("\n") + 1 for preview in previews)
    terminal_height = shutil.get_terminal_size(fallback=(120, 40)).lines
    return min(tallest_preview_height, max(8, round(terminal_height * 0.6)))


def run_fzf(
    rows: list[str],
    *,
    prompt: str,
    header: str,
    preview: str,
    preview_height: int,
) -> list[str]:
    args = [
        "fzf",
        "--ansi",
        "--height=~90%",
        "--layout=reverse",
        "--border=rounded",
        "--info=inline-right",
        f"--prompt={prompt}",
        f"--header={header}",
        "--pointer=▸",
        "--marker=✓",
        "--multi",
        "--bind=space:toggle+down,ctrl-a:select-all,ctrl-d:deselect-all",
        "--delimiter=\t",
        "--with-nth=2..",
        f"--preview={preview}",
        f"--preview-window=down,{preview_height},wrap,border-top",
    ]
    result = subprocess.run(args, input="\n".join(rows), capture_output=True, text=True, check=False)
    if result.returncode in (1, 130):
        return []
    result.check_returncode()
    return result.stdout.splitlines()


def confirm(description: str) -> bool:
    result = subprocess.run(
        ["gum", "confirm", description, "--affirmative=Confirm", "--negative=Cancel"],
        check=False,
    )
    return result.returncode == 0
