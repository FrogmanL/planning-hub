# Meta-schema: how to define a ticket type

A ticket type is one YAML file in `schemas/` (e.g. `bug.yaml`). Every type inherits all
fields in `_base.yaml`; a type file only **adds** fields. Do not redefine a base field.
Add a field only when a real ticket needs it, never speculatively.

## Type file

```yaml
type: Bug                    # required. Value of `type:` in ticket frontmatter. Capitalised, unique.
description: One line.       # optional
fields: [ ... ]              # required (may be empty). See below.
spoke_mapping:               # optional. How the TYPE itself maps into each spoke.
  plane: { label: Bug }      # keys must be spokes listed in _base.yaml
```

## Field

```yaml
- name: severity             # snake_case, unique across base + type
  kind: enum                 # see kinds below
  values: [low, medium, high, critical]   # enum only
  required: true             # default false
  default: low               # optional; used by `hub.py new`
  spoke_mapping:             # optional, per spoke
    plane:
      field: priority        # native field name in the spoke
      values: { low: low }   # hub value -> spoke value, for enums
      direction: push_only   # optional: push_only | bidirectional (default)
```

### Kinds

| kind | where it lives | value |
|---|---|---|
| `string` | frontmatter | single-line text |
| `enum` | frontmatter | one of `values` |
| `person` | frontmatter | a handle from `people.yaml`, or null |
| `ref` | frontmatter | another ticket's ID (`T-0007`), or null |
| `number` | frontmatter | an integer or decimal; unit is a team convention, not enforced |
| `iteration` | frontmatter | a handle from `iterations.yaml`, or null |
| `tags` | frontmatter | a list of short strings, no duplicates (case-insensitive) |
| `markdown` | **body**, under `## <Field name>` | free markdown |

A `markdown` field's heading is the field name with underscores as spaces and the first
letter capitalised: `repro_steps` -> `## Repro steps`.

## Envelope fields (not schema fields)

Every ticket has these in frontmatter regardless of type; they are not declared in schemas:

- `id`: `T-NNNN`, allocated by `tools/hub.py new`, must equal the filename.
- `type`: a type name from `schemas/`.
- `spoke_links`: map of spoke -> `{ id: <native id>, url: <optional> }`. Written by push
  adapters only. Empty (`{}`) until a ticket is pushed.

## Inheriting fields

`charge_code` is currently the **only** field with special resolution behavior, and the only
one worth calling out this explicitly: for every other nullable field, `null`/absent means
"unset" (no assignee, no iteration, ...). For `charge_code`, `null`/absent means **inherit
from the nearest ancestor (via `parent`) that has one set** — not "no charge." A ticket truly
has no charge code only if the entire chain up to its root is unset. Setting a value overrides
inheritance for that ticket and (by default, since descendants look at the same chain)
everything under it. There is currently no way to say "explicitly uncharged, don't inherit
from a charged parent" — if that's ever needed, it needs a deliberate sentinel value, not a
guess; don't invent one ad hoc.

Always read `charge_code` through `Hub.resolve_charge_code()` (or `hub.py list
--charge-code`/`--unresolved-charge-code`), never the raw frontmatter value — the raw value on
most tickets in a charged subtree will correctly be blank.

## Registries

`person` and `iteration` fields validate against a small registry file at the hub root
(`people.yaml`, `iterations.yaml`): a handle -> identity mapping. Add a new type of registry
the same way if a future field needs one — don't invent an unvalidated free-text field for
something that has a real, enumerable set of values (that's what an `enum` or a registry is
for); don't add a registry for something that doesn't repeat.

## `spoke_mapping` semantics

- `field` names the spoke-native field. `values` translates enum values hub -> spoke.
  A spoke value with no reverse mapping is **surfaced on pull, never guessed**.
- `direction: push_only` means the hub owns the field: pull reports a spoke-side change but
  never proposes overwriting the hub value. Default is `bidirectional`.
- A field with no mapping for a spoke is simply not projected there.
- Mappings for a spoke are added when that spoke's adapter is built, not before.

## Body convention

`## <Field name>` sections for `markdown` fields are **projected** (pushed to spokes).
Any other `##` heading (`## Notes`, `## Context`, `## Decisions`) is **hub-only**: never
pushed, never overwritten by pull. Put working reasoning there.
