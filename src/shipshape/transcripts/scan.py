"""Read Claude Code and pi session transcripts as one searchable corpus.

Both agents append JSONL to a per-project directory, so a session is a file and a turn is a line.
The two schemas differ enough to need translating (`scan_turns`), but agree on the two things a
search needs: an ISO timestamp per line, and a role.

Nothing here parses the whole corpus: ripgrep says which transcripts contain the term, and only
those get read. That is what keeps a 600 MB corpus searchable in about a second.
"""

from __future__ import annotations

import json
import shlex
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

CLAUDE_ROOT = Path.home() / ".claude" / "projects"
PI_ROOT = Path.home() / ".pi" / "agent" / "sessions"

# Enough lines to reach a session's cwd, title and opening exchange without reading a 16 MB file.
HEAD_LINES = 400


@dataclass(frozen=True)
class Turn:
    timestamp: datetime
    role: str
    text: str


@dataclass(frozen=True)
class Session:
    agent: str
    path: Path
    session_id: str
    cwd: str
    label: str
    started: datetime

    @property
    def is_subagent(self) -> bool:
        """Claude files a delegated agent's transcript under its parent session, so the id it reports
        resumes the parent conversation, not this branch of it."""
        return self.path.parent.name == "subagents"

    @property
    def resume_command(self) -> str:
        if self.agent == "pi":
            return shlex.join(["pi", "--session", self.session_id])
        return f"cd {shlex.quote(self.cwd)} && {shlex.join(['claude', '--resume', self.session_id])}"


def roots() -> list[Path]:
    return [root for root in (CLAUDE_ROOT, PI_ROOT) if root.is_dir()]


def read_lines(path: Path, limit: int | None = None) -> list[dict]:
    """Parse a transcript's JSON lines, skipping anything that is not one.

    An agent may be appending to this file right now, so a torn final line is a normal state of the
    world rather than a fault worth crashing a search over.
    """
    records = []
    with path.open(errors="replace") as handle:
        for index, line in enumerate(handle):
            if limit is not None and index >= limit:
                break
            if line.startswith("{") and line.rstrip().endswith("}"):
                records.append(json.loads(line))
    return records


def parse_time(value: str) -> datetime:
    """Read an ISO timestamp, treating a bare date or naive time as the machine's local zone."""
    parsed = datetime.fromisoformat(value)
    return parsed.astimezone(UTC) if parsed.tzinfo else parsed.astimezone().astimezone(UTC)


def block_text(block: dict) -> str:
    """Flatten one content block, whichever of the six shapes the two agents use it is."""
    for key in ("text", "thinking"):
        if isinstance(block.get(key), str):
            return block[key]
    if block.get("type") in ("tool_use", "toolCall"):
        return f"{block.get('name', '')} {json.dumps(block.get('input') or block.get('arguments') or {})}"
    content = block.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(inner.get("text", "") for inner in content if isinstance(inner, dict))
    return ""


TOOL_BLOCKS = {"tool_result", "tool_use", "toolCall"}


def turn_role(record: dict, message: dict) -> str:
    """Collapse both schemas to user / assistant / tool, to label a turn in the preview.

    A turn counts as tool traffic only when it is nothing else. Both agents routinely put prose and
    a tool call in one message, and calling those "tool" would misattribute the model's own words.
    """
    role = message.get("role") or record.get("type")
    blocks = message.get("content")
    block_types = {block.get("type") for block in blocks if isinstance(block, dict)} if isinstance(blocks, list) else set()
    if role in ("toolResult", "tool") or (block_types and block_types <= TOOL_BLOCKS):
        return "tool"
    return "assistant" if role == "assistant" else "user"


def message_text(content: str | list | None) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(block_text(block) for block in content if isinstance(block, dict))
    return ""


def scan_turns(records: list[dict]) -> list[Turn]:
    turns = []
    for record in records:
        message = record.get("message")
        timestamp = record.get("timestamp")
        if not isinstance(message, dict) or not isinstance(timestamp, str):
            continue
        text = message_text(message.get("content")).strip()
        if text:
            # Kept whole: the caller matches against this, and a term can sit thousands of
            # characters into a turn. Trimming for display is the preview's business.
            turns.append(Turn(parse_time(timestamp), turn_role(record, message), text))
    return turns


def session_label(records: list[dict], turns: list[Turn]) -> str:
    """Name a session by the best label it carries, in order of preference.

    Claude writes itself an `aiTitle`; pi writes nothing, so the opening user turn stands in. That
    opening is a skill invocation or a wall of pasted text more often than it is a sentence, which
    is why a real title wins wherever one exists.
    """
    candidates = [record.get("aiTitle") for record in records]
    candidates += [turn.text for turn in turns if turn.role == "user"]
    return next((" ".join(text.split())[:90] for text in candidates if text), "(no user turns)")


def read_session(path: Path, agent: str) -> Session | None:
    """Describe a session from a bounded head of its transcript. None if it holds no timestamped turn."""
    head = read_lines(path, limit=HEAD_LINES)
    turns = scan_turns(head)
    if not turns:
        return None
    header = next((record for record in head if record.get("cwd")), {})
    session = next((record for record in head if record.get("type") == "session"), {})
    identity = session.get("id") or next((record["sessionId"] for record in head if record.get("sessionId")), path.stem)
    return Session(
        agent=agent,
        path=path,
        session_id=identity,
        cwd=header.get("cwd") or session.get("cwd") or "(unknown)",
        label=session_label(head, turns),
        started=turns[0].timestamp,
    )


def agent_for(path: Path) -> str:
    return "pi" if PI_ROOT in path.parents else "claude"


def matching_files(query: str) -> list[Path]:
    """Ask ripgrep which transcripts contain `query` at all, so only those get parsed.

    Escaped as a transcript stores it, because ripgrep reads raw JSON while callers match decoded
    text: a search for `"foo"` has to find `\\"foo\\"` on disk.
    """
    escaped = json.dumps(query, ensure_ascii=False)[1:-1]
    result = subprocess.run(
        ["rg", "--files-with-matches", "--ignore-case", "--fixed-strings", "--no-messages",
         "--glob", "*.jsonl", "--", escaped, *(str(root) for root in roots())],
        capture_output=True,
        text=True,
        check=False,
    )
    return [Path(line) for line in result.stdout.splitlines()]
