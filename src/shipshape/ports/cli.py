# ruff: noqa: T201
"""`ports` — see what is listening on this machine, and reclaim it.

A bare run lists every listening TCP port with who holds it and which repo or worktree it came
from, flagging as ORPHAN anything launched from a directory that no longer exists. Pass a port
number to go straight to it, or `--free PORT` to reclaim one with no prompting.
"""

from __future__ import annotations

import argparse
import json
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import NamedTuple

from shipshape import procs
from shipshape.ports import containers
from shipshape.ui import (
    BOLD,
    CYAN,
    DIM,
    GREEN,
    RED,
    RESET,
    YELLOW,
    choose,
    collapse,
    confirm,
    get_preview_height,
    paint,
    require,
    run_fzf,
)

# A supervisor can respawn its child once before we reach it, so one repeat is enough; more would
# mean something is fighting us and the honest move is to say so rather than keep firing.
RECLAIM_ROUNDS = 2
STOP_CONTAINER = "Stop just this container"
DOWN_PROJECT = "Bring the whole compose project down"
LABEL_WIDTH = 22
WHERE_WIDTH = 40

# Shown when the cursor sits on a heading or separator rather than on something selectable.
COLUMNS_HELP = f"""{BOLD}What the columns mean{RESET}

PORT     the TCP port being listened on. Anything
         trying to use the same one will fail to start.
PROCESS  the app, or the executable, holding it open
WHERE    the repo or worktree it was launched from, the
         container's name, or where the app is installed
STATUS   {DIM}running{RESET} · {CYAN}container{RESET} · {RED}{BOLD}ORPHAN{RESET}

{DIM}SPACE ticks a row · ENTER reviews what you ticked{RESET}
{DIM}Nothing is stopped until you confirm.{RESET}"""

DEV_HELP = f"""{BOLD}dev{RESET}
{DIM}started from a repo, or published by a container{RESET}

Ports that came from code you were working on. A
process lands here when its working directory is inside
a git repo, or when a Docker container publishes the
port.

{RED}{BOLD}ORPHAN{RESET} means the directory it was launched from no
longer exists — a worktree deleted while its server
kept running. Nothing can still be using it, so those
are always safe to stop.

{DIM}This is a group heading, not something you can select.{RESET}"""

SYSTEM_HELP = f"""{BOLD}macOS & apps{RESET}
{DIM}not from your code{RESET}

Daemons and applications started by macOS rather than
by you in a terminal. They get {BOLD}/{RESET} as their working
directory, so there is no repo to attribute them to —
that is the only reason they sit apart, not that they
matter less.

They are dimmed but still selectable; nothing is hidden
from you. Stopping one stops a real app, so the confirm
step names exactly what it is first.

{DIM}This is a group heading, not something you can select.{RESET}"""

STALE_HELP = f"""{BOLD}stale containers{RESET}
{DIM}hold no ports{RESET}

Stopped Docker containers whose compose project has
nothing left on disk to belong to — left behind by
worktrees that were deleted weeks or months ago.

They block no ports and cost you nothing but disk, so
this is tidying rather than firefighting. A shared
project whose recorded directory merely happens to be a
deleted worktree is {BOLD}not{RESET} stale and never appears here.

{DIM}This is a group heading, not something you can select.{RESET}"""


class Entry(NamedTuple):
    port: int
    label: str
    where: str
    status: str
    from_code: bool  # launched from a git repo, or published by a container — as opposed to by macOS
    process: procs.Process | None
    location: procs.Location | None
    container: containers.Container | None

    @property
    def is_orphan(self) -> bool:
        return self.location is not None and self.location.is_orphan


class Plan(NamedTuple):
    ports: list[Entry]
    stop: list[containers.Container]
    down: list[str]
    remove: list[containers.Container]

    def is_empty(self) -> bool:
        return not (self.ports or self.stop or self.down or self.remove)


def describe(location: procs.Location) -> str:
    if location.path is None:
        return "unknown"
    if location.is_orphan:
        return f"{location.worktree or Path(location.path).name} (deleted)"
    if location.repo and location.worktree:
        return f"{location.repo}/{location.worktree}"
    return location.repo or collapse(location.path)


def provenance(process: procs.Process) -> str:
    """Where a non-repo process came from — its .app bundle, or the directory of its binary."""
    bundle = procs.app_bundle(process.executable)
    return collapse(str(bundle or Path(process.executable).parent))


def inventory() -> tuple[list[Entry], list[containers.Container]]:
    all_containers = containers.containers()
    published = containers.by_published_port(all_containers)
    table = procs.process_table()
    entries = []
    for listener in procs.listeners():
        container = published.get(listener.port)
        if container is not None:
            entries.append(
                Entry(
                    port=listener.port,
                    label="docker",
                    where=container.name,
                    status="container",
                    from_code=True,
                    process=None,
                    location=None,
                    container=container,
                ),
            )
            continue
        process = table.get(listener.pid)
        if process is None:
            continue  # exited between listing sockets and listing processes
        location = procs.locate(process.cwd)
        entries.append(
            Entry(
                port=listener.port,
                label=procs.label(process),
                where=describe(location) if location.repo or location.is_orphan else provenance(process),
                status="ORPHAN" if location.is_orphan else ("running" if location.repo else ""),
                from_code=bool(location.is_orphan or location.repo),
                process=process,
                location=location,
                container=None,
            ),
        )
    entries.sort(key=lambda entry: (not entry.from_code, not entry.is_orphan, entry.port))
    return entries, containers.stale(all_containers)


def fit(text: str, width: int) -> str:
    """Pad or shorten to exactly `width`, keeping the tail of a path — the distinctive end — and
    the head of anything else."""
    if len(text) <= width:
        return text.ljust(width)
    if text.startswith(("/", "~")):
        return f"…{text[-(width - 1):]}"
    return f"{text[: width - 1]}…"


def format_row(entry: Entry, widths: tuple[int, int]) -> str:
    label_width, where_width = widths
    colour = RED if entry.is_orphan else ("" if entry.from_code else DIM)
    status = {"ORPHAN": f"{RED}{BOLD}ORPHAN{RESET}", "container": f"{CYAN}container{RESET}"}.get(
        entry.status,
        paint(entry.status, DIM),
    )
    return (
        f"{paint(f'{entry.port:>5}', colour)}  "
        f"{paint(fit(entry.label, label_width), colour)}  "
        f"{paint(fit(entry.where, where_width), colour)}  {status}"
    )


def format_stale_row(container: containers.Container) -> str:
    return f"{DIM}{'':>5}  {'docker':<8}  {container.name}  ·  {container.status}{RESET}"


def port_preview(entry: Entry, all_containers: list[containers.Container]) -> str:
    lines = [f"{BOLD}Port {entry.port}{RESET}"]
    if entry.container is not None:
        lines.extend(container_preview(entry.container, all_containers))
    else:
        lines.extend(process_preview(entry))
    return "\n".join(lines)


def process_preview(entry: Entry) -> list[str]:
    process = entry.process
    location = entry.location
    if process is None or location is None:
        return []
    chain = procs.launch_chain(process.pid, procs.process_table())
    lines = [
        f"{DIM}{'a leftover process — safe to stop' if entry.is_orphan else 'a live process'}{RESET}",
        "",
        "Process",
        f"  {process.command[:160]}",
        f"  pid {process.pid}",
        "",
        "Launched from",
        f"  {location.path or 'unknown'}",
    ]
    if location.is_orphan:
        lines.append(f"  {RED}this directory no longer exists{RESET}")
    if location.repo:
        lines.append(f"  repo {location.repo}" + (f" · worktree {location.worktree}" if location.worktree else ""))
    if location.branch:
        lines.append(f"  branch {location.branch}")
    lines.extend(["", "Stopping it"])
    if len(chain) == 1:
        lines.append(f"  stop pid {process.pid}, then check the port is actually free")
    else:
        supervisors = ", ".join(f"{procs.label(parent)} ({parent.pid})" for parent in chain[1:])
        lines.extend(
            [
                f"  stop pid {process.pid} first, then check the port is actually free",
                f"  if it respawns, escalate to its supervisor: {supervisors}",
            ],
        )
    lines.append(f"  {DIM}force-kills only if asking nicely provably failed{RESET}")
    if entry.is_orphan:
        lines.extend(["", "Why safe", f"  {GREEN}nothing can be using it — its worktree is gone{RESET}"])
    else:
        lines.extend(["", f"{YELLOW}This is live. Something you are using may stop working.{RESET}"])
    return lines


def container_preview(container: containers.Container, all_containers: list[containers.Container]) -> list[str]:
    lines = [
        f"{DIM}a Docker container, not a normal process{RESET}",
        "",
        f"  {YELLOW}The PID listening on this port is Docker Desktop itself.{RESET}",
        f"  {YELLOW}Signalling it would take down every container, so this{RESET}",
        f"  {YELLOW}acts on the container instead.{RESET}",
        "",
        "Container",
        f"  {container.name}  ·  {container.status}",
        f"  publishes {', '.join(str(port) for port in container.ports)}",
        "",
    ]
    if container.project is None:
        lines.extend(
            [
                "Options",
                f"  docker stop {container.name}",
                f"  {DIM}started with plain `docker run`, so there is no compose{RESET}",
                f"  {DIM}project to bring down — stopping is the only action{RESET}",
                "",
                "Undo",
                f"  docker start {container.name}",
            ],
        )
        return lines
    members = containers.project_members(all_containers, container.project)
    plan = containers.compose_down_plan(container.project)
    lines.extend(
        [
            f"Compose project  {container.project}",
            f"  {len(members)} container(s): {', '.join(member.name for member in members)}",
            f"  {YELLOW}shared — anything using this project is affected{RESET}",
            "",
            "Options",
            f"  1  docker stop {container.name}      (undo: docker start {container.name})",
            f"  2  docker compose -p {container.project} down",
        ],
    )
    lines.extend(f"     {DIM}{step}{RESET}" for step in plan[:12])
    return lines


def stale_preview(container: containers.Container) -> str:
    return "\n".join(
        [
            f"{BOLD}{container.name}{RESET}",
            f"{DIM}a stopped container whose worktree is gone{RESET}",
            "",
            "State",
            f"  {container.status}",
            "  holds no ports",
            "",
            "Compose project",
            f"  {container.project}",
            f"  last run from {container.working_dir}",
            f"  {RED}that directory no longer exists{RESET}",
            "",
            "Removing it",
            f"  docker rm {container.name}",
            "",
            "Why safe",
            f"  {GREEN}no surviving directory maps to this project, and no{RESET}",
            f"  {GREEN}sibling container points anywhere that still exists{RESET}",
            f"  {DIM}named volumes are not removed by `docker rm`{RESET}",
        ],
    )


def port_key(port: int) -> str:
    return f"p{port}"


def container_key(name: str) -> str:
    return f"c{name}"


def render(snapshot: Path) -> tuple[list[str], int]:
    """Take a fresh reading and lay it out: fzf rows, plus previews written to `snapshot`.

    Rows are keyed by port (or container name) rather than by position, so that refreshing mid-session
    cannot leave a selection pointing at whatever now happens to sit in that row. Headings and
    separators are rows too — fzf has no notion of an unselectable line — and carry their own preview
    explaining the group, keyed to something no selection will ever resolve to.
    """
    entries, stale = inventory()
    all_containers = containers.containers()
    widths = (
        max(len("PROCESS"), min(LABEL_WIDTH, max((len(entry.label) for entry in entries), default=0))),
        max(len("WHERE"), min(WHERE_WIDTH, max((len(entry.where) for entry in entries), default=0))),
    )
    rows: list[str] = []
    previews: dict[str, str] = {}

    def add(key: str, row: str, preview: str) -> None:
        rows.append(f"{key}\t{row}")
        previews[key] = preview

    dev = [entry for entry in entries if entry.from_code]
    other = [entry for entry in entries if not entry.from_code]
    add("-", f"{DIM}{'PORT':>5}  {'PROCESS':<{widths[0]}}  {'WHERE':<{widths[1]}}  STATUS{RESET}", COLUMNS_HELP)
    if dev:
        add("-dev", f"{DIM}── dev · {len(dev)} · started from a repo, or a container ─────{RESET}", DEV_HELP)
        for entry in dev:
            add(port_key(entry.port), format_row(entry, widths), port_preview(entry, all_containers))
    if other:
        add("-system", f"{DIM}── macOS & apps · {len(other)} · not from your code ─────{RESET}", SYSTEM_HELP)
        for entry in other:
            add(port_key(entry.port), format_row(entry, widths), port_preview(entry, all_containers))
    if stale:
        add("-stale", f"{DIM}── stale containers · {len(stale)} · hold no ports ─────{RESET}", STALE_HELP)
        for container in stale:
            add(container_key(container.name), format_stale_row(container), stale_preview(container))

    # Replaced atomically: fzf runs the preview command concurrently with a refresh, so the file must
    # never be observed half-written.
    working = snapshot.with_suffix(".writing")
    working.write_text(json.dumps(previews))
    working.replace(snapshot)
    return rows, get_preview_height(previews.values())


def resolve(keys: list[str]) -> tuple[list[Entry], list[containers.Container], list[str]]:
    """Match ticked keys against a fresh reading, so nothing is acted on from a stale snapshot."""
    entries, stale = inventory()
    by_port = {port_key(entry.port): entry for entry in entries}
    by_name = {container_key(container.name): container for container in stale}
    picked_entries, picked_stale, vanished = [], [], []
    for key in keys:
        if key.startswith("-"):
            continue  # a heading or separator, which fzf will happily let you tick
        if key in by_port:
            picked_entries.append(by_port[key])
        elif key in by_name:
            picked_stale.append(by_name[key])
        else:
            vanished.append(f":{key[1:]}" if key.startswith("p") else key[1:])
    return picked_entries, picked_stale, vanished


def pause() -> None:
    """Hold the outcome on screen until acknowledged, before the list is drawn over it again."""
    print(f"{DIM}ENTER to return to the list…{RESET}", end="", flush=True)
    sys.stdin.readline()
    print("\r\033[2K", end="", flush=True)


def select(snapshot: Path) -> tuple[list[str], bool]:
    """Show the inventory in fzf; return the keys that were ticked and whether the user backed out."""
    rows, preview_height = render(snapshot)
    refresh = shlex.join([sys.executable, "-m", "shipshape.ports.cli", "rows", str(snapshot)])
    result = run_fzf(
        rows,
        prompt="select> ",
        header="SPACE select · CTRL-A all · CTRL-D clear · CTRL-R refresh · ENTER review · ESC quit",
        preview=shlex.join([sys.executable, "-m", "shipshape.ports.cli", "preview", str(snapshot)]) + " {1}",
        preview_height=preview_height,
        extra_binds=[f"ctrl-r:reload({refresh})"],
    )
    return [row.split("\t", 1)[0] for row in result.rows], result.aborted


def build_plan(entries: list[Entry], stale: list[containers.Container], all_containers: list[containers.Container]) -> Plan | None:
    """Turn a selection into concrete actions, asking how to treat each compose container."""
    plan = Plan(ports=[], stop=[], down=[], remove=list(stale))
    for entry in entries:
        if entry.container is None:
            plan.ports.append(entry)
            continue
        container = entry.container
        if container.project is None:
            plan.stop.append(container)
            continue
        members = containers.project_members(all_containers, container.project)
        answer = choose(
            f"Port {entry.port} · {container.name} (compose project {container.project})",
            [STOP_CONTAINER, f"{DOWN_PROJECT} — {len(members)} container(s)"],
        )
        if answer is None:
            return None
        if answer.startswith(DOWN_PROJECT):
            if container.project not in plan.down:
                plan.down.append(container.project)
        else:
            plan.stop.append(container)
    return plan


def summarise(plan: Plan) -> str:
    parts = []
    live = [entry for entry in plan.ports if not entry.is_orphan]
    if plan.ports:
        orphans = len(plan.ports) - len(live)
        detail = f"{orphans} orphaned" if orphans else ""
        detail += (", " if detail and live else "") + (f"{len(live)} LIVE" if live else "")
        parts.append(f"stop {len(plan.ports)} process group(s) ({detail})")
    if plan.stop:
        parts.append(f"stop {len(plan.stop)} container(s)")
    if plan.down:
        parts.append(f"compose down {len(plan.down)} project(s)")
    if plan.remove:
        parts.append(f"remove {len(plan.remove)} stale container(s)")
    return "; ".join(parts) + "?"


def reclaim(port: int, *, whole_chain: bool) -> list[procs.Step]:
    """Free a port, returning the signals it took. Never read the result as proof — no steps can also
    mean the holder is one we refuse to signal. Ask `procs.port_is_free` instead.

    Rounds exist because a supervisor can start a replacement child in the moment before it is
    itself stopped, leaving a brand new PID on the port.
    """
    steps: list[procs.Step] = []
    for _ in range(RECLAIM_ROUNDS):
        current = procs.holders(port)
        if not current:
            break
        table = procs.process_table()
        for pid in current:
            if pid in table:
                outcome = procs.stop_chain(
                    pid,
                    lambda: not procs.holders(port),
                    table,
                    whole_chain=whole_chain,
                )
                steps.extend(outcome.steps)
    return steps


def report_port(port: int, steps: list[procs.Step]) -> bool:
    freed = procs.port_is_free(port)
    for index, step in enumerate(steps):
        last = index == len(steps) - 1
        note = f"{GREEN}port free{RESET}" if freed and last else f"{DIM}port still held{RESET}"
        print(f"  {step.signal:<4} {step.label} ({step.pid})  {note}")
    colour = GREEN if freed else RED
    print(f"  {colour}port {port} is {'now free' if freed else 'STILL HELD'}{RESET}")
    return freed


def execute(plan: Plan) -> int:
    failures = 0
    for entry in plan.ports:
        print(f"{BOLD}Port {entry.port}{RESET} · {entry.label}")
        failures += not report_port(entry.port, reclaim(entry.port, whole_chain=entry.is_orphan))
    for container in plan.stop:
        result = containers.stop(container.name)
        failures += _report_docker(f"docker stop {container.name}", result, f"undo: docker start {container.name}")
    for project in plan.down:
        result = containers.compose_down(project)
        failures += _report_docker(f"docker compose -p {project} down", result, "")
    for container in plan.remove:
        result = containers.remove(container.name)
        failures += _report_docker(f"docker rm {container.name}", result, "")
    return failures


def _report_docker(description: str, result: subprocess.CompletedProcess[str], note: str) -> bool:
    if result.returncode == 0:
        print(f"{GREEN}✓{RESET} {description}" + (f"  {DIM}{note}{RESET}" if note else ""))
        return False
    print(f"{RED}✗ {description}{RESET}")
    print(f"  {result.stderr.strip()}")
    return True


def show_one(port: int, entries: list[Entry], all_containers: list[containers.Container]) -> int:
    """The focused view: `ports 8000`."""
    matches = [entry for entry in entries if entry.port == port]
    if not matches:
        print(f"{GREEN}Port {port} is free.{RESET}")
        return 0
    for entry in matches:
        print(port_preview(entry, all_containers))
        print()
    plan = build_plan(matches, [], all_containers)
    if plan is None or plan.is_empty():
        print(f"{DIM}Cancelled. Nothing changed.{RESET}")
        return 0
    if not confirm(summarise(plan)):
        print(f"{DIM}Cancelled. Nothing changed.{RESET}")
        return 0
    return int(execute(plan) > 0)


def free(port: int, *, force: bool) -> int:
    """The headless path: `ports --free 8000`. Only provable orphans go without a human."""
    entries, _ = inventory()
    matches = [entry for entry in entries if entry.port == port]
    if not matches:
        print(f"port {port} is already free")
        return 0
    blocked = [entry for entry in matches if not entry.is_orphan]
    if blocked and not force:
        for entry in blocked:
            owner = entry.container.name if entry.container else f"{entry.label} ({entry.process.pid})"
            print(f"port {port} is held by something LIVE — refusing.")
            print(f"  {owner}")
            print(f"  from {entry.where}")
        print("  use --force to stop it anyway, or run `ports` to decide interactively")
        return 1
    failures = 0
    for entry in matches:
        if entry.container is not None:
            result = containers.stop(entry.container.name)
            failures += _report_docker(f"docker stop {entry.container.name}", result, "")
            # A zero exit only says docker accepted the stop; the exit code here is what scripts act
            # on, so it has to reflect the port itself.
            if not procs.port_is_free(entry.port):
                print(f"port {port} is STILL HELD after stopping {entry.container.name}")
                failures += 1
            continue
        steps = reclaim(entry.port, whole_chain=entry.is_orphan)
        stopped = ", ".join(f"{step.label} ({step.pid})" for step in steps)
        if not procs.port_is_free(entry.port):
            print(f"port {port} is STILL HELD after stopping {stopped or 'nothing'}")
            failures += 1
        else:
            warning = "  ⚠ this was a live process" if not entry.is_orphan else ""
            print(f"freed :{port} — stopped {stopped}{warning}")
    return int(failures > 0)


def main() -> int:
    # Two hidden subcommands fzf calls back into: one per preview, one per CTRL-R refresh.
    if len(sys.argv) == 4 and sys.argv[1] == "preview":
        previews = json.loads(Path(sys.argv[2]).read_text())
        print(previews.get(sys.argv[3], ""))
        return 0
    if len(sys.argv) == 3 and sys.argv[1] == "rows":
        rows, _ = render(Path(sys.argv[2]))
        print("\n".join(rows))
        return 0

    parser = argparse.ArgumentParser(prog="ports", description="See what is listening, and reclaim it.")
    parser.add_argument("port", nargs="?", type=int, help="jump straight to one port")
    parser.add_argument("--free", type=int, metavar="PORT", help="reclaim a port with no prompting")
    parser.add_argument("--force", action="store_true", help="with --free, also stop live processes")
    arguments = parser.parse_args()

    if arguments.free is not None:
        return free(arguments.free, force=arguments.force)

    require("fzf", "gum")
    if not sys.stdin.isatty():
        # fzf and gum both block waiting on /dev/tty, so without one this would hang rather than fail.
        print("ports needs a terminal. From a script or an agent, use: ports --free PORT", file=sys.stderr)
        return 1
    if arguments.port is not None:
        print(f"{DIM}Checking listening ports…{RESET}", end="", flush=True)
        entries, _ = inventory()
        all_containers = containers.containers()
        print("\r\033[2K", end="", flush=True)
        return show_one(arguments.port, entries, all_containers)

    failures = 0
    with tempfile.TemporaryDirectory() as scratch:
        snapshot = Path(scratch) / "previews.json"
        while True:
            print(f"{DIM}Checking listening ports…{RESET}", end="", flush=True)
            keys, aborted = select(snapshot)
            print("\r\033[2K", end="", flush=True)
            if aborted:
                return int(failures > 0)  # ESC — the way out
            picked_entries, picked_stale, vanished = resolve(keys)
            if vanished:
                print(f"{DIM}gone since you looked: {', '.join(vanished)}{RESET}")
            if not picked_entries and not picked_stale:
                continue  # only a heading was ticked; nothing to act on
            plan = build_plan(picked_entries, picked_stale, containers.containers())
            if plan is None or plan.is_empty() or not confirm(summarise(plan)):
                print(f"{DIM}Cancelled. Nothing changed.{RESET}")
                continue
            failures += execute(plan)
            pause()


if __name__ == "__main__":
    raise SystemExit(main())
