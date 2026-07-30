"""Docker containers behind published ports, plus containers left behind by deleted worktrees.

On macOS a container's published port is held open by Docker Desktop's own backend process, so the
PID reported for that port is Docker itself — signalling it would take down every container at
once. The only safe reading is the other way round: if a port appears in Docker's published-port
map then a container owns it, and the container is what you act on.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path
from typing import NamedTuple

from shipshape.procs import nearest_surviving

_FORMAT = '{{.Names}}\t{{.State}}\t{{.Label "com.docker.compose.project"}}\t{{.Label "com.docker.compose.project.working_dir"}}\t{{.Ports}}\t{{.Status}}'
_PUBLISHED_PORT = re.compile(r":(\d+)->")
_PROJECT_SAFE = re.compile(r"[^a-z0-9_-]")


class Container(NamedTuple):
    name: str
    running: bool
    project: str | None  # compose project, absent for containers started with plain `docker run`
    working_dir: str | None  # directory compose was last run from; only set for compose containers
    ports: tuple[int, ...]
    status: str  # docker's own wording, e.g. "Up 2 hours (healthy)" / "Exited (0) 6 weeks ago"


def available() -> bool:
    return shutil.which("docker") is not None


def _docker(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["docker", *args], capture_output=True, text=True, check=False)


def containers() -> list[Container]:
    """Every container, running or not. Empty when Docker is absent or its daemon is down."""
    if not available():
        return []
    listing = _docker("ps", "-a", "--format", _FORMAT)
    if listing.returncode != 0:
        return []
    found = []
    for line in listing.stdout.splitlines():
        name, state, project, working_dir, ports, status = line.split("\t")
        found.append(
            Container(
                name=name,
                running=state == "running",
                project=project or None,
                working_dir=working_dir or None,
                ports=tuple(sorted({int(port) for port in _PUBLISHED_PORT.findall(ports)})),
                status=status,
            ),
        )
    return found


def by_published_port(found: list[Container]) -> dict[int, Container]:
    return {port: container for container in found if container.running for port in container.ports}


def project_members(found: list[Container], project: str) -> list[Container]:
    return [container for container in found if container.project == project]


def _could_exist(project: str, working_dir: str) -> bool:
    """Whether some surviving directory near `working_dir` would produce this compose project name.

    Compose derives its default project name from the directory it runs in, and records whichever
    directory happened to start the container last. A shared project (`docker compose -p ask-modo`)
    therefore points at whatever worktree last ran `make local` — frequently one since deleted.
    That must not read as abandoned, so the project name is matched against directories that are
    still there.
    """
    anchor = nearest_surviving(Path(working_dir))
    neighbours = {anchor.name, anchor.parent.name}
    for directory in (anchor, anchor.parent):
        neighbours.update(child.name for child in directory.iterdir() if child.is_dir())
    return project in {_PROJECT_SAFE.sub("", name.lower()) for name in neighbours}


def stale(found: list[Container]) -> list[Container]:
    """Stopped compose containers whose project has nothing left on disk to belong to.

    Two independent guards keep a shared project (whose recorded directory is often a deleted
    worktree) out of this list: a sibling container still pointing at a live directory, and the
    project name still matching a directory that exists.
    """
    abandoned = []
    for container in found:
        if container.running or not container.project or not container.working_dir:
            continue
        siblings = project_members(found, container.project)
        if any(member.working_dir and Path(member.working_dir).exists() for member in siblings):
            continue
        if _could_exist(container.project, container.working_dir):
            continue
        abandoned.append(container)
    return abandoned


def compose_down_plan(project: str) -> list[str]:
    """Exactly what `docker compose -p <project> down` would do, from docker's own dry run.

    Compose reports progress on stderr, so that is where the plan arrives — not stdout.
    """
    result = _docker("compose", "-p", project, "down", "--dry-run")
    return [line.strip() for line in result.stderr.splitlines() if line.strip()]


def stop(name: str) -> subprocess.CompletedProcess[str]:
    return _docker("stop", name)


def remove(name: str) -> subprocess.CompletedProcess[str]:
    return _docker("rm", name)


def compose_down(project: str) -> subprocess.CompletedProcess[str]:
    return _docker("compose", "-p", project, "down")
