# Ticket File Format

A tool-neutral format for tracking planning tickets (stories, bugs, ...) as plain files,
independent of any project-management tool. It's meant to be usable entirely on its own —
no external tool required — and later, optionally, projected into one (Jira, Rally, EWM,
Plane, ...) via a thin adapter. This document describes the format itself.

## Why

Different PM tools each reinvent "what is a ticket" slightly differently, and none of them
are especially pleasant for an AI assistant (or a human) to read and edit directly. Rather
than adopt whichever tool's schema first, this defines one small, tool-agnostic ticket format
that can be used for real planning immediately, and mapped into a specific tool later without
having to redesign it.

## Structure

Each ticket is one file: **YAML frontmatter**, then a **Markdown body**.

```markdown
---
id: T-0003
type: Bug
title: Label loss on push
status: backlog
priority: high
assignee: null
parent: T-0001
estimate: 5
iteration: sprint-24
time_spent: null
labels: [regression]
charge_code: null
severity: high
spoke_links: {}
---

## Description

Labels vanish when a ticket is pushed to the external tool.

## Repro steps

1. Push a ticket with a label
2. Look at the ticket in the external tool

## Notes

Working reasoning about the work itself, open questions — anything that isn't meant to be
projected into an external tool.
```

- **Filename** = `id` = `T-NNNN` (`T-0001`, `T-0002`, ...). IDs are opaque and sequential —
  they carry no meaning and are never reused or hand-picked.
- **Frontmatter** holds short, structured fields.
- **Body** holds longer free-text fields as `##` sections, plus anything hub-only.

## Frontmatter fields (every ticket)

| Field | Meaning | Values |
|---|---|---|
| `id` | Matches the filename | `T-NNNN` |
| `type` | Which type schema this ticket follows | e.g. `Story`, `Bug` |
| `title` | Short, single-line | free text |
| `status` | Where it stands | `backlog`, `todo`, `in_progress`, `blocked`, `in_review`, `done`, `cancelled` |
| `priority` | | `none`, `low`, `medium`, `high`, `urgent` |
| `assignee` | Who owns it | a handle, or `null` |
| `parent` | Groups tickets (e.g. a task under a larger story) | another ticket's `id`, or `null` |
| `estimate` | Planned effort | a number; unit (points, hours, ...) is a team convention, not enforced |
| `iteration` | Which sprint/iteration this is scheduled in | a handle from a registry file, or `null` |
| `time_spent` | Actual effort logged so far, same unit convention as `estimate` | a number, updated by hand as work happens |
| `labels` | Free-text tags | a list, e.g. `[frontend, tech-debt]`, `[]` if none; no duplicates (case-insensitive) |
| `charge_code` | What this is billed against | free text, or `null` — **inherits, see below** |
| `spoke_links` | Correlation record: which external tool(s) this ticket has been pushed to, and its native ID there | a map of spoke name -> `{ id, url }` (`url` optional); `{}` until pushed anywhere. Spoke names come from `schemas/_base.yaml` (currently `plane`, `ewm`, `jira`, `rally`) |

A **type** (`Story`, `Bug`, ...) can add its own fields on top of these — e.g. `Bug` adds
`severity`. Types are defined once and reused; adding a field to a type is a deliberate,
occasional edit, not something done per-ticket.

`iteration` validates against a small registry file (handle -> name/dates), the same pattern
used for `assignee` (handle -> person identity): a fixed, enumerable set gets a registry and
validation; something that doesn't repeat, like `labels`, stays free text instead.

`time_spent` is a single running total, not a per-entry work log (who logged what, on which
day) — that's a deliberately bigger feature, worth adding only if a plain total turns out not
to be enough.

`charge_code` is the **only inheriting field** in the format, and it's a deliberate exception
to how every other nullable field works. For every other field, `null`/absent means "unset."
For `charge_code`, `null`/absent means **"use the nearest ancestor's `charge_code`"** — a
ticket only has no charge code at all if nothing set it anywhere up its `parent` chain to the
root. Setting a value on a ticket applies it to that ticket and, by inheritance, everything
under it, until some descendant overrides it with its own value. This is resolved by whatever
reads the hub (a script, an adapter, a person) — never read the raw frontmatter field as the
answer to "what does this cost against," since most tickets in a charged subtree correctly
leave it blank. There's currently no way to mark a ticket "explicitly uncharged" under a
charged parent — that's a real gap if it ever comes up, not something to work around silently.

`spoke_links` is also what makes a push **idempotent**: its presence for a given tool is the
signal that this ticket already exists there, with the ID to update rather than a reason to
create a duplicate. `spoke_links: {}` (the default) means "never pushed anywhere."

## Body sections: projected vs. hub-only

Some frontmatter/body fields are meant to be **projected** — pushed into an external tool
if/when this ticket is pushed anywhere. Others are **hub-only** and never leave this format:

- `## Description`, `## Repro steps`, `## Acceptance criteria`, etc. — whichever `##` sections
  a ticket's type declares — are **projected**. They should read well in another tool.
- **Any other `##` heading** (`## Notes`, `## Context`, `## Decisions`, ...) is **hub-only**.
  Reasoning, alternatives considered, and open questions about the ticket's work belong here,
  not in a projected section, so they never get clobbered by or leaked into an external tool.
  A cancelled ticket's reason, and the manual sync log, go under `## Notes`.
- A ticket never carries commentary about the ticketing system itself (schema gaps, format
  limitations, workarounds), in any section — unless the ticket's subject is the tool.

## Ticket types defined so far

**Base fields** (all types): `title`, `description` (projected, `## Description`), `status`,
`priority`, `assignee`, `parent`, `estimate`, `iteration`, `time_spent`, `labels`,
`charge_code`, and the envelope field `spoke_links`. `title`, `description`, `status` and
`priority` are required; `status` defaults to `backlog` and `priority` to `none`. Everything
else is optional.

**Story** — adds:
| Field | Kind | Required |
|---|---|---|
| `acceptance_criteria` | projected, `## Acceptance criteria` | no |

**Bug** — adds:
| Field | Kind | Required |
|---|---|---|
| `severity` | enum: `low`, `medium`, `high`, `critical` | yes |
| `repro_steps` | projected, `## Repro steps` | no |

## Mapping to an external tool (optional, added later)

A type or field can optionally declare a `spoke_mapping`: which native field in a given
external tool it corresponds to, and how enum values translate (e.g. hub `status: blocked` ->
whatever a specific tool calls that). This is what a push/pull integration would use to
translate automatically. It's entirely optional — the format is fully usable for planning
with no mapping defined and no external tool connected at all. The base fields and the Story
and Bug types currently carry Plane mappings; no adapter ships, so nothing applies them
automatically (see `HUB.md` for the manual push/pull process).

## What this format is not

- Not a database — it's plain files, meant to be read and diffed like any other text.
- Not tied to any tool's field names, workflow states, or hierarchy rules.
- Not a live sync target — if/when it's connected to a tool later, that's a deliberate,
  on-demand action ("push this ticket"), never continuous background sync.
