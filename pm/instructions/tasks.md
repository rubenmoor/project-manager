## Tasks

```sh
pm task add 2 "Stream one table as CSV" --worktree app <<'EOF'
Implement `GET /api/export/<table>.csv` in `app/api/export.py` with the stdlib `csv` module.

Done when: a test exports a table with 3 rows and an empty table.
EOF
pm task add 1 "write plan: Export endpoint" --kind plan --worktree app <<'EOF'
Write the plan for part 1 of the masterplan.
EOF
pm task add 2 "Docs for the export endpoint" --priority 690 --worktree app <<'EOF'
…
EOF
pm task add 2 "Untangle the cache invalidation" --model opus --worktree app <<'EOF'   # needs deep reasoning
…
EOF
pm task set 6 [--subject …] [--body -] [--priority N|none] [--web|--no-web] [--model opus|none] [--worktree <name>]   # open, not running
pm task priority 6 710          # none: back to the plan's
pm task done 5 "2026-10-10, app/api/export.py, tests/test_export.py"
pm task drop 6 "covered by task 5"
```

### Dependencies

A task that needs another task's result requires it: pm runs it only once that task is done (and, if it ran in another work tree, merged into the trunk). This is the only strict order; priority is a hint for what goes first and orders nothing across work trees.

```sh
pm task add 2 "Docs for the export endpoint" --requires 5 --requires 7 --worktree docs <<'EOF'
…
EOF
pm task requires 9 5 7          # an open task; a running one only by its own worker
pm task requires 9 --remove 7
pm task drop 6 "split into tasks 11 and 12" --replaced-by 12   # 12 takes over what 6 required and what required 6
```

- Refused: a cycle, a dropped prerequisite, a closed task
- Prerequisite fails: it stays open (investigated), its dependents wait
- Prerequisite dropped: its dependents wait for good; drop it with `--replaced-by`, or `pm task requires <dependent> --remove <it>`; `pm task redo` moves the links to the new task
- `pm show <task>` lists what it requires and what requires it

A task fits one worker run: one change you can describe, check and commit.

**Model:** pm picks the worker's model by task kind (`[models]` in the config: usually Sonnet for work, Opus for planning and investigations) and switches a task to Opus after a failed run. When splitting a plan, mark a work task that needs deep reasoning (subtle design, hard bugs, wide refactors) `--model opus`; leave the rest to pm.

**Work trees:** `pm task add` needs `--worktree <name>`, one of the work trees your prompt lists with what belongs in each; pick it by what the task is about, not by your own task's. A work tree runs one task at a time, in parallel with the other work trees; pm merges it into the trunk after each task. **Priority orders tasks only within one work tree:** a task that must wait for another requires it (`--requires`), in whichever work tree. In a work tree, stay in it; pm's commands still use the trunk's database.

**Web access:** workers have no WebSearch or WebFetch, except for a task marked web (`--web`). Mark only a task that needs it, e.g. "verify the sourced figures"; its worker gets both tools once the human approves the sign-off (if `"web"` is in `signoff`). Your own task needs the web: `pm task set <your task> --web`, then stop without closing it, even when pm reminds you it is open; the next run has the tools. Don't ask for web access with a decision, and don't edit settings to grant it.

### Follow-up tasks

Work you find beyond your task is a follow-up task: add it, don't do it, don't leave it in a note.

- A bug or gap your task doesn't cover, a missing test, a part the plan forgot, cleanup your change makes necessary
- Your task can't be done without it: it's part of your task, do it
- Into the plan it belongs to, usually your task's; `pm show <your task>` lists the plan's tasks: don't add one twice
- Body: what, where, done when; for a worker without your context
- No `--priority` unless it should rather run earlier or later than its plan's other tasks
- It needs another task's result first: `--requires <task id>`, not a priority
- It needs the human's answer first: also `pm decision add` and `pm task-pending-decision <new task> <decision>`

```sh
pm task add 2 "Escape newlines in CSV cells" --worktree app <<'EOF'
`app/api/export.py` writes cells with newlines unquoted; found in task 5.

Done when: a test exports a cell with a newline and reads it back.
EOF
```
