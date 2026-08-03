"""Processes, listening sockets, and where each process was launched from. No terminal UI here.

The load-bearing fact: a process keeps its working directory even after that directory is deleted,
and the kernel still reports the stale path. So a listener whose working directory no longer exists
is a provable orphan — something left running by a worktree that has since been removed.
"""

from __future__ import annotations

import os
import signal
import subprocess
import time
from functools import cache
from pathlib import Path
from typing import TYPE_CHECKING, NamedTuple

if TYPE_CHECKING:
    from collections.abc import Callable

# Never signalled, whether it holds the port itself or merely supervises whatever does. Shells and
# multiplexers are the user's terminal tab or agent session. Docker's backend is the process the
# kernel credits with every container's published port, so signalling it stops every container at
# once — and it is what a port resolves to whenever Docker cannot be asked which container owns it.
NEVER_SIGNAL = frozenset(
    {
        "bash", "csh", "dash", "fish", "ksh", "login", "screen", "sh", "tcsh", "tmux", "zsh",
        "com.docker.backend", "com.docker.vpnkit", "vpnkit",
    },
)  # fmt: skip
# Argv[0] basenames that say nothing on their own, so the label borrows the first real argument.
INTERPRETERS = frozenset({"Python", "node", "perl", "python", "python3", "ruby", "uv", "uvx"})
SETTLE_SECONDS = 3.0
# A supervisor takes a moment to notice its child died and start a new one, so a port can look free
# while it is about to be retaken. Success is only success if it survives this long.
CONFIRM_SECONDS = 1.0
POLL_SECONDS = 0.1


class Process(NamedTuple):
    pid: int
    ppid: int
    executable: str  # path to the binary, free of arguments — many contain spaces, so never re-split a command
    command: str
    cwd: str | None  # None when the kernel won't tell us (processes owned by another user)


class Listener(NamedTuple):
    port: int
    pid: int


class Location(NamedTuple):
    """Where a process was launched from, resolved against git."""

    path: str | None
    exists: bool
    repo: str | None  # main-checkout directory name, e.g. "ask-modo"
    worktree: str | None  # worktree directory name, when the path is a worktree rather than the checkout
    branch: str | None

    @property
    def is_orphan(self) -> bool:
        return self.path is not None and not self.exists


class Step(NamedTuple):
    """One signal sent while reclaiming."""

    pid: int
    label: str
    signal: str


class Outcome(NamedTuple):
    steps: list[Step]
    cleared: bool


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, capture_output=True, text=True, check=False)


def label(process: Process) -> str:
    """A short human name: the enclosing .app bundle, else the executable — and for bare
    interpreters, the first real argument, since "python" alone identifies nothing."""
    bundle = app_bundle(process.executable)
    if bundle is not None:
        return bundle.stem
    name = Path(process.executable).name
    if name not in INTERPRETERS:
        return name
    arguments = process.command.split()[1:]
    detail = next((Path(argument).name for argument in arguments if not argument.startswith("-")), None)
    return f"{name} {detail}" if detail else name


def app_bundle(executable: str) -> Path | None:
    """The innermost `.app` directory containing an executable, if it lives in one."""
    return next((parent for parent in Path(executable).parents if parent.suffix == ".app"), None)


def listeners() -> list[Listener]:
    """(port, pid) for every listening TCP socket, deduped across the IPv4 and IPv6 entries."""
    found: set[Listener] = set()
    pid = None
    for line in _run("lsof", "-nP", "-iTCP", "-sTCP:LISTEN", "-Fpn").stdout.splitlines():
        if line.startswith("p"):
            pid = int(line[1:])
        elif line.startswith("n") and pid is not None:
            port = line.rsplit(":", 1)[-1]
            if port.isdigit():
                found.add(Listener(int(port), pid))
    return sorted(found)


def holders(port: int) -> list[int]:
    """PIDs currently listening on one port. Empty means the port is free.

    Asks about the single port rather than filtering a full socket listing — this runs on a poll loop
    while a port is being reclaimed, so the wasted scans would dominate.
    """
    output = _run("lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-t").stdout
    return sorted({int(pid) for pid in output.split()})


def _working_directories() -> dict[int, str]:
    directories: dict[int, str] = {}
    pid = None
    for line in _run("lsof", "-a", "-d", "cwd", "-Fn").stdout.splitlines():
        if line.startswith("p"):
            pid = int(line[1:])
        elif line.startswith("n") and pid is not None:
            directories[pid] = line[1:]
    return directories


def _executables() -> dict[int, str]:
    """Executable paths, read separately from the command line because both can contain spaces and
    only one unsplittable field per `ps` call can be parsed unambiguously."""
    executables = {}
    for line in _run("ps", "-Ao", "pid=,comm=").stdout.splitlines():
        pid, executable = line.split(maxsplit=1)
        executables[int(pid)] = executable
    return executables


def process_table() -> dict[int, Process]:
    """Every process, with parent, executable, full command, and working directory. Deliberately
    uncached — callers re-read it between rounds of signalling."""
    directories = _working_directories()
    executables = _executables()
    table = {}
    for line in _run("ps", "-Ao", "pid=,ppid=,command=").stdout.splitlines():
        pid, ppid, command = line.split(maxsplit=2)
        table[int(pid)] = Process(
            pid=int(pid),
            ppid=int(ppid),
            executable=executables.get(int(pid), command),
            command=command,
            cwd=directories.get(int(pid)),
        )
    return table


def running_executable(pid: int) -> str | None:
    """What a PID is running right now, or None if it is gone.

    One call answers both "is it still alive" and "is it still the thing we meant", which matters
    because a PID that exits can be reissued to something else — including a shell.
    """
    return _run("ps", "-p", str(pid), "-o", "comm=").stdout.strip() or None


def alive(pid: int) -> bool:
    return running_executable(pid) is not None


def protected(executable: str | None) -> bool:
    """Whether signalling this executable would take down the terminal session or the Docker engine."""
    return executable is not None and Path(executable).name.lstrip("-") in NEVER_SIGNAL


def _git(directory: Path, *args: str) -> str | None:
    result = _run("git", "-C", str(directory), *args)
    return result.stdout.strip() if result.returncode == 0 else None


def _worktree_records(repo: Path) -> list[Location]:
    """Parse `git worktree list --porcelain`, which still reports worktrees whose directory was
    deleted (flagged `prunable`) along with the branch they had checked out."""
    porcelain = _git(repo, "worktree", "list", "--porcelain")
    if porcelain is None:
        return []
    records: list[Location] = []
    path, branch = None, None
    for line in [*porcelain.splitlines(), ""]:
        if line.startswith("worktree "):
            path, branch = line.removeprefix("worktree "), None
        elif line.startswith("branch "):
            branch = line.removeprefix("branch ").removeprefix("refs/heads/")
        elif not line and path:
            checkout = Path(path)
            main = records[0].path if records else path
            records.append(
                Location(
                    path=path,
                    exists=checkout.exists(),
                    repo=Path(main).name,
                    worktree=None if path == main else checkout.name,
                    branch=branch,
                ),
            )
            path, branch = None, None
    return records


def nearest_surviving(path: Path) -> Path:
    """The closest ancestor of a (possibly deleted) path that still exists on disk."""
    return next(candidate for candidate in [path, *path.parents] if candidate.exists())


@cache
def _worktree_index(anchor: str) -> dict[str, Location]:
    """Map every worktree path (deleted ones included) to its repo and branch, for repos found
    beside a given anchor directory. Main checkouts are visited first so that listing one repo
    covers all of its worktrees in a single git call."""
    anchor_path = Path(anchor)
    search = [anchor_path.parent, anchor_path]
    repos = [directory for directory in search if (directory / ".git").exists()]
    for directory in search:
        repos.extend(child for child in sorted(directory.iterdir()) if (child / ".git").exists())
    index: dict[str, Location] = {}
    for repo in repos:
        if str(repo) in index:
            continue
        for record in _worktree_records(repo):
            index.setdefault(record.path, record)
    return index


def locate(cwd: str | None) -> Location:
    """Resolve a working directory to repo, worktree, and branch — even if it has been deleted."""
    if cwd is None:
        return Location(None, exists=False, repo=None, worktree=None, branch=None)
    path = Path(cwd)
    if not path.exists():
        index = _worktree_index(str(nearest_surviving(path)))
        match = next((index[known] for known in index if cwd == known or cwd.startswith(f"{known}/")), None)
        # Cleanly removed worktrees leave no git record, so fall back to naming the directory itself.
        return match or Location(cwd, exists=False, repo=None, worktree=path.name, branch=None)
    toplevel = _git(path, "rev-parse", "--show-toplevel")
    if not toplevel:
        return Location(cwd, exists=True, repo=None, worktree=None, branch=None)
    common = _git(path, "rev-parse", "--path-format=absolute", "--git-common-dir")
    checkout = Path(common).parent if common else Path(toplevel)
    return Location(
        path=cwd,
        exists=True,
        repo=checkout.name,
        worktree=None if Path(toplevel) == checkout else Path(toplevel).name,
        branch=_git(path, "branch", "--show-current") or None,
    )


def launch_chain(pid: int, table: dict[int, Process]) -> list[Process]:
    """A process and the ancestors that supervise it: same working directory, not a shell.

    `fastapi dev` and `next dev` hold the port in a child process and respawn it the moment it
    dies, so freeing a port often means stopping a parent — but never so far up that we reach the
    shell that launched it.
    """
    chain = [table[pid]]
    while True:
        parent = table.get(chain[-1].ppid)
        if parent is None or parent.pid <= 1 or parent.cwd != chain[0].cwd:
            return chain
        if protected(parent.executable):
            return chain
        chain.append(parent)


def _keeps_holding(is_clear: Callable[[], bool]) -> bool:
    """Whether an already-true condition stays true, rather than flickering true mid-respawn."""
    deadline = time.monotonic() + CONFIRM_SECONDS
    while time.monotonic() < deadline:
        time.sleep(POLL_SECONDS)
        if not is_clear():
            return False
    return True


def _settles(is_clear: Callable[[], bool], target: int | None = None) -> bool:
    """Wait for a condition to hold after signalling, and to keep holding.

    Gives up the moment `target` is gone while the condition still fails: something has already
    replaced it, so waiting out the full timeout only delays escalating to whatever did that.
    """
    deadline = time.monotonic() + SETTLE_SECONDS
    while time.monotonic() < deadline:
        if is_clear() and _keeps_holding(is_clear):
            return True
        if target is not None and not alive(target):
            return False
        time.sleep(POLL_SECONDS)
    return False


def port_is_free(port: int) -> bool:
    """Free, and still free a moment later. Never trust a single look at a port being reclaimed."""
    return not holders(port) and _keeps_holding(lambda: not holders(port))


def stop_chain(
    pid: int,
    is_clear: Callable[[], bool],
    table: dict[int, Process],
    *,
    whole_chain: bool = False,
) -> Outcome:
    """Stop `pid` politely, force-killing only once asking nicely has demonstrably failed.

    By default this escalates as little as possible: the process itself, then one supervising
    ancestor at a time, stopping the moment `is_clear()` holds. That is right for something still in
    use — take down no more of a working setup than the job requires.

    `whole_chain` signals the entire launch chain instead, for when every process in it is known to
    be leftover: nothing is gained by leaving a supervisor behind whose worktree has been deleted.

    Anything in `NEVER_SIGNAL` is skipped even when it is the target itself, so an uncleared outcome
    can mean "refused" as well as "would not die". Callers must not read no-steps as success.
    """
    chain = launch_chain(pid, table)
    steps: list[Step] = []
    for stage in (signal.SIGTERM, signal.SIGKILL):
        signalled = False
        for process in chain:
            # Judged on what the PID is running *now*, not on what it was when the list was drawn:
            # it may have exited, and its number may have been reissued to a shell.
            current = running_executable(process.pid)
            if current is None or protected(current):
                continue
            os.kill(process.pid, stage)
            steps.append(Step(process.pid, label(process), stage.name.removeprefix("SIG")))
            signalled = True
            if not whole_chain and _settles(is_clear, target=process.pid):
                return Outcome(steps, cleared=True)
        if signalled and whole_chain and _settles(is_clear):
            return Outcome(steps, cleared=True)
    return Outcome(steps, cleared=is_clear())
