# Planning hub

This directory is a tool-neutral planning hub. **Read `HUB.md` before creating or editing
anything here** — it holds the rules (ticket IDs, validation, hub-only vs projected content,
what never to touch).

- Run the tool with `uv run tools/hub.py {validate|new|list|tree}`. Validate after every edit.
- `docs/ticket-format.md` describes the ticket format; `README.md` covers install and usage.
- Do not push to or pull from any external tool (Plane, EWM, Jira, Rally) unless explicitly asked.
