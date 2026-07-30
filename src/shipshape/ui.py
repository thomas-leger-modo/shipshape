# ruff: noqa: T201
"""Shared terminal-UI helpers: ANSI colours, fzf selection, gum confirmation."""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING, NamedTuple

if TYPE_CHECKING:
    from collections.abc import Iterable

FZF_NO_MATCH = 1
FZF_ABORTED = 130


class FzfResult(NamedTuple):
    """Rows the user accepted, and whether they backed out rather than matching nothing — callers
    that loop need to tell "I am done" from "that filter found nothing"."""

    rows: list[str]
    aborted: bool

GREEN = "\033[32m"
YELLOW = "\033[33m"
RED = "\033[31m"
CYAN = "\033[36m"
DIM = "\033[2m"
BOLD = "\033[1m"
RESET = "\033[0m"

_INSTALL_HINTS = {
    "fzf": "brew install fzf",
    "gum": "brew install gum",
    "gh": "https://cli.github.com",
    "rg": "brew install ripgrep",
}


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


def paint(text: str, colour: str) -> str:
    """Colour `text`, or leave it alone — so an uncoloured cell emits no stray reset code."""
    return f"{colour}{text}{RESET}" if colour else text


def collapse(path: str) -> str:
    """Shorten a path for display by folding the home directory to `~`."""
    absolute = Path(path)
    try:
        return f"~/{absolute.relative_to(Path.home())}"
    except ValueError:
        return str(absolute)


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
    extra_binds: Iterable[str] = (),
    multi: bool = True,
) -> FzfResult:
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
        # Marking rows only earns its keystroke when the caller can act on several at once.
        *(("--multi", "--bind=space:toggle+down,ctrl-a:select-all,ctrl-d:deselect-all") if multi else ()),
        "--delimiter=\t",
        "--with-nth=2..",
        f"--preview={preview}",
        f"--preview-window=down,{preview_height},wrap,border-top",
        *(f"--bind={bind}" for bind in extra_binds),
    ]
    result = subprocess.run(args, input="\n".join(rows), capture_output=True, text=True, check=False)
    if result.returncode in (FZF_NO_MATCH, FZF_ABORTED):
        return FzfResult([], aborted=result.returncode == FZF_ABORTED)
    result.check_returncode()
    return FzfResult(result.stdout.splitlines(), aborted=False)


def confirm(description: str) -> bool:
    result = subprocess.run(
        ["gum", "confirm", description, "--affirmative=Confirm", "--negative=Cancel"],
        check=False,
    )
    return result.returncode == 0


def choose(header: str, options: list[str]) -> str | None:
    """Pick one of `options` with gum; None if cancelled. gum draws on stderr, so only stdout is
    captured — piping both would hide the menu."""
    result = subprocess.run(
        ["gum", "choose", f"--header={header}", *options],
        stdout=subprocess.PIPE,
        text=True,
        check=False,
    )
    return result.stdout.strip() or None if result.returncode == 0 else None
