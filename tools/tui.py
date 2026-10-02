#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["pyyaml>=6", "windows-curses>=2.3; sys_platform == 'win32'"]
# ///
"""Terminal views over the planning hub: tree, kanban and scrum, with editing (curses, btop-ish; Windows uses the windows-curses package).

    uv run tools/tui.py [tree|kanban]

Keys: 1/2/3/Tab switch view (3 = scrum: kanban for one iteration, no backlog; [ ] or i change iteration) · j/k move · h/l collapse/expand (tree) or change column (kanban)
· Space fold · Enter open the ticket full-screen · d inline detail pane · x closed toggle · r reload · q quit.
Editing: H/L move the ticket to the previous/next status · s status · p priority · n add a dated
note · e any field or section (description, notes, ...) in a built-in text editor (Ctrl-S saves)
· E open the whole ticket in $EDITOR. Every edit is validated like `hub.py validate` and rolled
back if rejected. Cancelling asks for a reason and records it under `## Notes`.
`--dump VIEW [--size WxH] [--keys jjl]` renders one frame as plain text (for testing, no tty).
"""

from __future__ import annotations

import argparse
import curses
import datetime
import locale
import os
import re
import shlex
import subprocess
import sys
import textwrap
from pathlib import Path

import yaml

from hub import DEFAULT_ROOT, FRONTMATTER_RE, HEADING_RE, Hub, heading_for, safe_stdio

try:
    import termios  # POSIX only; used to free Ctrl-S from XOFF
except ImportError:
    termios = None

STATUSES = ["backlog", "todo", "in_progress", "blocked", "in_review", "done", "cancelled"]
CLOSED = {"done", "cancelled"}
STATUS_STYLE = {"backlog": "dim", "todo": "cyan", "in_progress": "yellow", "blocked": "red",
                "in_review": "bold blue", "done": "green", "cancelled": "dim magenta"}
PRIO_RANK = {"urgent": 0, "high": 1, "medium": 2, "low": 3, "none": 4}
PRIO_MARK = {"urgent": "!!", "high": "▲", "medium": "●", "low": "▽", "none": ""}
PRIO_STYLE = {"urgent": "bold red", "high": "yellow", "medium": "cyan", "low": "dim", "none": "dim"}
COLORS = {"red": curses.COLOR_RED, "green": curses.COLOR_GREEN, "yellow": curses.COLOR_YELLOW,
          "blue": curses.COLOR_BLUE, "magenta": curses.COLOR_MAGENTA, "cyan": curses.COLOR_CYAN,
          "white": curses.COLOR_WHITE}


# Single-cell ASCII stand-ins for the box/arrow glyphs, for consoles that can't draw them.
ASCII_TABLE = str.maketrans("▲●▽╭╮╰╯─│▶…·→▮▸▾↑└├", "^*v++++-|>~.>#>v^`+")
ascii_mode = False


# ---- drawing surface -------------------------------------------------------------------


class Canvas:
    """A grid of (char, style) cells. Styles are space-separated tokens: bold dim rev + a color."""

    def __init__(self, h: int, w: int):
        self.h, self.w = h, w
        self.cells = [[(" ", "")] * w for _ in range(h)]

    def put(self, y: int, x: int, text: str, style: str = ""):
        if not 0 <= y < self.h:
            return
        if ascii_mode:
            text = text.translate(ASCII_TABLE)
        for i, ch in enumerate(text):
            if 0 <= x + i < self.w:
                self.cells[y][x + i] = (ch, style)

    def fill(self, y: int, x: int, w: int, style: str):
        self.put(y, x, " " * w, style)

    def box(self, y, x, h, w, title="", style="", title_style="bold", bottom=""):
        if h < 2 or w < 2:
            return
        self.put(y, x, "╭" + "─" * (w - 2) + "╮", style)
        self.put(y + h - 1, x, "╰" + "─" * (w - 2) + "╯", style)
        for r in range(1, h - 1):
            self.put(y + r, x, "│", style)
            self.put(y + r, x + w - 1, "│", style)
        if title:
            self.put(y, x + 2, title[: w - 4], title_style)
        if bottom:
            self.put(y + h - 1, x + w - 2 - len(bottom), bottom, style)

    def text(self) -> str:
        return "\n".join("".join(ch for ch, _ in row).rstrip() for row in self.cells)


def clip(s: str, w: int) -> str:
    return s if len(s) <= w else (s[: max(w - 1, 0)] + "…" if w > 0 else "")


def sty(base: str, sel: bool) -> str:
    return f"{base} rev" if sel else base


# ---- ticket file edits ------------------------------------------------------------------


def dump_value(v) -> str:
    s = yaml.safe_dump(v, default_flow_style=True, width=10**9, allow_unicode=True)
    return s.strip().removesuffix("...").strip()  # plain top-level scalars get a trailing '...'


def set_front_field(text: str, key: str, value) -> str:
    """Set one top-level frontmatter key in place, keeping its trailing comment and every other line."""
    m = FRONTMATTER_RE.match(text)
    if not m:
        raise ValueError("ticket has no frontmatter")
    lines = m.group(1).split("\n")
    new = f"{key}: {dump_value(value)}"
    pat = re.compile(rf"^{re.escape(key)}:(?:[ \t]*(?:\"[^\"]*\"|'[^']*'|\[[^\]]*\]|[^#\n]*?))?"
                     r"(?P<c>[ \t]+#.*)?[ \t]*$")
    for i, ln in enumerate(lines):
        mm = pat.match(ln)
        if mm:
            j = i + 1  # swallow a block-style value (indented or "- " lines) under the key
            while j < len(lines) and (lines[j][:1] in (" ", "\t") or lines[j].startswith("- ")):
                j += 1
            lines[i:j] = [new + (mm.group("c") or "")]
            break
    else:
        lines.append(new)
    return text[: m.start(1)] + "\n".join(lines) + text[m.end(1):]


def section_spans(lines: list[str]) -> list[tuple[str, int, int]]:
    """(heading, heading line, end line) for each `## ` section, skipping code fences (as hub.py does)."""
    heads: list[tuple[str, int]] = []
    fence = None
    for i, line in enumerate(lines):
        stripped = line.lstrip()
        if fence is None and stripped.startswith(("```", "~~~")):
            fence = stripped[:3]
        elif fence is not None and stripped.startswith(fence):
            fence = None
        elif fence is None and (m := HEADING_RE.match(line)):
            heads.append((m.group(1), i))
    return [(h, i, heads[n + 1][1] if n + 1 < len(heads) else len(lines)) for n, (h, i) in enumerate(heads)]


def get_section(text: str, heading: str) -> str | None:
    m = FRONTMATTER_RE.match(text)
    lines = m.group(2).split("\n") if m else []
    for h, a, b in section_spans(lines):
        if h.casefold() == heading.casefold():
            return "\n".join(lines[a + 1:b]).strip()
    return None


def set_section(text: str, heading: str, new_body: str, expect_old: str | None = None) -> str:
    """Replace one `## heading` section's body (appending the section if absent). If expect_old is
    given and the section on disk no longer matches it, refuse rather than clobber."""
    m = FRONTMATTER_RE.match(text)
    if not m:
        raise ValueError("ticket has no frontmatter")
    lines = m.group(2).split("\n")
    body = new_body.strip("\n").split("\n") if new_body.strip() else []
    hit = next((sp for sp in section_spans(lines) if sp[0].casefold() == heading.casefold()), None)
    if hit:
        _, a, b = hit
        if expect_old is not None and "\n".join(lines[a + 1:b]).strip() != expect_old.strip():
            raise ValueError(f"## {heading} changed on disk while editing; reopen it")
        lines[a:b] = [lines[a], ""] + body + [""]
    else:
        while lines and not lines[-1].strip():
            lines.pop()
        lines += ["", f"## {heading}", ""] + body
    return text[: m.start(2)] + "\n".join(lines).rstrip("\n") + "\n"


def append_note(text: str, line: str) -> str:
    """Add a line at the end of the `## Notes` section, creating the section if needed."""
    old = get_section(text, "Notes")
    return set_section(text, "Notes", f"{old}\n{line}" if old else line)


def parse_input(kind: str, text: str):
    s = text.strip()
    if kind == "string":
        return s
    if not s:
        return None
    if kind == "number":
        return yaml.safe_load(s)
    if kind == "tags":
        return [x.strip() for x in s.split(",") if x.strip()]
    return s


# ---- popups ----------------------------------------------------------------------------


class Pick:
    """A list popup. done(value) runs on Enter."""

    def __init__(self, title, options, current, done):
        self.title, self.options, self.done = title, options, done
        self.sel = next((i for i, (_, v) in enumerate(options) if v == current), 0)

    def key(self, k: str) -> bool:
        if k in ("j", "down"):
            self.sel = min(self.sel + 1, len(self.options) - 1)
        elif k in ("k", "up"):
            self.sel = max(self.sel - 1, 0)
        elif k == "enter":
            self.done(self.options[self.sel][1])
            return True
        elif k in ("esc", "q"):
            return True
        return False

    def draw(self, c: Canvas):
        w = min(max([len(self.title) + 4] + [len(lbl) + 6 for lbl, _ in self.options]), c.w - 4)
        h = min(len(self.options) + 2, c.h - 2)
        y, x = (c.h - h) // 2, (c.w - w) // 2
        top = max(0, min(self.sel - (h - 2) + 1, len(self.options) - (h - 2)))
        for r in range(h):
            c.fill(y + r, x, w, "")
        c.box(y, x, h, w, f" {self.title} ", "magenta", "bold magenta")
        for i in range(h - 2):
            idx = top + i
            if idx < len(self.options):
                on = idx == self.sel
                c.fill(y + 1 + i, x + 1, w - 2, "rev" if on else "")
                c.put(y + 1 + i, x + 2, clip(self.options[idx][0], w - 4), "rev" if on else "")


class Prompt:
    """A one-line text popup. done(text) runs on Enter."""

    def __init__(self, title, text, done, hint="Enter save · Esc cancel"):
        self.title, self.text, self.done, self.hint = title, text, done, hint

    def key(self, k: str) -> bool:
        if k == "enter":
            self.done(self.text)
            return True
        if k == "esc":
            return True
        if k == "backspace":
            self.text = self.text[:-1]
        elif k == "ctrl-u":
            self.text = ""
        elif len(k) == 1 and k.isprintable():
            self.text += k
        return False

    def draw(self, c: Canvas):
        w = min(72, c.w - 4)
        h = 5
        y, x = (c.h - h) // 2, (c.w - w) // 2
        for r in range(h):
            c.fill(y + r, x, w, "")
        c.box(y, x, h, w, f" {self.title} ", "magenta", "bold magenta")
        room = w - 5
        c.put(y + 2, x + 2, self.text[-room:], "")
        c.put(y + 2, x + 2 + min(len(self.text), room), " ", "rev")
        c.put(y + 3, x + 2, clip(self.hint, w - 4), "dim")


class TextArea:
    """Full-screen multi-line editor. Ctrl-S calls done(text): None means saved, a string means
    rejected (shown, and the editor stays open so nothing typed is lost)."""

    def __init__(self, title, text, done):
        self.title, self.done = title, done
        self.lines = text.split("\n")
        self.orig = text
        self.r = self.c = self.top = 0
        self.armed = False
        self.err = ""

    @property
    def footer(self):
        return "Ctrl-S save · Esc cancel · arrows/Home/End/PgUp/PgDn move"

    def text(self) -> str:
        return "\n".join(self.lines)

    def key(self, k: str) -> bool:
        was_armed, self.armed, self.err = self.armed, False, ""
        ln = self.lines[self.r]
        if k == "esc":
            if self.text() != self.orig and not was_armed:
                self.armed = True
                return False
            return True
        if k == "ctrl-s":
            self.err = self.done(self.text()) or ""
            return not self.err
        if k == "up":
            self.r = max(self.r - 1, 0)
        elif k == "down":
            self.r = min(self.r + 1, len(self.lines) - 1)
        elif k == "pgup":
            self.r = max(self.r - 10, 0)
        elif k == "pgdn":
            self.r = min(self.r + 10, len(self.lines) - 1)
        elif k == "left":
            if self.c > 0:
                self.c -= 1
            elif self.r > 0:
                self.r -= 1
                self.c = len(self.lines[self.r])
        elif k == "right":
            if self.c < len(ln):
                self.c += 1
            elif self.r < len(self.lines) - 1:
                self.r, self.c = self.r + 1, 0
        elif k == "home":
            self.c = 0
        elif k == "end":
            self.c = len(ln)
        elif k == "enter":
            self.lines[self.r:self.r + 1] = [ln[: self.c], ln[self.c:]]
            self.r, self.c = self.r + 1, 0
        elif k == "backspace":
            if self.c > 0:
                self.lines[self.r] = ln[: self.c - 1] + ln[self.c:]
                self.c -= 1
            elif self.r > 0:
                prev = self.lines[self.r - 1]
                self.lines[self.r - 1:self.r + 1] = [prev + ln]
                self.r, self.c = self.r - 1, len(prev)
        elif k == "delete":
            if self.c < len(ln):
                self.lines[self.r] = ln[: self.c] + ln[self.c + 1:]
            elif self.r < len(self.lines) - 1:
                self.lines[self.r:self.r + 2] = [ln + self.lines[self.r + 1]]
        elif k == "tab":
            self.lines[self.r] = ln[: self.c] + "    " + ln[self.c:]
            self.c += 4
        elif len(k) == 1 and k.isprintable():
            self.lines[self.r] = ln[: self.c] + k + ln[self.c:]
            self.c += 1
        self.c = min(self.c, len(self.lines[self.r]))
        return False

    def draw(self, c: Canvas):
        y, x, h, w = 1, 0, c.h - 2, c.w
        iw, ih = w - 4, h - 2
        for r in range(h):
            c.fill(y + r, x, w, "")
        dirty = " *" if self.text() != self.orig else ""
        c.box(y, x, h, w, f" {self.title}{dirty} ", "magenta", "bold magenta")
        rows, cur_row = [], 0
        for i, ln in enumerate(self.lines):
            if i == self.r:
                cur_row = len(rows) + self.c // iw
            rows += [(i, k * iw) for k in range(len(ln) // iw + 1)]
        self.top = max(0, min(self.top, cur_row))
        if cur_row >= self.top + ih:
            self.top = cur_row - ih + 1
        for n in range(ih):
            if self.top + n < len(rows):
                i, start = rows[self.top + n]
                c.put(y + 1 + n, x + 2, self.lines[i][start:start + iw], "")
        c.put(y + 1 + cur_row - self.top, x + 2 + self.c % iw,
              (self.lines[self.r] + " ")[self.c], "rev")
        if self.armed:
            c.put(y + h - 1, x + 2, " unsaved changes: Esc again to discard, Ctrl-S to save ", "bold yellow")
        elif self.err:
            c.put(y + h - 1, x + 2, clip(f" rejected: {self.err} ", w - 4), "bold red")


class TicketView:
    """Full-screen read view of one ticket: every field and every section, scrollable."""

    def __init__(self, browser: "Browser", tid: str):
        self.b, self.tid = browser, tid
        self.scroll = self.focus = 0
        self.jump = False
        self.offsets: list[int] = []

    footer = "j/k scroll · [ ] section · e edit (field or section) · n add note · s status · p priority · Esc close"

    def headings(self) -> list[str]:
        t = self.b.m.tickets.get(self.tid)
        return [h for h, _ in t.sections] if t else []

    def key(self, k: str) -> bool:
        n = len(self.headings())
        if k in ("esc", "q", "enter"):
            return True
        if k in ("j", "down"):
            self.scroll += 1
        elif k in ("k", "up"):
            self.scroll = max(self.scroll - 1, 0)
        elif k == "pgdn":
            self.scroll += 10
        elif k == "pgup":
            self.scroll = max(self.scroll - 10, 0)
        elif k == "g":
            self.scroll = 0
        elif k == "G":
            self.scroll = 10**6
        elif k in ("]", "tab") and n:
            self.focus, self.jump = (self.focus + 1) % n, True
        elif k == "[" and n:
            self.focus, self.jump = (self.focus - 1) % n, True
        elif k == "e":
            heads = self.headings()
            self.b.edit_menu(self.tid, ("section", heads[self.focus]) if heads else None)
        elif k == "n":
            self.b.add_note(self.tid)
        elif k in ("s", "p"):
            self.b.edit_field(self.tid, "status" if k == "s" else "priority")
        return False

    def draw(self, c: Canvas):
        t = self.b.m.tickets.get(self.tid)
        if not t:
            return
        y, x, h, w = 1, 0, c.h - 2, c.w
        iw, ih = w - 4, h - 2
        fm = t.front
        projected = {heading_for(f["name"]).casefold() for f in self.b.m.hub.types.get(t.type, [])
                     if f["kind"] == "markdown"}
        lines: list[tuple[str, str]] = [(f"{fm.get('title', '')}", "bold")]
        bits = [f"{k} {fm[k]}" for k in ("status", "priority", "assignee", "parent", "iteration") if fm.get(k)]
        if fm.get("estimate") is not None:
            spent = fm.get("time_spent")
            bits.append(f"estimate {fm['estimate']}" + (f" (spent {spent})" if spent is not None else ""))
        if fm.get("labels"):
            bits.append("labels " + ",".join(map(str, fm["labels"])))
        if fm.get("spoke_links"):
            bits.append("pushed " + ",".join(f"{k}:{v.get('id')}" for k, v in fm["spoke_links"].items()))
        lines += [(ln, "dim") for ln in textwrap.wrap("  ".join(bits), iw)] + [("", "")]
        self.offsets = []
        self.focus = min(self.focus, max(len(t.sections) - 1, 0))
        for i, (head, body) in enumerate(t.sections):
            self.offsets.append(len(lines))
            tag = "projected" if head.casefold() in projected else "hub-only"
            on = i == self.focus
            lines.append((f"{'▶' if on else ' '} ## {head}   ({tag})", "bold magenta" if on else "bold"))
            for para in body.split("\n") or [""]:
                indent = re.match(r"\s*(?:[-*] |\d+\. )?", para).group(0)
                lines += [(f"  {ln}", "") for ln in textwrap.wrap(
                    para, iw - 2, subsequent_indent=" " * len(indent), drop_whitespace=True) or [""]]
            lines.append(("", ""))
        if self.jump and self.offsets:
            self.scroll, self.jump = self.offsets[self.focus], False
        self.scroll = max(0, min(self.scroll, max(len(lines) - ih, 0)))
        for r in range(h):
            c.fill(y + r, x, w, "")
        c.box(y, x, h, w, f" {self.tid} · {t.type} ", "magenta", "bold magenta",
              f" {min(self.scroll + ih, len(lines))}/{len(lines)} ")
        for n in range(ih):
            if self.scroll + n < len(lines):
                text, style = lines[self.scroll + n]
                c.put(y + 1 + n, x + 2, clip(text, iw), style)


# ---- data ------------------------------------------------------------------------------


class Model:
    def __init__(self, hub: Hub):
        self.hub = hub
        self.load()

    def load(self):
        self.tickets: dict = {}
        self.problems = 0
        for path in self.hub.ticket_paths():
            t, errs = self.hub.load_ticket(path)
            if t:
                self.tickets[t.id] = t
            else:
                self.problems += 1
        self.children: dict[str, list[str]] = {}
        self.roots: list[str] = []
        for tid, t in self.tickets.items():
            parent = t.front.get("parent")
            if parent in self.tickets and parent != tid:
                self.children.setdefault(parent, []).append(tid)
            else:
                self.roots.append(tid)
        for kids in self.children.values():
            kids.sort()
        self.roots.sort()
        # tickets caught in a parent cycle are unreachable from any root; surface them as roots
        reach: set[str] = set()
        stack = list(self.roots)
        while stack:
            tid = stack.pop()
            if tid not in reach:
                reach.add(tid)
                stack.extend(self.children.get(tid, []))
        self.roots += sorted(set(self.tickets) - reach)
        self._progress: dict[str, tuple[int, int]] = {}

    def f(self, tid: str, key: str):
        return self.tickets[tid].front.get(key)

    def status(self, tid: str) -> str:
        return self.f(tid, "status") or "backlog"

    def prio(self, tid: str) -> str:
        return self.f(tid, "priority") or "none"

    def progress(self, tid: str) -> tuple[int, int]:
        """(done, total) over non-cancelled descendants."""
        if tid not in self._progress:
            self._progress[tid] = (0, 0)  # cycle guard
            done = total = 0
            for kid in self.children.get(tid, []):
                s = self.status(kid)
                if s != "cancelled":
                    total += 1
                    done += s == "done"
                d, n = self.progress(kid)
                done, total = done + d, total + n
            self._progress[tid] = (done, total)
        return self._progress[tid]

    def subtree_open(self, tid: str, seen=frozenset()) -> bool:
        if tid in seen:
            return False
        return self.status(tid) not in CLOSED or any(
            self.subtree_open(k, seen | {tid}) for k in self.children.get(tid, []))

    def ancestors(self, tid: str) -> list[str]:
        out, cur = [], self.f(tid, "parent")
        while cur in self.tickets and cur not in out and cur != tid:
            out.append(cur)
            cur = self.f(cur, "parent")
        return out

    def counts(self) -> dict[str, int]:
        c = {s: 0 for s in STATUSES}
        for tid in self.tickets:
            c[self.status(tid)] = c.get(self.status(tid), 0) + 1
        return c


# ---- the browser -----------------------------------------------------------------------


class Browser:
    def __init__(self, model: Model, view: str = "tree"):
        self.m = model
        self.view = view
        self.detail = False
        self.collapsed: set[str] = set()
        self.hide_closed = False       # tree: hide done/cancelled subtrees
        self.show_cancelled = False    # kanban/scrum: show the cancelled column
        self.iter: str | None = None   # scrum: the iteration handle being shown
        self.ensure_iter()
        self.tsel = self.ttop = 0
        self.kcol = 0
        self.ksel: dict[str, int] = {}
        self.ktop: dict[str, int] = {}
        self.quit = False
        self.modals: list = []
        self.msg = ""
        self.want_editor: str | None = None  # ticket ID to open in $EDITOR; the backend picks this up

    # -- tree model --

    def rows(self) -> list[tuple[str, str, bool]]:
        """Visible tree rows: (id, line prefix, has_children)."""
        rows: list[tuple[str, str, bool]] = []

        def walk(tid, lead, connector, seen):
            kids = [k for k in self.m.children.get(tid, [])
                    if not self.hide_closed or self.m.subtree_open(k)]
            rows.append((tid, lead + connector, bool(kids)))
            if tid in self.collapsed or tid in seen:
                return
            cont = "" if not connector else ("    " if connector.startswith("└") else "│   ")
            for i, k in enumerate(kids):
                walk(k, lead + cont, "└── " if i == len(kids) - 1 else "├── ", seen | {tid})

        for r in self.m.roots:
            if not self.hide_closed or self.m.subtree_open(r):
                walk(r, "", "", frozenset())
        return rows

    # -- kanban model --

    def cols(self) -> list[str]:
        cols = STATUSES if self.show_cancelled else STATUSES[:-1]
        return cols[1:] if self.view == "scrum" else cols  # scrum has no backlog column

    def cards(self, status: str) -> list[str]:
        ids = [t for t in self.m.tickets if self.m.status(t) == status]
        if self.view == "scrum":
            ids = [t for t in ids if self.iter and self.m.f(t, "iteration") == self.iter]
        return sorted(ids, key=lambda t: (PRIO_RANK.get(self.m.prio(t), 4), t))

    # -- iterations (scrum) --

    def iterations(self) -> list[str]:
        """Handles oldest -> newest: by `starts` when every iteration has one, else file order."""
        its = self.m.hub.iterations
        if its and all(isinstance(v, dict) and v.get("starts") for v in its.values()):
            return sorted(its, key=lambda h: str(its[h]["starts"]))
        return list(its)

    def default_iter(self) -> str | None:
        """The most recent iteration that has started (the last one if none has, or no dates)."""
        order = self.iterations()
        today = datetime.date.today().isoformat()
        its = self.m.hub.iterations
        started = [h for h in order if isinstance(its[h], dict) and its[h].get("starts")
                   and str(its[h]["starts"])[:10] <= today]
        return (started or order or [None])[-1]

    def ensure_iter(self):
        if self.iter not in self.m.hub.iterations:
            self.iter = self.default_iter()

    def set_iteration(self, handle: str):
        self.iter = handle
        self.ksel.clear()
        self.ktop.clear()
        self.kcol = min(self.kcol, len(self.cols()) - 1)

    def step_iteration(self, d: int):
        order = self.iterations()
        if self.iter in order:
            new = order[max(0, min(order.index(self.iter) + d, len(order) - 1))]
            if new == self.iter:
                self.msg = "no " + ("newer" if d > 0 else "older") + " iteration"
            else:
                self.set_iteration(new)

    def pick_iteration(self):
        its = self.m.hub.iterations
        if its:
            self.modals.append(Pick("iteration", [(f"{h}  {its[h].get('name', '')}", h) for h in reversed(self.iterations())],
                                    self.iter, self.set_iteration))

    def iteration_summary(self) -> str:
        it = self.m.hub.iterations.get(self.iter) or {}
        tix = [t for t in self.m.tickets if self.m.f(t, "iteration") == self.iter and self.m.status(t) != "cancelled"]
        num = lambda t, k: (v if isinstance(v := self.m.f(t, k), (int, float)) and not isinstance(v, bool) else 0)
        est = sum(num(t, "estimate") for t in tix)
        done_est = sum(num(t, "estimate") for t in tix if self.m.status(t) == "done")
        dates = f"  {str(it['starts'])[:10]} → {str(it.get('ends', '?'))[:10]}" if it.get("starts") else ""
        return (f"{self.iter}  {it.get('name', '')}{dates}  ·  {len(tix)} tickets, "
                f"{sum(self.m.status(t) == 'done' for t in tix)} done  ·  estimate {est:g} ({done_est:g} done)"
                f"  ·  spent {sum(num(t, 'time_spent') for t in tix):g}")

    # -- selection --

    def selected(self) -> str | None:
        if self.view == "tree":
            rows = self.rows()
            return rows[min(self.tsel, len(rows) - 1)][0] if rows else None
        col = self.cols()[self.kcol]
        cards = self.cards(col)
        return cards[min(self.ksel.get(col, 0), len(cards) - 1)] if cards else None

    def select(self, tid: str | None):
        if tid is None or tid not in self.m.tickets:
            return
        if self.view == "tree":
            self.collapsed -= set(self.m.ancestors(tid))
            for i, (rid, _, _) in enumerate(self.rows()):
                if rid == tid:
                    self.tsel = i
                    return
        else:
            it = self.m.f(tid, "iteration")
            if self.view == "scrum" and it in self.m.hub.iterations and it != self.iter:
                self.set_iteration(it)
            s = self.m.status(tid)
            if s in self.cols() and tid in self.cards(s):
                self.kcol = self.cols().index(s)
                self.ksel[s] = self.cards(s).index(tid)
            else:
                self.kcol = min(self.kcol, len(self.cols()) - 1)

    def switch(self, view: str):
        if view != self.view:
            keep = self.selected()
            self.view = view
            self.ensure_iter()
            self.select(keep)

    def reload(self, keep: str | None = None):
        keep = keep or self.selected()
        self.m.load()
        self.select(keep)

    # -- editing --

    def check_file(self, path: Path) -> list[str]:
        hub = self.m.hub
        t, errs = hub.load_ticket(path)
        return errs if not t else hub.check_ticket(t, {p.stem for p in hub.ticket_paths()})

    def mutate(self, tid: str, fn, done_msg: str):
        """Apply fn(text) -> text to the ticket file as it is on disk now; roll back if invalid."""
        path = self.m.tickets[tid].path
        old = path.read_text(encoding="utf-8")
        try:
            path.write_text(fn(old), encoding="utf-8")
            errs = self.check_file(path)
        except ValueError as e:
            errs = [str(e)]
        if errs:
            path.write_text(old, encoding="utf-8")
            self.msg = "rejected, file unchanged: " + errs[0].split(": ", 1)[-1]
            return False
        links = (self.m.f(tid, "spoke_links") or {})
        self.reload(tid)
        self.msg = f"{tid}: {done_msg}" + (f"  (pushed to {', '.join(links)}: not synced)" if links else "")
        return True

    def set_field(self, tid: str, name: str, value):
        self.mutate(tid, lambda text: set_front_field(text, name, value), f"{name} -> {value if value is not None else 'none'}")

    def set_status(self, tid: str, new: str):
        if new == self.m.status(tid):
            return
        if new != "cancelled":
            self.set_field(tid, "status", new)
            return

        def cancel(reason: str):
            reason = reason.strip()
            if not reason:
                self.msg = "cancelling needs a reason (recorded under ## Notes)"
                return
            note = f"- Cancelled {datetime.date.today().isoformat()}: {reason}"
            self.mutate(tid, lambda text: append_note(set_front_field(text, "status", "cancelled"), note),
                        "cancelled, reason added to ## Notes")

        self.modals.append(Prompt(f"{tid}: why cancelled?", "", cancel))

    def shift_status(self, d: int):
        tid = self.selected()
        if not tid:
            return
        self.m.load()  # act on what is on disk, not a stale view
        cols = self.cols()
        cur = self.m.status(tid)
        if cur not in cols:
            self.msg = f"{cur} is not a visible column"
            return
        new = cols[max(0, min(cols.index(cur) + d, len(cols) - 1))]
        if new == cur:
            self.msg = f"already {'last' if d > 0 else 'first'} column"
        else:
            self.set_status(tid, new)

    def edit_field(self, tid: str, name: str):
        hub = self.m.hub
        fdef = next(f for f in hub.types[self.m.f(tid, "type")] if f["name"] == name)
        kind, cur = fdef["kind"], self.m.f(tid, name)
        if name == "status":
            self.modals.append(Pick(f"{tid} status", [(v, v) for v in STATUSES], cur, lambda v: self.set_status(tid, v)))
        elif kind == "enum":
            self.modals.append(Pick(f"{tid} {name}", [(v, v) for v in fdef["values"]], cur,
                                    lambda v: self.set_field(tid, name, v)))
        elif kind in ("person", "iteration"):
            registry = hub.people if kind == "person" else hub.iterations
            if not registry:
                self.msg = f"no {'people' if kind == 'person' else 'iterations'} registered yet ({kind}s.yaml)".replace("persons", "people")
                return
            self.modals.append(Pick(f"{tid} {name}", [("(none)", None)] + [(h, h) for h in registry], cur,
                                    lambda v: self.set_field(tid, name, v)))
        else:
            shown = ", ".join(map(str, cur)) if isinstance(cur, list) else ("" if cur is None else str(cur))
            hint = {"tags": "comma-separated", "number": "a number", "ref": "a ticket ID like T-0001"}.get(kind, "")
            self.modals.append(Prompt(f"{tid} {name}", shown, lambda text: self.set_field(tid, name, parse_input(kind, text)),
                                      (hint + " · " if hint else "") + "empty clears · Enter save · Esc cancel"))

    def edit_menu(self, tid: str | None = None, current=None):
        tid = tid or self.selected()
        if not tid:
            return
        self.m.load()
        t = self.m.tickets[tid]
        fields = [f for f in self.m.hub.types.get(t.type, [])
                  if f["kind"] != "markdown" and f["name"] not in ("id", "type")]

        def label(f):
            v = self.m.f(tid, f["name"])
            return f"{f['name']:<12} {', '.join(map(str, v)) if isinstance(v, list) else ('' if v is None else v)}"

        options = [(label(f), f["name"]) for f in fields]
        heads = [h for h, _ in t.sections]
        for want in [heading_for(f["name"]) for f in self.m.hub.types.get(t.type, []) if f["kind"] == "markdown"] + ["Notes"]:
            if not any(h.casefold() == want.casefold() for h in heads):
                heads.append(want)
        for h in heads:
            body = dict((k.casefold(), v) for k, v in t.sections).get(h.casefold(), "")
            options.append((f"## {h:<9} ({len(body.splitlines())} lines)" if body else f"## {h:<9} (empty)",
                            ("section", h)))
        self.modals.append(Pick(f"edit {tid}", options, current,
                                lambda v: self.edit_section(tid, v[1]) if isinstance(v, tuple) else self.edit_field(tid, v)))

    def edit_section(self, tid: str, heading: str):
        path = self.m.tickets[tid].path
        old = get_section(path.read_text(encoding="utf-8"), heading) or ""

        def save(new: str):
            if new.strip() == old.strip():
                self.msg = "no changes"
                return None
            ok = self.mutate(tid, lambda text: set_section(text, heading, new, expect_old=old), f"## {heading} saved")
            return None if ok else self.msg.removeprefix("rejected, file unchanged: ")

        self.modals.append(TextArea(f"{tid} · ## {heading}", old, save))

    def add_note(self, tid: str | None = None):
        tid = tid or self.selected()
        if not tid:
            return

        def add(text: str):
            if text.strip():
                line = f"- {datetime.date.today().isoformat()}: {text.strip()}"
                self.mutate(tid, lambda t: append_note(t, line), "note added")

        self.modals.append(Prompt(f"{tid}: note (dated, added under ## Notes)", "", add))

    def after_external(self, tid: str):
        path = self.m.tickets[tid].path
        errs = self.check_file(path)
        self.reload(tid)
        self.msg = f"{tid}: saved" if not errs else "INVALID, fix it: " + errs[0].split(": ", 1)[-1]

    # -- keys --

    def key(self, k: str):
        self.msg = ""
        if self.modals:
            top = self.modals[-1]
            if top.key(k) and top in self.modals:  # a done() callback may have pushed the next popup
                self.modals.remove(top)
            return
        if k in ("q", "esc"):
            self.quit = True
        elif k == "H":
            self.shift_status(-1)
        elif k == "L":
            self.shift_status(1)
        elif k in ("s", "p", "e", "E") and self.selected():
            tid = self.selected()
            if k == "e":
                self.edit_menu()
            elif k == "E":
                self.want_editor = tid
            else:
                self.edit_field(tid, "status" if k == "s" else "priority")
        elif k == "1":
            self.switch("tree")
        elif k == "2":
            self.switch("kanban")
        elif k == "3":
            self.switch("scrum")
        elif k == "tab":
            order = ["tree", "kanban", "scrum"]
            self.switch(order[(order.index(self.view) + 1) % len(order)])
        elif self.view == "scrum" and k in ("[", "]", "i"):
            if k == "i":
                self.pick_iteration()
            else:
                self.step_iteration(-1 if k == "[" else 1)
        elif k == "enter" and self.selected():
            self.modals.append(TicketView(self, self.selected()))
        elif k == "d":
            self.detail = not self.detail
        elif k == "n" and self.selected():
            self.add_note()
        elif k == "r":
            self.reload()
        elif self.view == "tree":
            self.key_tree(k)
        else:
            self.key_kanban(k)

    def key_tree(self, k: str):
        rows = self.rows()
        if not rows:
            return
        self.tsel = min(self.tsel, len(rows) - 1)
        tid, _, has_kids = rows[self.tsel]
        if k in ("j", "down"):
            self.tsel = min(self.tsel + 1, len(rows) - 1)
        elif k in ("k", "up"):
            self.tsel = max(self.tsel - 1, 0)
        elif k == "g":
            self.tsel = 0
        elif k == "G":
            self.tsel = len(rows) - 1
        elif k in ("l", "right"):
            if has_kids and tid in self.collapsed:
                self.collapsed.discard(tid)
            elif has_kids:
                self.tsel = min(self.tsel + 1, len(rows) - 1)
        elif k in ("h", "left"):
            if has_kids and tid not in self.collapsed:
                self.collapsed.add(tid)
            else:
                anc = self.m.ancestors(tid)
                if anc:
                    self.select(anc[0])
        elif k == " " and has_kids:
            self.collapsed ^= {tid}
        elif k == "z":
            self.collapsed = {r for r, _, kids in rows if kids}
            self.tsel = 0
        elif k == "a":
            self.collapsed.clear()
        elif k == "x":
            keep = tid
            self.hide_closed = not self.hide_closed
            self.select(keep)

    def key_kanban(self, k: str):
        cols = self.cols()
        col = cols[self.kcol]
        n = len(self.cards(col))
        if k in ("l", "right"):
            self.kcol = min(self.kcol + 1, len(cols) - 1)
        elif k in ("h", "left"):
            self.kcol = max(self.kcol - 1, 0)
        elif k in ("j", "down"):
            self.ksel[col] = min(self.ksel.get(col, 0) + 1, max(n - 1, 0))
        elif k in ("k", "up"):
            self.ksel[col] = max(self.ksel.get(col, 0) - 1, 0)
        elif k == "g":
            self.ksel[col] = 0
        elif k == "G":
            self.ksel[col] = max(n - 1, 0)
        elif k == "x":
            self.show_cancelled = not self.show_cancelled
            self.kcol = min(self.kcol, len(self.cols()) - 1)

    # -- drawing --

    def draw(self, c: Canvas):
        if c.h < 8 or c.w < 40:
            c.put(0, 0, "terminal too small", "red")
            return
        self.draw_header(c)
        detail_h = 9 if self.detail and c.h >= 24 else 0
        body_h = c.h - 2 - detail_h
        if self.view == "tree":
            self.draw_tree(c, 1, 0, body_h, c.w)
        elif self.view == "scrum":
            self.draw_scrum(c, 1, 0, body_h, c.w)
        else:
            self.draw_kanban(c, 1, 0, body_h, c.w)
        tid = self.selected()
        if detail_h and tid:
            self.draw_detail(c, 1 + body_h, 0, detail_h, c.w, tid)
        self.draw_footer(c)
        for modal in self.modals:
            modal.draw(c)

    def draw_header(self, c: Canvas):
        x = 1
        c.put(0, x, " hub ", "bold cyan rev")
        x += 6
        for key, name in (("tree", "1 tree"), ("kanban", "2 kanban"), ("scrum", "3 scrum")):
            on = self.view == key
            c.put(0, x, f" {name} ", "bold rev" if on else "dim")
            x += len(name) + 3
        counts = self.m.counts()
        parts = [(f"{counts[s]} {s.replace('_', ' ')}", STATUS_STYLE[s]) for s in STATUSES if counts[s]]
        total = sum(len(p) + 3 for p, _ in parts)
        x = c.w - total - 1
        if x > 30:
            for text, style in parts:
                c.put(0, x, "▮ ", style)
                c.put(0, x + 2, text, "")
                x += len(text) + 3

    def draw_footer(self, c: Canvas):
        if self.view == "tree":
            help_ = "j/k move  h/l fold  H/L status  s status  p prio  n note  e edit  E editor  x hide closed  Enter open  d pane  q quit"
        elif self.view == "scrum":
            help_ = "h/l column  j/k card  [ ] iteration  i pick  H/L move card  s status  n note  e edit  E editor  x cancelled  Enter open  q quit"
        else:
            help_ = "h/l column  j/k card  H/L move card  s status  p prio  n note  e edit  E editor  x cancelled  Enter open  d pane  q quit"
        if self.msg:
            c.put(c.h - 1, 1, clip(self.msg, c.w - 2), "bold yellow")
            return
        if self.modals and hasattr(self.modals[-1], "footer"):
            c.put(c.h - 1, 1, clip(self.modals[-1].footer, c.w - 2), "dim")
            return
        if self.m.problems:
            help_ += f"   ! {self.m.problems} unreadable ticket(s): run hub.py validate"
        c.put(c.h - 1, 1, clip(help_, c.w - 2), "dim")

    def draw_tree(self, c: Canvas, y, x, h, w):
        rows = self.rows()
        inner_h, inner_w = h - 2, w - 2
        self.tsel = max(0, min(self.tsel, len(rows) - 1))
        self.ttop = max(0, min(self.ttop, self.tsel))
        if self.tsel >= self.ttop + inner_h:
            self.ttop = self.tsel - inner_h + 1
        shown = f" {self.tsel + 1}/{len(rows)} " if rows else ""
        title = " tree " + ("(closed hidden) " if self.hide_closed else "")
        c.box(y, x, h, w, title, "cyan", "bold cyan", shown)
        wide = inner_w >= 70
        for i in range(inner_h):
            idx = self.ttop + i
            if idx >= len(rows):
                break
            tid, prefix, has_kids = rows[idx]
            sel = idx == self.tsel
            ry, rx = y + 1 + i, x + 1
            status = self.m.status(tid)
            prio = self.m.prio(tid)
            done, total = self.m.progress(tid)
            prog = f"{done}/{total}" if total else ""
            est = self.m.f(tid, "estimate")
            right = f"{prog:>5} {status:<11} {PRIO_MARK[prio]:<2}"
            if wide:
                right += f" {str(self.m.f(tid, 'assignee') or ''):<8.8} {'' if est is None else est:>4}"
            if sel:
                c.fill(ry, rx, inner_w, "rev")
            marker = ("▸ " if tid in self.collapsed else "▾ ") if has_kids else "· "
            dim = status in CLOSED
            left_w = inner_w - len(right) - 2
            lead = f" {prefix}"
            c.put(ry, rx, clip(lead, left_w), sty("dim", sel))
            px = rx + len(lead)
            for text, style in ((marker, "dim"), (f"{tid}  ", "bold" if not dim else "dim"),
                                (str(self.m.f(tid, "title") or ""), "dim" if dim else "")):
                room = rx + left_w - px
                if room <= 0:
                    break
                c.put(ry, px, clip(text, room), sty(style, sel))
                px += len(text)
            rx2 = rx + inner_w - len(right) - 1
            c.put(ry, rx2, f"{prog:>5} ", sty("dim", sel))
            c.put(ry, rx2 + 6, f"{status:<11}", sty(STATUS_STYLE[status], sel))
            c.put(ry, rx2 + 18, f"{PRIO_MARK[prio]:<2}", sty(PRIO_STYLE[prio], sel))
            if wide:
                c.put(ry, rx2 + 21, right[21:], sty("dim", sel))
        if not rows:
            c.put(y + 1, x + 2, "No tickets.", "dim")

    def draw_scrum(self, c: Canvas, y, x, h, w):
        self.ensure_iter()
        if not self.iter:
            c.box(y, x, h, w, " scrum ", "cyan", "bold cyan")
            c.put(y + 1, x + 2, "No iterations defined: add one to iterations.yaml (handle, name, starts, ends).", "dim")
            return
        order = self.iterations()
        pos = f" {order.index(self.iter) + 1}/{len(order)} " if self.iter in order else ""
        c.put(y, x + 1, clip(self.iteration_summary(), w - len(pos) - 3), "bold")
        c.put(y, x + w - len(pos) - 1, pos, "dim")
        self.draw_kanban(c, y + 1, x, h - 1, w)

    def draw_kanban(self, c: Canvas, y, x, h, w):
        cols = self.cols()
        self.kcol = max(0, min(self.kcol, len(cols) - 1))
        cw = w // len(cols)
        card_h = 5
        for ci, status in enumerate(cols):
            cx = x + ci * cw
            wcol = cw if ci < len(cols) - 1 else w - ci * cw
            focus = ci == self.kcol
            cards = self.cards(status)
            inner_h, inner_w = h - 2, wcol - 2
            per = max(inner_h // card_h, 1)
            sel = max(0, min(self.ksel.get(status, 0), len(cards) - 1))
            self.ksel[status] = sel
            top = max(0, min(self.ktop.get(status, 0), sel))
            if sel >= top + per:
                top = sel - per + 1
            self.ktop[status] = top
            border = STATUS_STYLE[status] + (" bold" if focus else "")
            title = f" {status.replace('_', ' ')} {len(cards)} "
            more = f" {sel + 1}/{len(cards)} " if cards and len(cards) > per else ""
            c.box(y, cx, h, wcol, title, border, border, more)
            for i in range(per):
                idx = top + i
                if idx >= len(cards):
                    break
                self.draw_card(c, y + 1 + i * card_h, cx + 1, inner_w, cards[idx], focus and idx == sel)

    def draw_card(self, c: Canvas, y, x, w, tid, sel):
        prio = self.m.prio(tid)
        est = self.m.f(tid, "estimate")
        if sel:
            for r in range(4):
                c.fill(y + r, x, w, "rev")
        right = ("" if est is None else f"{est}pt") + (" " if est is not None and PRIO_MARK[prio] else "")
        mark = PRIO_MARK[prio]
        c.put(y, x + 1, tid, sty("bold", sel))
        tail = f"{right}{mark}"
        c.put(y, x + w - len(tail) - 1, right, sty("dim", sel))
        c.put(y, x + w - len(mark) - 1, mark, sty(PRIO_STYLE[prio], sel))
        lines = textwrap.wrap(str(self.m.f(tid, "title") or ""), max(w - 2, 4)) or [""]
        if len(lines) > 2:
            lines = [lines[0], clip(" ".join(lines[1:]), w - 2)]
        for i, line in enumerate(lines[:2]):
            c.put(y + 1 + i, x + 1, clip(line, w - 2), sty("dim" if self.m.status(tid) in CLOSED else "", sel))
        meta = []
        parent = self.m.f(tid, "parent")
        if parent:
            meta.append(f"↑{parent}")
        if self.m.f(tid, "assignee"):
            meta.append(f"@{self.m.f(tid, 'assignee')}")
        done, total = self.m.progress(tid)
        if total:
            meta.append(f"{done}/{total}")
        c.put(y + 3, x + 1, clip(" ".join(meta), w - 2), sty("dim", sel))

    def draw_detail(self, c: Canvas, y, x, h, w, tid):
        t = self.m.tickets[tid]
        fm = t.front
        c.box(y, x, h, w, f" {tid} ", "magenta", "bold magenta")
        inner = w - 4
        c.put(y + 1, x + 2, clip(f"{fm.get('type', '')}  {fm.get('title', '')}", inner), "bold")
        bits = [f"status {fm.get('status')}", f"priority {fm.get('priority')}"]
        for key in ("assignee", "parent", "iteration"):
            if fm.get(key):
                bits.append(f"{key} {fm[key]}")
        if fm.get("estimate") is not None:
            spent = fm.get("time_spent")
            bits.append(f"estimate {fm['estimate']}" + (f" (spent {spent})" if spent is not None else ""))
        if fm.get("labels"):
            bits.append("labels " + ",".join(map(str, fm["labels"])))
        c.put(y + 2, x + 2, clip("  ".join(bits), inner), "dim")
        desc = (t.section("description") or [""])[0].strip()
        out: list[str] = []
        for para in desc.splitlines():
            out += textwrap.wrap(para, inner) or [""]
        for i, line in enumerate(out[: h - 5]):
            c.put(y + 4 + i, x + 2, line, "")
        if len(out) > h - 5:
            c.put(y + h - 2, x + 2, "…", "dim")


# ---- backends --------------------------------------------------------------------------


KEYMAP = {curses.KEY_UP: "up", curses.KEY_DOWN: "down", curses.KEY_LEFT: "left",
          curses.KEY_RIGHT: "right", curses.KEY_ENTER: "enter", curses.KEY_BACKSPACE: "backspace",
          curses.KEY_HOME: "home", curses.KEY_END: "end", curses.KEY_PPAGE: "pgup",
          curses.KEY_NPAGE: "pgdn", curses.KEY_DC: "delete"}
CHARMAP = {"\n": "enter", "\r": "enter", "\t": "tab", "\x1b": "esc", "\x7f": "backspace",
           "\b": "backspace", "\x15": "ctrl-u", "\x13": "ctrl-s"}


def run_editor(scr, tid: str, browser: Browser):
    curses.def_prog_mode()
    curses.endwin()
    try:
        path = str(browser.m.tickets[tid].path)
        if os.name == "nt":  # let the shell parse quoting and backslashes
            subprocess.call(f'{os.environ.get("EDITOR") or "notepad"} "{path}"', shell=True)
        else:
            subprocess.call([*shlex.split(os.environ.get("EDITOR") or "vi"), path])
    except OSError as e:
        browser.msg = f"cannot run $EDITOR: {e}"
        curses.reset_prog_mode()
        return
    curses.reset_prog_mode()
    scr.clear()
    browser.after_external(tid)


def run_curses(browser: Browser):
    def loop(scr):
        curses.curs_set(0)
        curses.use_default_colors()
        pairs = {}
        for i, (name, col) in enumerate(COLORS.items(), 1):
            curses.init_pair(i, col, -1)
            pairs[name] = curses.color_pair(i)
        flags = {"bold": curses.A_BOLD, "dim": curses.A_DIM, "rev": curses.A_REVERSE}

        def attr(style: str) -> int:
            a = 0
            for tok in style.split():
                a |= flags.get(tok) or pairs.get(tok, 0)
            return a

        scr.keypad(True)
        try:  # let Ctrl-S reach us instead of freezing the terminal (XOFF)
            if termios is None:
                raise OSError
            attrs = termios.tcgetattr(sys.stdin.fileno())
            attrs[0] &= ~termios.IXON
            termios.tcsetattr(sys.stdin.fileno(), termios.TCSANOW, attrs)
        except (getattr(termios, "error", OSError), OSError, ValueError):
            pass
        while not browser.quit:
            h, w = scr.getmaxyx()
            canvas = Canvas(h, w)
            browser.draw(canvas)
            scr.erase()
            for y, row in enumerate(canvas.cells):
                x = 0
                while x < w:  # write runs of equal style at once
                    end = x
                    while end < w and row[end][1] == row[x][1]:
                        end += 1
                    try:
                        scr.addstr(y, x, "".join(ch for ch, _ in row[x:end]), attr(row[x][1]))
                    except curses.error:  # bottom-right cell
                        pass
                    x = end
            scr.refresh()
            try:
                k = scr.get_wch()
            except curses.error:
                continue
            if k == curses.KEY_RESIZE and hasattr(curses, "resize_term"):
                curses.resize_term(0, 0)  # PDCurses (Windows) needs this to pick up the new size
                continue
            name = KEYMAP.get(k) if isinstance(k, int) else CHARMAP.get(k, k)
            if name:
                browser.key(name)
            if browser.want_editor:
                tid, browser.want_editor = browser.want_editor, None
                run_editor(scr, tid, browser)

    locale.setlocale(locale.LC_ALL, "")
    os.environ.setdefault("ESCDELAY", "25")
    curses.wrapper(loop)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="tui.py", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("view", nargs="?", choices=["tree", "kanban", "scrum"], default="tree")
    p.add_argument("--root", type=Path, default=DEFAULT_ROOT, help="hub root (default: parent of tools/)")
    p.add_argument("--ascii", action=argparse.BooleanOptionalAction, default=None,
                   help="draw with plain ASCII instead of box/arrow glyphs (default: on for the legacy Windows console)")
    p.add_argument("--dump", action="store_true", help="print one frame as plain text and exit")
    p.add_argument("--size", default="110x36", metavar="WxH", help="frame size for --dump")
    p.add_argument("--keys", default="", help="keys to press before --dump; Enter is '\\n', Tab is '\\t'")
    args = p.parse_args(argv)
    global ascii_mode
    ascii_mode = args.ascii if args.ascii is not None else (
        os.name == "nt" and not (os.environ.get("WT_SESSION") or os.environ.get("TERM_PROGRAM")))
    safe_stdio()

    hub = Hub(args.root.resolve())
    if hub.errors:
        print("\n".join(hub.errors))
        print(f"{len(hub.errors)} schema/people problem(s); fix these first (hub.py validate)")
        return 1
    browser = Browser(Model(hub), args.view)
    if args.dump:
        for ch in args.keys:
            browser.key(CHARMAP.get(ch, ch))
        w, h = (int(n) for n in args.size.lower().split("x"))
        canvas = Canvas(h, w)
        browser.draw(canvas)
        print(canvas.text())
        return 0
    if not sys.stdout.isatty():
        print("tui.py needs a terminal (use --dump for a plain-text frame)", file=sys.stderr)
        return 1
    run_curses(browser)
    return 0


if __name__ == "__main__":
    sys.exit(main())
