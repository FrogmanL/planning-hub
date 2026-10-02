#!/bin/sh
# Launch the planning-hub terminal UI. Usage: ./tui.sh [tree|kanban|scrum] [options]
exec uv run "$(dirname "$0")/tools/tui.py" "$@"
