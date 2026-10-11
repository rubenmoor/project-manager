# pm: instructions for agents

`pm` keeps a project's work as **milestones → plans → tasks**, plus **decisions** that only the human can make. Everything lives in `tasks.sql` (SQL text, tracked by git) and is changed only through `pm` commands. `pm run` works the queue one task at a time with a `claude -p` worker.

## Rules

1. **Give every subject on the command line and pipe the body on stdin.** Without a subject, `pm milestone|plan|task add` opens an editor and fails without a terminal. Use a heredoc with a quoted delimiter (`<<'EOF'`), so `$`, backticks and `\` in Markdown stay as written.
2. **Commands that create something print the new integer id on stdout**, and nothing else. Capture it: `tid=$(pm task add 4 "…" <<'EOF' … EOF)`.
3. **IDs are plain integers**, unique per kind: `pm show 12` is task 12, `pm plan edit 3` is plan 3.
4. **Never edit `tasks.sql` by hand** unless the human asks. If you do, run `pm validate` before committing.
5. **Commits:** outside `pm run`, each `pm` command commits `tasks.sql` itself (`pm: …`). As a worker inside `pm run` (environment `PM_WORKER=1`), `pm` doesn't commit: commit `tasks.sql` together with your work, in one commit. Never push.
6. **Bodies are Markdown**, written for an engineer who knows the code but not the intentions: what, why, which files, how to tell it's done.

## Model

- **Milestone:** subject, body (Markdown), status `open` | `implemented`, created date. `pm milestone add` also creates its first plan, the **masterplan** (body "tbd."), and the task "write the masterplan". Once a milestone has no open task, `pm run` queues the task "post-milestone cleanup" in its first plan (body: `cleanup_prompt` in project-manager.toml); when that is closed too, the milestone becomes `implemented`.
- **Plan:** belongs to a milestone; subject, body (required), priority (optional; the masterplan: `masterplan_priority`, 10000). Adding a plan, or changing its subject or body, queues the task "create tasks for plan N", unless that task is already open.
- **Task:** belongs to a plan; subject, body (required while open), status `open` | `done` | `dropped`, note (done: date and files or commit; dropped: the reason), priority (optional; default: the plan's, else 0).
- **Decision:** subject (the question), body (context), options (the recommended one first), status `pending` | `answered` | `irrelevant`. A task linked to a pending decision waits. When the human answers, the task becomes workable again and its worker sees the answer in `pm show`. When a decision is marked irrelevant, the open tasks waiting for it are dropped.
- **Queue:** open tasks of open milestones that no pending decision blocks; highest priority first, then by milestone, plan and task id. `pm run` takes the first one.

## Inspect

```sh
pm status        # open milestones, the first 10 queued tasks, pending decisions
pm queue         # the whole queue
pm show 7        # task 7 with its plan, its milestone, the plan's other tasks and its decisions
pm validate      # check tasks.sql; exit 1 with the problems
```

`pm queue` prints one line per task; the priority appears only when it isn't 0:

```
Queue (3):
  +10000  task 1: write the masterplan
    +800  task 3: create tasks for plan 2
            task 4: tidy up
```

`pm plans list` (or `pm plan list`): plans of open milestones, one per line, highest priority first:

```
 10000  plan 1: masterplan
  1000  plan 3: Database schema
   800  plan 2: Export endpoint
```

`pm show` is what a worker gets in its prompt: Markdown headings for the milestone, plan and task, then the decisions, for example `Decision 1 (answered): Which delimiter? → semicolon`.

## Milestones

```sh
pm milestone add "Users can export their data as CSV" <<'EOF'
Users ask for their data outside the app; support exports it by hand today.

## Outcome
- "Export" on the account page downloads one CSV per table the user owns.

## Out of scope
- Import, other formats.

## Done when
- Covered by tests, including an account with no data.
EOF
# prints the milestone id, e.g. 1; also creates plan "masterplan" and the task "write the masterplan"

pm milestone set 1 --subject "CSV export"
pm milestone set 1 --body - <<'EOF'
New body, replacing the old one.
EOF
pm milestone set 1 --status implemented     # or open
```

Milestones are usually written by the human. A worker changes one only when its task says so.

## Plans

```sh
pm plan add 1 "Backend endpoint" <<'EOF'
`GET /api/export/<table>.csv` streams the rows of one table as CSV.

- Files: `app/api/export.py` (new), `app/api/routes.py`.
- Columns named as in the API, not the database.
EOF
# prints the plan id, e.g. 2, and queues "create tasks for plan 2"

pm plan add 1 "UI button" --priority 500 <<'EOF'
…
EOF

pm plan edit 1 --body - <<'EOF'
The masterplan, divided into parts:

1. Backend endpoint
2. UI button
EOF
pm plan edit 2 --subject "Export endpoint"
pm plan edit 2 --priority 1010   # its tasks without their own priority move up
pm plan edit 2 --priority none   # back to 0
```

A new subject or body queues "create tasks for plan N" again, unless it is already open. A priority change doesn't.

## Tasks

```sh
pm task add 2 "Stream one table as CSV" <<'EOF'
Implement `GET /api/export/<table>.csv` in `app/api/export.py` with the stdlib `csv` module.

Done when: a test exports a table with 3 rows and an empty table.
EOF
# prints the task id, e.g. 5

pm task add 2 "Docs for the export endpoint" --priority 790 <<'EOF'
…
EOF

pm task done 5 "2026-10-10, app/api/export.py, tests/test_export.py"
pm task drop 6 "covered by task 5"
pm task priority 6 810      # earlier than the plan's other tasks (plan priority 800)
pm task priority 6 none     # back to the plan's priority
```

A task should fit one worker run: one change you can describe, check and commit.

## Decisions

```sh
did=$(pm decision add "Which CSV delimiter?" \
        --body "German Excel opens ';' files directly, ',' needs an import step." \
        --option "semicolon" --option "comma")
pm task-pending-decision 5 "$did"       # task 5 waits until the human answers
```

`--option` is repeatable; put the recommended option first. `--body` takes `-` to read stdin. `decision add` doesn't link tasks: run `pm task-pending-decision <task> <decision>` for every task that waits for the answer, including your own task if it can't go on.

The human answers with `pm decide`, or at the start of `pm run`. Don't run `pm decide` yourself: it asks the human in the terminal.

## Run (for the human)

```sh
pm init              # once per project: project-manager.toml and tasks.sql, committed
pm run               # work the queue until it is empty, a worker fails or the tree is dirty
pm run --once        # one task
pm run --dry-run     # print the next worker command; change nothing
pm decide            # answer pending decisions: a number, own text, s = skip, x <reason> = irrelevant
```

Agents don't start `pm run`: a worker is already one task of a run.

## Priorities

- Higher runs first; a task without its own priority gets its plan's.
- Rules for choosing them: `plan_priority_prompt` and `task_priority_prompt` in project-manager.toml, printed at the end of `pm instructions`.
- `pm run` appends them to the worker prompt: the plan rules for tasks of a masterplan (they write plans), the task rules for "create tasks for plan N".

## Recipes

**Write the masterplan** (task "write the masterplan" of plan P): read the milestone (`pm show <task>`) and the code. Write the plan with `pm plan edit P --body -`, divided into parts, and state there that the masterplan's own tasks are not implementation work: each writes the plan for one part. That edit queues "create tasks for plan P". Finish with `pm task done`.

**Split a plan into tasks** (task "create tasks for plan P"): add one `pm task add P "<subject>"` per task, in the order they should run. Priorities: see "Priorities". Raise a decision for each question only the human can answer, and link the tasks that wait for it. Then `pm task done <your task> "<date>, tasks …"`.

**Write a part's plan** (a task of the masterplan): `pm plan add <milestone> "<part subject>" --priority N` with the full body, N by "Priorities". That queues its "create tasks" task. Then close your task.

**Post-milestone cleanup** (task "post-milestone cleanup"): work through its body for the whole project, not just this milestone. Change other milestones with `pm milestone set`; for a pending decision that seems obsolete, say so in a new decision for the human. Then close your task.

**Your task needs an answer first:** add the decision, `pm task-pending-decision <your task> <decision>`, commit what's done, stop. `pm run` reports the task as blocked and goes on with the next.

**Finish a task:** `pm task done <task> "<YYYY-MM-DD, files or commit>"` or `pm task drop <task> "<reason>"`. Run the project's checks, then commit your work and `tasks.sql` in one commit: `task <id>: <subject>`. Don't push.

## Errors

| Message | Meaning |
|---|---|
| `no plan 9`, `no task 9`, `no milestone 9`, `no decision 9` | No such id; look it up with `pm status`, `pm queue` or `pm show` |
| `the change breaks the schema, nothing written: … a plan needs a body` | Empty body: stdin was empty or not piped |
| `… an open task needs a body` | The same for a task |
| `` `pm task add` without a subject needs a terminal `` | Give the subject on the command line |
| `decision 3 is not pending` | It was answered or marked irrelevant already |
| `the database has errors (pm validate)` | `tasks.sql` was edited by hand into an invalid state; `pm validate` lists the problems |
| `no project-manager.toml: run pm init first` | The project has no pm yet; that's the human's call |
