# ruff: noqa: T201
"""Search every Claude Code and pi transcript on this machine for a keyword.

Results are grouped by session rather than listed as matching lines, because the question being
asked is almost always "which conversation was this" — and the session that mentions a thing forty
times is the one you want, not the twelve that mention it once in passing. Sessions are ordered on
that count.
"""

from __future__ import annotations

import argparse
import shlex
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

from shipshape.transcripts import scan
from shipshape.transcripts.scan import Session, Turn
from shipshape.ui import (
    BOLD,
    CYAN,
    DIM,
    GREEN,
    RESET,
    YELLOW,
    collapse,
    get_preview_height,
    require,
    run_fzf,
)

PREVIEW_TURNS = 12
PREVIEW_CHARS = 500
AGENT_COLOURS = {"claude": CYAN, "pi": GREEN}


@dataclass(frozen=True)
class Hit:
    session: Session
    turns: list[Turn]
    total: int


def search(query: str) -> list[Hit]:
    """Parse only the transcripts ripgrep already matched, and keep the turns that mention `query`."""
    hits = []
    for path in scan.matching_files(query):
        session = scan.read_session(path, scan.agent_for(path))
        if not session:
            continue
        matched = [turn for turn in scan.scan_turns(scan.read_lines(path)) if query.lower() in turn.text.lower()]
        if matched:
            hits.append(Hit(session, matched[:PREVIEW_TURNS], len(matched)))
    return sorted(hits, key=lambda hit: (hit.total, hit.session.started), reverse=True)


def format_row(hit: Hit) -> str:
    session = hit.session
    badge = f"{AGENT_COLOURS[session.agent]}{session.agent:<6}{RESET}"
    when = session.started.astimezone().strftime("%d %b %H:%M")
    marker = f" {YELLOW}subagent{RESET}" if session.is_subagent else ""
    return f"{badge} {DIM}{when}{RESET} {hit.total:>4} hit  {session.label}{marker}"


def format_preview(hit: Hit) -> str:
    session = hit.session
    lines = [
        f"{BOLD}{session.label}{RESET}",
        (
            f"{DIM}{session.agent} · {session.started.astimezone():%d %b %Y %H:%M} · "
            f"{hit.total} matching turn(s) · {collapse(session.cwd)}{RESET}"
        ),
        "Resume",
        f"  {session.resume_command}",
        *([f"{DIM}  (session id is the parent conversation's){RESET}"] if session.is_subagent else []),
        "Transcript",
        f"  {collapse(str(session.path))}",
        "Matching turns",
    ]
    for turn in hit.turns:
        text = " ".join(turn.text.split())[:PREVIEW_CHARS]
        lines.append(f"  {DIM}{turn.timestamp.astimezone():%d %b %H:%M}{RESET} {YELLOW}{turn.role:<9}{RESET} {text}")
    return "\n".join(lines)


def select(hits: list[Hit]) -> Hit | None:
    """Show the hits in fzf and return the one chosen.

    Previews are rendered to numbered files up front and displayed with `cat`, rather than fzf
    re-invoking this program per row: the text cannot change once the search is done, so paying
    interpreter startup on every keystroke buys nothing.
    """
    previews = [format_preview(hit) for hit in hits]
    with tempfile.TemporaryDirectory() as directory:
        for index, text in enumerate(previews):
            Path(directory, str(index)).write_text(text)
        rows = [f"{index}\t{format_row(hit)}" for index, hit in enumerate(hits)]
        selected = run_fzf(
            rows,
            prompt="transcript> ",
            header=f"{len(hits)} session(s) mention it  |  ENTER show how to resume",
            preview=f"cat {shlex.quote(directory)}/{{1}}",
            preview_height=get_preview_height(previews),
            multi=False,
        ).rows
    return hits[int(selected[0].split("\t", 1)[0])] if selected else None


def shell_init() -> str:
    return """search-transcripts() {
  if [[ "$1" == "--help" || "$1" == "-h" || "$1" == "--init" ]]; then
    command search-transcripts "$@"
    return
  fi
  local resume_command
  resume_command="$(command search-transcripts --print-command "$@")" || return
  [[ -n "$resume_command" ]] && print -rz -- "$resume_command"
}"""


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="search-transcripts",
        description="Find which Claude Code or pi conversation mentioned something.",
    )
    parser.add_argument("query", nargs="?", help="text to find, matched literally and case-insensitively")
    parser.add_argument("--init", choices=["zsh"], help=argparse.SUPPRESS)
    parser.add_argument("--print-command", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(sys.argv[1:])
    if args.init:
        print(shell_init())
        return 0
    if args.query is None:
        parser.error("the following arguments are required: query")
    require("rg", "fzf")

    hits = search(args.query)
    if not hits:
        print(f"{DIM}No transcript mentions {args.query!r}.{RESET}", file=sys.stderr if args.print_command else None)
        return 1

    chosen = select(hits)
    if chosen and args.print_command:
        print(chosen.session.resume_command)
    elif chosen:
        print(f"{BOLD}{chosen.session.label}{RESET} {DIM}· {chosen.session.agent} · {chosen.total} hit(s){RESET}")
        print(f"  {chosen.session.resume_command}")
        print(f"  {DIM}{collapse(str(chosen.session.path))}{RESET}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
