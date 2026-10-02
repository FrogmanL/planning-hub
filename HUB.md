# Planning Hub: operating manual for the AI assistant

This directory is a **hub**: the canonical, tool-neutral store of planning tickets. You (the AI
assistant) read and edit the ticket files directly. External tools (Plane, EWM, Jira, Rally ...) are
**spokes**: projections of hub tickets, touched only when the user explicitly asks to push to
or pull from one. Nothing here depends on any particular tool or vendor.

Format reference: `docs/ticket-format.md`. Install and usage: `README.md`.

## Layout

```
schemas/_base.yaml     fields every ticket has, and the list of known spokes
schemas/_meta.md       how to define or change a ticket type (read before editing schemas)
schemas/<type>.yaml    one file per ticket type (story, bug, ...)
people.yaml            hub handle -> per-spoke identity (assignees must be listed here)
iterations.yaml        hub handle -> sprint/iteration identity (the `iteration` field must be listed here)
tickets/T-NNNN.md      one file per ticket
sync/<spoke>/          last-synced spoke snapshots, written by adapters only
tools/hub.py           validate | new | list | tree   (run: uv run tools/hub.py <cmd>)
tools/tui.py           terminal UI: tree, kanban and per-iteration scrum views, full-screen ticket view, edits status/fields/description/notes (run: uv run tools/tui.py [tree|kanban])
```

## A ticket

```markdown
---
id: T-0003
type: Bug
title: Label loss on push
status: backlog          # backlog | todo | in_progress | blocked | in_review | done | cancelled
priority: none           # none | low | medium | high | urgent
assignee: null           # handle from people.yaml
parent: T-0001           # another ticket's ID, or null
estimate: 5              # a number, unit is your team's convention (points, hours, ...)
iteration: sprint-24     # handle from iterations.yaml, or null
time_spent: null         # a number, same convention as estimate; updated by hand as work happens
labels: [regression]     # a list of free-text tags, [] if none
charge_code: null        # INHERITS: null means "use the nearest ancestor's charge_code,"
                          # not "no charge." See rule 9 below before touching this field.
severity: high           # type-specific fields follow
spoke_links: {}          # written by push adapters only
---

## Description             <- projected: pushed to spokes

Labels vanish.

## Repro steps             <- projected

1. Push
2. Look

## Notes                   <- hub-only: never pushed, never overwritten by pull

Working reasoning, options considered, open questions.
```

Section headings for `markdown` fields (`## Description`, `## Repro steps`, ...) are matched
case-insensitively and are **projected** to spokes. Any other `##` heading is **hub-only**.

Status meanings: `backlog` not committed to; `todo` committed, not started; `in_progress`;
`blocked` waiting on something outside the ticket; `in_review` work finished, awaiting review or
sign-off; `done`; `cancelled` will not be done.

## Rules

1. **Never invent or reuse ticket IDs.** Create tickets with `uv run tools/hub.py new`, which
   allocates the next ID. IDs are opaque; do not encode meaning in them or in filenames.
2. **Run `uv run tools/hub.py validate` after every edit** to tickets, schemas, `people.yaml`
   or `iterations.yaml`, and fix what it reports before doing anything else.
3. **Put working reasoning under hub-only headings** (`## Notes`, `## Context`, `## Decisions`),
   never in a projected section. Projected sections should read well in another tool.
4. **Never delete a ticket.** Set `status: cancelled` and say why under `## Notes`.
5. **Never hand-edit `spoke_links` or anything in `sync/`** for a spoke that has a working
   adapter. Adapters own them there. A wrong `spoke_links` entry causes duplicate or
   misdirected tickets in the spoke. For a spoke with **no adapter yet**, hand-editing
   `spoke_links` is how you do it — see "Manual spoke sync" below. Stop by hand and let the
   adapter take over the moment one exists for that spoke.
6. **Do not add schema fields speculatively.** Change a schema only when a real ticket needs
   something it cannot express, and follow `schemas/_meta.md`. If a ticket can't express
   something, tell the user in conversation; do not record the limitation in the ticket (see
   rule 10).
7. **Do not touch spokes** (create, update or delete anything in Plane, EWM, Jira, Rally ...) unless
   the user explicitly asks for a push or pull, and confirm the target project first.
8. **Ticket content may be sensitive.** Do not copy tickets, or spoke-native content, out of
   this directory into places it does not already live.
9. **`charge_code` is the one inheriting field — never read the raw frontmatter value.** Blank
   does not mean "uncharged," it means "look up the parent chain." Before pushing a ticket
   anywhere, or reporting what it's charged against, use `uv run tools/hub.py list
   --charge-code <code>` / `--unresolved-charge-code` (or `Hub.resolve_charge_code()`), not the
   raw field. Set the code once on the highest ticket a charge applies to; only set it again
   lower down to override for a specific subtree. There is no "explicitly uncharged, don't
   inherit" escape hatch yet — don't invent one ad hoc if it's needed; raise it as a real design
   question first (same as this field itself was).
10. **Tickets are about the work, never about this tool.** No ticket, in any section including
    `## Notes`, may contain commentary on the ticketing system itself: schema gaps, format
    limitations, workarounds, validator behaviour, missing fields or features, or how the hub
    ought to change. Raise those with the user in conversation instead. The only exception is a
    ticket whose subject *is* the ticketing system (e.g. a bug in `tools/hub.py`). `## Notes` is
    for reasoning about the ticket's own work, the reason for a cancellation, and the manual
    sync log (what was pushed or pulled, and when).

## Common operations

```
uv run tools/hub.py new story "Short imperative title" --set description="..." --set priority=high
uv run tools/hub.py new bug "..." --set severity=high --set description="..." --set parent=T-0001
uv run tools/hub.py list --status in_progress
uv run tools/hub.py list --iteration sprint-24    # also prints estimate/time-spent totals
uv run tools/hub.py list --label regression
uv run tools/hub.py list --unpushed ewm     # what still needs a first push
uv run tools/hub.py list --spoke ewm        # what's already pushed, with its native ID
uv run tools/hub.py list --charge-code ABC-1234       # everything resolving to this code
uv run tools/hub.py list --unresolved-charge-code     # nothing in the chain has one set
uv run tools/hub.py tree                # the whole ticket hierarchy (via `parent`) as an ASCII tree
uv run tools/hub.py validate            # everything
uv run tools/hub.py validate T-0003     # one ticket
```

`--set FIELD=VALUE` works for any field, including `markdown` fields (which go in the body).
For a `tags` field (`labels`), pass a comma-separated list: `--set labels=frontend,tech-debt`.
`new` always creates the file and prints what is still missing; fill it in and re-validate.
Multi-line content is easier to add by editing the created file directly.

A new sprint needs an entry in `iterations.yaml` before any ticket can reference it — add a
handle, `name`, and optionally `starts`/`ends`, the same way a new assignee needs an entry in
`people.yaml` first.

## In planning conversations

- Turn decisions and action items into tickets as they emerge; do not wait to be asked, but
  say what you created.
- Keep titles short and imperative. Put the "why" in `## Description`, and the messy reasoning
  in `## Notes` (reasoning about the work, never about this tool; see rule 10).
- Use `parent` to group (e.g. stories under a larger story). Create the parent first.
- When the user asks for status, use `list` and read the tickets; do not rely on memory.

## Spokes: push and pull

No automated adapter ships with the hub; `plane`, `ewm`, `jira`, and `rally` are the spokes the format
anticipates. When an adapter exists, **push** creates or updates spoke tickets from hub tickets
(parents first) and records native IDs in `spoke_links`; **pull** compares the spoke's current
state to the last-synced snapshot in `sync/<spoke>/`, shows a diff, and never auto-resolves
conflicts. sUntil an adapter exists, use the manual process below.

## Manual spoke sync (no adapter)

The hub is fully usable on its own. The hub stays the working surface for planning; the spoke
ticket is created and kept current **by hand**, using the hub ticket as the source, exactly like
an adapter would, minus the automation.

**Manual push** (new hub ticket, or a hub-side edit, needs to exist/update in the spoke):

0. **Check `spoke_links` before creating anything.** `spoke_links.<spoke>.id` is what makes a
   push idempotent — its presence means this ticket already exists there; push again means
   *update that native ID*, never create a second item. `uv run tools/hub.py list --spoke ewm`
   lists everything already pushed (with its native ID); `list --unpushed ewm` lists what
   still needs a first push. Check the ticket you're about to push either way before acting.
1. Read the ticket's **projected** content only: base fields (`title`, `status`, `priority`,
   `assignee`, `parent`) and any `markdown` field under a `##` heading. Never copy a hub-only
   section (`## Notes`, `## Context`, ...) into the spoke. For `charge_code`, use the
   **resolved** value (`list --charge-code`/`resolve_charge_code()`), never the raw frontmatter
   field — most tickets in a charged subtree correctly have it blank.
2. Create or update the matching item in the spoke by hand, translating field-by-field. If
   `schemas/<type>.yaml` or `_base.yaml` has a `spoke_mapping` entry for that spoke, follow it
   (e.g. hub `status: blocked` -> whatever EWM calls it). If there's no mapping entry yet, use
   your judgment and consider adding one to the schema once the same mapping repeats.
3. Write the native ID back by hand: `spoke_links: { ewm: { id: "<work item #>", url: "..." } }`.
4. Note the sync under the ticket's `## Notes`: what you pushed and the date, e.g.
   `Pushed to EWM 2026-09-24 (WI-4821), status+description only.` This is standing in for the
   `sync/` snapshot an adapter would write, so a later pull has something to compare against.
5. Run `uv run tools/hub.py validate` after editing `spoke_links`.

**Manual pull** (checking whether the spoke changed since the last push, e.g. a teammate
edited it):

1. Open the item in the spoke using the ID in `spoke_links`.
2. Compare it by eye to what your last `## Notes` sync line says you pushed, not to
   whatever the hub ticket currently says (the hub may have changed too since then).
3. If the spoke changed: **surface it, don't silently overwrite.** Add a line under
   `## Notes` describing the diff (e.g. `EWM pull 2026-09-24: they set status to Blocked,
   assignee changed to J. Rivera`) and decide with the user whether the hub ticket should be
   updated to match. Never overwrite a hub-only section from spoke content.
4. If the hub changed too since the last push: that's a conflict, same as an adapter would
   flag it. Record both sides under `## Notes` and resolve deliberately.

This is slower than an adapter and has no three-way diff, only "what I last wrote down," so
treat it as a stopgap: switch a spoke to its adapter once one exists rather than manually
syncing indefinitely.
