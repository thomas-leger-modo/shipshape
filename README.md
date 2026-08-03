# shipshape

Four small terminal tools for keeping a machine, a repo, and a task board tidy after PRs move:

- **`ports`** — see what is listening on every TCP port, which repo or worktree it came from, and
  whether a Docker container owns it. Flags anything launched from a directory that no longer exists
  as an orphan, and reclaims what you pick.
- **`prune-local-branches`** — find local branches and worktrees fully merged into the trunk and
  safe to delete, then remove the ones you pick. Read-only until you confirm; re-checks safety
  immediately before deleting.
- **`search-transcripts`** — find which past Claude Code or pi conversation mentioned something,
  and get the command to reopen it.
- **`update-tickets`** — reconcile the status of your Notion task-board tickets against the real
  state of their linked GitHub PRs and production deployments, and apply the changes you approve.

All four are interactive `fzf`/`gum` pickers with previews. Nothing is changed without an explicit
confirmation.

## Requirements

- macOS — `ports` reads the process table via macOS `lsof`/`ps` flags and recognises `.app` bundles
- Python ≥ 3.12 and [`uv`](https://docs.astral.sh/uv/)
- [`fzf`](https://github.com/junegunn/fzf) and [`gum`](https://github.com/charmbracelet/gum)
  (`brew install fzf gum`)
- [`gh`](https://cli.github.com), authenticated (`gh auth login`) — used for PR and deployment state
- [`ripgrep`](https://github.com/BurntSushi/ripgrep) (`brew install ripgrep`) — used by
  `search-transcripts`
- Docker, optionally — without it `ports` simply reports no containers

## Install

```bash
uv tool install git+https://github.com/thomas-leger-modo/shipshape
```

This puts `ports`, `prune-local-branches`, `search-transcripts` and `update-tickets` on your PATH.

To hack on it locally, install from a clone in editable mode:

```bash
uv tool install --editable ~/code/shipshape
```

## `ports`

No configuration. Run it anywhere:

```bash
ports                  # list everything listening, pick what to reclaim
ports 8000             # go straight to one port
ports --free 8000      # reclaim it with no prompting (orphans only)
ports --free 8000 --force   # also allowed to stop something still live
```

### What it tells you

Every listening TCP port, in two groups. **dev** is anything launched from a git repo or published
by a Docker container — the ports that came from code you were working on. **macOS & apps** is
everything else: daemons and apps launched by the system, which get `/` as their working directory
and so belong to no repo. They are dimmed but still visible and still selectable — nothing is
hidden. For each port you get the process, the repo or worktree it was launched from, and whether
Docker owns it.

A port is marked **ORPHAN** when the directory its process was launched from no longer exists. A
process keeps its working directory even after that directory is deleted, so this is proof — not a
guess — that the worktree behind it is gone. Where git still remembers the removed worktree, the
repo and branch are recovered too.

Acting on something returns you to the list rather than quitting, so you can clear several things in
one sitting. The outcome stays on screen until you press ENTER. **`ESC` is how you leave** — and it is
the only thing that does, so a filter that happens to match nothing will not drop you back to the
shell.

`CTRL-R` re-reads everything without leaving the picker — useful when you have just stopped something
in another terminal, or are waiting for a server to come up. Refreshing clears any ticks.

Rows are identified by port rather than by position, and whatever you tick is matched against a fresh
reading at the moment you confirm. So a refresh can never leave a selection pointing at whatever has
since moved into that row, and a process that exited while you were looking is reported as gone
rather than acted on by a stale PID.

### Reclaiming

Stopping the process that holds a port is often not enough: `fastapi dev` and `next dev` hold the
port in a child process and start a replacement the moment it dies. So `ports` asks the child to
stop, checks whether the port is *actually and durably* free, and only then walks one step up to the
supervisor and tries again — never so far up that it reaches the shell that launched it. Force-kills
only once asking nicely has demonstrably failed.

An orphan is treated differently from something live: with the worktree gone, the whole launch chain
is leftover and all of it is stopped. Something still live gets the smallest intervention that frees
the port.

### Docker

A container's published port is held open by Docker Desktop's own backend process, so the PID
reported for that port is Docker itself — signalling it would take down every container at once.
`ports` recognises container-owned ports and acts on the container instead, offering `docker stop`
or, for a compose project, `docker compose -p <project> down` with the exact blast radius taken from
docker's own dry run.

Stopped containers whose compose project has nothing left on disk to belong to are listed separately
as stale, and can be removed. A shared project whose recorded directory happens to be a deleted
worktree is not stale, and is never offered.

### Non-interactive use

`--free PORT` stops only provable orphans. If something live holds the port it refuses, names the
owner, and exits non-zero — so a script or an agent can always reclaim its own mess and can never
steal a server you are using. `--force` lifts that restriction.

## `prune-local-branches`

No configuration. Run it inside any git repo:

```bash
prune-local-branches
```

The trunk is auto-detected from the remote's default branch (`origin/HEAD`), falling back to
`origin/main`, so `main`, `master`, etc. all work. A branch/worktree is only offered when it has no
uncommitted or untracked changes **and** is fully merged into the trunk.

## `search-transcripts`

No configuration. Run it anywhere:

```bash
search-transcripts MEN-13249
search-transcripts "program design gate"
```

It searches every Claude Code transcript (`~/.claude/projects/`, including the ones written by
delegated subagents) and every pi transcript (`~/.pi/agent/sessions/`) — around 600 MB here, in
about a second, because ripgrep decides which files are worth parsing and only those are read.

Results are grouped by conversation, not listed as matching lines, and ranked by how many turns
mention the term: the session that says it a hundred times is the one you want, not the twelve that
say it once. Each row shows the agent, when the conversation started, the match count and its title
— Claude's own where it has one, otherwise the opening user turn. The preview lists the matching
turns with timestamps, and `ENTER` prints the command to reopen the conversation plus the path to
its transcript.

Rows marked **subagent** are Claude's delegated-agent transcripts. Claude files those under the
parent session's id, so resuming one reopens the parent conversation rather than that branch of it.

Answering "which conversation did X" needs a keyword that was actually written down. When nothing
matches, the fallback that works is a timestamp from elsewhere — `git reflog` for a branch, a file's
mtime — and then `~/.zsh_history`, which records the epoch of every command and shows which agent
held the shell at that moment.

## `update-tickets`

`update-tickets` reads its config from `~/.config/shipshape/` (respecting `$XDG_CONFIG_HOME`).

1. **Config** — copy the example and fill in your Notion data source ID:

   ```bash
   mkdir -p ~/.config/shipshape
   cp config.example.toml ~/.config/shipshape/config.toml
   # then edit ~/.config/shipshape/config.toml
   ```

2. **Token** — create a Notion integration with read access to your task board and read access to
   the workspace's users, then either export the token or drop it in a gitignored env file:

   ```bash
   echo 'NOTION_TOKEN=secret_xxx' >> ~/.config/shipshape/.env
   ```

   (Or just export `NOTION_TOKEN` in your shell.)

3. **Resolve your user ID** (once):

   ```bash
   update-tickets --setup            # uses `git config user.email`
   update-tickets --setup you@org.com
   ```

4. **Run it:**

   ```bash
   update-tickets
   ```

### How status is derived

`update-tickets` never drags a ticket backward; it only proposes forward moves.

| PR signal                                          | Proposed status   |
| -------------------------------------------------- | ----------------- |
| Open PR, no `Ready for Review` label               | In Progress       |
| Open PR, `Ready for Review` on every open PR       | In Review         |
| All PRs merged, none in prod                       | Ready for Release |
| All PRs merged, ≥1 in prod                         | Released          |

"In prod" means the merge commit is an ancestor of a commit with a successful `prod` GitHub
Deployment. Tickets with no linked PRs, a PR closed without merging, or a PR lookup error are left
untouched.

The GitHub owner and repo are read from each PR link on the ticket, so PRs across any repos or orgs
are reconciled — there is no GitHub configuration.
