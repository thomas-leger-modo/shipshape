# shipshape

Two small terminal tools for keeping a repo and its task board tidy after PRs move:

- **`prune-local-branches`** — find local branches and worktrees fully merged into the trunk and
  safe to delete, then remove the ones you pick. Read-only until you confirm; re-checks safety
  immediately before deleting.
- **`update-tickets`** — reconcile the status of your Notion task-board tickets against the real
  state of their linked GitHub PRs and production deployments, and apply the changes you approve.

Both are interactive `fzf`/`gum` pickers with previews. Nothing is written without an explicit
confirmation.

## Requirements

- Python ≥ 3.12 and [`uv`](https://docs.astral.sh/uv/)
- [`fzf`](https://github.com/junegunn/fzf) and [`gum`](https://github.com/charmbracelet/gum)
  (`brew install fzf gum`)
- [`gh`](https://cli.github.com), authenticated (`gh auth login`) — used for PR and deployment state

## Install

```bash
uv tool install git+https://github.com/thomas-leger-modo/shipshape
```

This puts `prune-local-branches` and `update-tickets` on your PATH.

To hack on it locally, install from a clone in editable mode:

```bash
uv tool install --editable ~/code/shipshape
```

## `prune-local-branches`

No configuration. Run it inside any git repo:

```bash
prune-local-branches
```

The trunk is auto-detected from the remote's default branch (`origin/HEAD`), falling back to
`origin/main`, so `main`, `master`, etc. all work. A branch/worktree is only offered when it has no
uncommitted or untracked changes **and** is fully merged into the trunk.

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
