# planning-hub

Tool-neutral planning hub: markdown tickets, a validation CLI and terminal UI, and a format for projecting them into external PM tools such as Plane, EWM, Jira, or Rally.

## What it is

Tickets (stories, bugs, ...) are plain markdown files with YAML frontmatter, stored in `tickets/`. The hub is the canonical copy. It is meant to be read and edited directly by a person or an AI assistant, with no API round-trip. External PM tools are **spokes**: projections of hub tickets, touched only when you explicitly push to or pull from one.

- Schemas define the ticket types and the fields each one has (`schemas/`).
- `tools/hub.py` validates, creates, lists and prints ticket trees.
- `tools/tui.py` is a terminal UI with tree, kanban and per-iteration scrum views, and editing.
- `HUB.md` is the operating manual for an AI assistant (Claude Code or similar) working in the hub.
- No spoke adapters are included. `HUB.md` describes how to sync with a spoke by hand. The plan is that this is handled via MCPs/APIs for each system or systems.

## Requirements

- Python 3.11+
- [uv](https://docs.astral.sh/uv/). Both scripts declare their dependency (`pyyaml`) inline, so `uv run` handles it. There is nothing to install.

The TUI uses `curses`, so it needs a real terminal. On Linux and macOS that is the standard library; on Windows `uv run` installs the `windows-curses` package automatically (Windows Terminal or the VS Code terminal work best).

## Install

```
git clone <this repo> planning-hub
cd planning-hub
uv run tools/hub.py validate      # expect: OK: 0 ticket(s), 2 type(s)
```

## Usage

Create tickets (IDs are allocated for you: `T-0001`, `T-0002`, ...):

```
uv run tools/hub.py new story "Short imperative title" --set description="Why this matters" --set priority=high
uv run tools/hub.py new bug "Label loss on push" --set severity=high --set description="..." --set parent=T-0001
```

Query and check:

```
uv run tools/hub.py list --status in_progress
uv run tools/hub.py list --iteration sprint-24      # also prints estimate/time-spent totals
uv run tools/hub.py list --label regression
uv run tools/hub.py list --charge-code ABC-1234     # resolved through the parent chain
uv run tools/hub.py tree                            # ticket hierarchy
uv run tools/hub.py validate                        # run after every edit
```

Browse and edit in the terminal:

```
./tui.sh                       # Linux/macOS (or: ./tui.sh kanban)
.\tui.ps1                      # Windows PowerShell (or: .\tui.ps1 kanban)
uv run tools/tui.py            # equivalent, from the repo root
```

The launchers work from any directory and pass arguments through (`tree`, `kanban`, `scrum`, `--ascii`, ...). If PowerShell blocks the script, run the `uv run` form instead.

Keys: `1`/`2`/`3` switch tree, kanban and scrum views; `j`/`k` move; `Enter` opens a ticket; `H`/`L` move a ticket to the previous or next status; `s` status, `p` priority, `n` add a note, `e` edit a field or section, `E` open in `$EDITOR`, `q` quit. Every edit is validated and rolled back if rejected. The footer shows the full key list.

You can also edit ticket files by hand. Run `validate` afterwards.

**Windows notes.** Consoles that cannot draw box characters (for example a cp1252 console) get plain ASCII: `tree` switches its connectors automatically, and the TUI defaults to `--ascii` on the legacy console. Pass `--ascii` or `--no-ascii` to the TUI to override. The `E` key opens `$EDITOR`, falling back to `notepad` on Windows and `vi` elsewhere.

### Setting up your own hub

- **People:** add handles to `people.yaml` before assigning tickets to them.
- **Sprints:** add handles to `iterations.yaml` before setting a ticket's `iteration`.
- **Ticket types:** add a `schemas/<type>.yaml`, following `schemas/_meta.md`. Only add fields a real ticket needs.

### Using it with an AI assistant

Open the directory in Claude Code (or a similar tool). `CLAUDE.md` points the assistant at `HUB.md`, which covers ID allocation, validation, hub-only versus projected content, and what never to touch. The assistant is told not to push to or pull from any external tool unless you ask.

## Layout

```
schemas/        _base.yaml (fields every ticket has), _meta.md, one file per ticket type
people.yaml     hub handle -> per-spoke identity
iterations.yaml hub handle -> sprint identity
tickets/        one file per ticket: T-NNNN.md
sync/           last-synced spoke snapshots (written by adapters only)
tools/          hub.py (CLI), tui.py (terminal UI)
tui.sh, tui.ps1 launchers for the terminal UI (Linux/macOS, Windows PowerShell)
docs/           ticket-format.md: the format specification
HUB.md          operating manual for an AI assistant
```

## Design in brief

- **Hub and spokes.** The hub is the working surface. Spokes are updated by deliberate, on-demand push and pull, never by continuous sync.
- **Push** creates or updates spoke tickets from hub tickets and records native IDs in `spoke_links`. This makes later pushes idempotent.
- **Pull** shows a diff against the last-synced state and never auto-resolves conflicts.
- **Projected versus hub-only.** Sections a ticket type declares (`## Description`, ...) are projected to spokes. Any other `##` section (such as `## Notes`) stays in the hub.

See `docs/ticket-format.md` for the full format.

## Status

Schema version 0.1; the format has not been frozen.
