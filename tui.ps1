# Launch the planning-hub terminal UI. Usage: .\tui.ps1 [tree|kanban|scrum] [options]
uv run (Join-Path $PSScriptRoot 'tools/tui.py') @args
exit $LASTEXITCODE
