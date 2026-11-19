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
pm task set 6 [--subject …] [--body -] [--priority N|none] [--web|--no-web] [--model opus|none] [--tries sonnet,opus|none] [--verify-model opus|none] [--verify|--no-verify] [--worktree <name>]   # open, not running
pm task priority 6 710          # none: back to the plan's
pm task done 5 "2026-10-10, app/api/export.py, tests/test_export.py"   # a task that isn't verified
pm task done 5 "2026-10-10, app/api/export.py" --evidence - <<'EOF'     # a verified task: then a verify run checks it
exports a 3-row table: ran `pytest tests/test_export.py -k rows`, 1 passed
an empty table gives the header only: ran `pytest -k empty`, 1 passed
EOF
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
pm task drop 8 "decision 40: no export" --release              # what required 8 no longer does
```

- Refused: a cycle, a dropped prerequisite, a closed task
- Prerequisite fails: it stays open (investigated), its dependents wait
- Dropping a task that open tasks require: refused, with the chain below it, unless `--replaced-by <task>` (they wait for it instead) or `--release` (they stop waiting); pointless too: drop them first, end of the chain first
- A decision that made the task pointless often makes the whole chain below it pointless: check each task, keep, rewrite (`pm task set --body -`), drop, or raise a decision and make it pending
- Still stuck (`pm kill`, a drop by hand in the past): `pm status` warns; `pm task redo`, `pm task requires <dependent> --remove <it>` or drop the dependent
- Never make a replacement require the task it replaces
- `pm show <task>` lists what it requires and what requires it

A task fits one worker run: one change you can describe, check and commit.

**Model:** pm picks the worker's model by task kind (`[models]` in the config: usually Sonnet for work, Opus for planning and investigations) and climbs a ladder per try (`tries`, e.g. Sonnet, then Opus) after a rejected try or a failed run. By default leave all of it to the config. When splitting a plan:
- `--model opus`: a work task that needs deep reasoning (subtle design, hard bugs, wide refactors); every try
- `--tries sonnet,opus`: a ladder of its own, e.g. a task that a cheap first try may well solve
- `--no-verify` (with `--model sonnet`): a fixed recipe whose evidence is a script's exit code; `--verify-model` only for a reason

### Verification

A task of a verified kind (`pm show <task>`: `models: sonnet → opus, verified by opus`) is done only once an independent verify run accepts it.

- **Worker:** infer the acceptance criteria from the subject and body, in the context of the plan and milestone: what the requester would check to call it done, including cases the body only implies; prove each one (command, test, screenshot), fix and repeat until all pass; then `pm task done <task> "<note>" --evidence -`, one line per criterion: the criterion, what you ran, what it showed. Missing or vague evidence fails the task straight away
- **Verify run** (a fresh session, `PM_VERIFY=1`): infers the criteria itself before reading the evidence, re-runs or spot-checks the proofs, compares with the task, not the claims; read-only (a changed tracked file fails the run and is discarded); ends with `pm task accept <task> "<note>"` or `pm task reject <task> <<'EOF' <what fails, how to check it> EOF`; work beyond the task's spec: a follow-up task, not a rejection
- **Rework:** the worker's session resumes with the findings (a fresh session when the try's model differs, with the findings and the diff so far); `pm show <task>` lists every try with evidence and findings
- After `max_tries` rejected tries: a decision for the human (one more try, accept as is, drop; drop reverts the task's commits in its work tree)

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
