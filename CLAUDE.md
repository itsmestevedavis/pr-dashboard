# pr-dashboard

A local PR dashboard server (`server.py`) that spawns headless `claude -p` jobs for
PR workflows. Launched via `./start.sh` (which `cd`s into this repo first, so jobs run
with their working directory here and this file loads as project memory).

## PR workflows

Each Nudge/#Channel/Review/Address/etc. button spawns a headless `claude -p` session
whose prompt already carries the full step-by-step workflow, appended from an editable
file under `~/.config/pr-dashboard/` (e.g. `nudge_workflow.md`). There is nothing to
follow here — read and obey the workflow embedded in the prompt you were given. Edit or
reset those files from the dashboard's Status tab.
