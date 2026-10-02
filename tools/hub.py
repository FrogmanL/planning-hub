#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["pyyaml>=6"]
# ///
"""Planning hub tool: validate | new | list. See HUB.md for the operating manual."""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from pathlib import Path

import yaml

DEFAULT_ROOT = Path(__file__).resolve().parent.parent

KINDS = {"string", "enum", "person", "ref", "markdown", "number", "iteration", "tags"}
ENVELOPE = ("id", "type", "spoke_links")
ID_RE = re.compile(r"^T-(\d{4,})$")
FILE_RE = re.compile(r"^T-\d{4,}\.md$")
NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")
HANDLE_RE = re.compile(r"^[a-z][a-z0-9_-]*$")
FRONTMATTER_RE = re.compile(r"\A---[ \t]*\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|\Z)(.*)\Z", re.S)
HEADING_RE = re.compile(r"^##(?!#)\s+(.+?)\s*$")

FIELD_KEYS = {"name", "kind", "values", "required", "default", "spoke_mapping"}
MAPPING_KEYS = {"field", "values", "direction"}
DIRECTIONS = {"push_only", "bidirectional"}
TYPE_KEYS = {"type", "description", "fields", "spoke_mapping"}
LINK_KEYS = {"id", "url"}


class StrictLoader(yaml.SafeLoader):
    """SafeLoader that rejects duplicate mapping keys (PyYAML silently keeps the last)."""


def _construct_unique_mapping(loader, node, deep=False):
    seen = set()
    for key_node, _ in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in seen:
            raise yaml.constructor.ConstructorError(
                None, None, f"duplicate key {key!r}", key_node.start_mark
            )
        seen.add(key)
    return loader.construct_mapping(node, deep)


StrictLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_unique_mapping)


def safe_stdio():
    """Never crash on a console that can't encode a character (e.g. cp1252 on Windows)."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass


def can_encode(text: str) -> bool:
    try:
        text.encode(sys.stdout.encoding or "ascii")
        return True
    except UnicodeEncodeError:
        return False


def load_yaml(path: Path):
    return yaml.load(path.read_text(encoding="utf-8"), Loader=StrictLoader)


def heading_for(field_name: str) -> str:
    text = field_name.replace("_", " ")
    return text[:1].upper() + text[1:]


def split_sections(body: str) -> list[tuple[str, str]]:
    """Split a ticket body into (heading, text) for each `## ` heading outside code fences."""
    sections: list[tuple[str, list[str]]] = []
    fence = None
    for line in body.splitlines():
        stripped = line.lstrip()
        if fence is None and stripped.startswith(("```", "~~~")):
            fence = stripped[:3]
        elif fence is not None and stripped.startswith(fence):
            fence = None
        elif fence is None and (m := HEADING_RE.match(line)):
            sections.append((m.group(1), []))
            continue
        if sections:
            sections[-1][1].append(line)
    return [(h, "\n".join(lines).strip()) for h, lines in sections]


@dataclass
class Ticket:
    path: Path
    id: str
    front: dict
    sections: list[tuple[str, str]]

    @property
    def type(self):
        return self.front.get("type")

    def section(self, field_name: str):
        want = heading_for(field_name).casefold()
        found = [text for h, text in self.sections if h.casefold() == want]
        return found


class Hub:
    def __init__(self, root: Path):
        self.root = root
        self.errors: list[str] = []
        self.spokes: list[str] = []
        self.base: list[dict] = []
        self.types: dict[str, list[dict]] = {}  # type name -> base + own fields
        self.people: dict[str, dict] = {}
        self.iterations: dict[str, dict] = {}
        self._load_schemas()
        self._load_people()
        self._load_iterations()

    def err(self, where: str, msg: str):
        self.errors.append(f"{where}: {msg}")

    def rel(self, path: Path) -> str:
        try:
            return str(path.relative_to(self.root))
        except ValueError:
            return str(path)

    # ---- schemas -------------------------------------------------------------------

    def _load_schemas(self):
        sdir = self.root / "schemas"
        base_path = sdir / "_base.yaml"
        try:
            base = load_yaml(base_path)
        except (OSError, yaml.YAMLError) as e:
            self.err(self.rel(base_path), f"cannot load: {e}")
            return
        where = self.rel(base_path)
        if not isinstance(base, dict):
            self.err(where, "must be a mapping")
            return
        spokes = base.get("spokes")
        if not (isinstance(spokes, list) and all(isinstance(s, str) for s in spokes)):
            self.err(where, "`spokes` must be a list of names")
        else:
            self.spokes = spokes
        taken: set[str] = set()
        self.base = self._check_fields(where, base.get("fields"), taken)

        for path in sorted(sdir.glob("*.yaml")):
            if path.name.startswith("_"):
                continue
            where = self.rel(path)
            try:
                data = load_yaml(path)
            except (OSError, yaml.YAMLError) as e:
                self.err(where, f"cannot load: {e}")
                continue
            if not isinstance(data, dict):
                self.err(where, "must be a mapping")
                continue
            for key in set(data) - TYPE_KEYS:
                self.err(where, f"unknown key {key!r}")
            tname = data.get("type")
            if not isinstance(tname, str) or not tname:
                self.err(where, "`type` is required")
                continue
            if path.stem != tname.lower():
                self.err(where, f"filename must be {tname.lower()}.yaml")
            if tname in self.types:
                self.err(where, f"duplicate type {tname!r}")
                continue
            own = self._check_fields(where, data.get("fields", []), set(taken))
            mapping = data.get("spoke_mapping") or {}
            if not isinstance(mapping, dict):
                self.err(where, "`spoke_mapping` must be a mapping")
            else:
                for spoke, entry in mapping.items():
                    if spoke not in self.spokes:
                        self.err(where, f"spoke_mapping: unknown spoke {spoke!r}")
                    if not isinstance(entry, dict):
                        self.err(where, f"spoke_mapping.{spoke} must be a mapping")
            self.types[tname] = self.base + own

    def _check_fields(self, where: str, fields, taken: set[str]) -> list[dict]:
        out: list[dict] = []
        if not isinstance(fields, list):
            self.err(where, "`fields` must be a list")
            return out
        for i, f in enumerate(fields, 1):
            if not isinstance(f, dict):
                self.err(where, f"field #{i} must be a mapping")
                continue
            name = f.get("name")
            loc = f"{where} field {name!r}"
            if not isinstance(name, str) or not NAME_RE.match(name):
                self.err(where, f"field #{i}: `name` must be snake_case")
                continue
            if name in taken or name in ENVELOPE:
                self.err(loc, "duplicate or reserved name")
                continue
            taken.add(name)
            for key in set(f) - FIELD_KEYS:
                self.err(loc, f"unknown key {key!r}")
            kind = f.get("kind")
            if kind not in KINDS:
                self.err(loc, f"`kind` must be one of {sorted(KINDS)}")
                continue
            values = f.get("values")
            if kind == "enum":
                if not (isinstance(values, list) and values and all(isinstance(v, str) for v in values)):
                    self.err(loc, "enum needs a non-empty `values` list of strings")
                    continue
                if f.get("default") is not None and f["default"] not in values:
                    self.err(loc, "`default` is not one of `values`")
            elif "values" in f:
                self.err(loc, "`values` is only valid for enum")
            mapping = f.get("spoke_mapping") or {}
            if not isinstance(mapping, dict):
                self.err(loc, "`spoke_mapping` must be a mapping")
                mapping = {}
            for spoke, entry in mapping.items():
                if spoke not in self.spokes:
                    self.err(loc, f"spoke_mapping: unknown spoke {spoke!r}")
                if not isinstance(entry, dict):
                    self.err(loc, f"spoke_mapping.{spoke} must be a mapping")
                    continue
                for key in set(entry) - MAPPING_KEYS:
                    self.err(loc, f"spoke_mapping.{spoke}: unknown key {key!r}")
                if "direction" in entry and entry["direction"] not in DIRECTIONS:
                    self.err(loc, f"spoke_mapping.{spoke}: direction must be one of {sorted(DIRECTIONS)}")
                if isinstance(entry.get("values"), dict) and kind == "enum":
                    for hub_value in entry["values"]:
                        if hub_value not in values:
                            self.err(loc, f"spoke_mapping.{spoke}: {hub_value!r} is not a hub value")
            out.append(f)
        return out

    def _load_people(self):
        path = self.root / "people.yaml"
        where = self.rel(path)
        try:
            data = load_yaml(path)
        except (OSError, yaml.YAMLError) as e:
            self.err(where, f"cannot load: {e}")
            return
        if data is None:
            data = {}
        if not isinstance(data, dict):
            self.err(where, "must be a mapping with a `people` key")
            return
        people = data.get("people") or {}
        if not isinstance(people, dict):
            self.err(where, "`people` must be a mapping of handle -> identity")
            return
        for handle, ident in people.items():
            if not (isinstance(handle, str) and HANDLE_RE.match(handle)):
                self.err(where, f"bad handle {handle!r}")
            if not isinstance(ident, dict):
                self.err(where, f"{handle}: must be a mapping")
                continue
            for key in set(ident) - {"name", *self.spokes}:
                self.err(where, f"{handle}: unknown key {key!r}")
        self.people = people

    def _load_iterations(self):
        path = self.root / "iterations.yaml"
        where = self.rel(path)
        try:
            data = load_yaml(path)
        except (OSError, yaml.YAMLError) as e:
            self.err(where, f"cannot load: {e}")
            return
        if data is None:
            data = {}
        if not isinstance(data, dict):
            self.err(where, "must be a mapping with an `iterations` key")
            return
        iterations = data.get("iterations") or {}
        if not isinstance(iterations, dict):
            self.err(where, "`iterations` must be a mapping of handle -> identity")
            return
        for handle, ident in iterations.items():
            if not (isinstance(handle, str) and HANDLE_RE.match(handle)):
                self.err(where, f"bad handle {handle!r}")
            if not isinstance(ident, dict):
                self.err(where, f"{handle}: must be a mapping")
                continue
            for key in set(ident) - {"name", "starts", "ends"}:
                self.err(where, f"{handle}: unknown key {key!r}")
        self.iterations = iterations

    # ---- tickets -------------------------------------------------------------------

    def ticket_paths(self) -> list[Path]:
        return sorted(p for p in (self.root / "tickets").iterdir() if p.name != ".gitkeep")

    def load_ticket(self, path: Path) -> tuple[Ticket | None, list[str]]:
        where = self.rel(path)
        if not FILE_RE.match(path.name):
            return None, [f"{where}: filename must be T-NNNN.md"]
        m = FRONTMATTER_RE.match(path.read_text(encoding="utf-8"))
        if not m:
            return None, [f"{where}: missing YAML frontmatter"]
        try:
            front = yaml.load(m.group(1), Loader=StrictLoader)
        except yaml.YAMLError as e:
            return None, [f"{where}: bad frontmatter: {str(e).splitlines()[0]}"]
        if not isinstance(front, dict):
            return None, [f"{where}: frontmatter must be a mapping"]
        return Ticket(path, path.stem, front, split_sections(m.group(2))), []

    def check_ticket(self, t: Ticket, all_ids: set[str]) -> list[str]:
        where = self.rel(t.path)
        errs: list[str] = []
        fm = t.front

        def bad(msg):
            errs.append(f"{where}: {msg}")

        if fm.get("id") != t.id:
            bad(f"`id` is {fm.get('id')!r}, must equal filename ({t.id})")
        fields = self.types.get(t.type) if isinstance(t.type, str) else None
        if fields is None:
            bad(f"unknown type {t.type!r} (known: {sorted(self.types)})")
            return errs

        allowed = set(ENVELOPE)
        for f in fields:
            if f["kind"] == "markdown":
                if f["name"] in fm:
                    bad(f"{f['name']}: belongs in the body under '## {heading_for(f['name'])}'")
            else:
                allowed.add(f["name"])
        for key in fm:
            if key not in allowed and not any(f["name"] == key for f in fields):
                bad(f"unknown frontmatter key {key!r}")

        for f in fields:
            name, kind, required = f["name"], f["kind"], f.get("required", False)
            if kind == "markdown":
                found = t.section(name)
                if len(found) > 1:
                    bad(f"duplicate section '## {heading_for(name)}'")
                elif required and not (found and found[0]):
                    bad(f"required section '## {heading_for(name)}' is missing or empty")
                continue
            val = fm.get(name)
            if val is None or val == "":
                if required:
                    bad(f"{name}: required")
                continue
            if kind == "string":
                if not isinstance(val, str) or "\n" in val:
                    bad(f"{name}: must be a single-line string")
            elif kind == "enum":
                if val not in f["values"]:
                    bad(f"{name}: {val!r} not in {f['values']}")
            elif kind == "person":
                if val not in self.people:
                    bad(f"{name}: {val!r} is not a handle in people.yaml")
            elif kind == "ref":
                if not (isinstance(val, str) and ID_RE.match(val)):
                    bad(f"{name}: must be a ticket ID like T-0001")
                elif val == t.id:
                    bad(f"{name}: a ticket cannot reference itself")
                elif val not in all_ids:
                    bad(f"{name}: {val} does not exist")
            elif kind == "number":
                if isinstance(val, bool) or not isinstance(val, (int, float)):
                    bad(f"{name}: must be a number")
            elif kind == "iteration":
                if val not in self.iterations:
                    bad(f"{name}: {val!r} is not a handle in iterations.yaml")
            elif kind == "tags":
                if not (isinstance(val, list) and all(isinstance(x, str) and x.strip() and "\n" not in x for x in val)):
                    bad(f"{name}: must be a list of short, non-empty strings")
                elif len({x.strip().casefold() for x in val}) != len(val):
                    bad(f"{name}: has duplicate values (case-insensitive)")

        links = fm.get("spoke_links")
        if links is None:
            bad("spoke_links: required (use {})")
        elif not isinstance(links, dict):
            bad("spoke_links: must be a mapping")
        else:
            for spoke, link in links.items():
                if spoke not in self.spokes:
                    bad(f"spoke_links: unknown spoke {spoke!r}")
                elif not isinstance(link, dict) or link.get("id") in (None, ""):
                    bad(f"spoke_links.{spoke}: needs an `id`")
                elif set(link) - LINK_KEYS:
                    bad(f"spoke_links.{spoke}: unknown keys {sorted(set(link) - LINK_KEYS)}")
        return errs

    def resolve_charge_code(self, tickets: dict[str, "Ticket"], tid: str) -> tuple[str | None, str | None]:
        """Walk the `parent` chain for the nearest set `charge_code`. `charge_code` is the one
        inheriting field: null/absent means inherit, not "no charge" -- see schemas/_meta.md.
        Returns (code, source_ticket_id), or (None, None) if nothing in the chain has one set."""
        seen: set[str] = set()
        cur = tid
        while cur in tickets and cur not in seen:
            seen.add(cur)
            code = tickets[cur].front.get("charge_code")
            if code:
                return code, cur
            cur = tickets[cur].front.get("parent")
        return None, None

    def validate(self, only: set[str] | None = None) -> tuple[list[str], int]:
        errs: list[str] = []
        tickets: dict[str, Ticket] = {}
        for path in self.ticket_paths():
            t, load_errs = self.load_ticket(path)
            if only is None or path.stem in only:
                errs += load_errs
            if t:
                tickets[t.id] = t
        all_ids = set(tickets)
        for tid in sorted(only or ()):
            if not (self.root / "tickets" / f"{tid}.md").exists():
                errs.append(f"{tid}: no such ticket")
        for tid, t in tickets.items():
            if only is None or tid in only:
                errs += self.check_ticket(t, all_ids)
        if only is None:
            for tid, t in tickets.items():
                seen, cur = {tid}, t.front.get("parent")
                while isinstance(cur, str) and cur in tickets:
                    if cur in seen:
                        errs.append(f"{self.rel(t.path)}: parent chain forms a cycle at {cur}")
                        break
                    seen.add(cur)
                    cur = tickets[cur].front.get("parent")
        return errs, len(tickets)

    # ---- new -----------------------------------------------------------------------

    def new(self, type_name: str, title: str, sets: list[str]) -> Path:
        tname = next((n for n in self.types if n.casefold() == type_name.casefold()), None)
        if tname is None:
            raise SystemExit(f"unknown type {type_name!r} (known: {sorted(self.types)})")
        fields = self.types[tname]
        values: dict[str, str | int | float | None] = {"title": title}
        by_name = {f["name"]: f for f in fields}
        for item in sets:
            key, sep, val = item.partition("=")
            if not sep or key not in by_name:
                raise SystemExit(f"--set {item!r}: expected FIELD=VALUE with FIELD one of {sorted(by_name)}")
            field = by_name[key]
            if field["kind"] == "number" and val not in ("", "null"):
                try:
                    values[key] = int(val) if re.fullmatch(r"-?\d+", val) else float(val)
                except ValueError:
                    raise SystemExit(f"--set {item!r}: not a number")
            elif field["kind"] == "tags":
                values[key] = [t.strip() for t in val.split(",") if t.strip()]
            else:
                values[key] = val if val not in ("", "null") or field["kind"] == "markdown" else None

        front: dict = {"id": None, "type": tname}
        for f in fields:
            if f["kind"] != "markdown":
                front[f["name"]] = values.get(f["name"], f.get("default"))
        front["spoke_links"] = {}
        body = ""
        for f in fields:
            if f["kind"] == "markdown":
                body += f"## {heading_for(f['name'])}\n\n{(values.get(f['name']) or '').strip()}\n\n"
        body += "## Notes\n\n"

        tickets_dir = self.root / "tickets"
        nums = [int(m.group(1)) for p in tickets_dir.iterdir() if (m := re.match(r"^T-(\d+)\.md$", p.name))]
        num = max(nums, default=0) + 1
        while True:
            tid = f"T-{num:04d}"
            path = tickets_dir / f"{tid}.md"
            front["id"] = tid
            text = "---\n" + yaml.safe_dump(front, sort_keys=False, allow_unicode=True) + "---\n\n" + body
            try:
                with open(path, "x", encoding="utf-8") as fh:
                    fh.write(text)
                return path
            except FileExistsError:
                num += 1


def cmd_validate(hub: Hub, args) -> int:
    errs, count = hub.validate(set(args.ids) if args.ids else None)
    for e in errs:
        print(e)
    if errs:
        print(f"{len(errs)} problem(s) in {count} ticket(s)")
        return 1
    print(f"OK: {count} ticket(s), {len(hub.types)} type(s)")
    return 0


def cmd_new(hub: Hub, args) -> int:
    path = hub.new(args.type, args.title, args.set)
    print(f"created {hub.rel(path)}")
    t, errs = hub.load_ticket(path)
    errs += hub.check_ticket(t, {p.stem for p in hub.ticket_paths()}) if t else []
    for e in errs:
        print(f"still needs: {e}")
    return 0


def _is_number(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def cmd_list(hub: Hub, args) -> int:
    if args.spoke and args.spoke not in hub.spokes:
        raise SystemExit(f"--spoke {args.spoke!r}: unknown spoke (known: {sorted(hub.spokes)})")
    if args.unpushed and args.unpushed not in hub.spokes:
        raise SystemExit(f"--unpushed {args.unpushed!r}: unknown spoke (known: {sorted(hub.spokes)})")

    need_charge = bool(args.charge_code or args.unresolved_charge_code)
    all_tickets: dict[str, Ticket] = {}
    if need_charge:
        for path in hub.ticket_paths():
            t, _ = hub.load_ticket(path)
            if t:
                all_tickets[t.id] = t

    rows = []
    est_total, est_n, spent_total, spent_n = 0, 0, 0, 0
    for path in hub.ticket_paths():
        t, errs = hub.load_ticket(path)
        if not t:
            print("\n".join(errs), file=sys.stderr)
            continue
        fm = t.front
        links = fm.get("spoke_links") or {}
        if args.status and fm.get("status") != args.status:
            continue
        if args.type and str(fm.get("type", "")).casefold() != args.type.casefold():
            continue
        if args.parent and fm.get("parent") != args.parent:
            continue
        if args.assignee and fm.get("assignee") != args.assignee:
            continue
        if args.iteration and fm.get("iteration") != args.iteration:
            continue
        if args.label and args.label not in (fm.get("labels") or []):
            continue
        if args.spoke and args.spoke not in links:
            continue
        if args.unpushed and args.unpushed in links:
            continue
        charge, charge_src = (hub.resolve_charge_code(all_tickets, t.id) if need_charge else (None, None))
        if args.charge_code and charge != args.charge_code:
            continue
        if args.unresolved_charge_code and charge is not None:
            continue
        est, spent = fm.get("estimate"), fm.get("time_spent")
        if _is_number(est):
            est_total += est
            est_n += 1
        if _is_number(spent):
            spent_total += spent
            spent_n += 1
        row = [t.id, str(fm.get("type", "?")), str(fm.get("status", "?")),
               str(fm.get("priority", "?")), str(fm.get("assignee") or "-"),
               str(est) if est is not None else "-", str(spent) if spent is not None else "-",
               str(fm.get("iteration") or "-"), ",".join(fm.get("labels") or []) or "-"]
        if args.spoke:
            link = links.get(args.spoke) or {}
            row.append(str(link.get("id") or "-"))
        if need_charge:
            if charge is None:
                row.append("-")
            elif charge_src == t.id:
                row.append(charge)
            else:
                row.append(f"{charge} (<-{charge_src})")
        row.append(str(fm.get("title", "")))
        rows.append(row)
    ncols = len(rows[0]) - 1 if rows else (9 + bool(args.spoke) + need_charge)
    widths = [max((len(r[i]) for r in rows), default=0) for i in range(ncols)]
    for r in rows:
        print("  ".join(c.ljust(widths[i]) for i, c in enumerate(r[:ncols])) + "  " + r[ncols])
    if args.iteration:
        print(f"\ntotal estimate: {est_total} across {est_n}/{len(rows)} ticket(s) with an estimate set")
        print(f"total time spent: {spent_total} across {spent_n}/{len(rows)} ticket(s) with time logged")
    if args.unpushed:
        print(f"\n{len(rows)} ticket(s) not yet pushed to {args.unpushed}")
    if args.unresolved_charge_code:
        print(f"\n{len(rows)} ticket(s) with no charge_code set anywhere in their parent chain")
    return 0


def cmd_tree(hub: Hub, args) -> int:
    tickets: dict[str, Ticket] = {}
    for path in hub.ticket_paths():
        t, errs = hub.load_ticket(path)
        if not t:
            print("\n".join(errs), file=sys.stderr)
            continue
        tickets[t.id] = t
    if not tickets:
        print("No tickets.")
        return 0

    children: dict[str, list[str]] = {}
    roots: list[str] = []
    for tid, t in tickets.items():
        parent = t.front.get("parent")
        if parent in tickets:
            children.setdefault(parent, []).append(tid)
        else:
            roots.append(tid)
    for kids in children.values():
        kids.sort()
    roots.sort()

    def label(tid: str) -> str:
        fm = tickets[tid].front
        return f"{tid}  [{fm.get('status', '?')}]  {fm.get('title', '')}"

    printed: set[str] = set()
    fancy = can_encode("└├│─")  # fall back to ASCII on consoles that can't draw box characters
    elbow, tee, bar = ("└── ", "├── ", "│   ") if fancy else ("`-- ", "|-- ", "|   ")

    def render(tid: str, prefix: str, seen: frozenset[str]):
        kids = children.get(tid, [])
        for i, kid in enumerate(kids):
            last = i == len(kids) - 1
            connector = elbow if last else tee
            cont = "    " if last else bar
            if kid in seen:
                print(f"{prefix}{connector}{kid}  (parent cycle -- run hub.py validate)")
                continue
            printed.add(kid)
            print(f"{prefix}{connector}{label(kid)}")
            render(kid, prefix + cont, seen | {kid})

    for tid in roots:
        printed.add(tid)
        print(label(tid))
        render(tid, "", frozenset({tid}))

    stray = sorted(set(tickets) - printed)
    if stray:
        print("\nNot reachable from a root (parent cycle -- run hub.py validate):")
        for tid in stray:
            printed.add(tid)
            print(f"  {label(tid)}")
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="hub.py", description=__doc__)
    p.add_argument("--root", type=Path, default=DEFAULT_ROOT, help="hub root (default: parent of tools/)")
    sub = p.add_subparsers(dest="cmd", required=True)

    v = sub.add_parser("validate", help="check schemas, people and tickets (all, or the given IDs)")
    v.add_argument("ids", nargs="*", metavar="T-NNNN")

    n = sub.add_parser("new", help="create a ticket with the next free ID")
    n.add_argument("type")
    n.add_argument("title")
    n.add_argument("--set", action="append", default=[], metavar="FIELD=VALUE",
                   help="set a field (markdown fields go in the body); repeatable")

    ls = sub.add_parser("list", help="list tickets")
    ls.add_argument("--status")
    ls.add_argument("--type")
    ls.add_argument("--parent", metavar="T-NNNN")
    ls.add_argument("--assignee")
    ls.add_argument("--iteration", help="a handle from iterations.yaml; also prints estimate/time-spent totals")
    ls.add_argument("--label", help="a single label/tag value; matches tickets that have it")
    ls.add_argument("--spoke", help="a spoke name; shows only tickets already pushed there, plus their native ID")
    ls.add_argument("--unpushed", metavar="SPOKE", help="a spoke name; shows only tickets NOT yet pushed there")
    ls.add_argument("--charge-code", dest="charge_code",
                     help="shows only tickets whose RESOLVED charge_code (own or inherited via parent) equals this")
    ls.add_argument("--unresolved-charge-code", action="store_true",
                     help="shows only tickets with no charge_code anywhere in their parent chain")

    sub.add_parser("tree", help="show the ticket hierarchy (via `parent`) as an ASCII tree")

    args = p.parse_args(argv)
    safe_stdio()
    hub = Hub(args.root.resolve())
    if hub.errors:
        print("\n".join(hub.errors))
        print(f"{len(hub.errors)} schema/people problem(s); fix these before working with tickets")
        return 1
    return {"validate": cmd_validate, "new": cmd_new, "list": cmd_list, "tree": cmd_tree}[args.cmd](hub, args)


if __name__ == "__main__":
    sys.exit(main())
