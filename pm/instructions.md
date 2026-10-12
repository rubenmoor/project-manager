# pm: instructions for agents

`pm` keeps a project's work as **milestones → plans → tasks**, plus **decisions** that only the human can make. Everything lives in `tasks.sql` (SQL text, tracked by git) and is changed only through `pm` commands. `pm run` works the queue one task at a time with a `claude -p` worker.

## Rules

1. **Give every subject on the command line and pipe the body on stdin.** Without a subject, `pm milestone|plan|task add` opens an editor and fails without a terminal. Use a heredoc with a quoted delimiter (`<<'EOF'`), so `$`, backticks and `\` in Markdown stay as written.
2. **Commands that create something print the new integer id on stdout**, and nothing else. Capture it: `tid=$(pm task add 4 "…" <<'EOF' … EOF)`.
3. **IDs are plain integers**, unique per kind: `pm show 12` is task 12, `pm plan edit 3` is plan 3.
4. **Never edit `tasks.sql` by hand** unless the human asks. If you do, run `pm validate` before committing.
5. **Commits:** outside `pm run`, each `pm` command commits `tasks.sql` itself (`pm: …`). As a worker inside `pm run` (environment `PM_WORKER=1`): stage (`git add`) the changes meant to stay, delete temporary files, don't commit; `pm run` commits what is staged with `tasks.sql` as `task <id>: <subject>`. Never push.
6. **Bodies are Markdown**, written for an engineer who knows the code but not the intentions: what, why, which files, how to tell it's done.

## Model

- **Milestone:** subject, body (Markdown), status `open` | `on-hold` | `implemented`, created date. `pm milestone add` also creates its first plan, the **masterplan** (body "tbd."), and the task "write the masterplan". Once a milestone has no open task, `pm run` queues the task "post-milestone cleanup" in its first plan (body: `cleanup_prompt` in project-manager.toml); when that is closed too, the milestone becomes `implemented`. Changing its subject or body queues "revise the masterplan" in the masterplan, unless a masterplan task is open; refused once the milestone is implemented or every plan of it is done: add a new milestone. On hold (`pm milestone hold`): its plans and tasks stay out of the queue until `pm milestone resume`; a running task finishes. Only the human adds or changes milestones: `pm milestone …` is refused in a worker (`PM_TASK` set).
- **Plan:** belongs to a milestone; subject, body (required), priority (optional; the masterplan: `planning_priority`, 1000). Adding a plan, or changing its subject or body, queues the task "create tasks for plan N" (priority `planning_priority`, 1000), unless one is waiting (a running one doesn't count: it may have read the old text). The same holds for the revision tasks below.
- **Plan tree:** a plan's parent is the plan of the task that wrote it (same milestone), else the masterplan. Changing a plan's subject or body also queues "revise plan M" in each child plan that isn't done (an open one gets the new parent text).
- **Done plan:** has tasks, none open, and every child plan is done; the masterplan is done when every plan of its milestone is. A done plan can't be changed and takes no new tasks: add a new plan. A closed (done or dropped) task can't be changed.
- **Task:** belongs to a plan; subject, body (required while open), kind, status `open` | `done` | `dropped`, note (done: date and files or commit; dropped: the reason), priority (optional; default: the plan's, else 0).
- **Task kind:**
  - `work` (default): anything else
  - `plan`: writes a plan (`pm task add … --kind plan`)
  - made by pm only: `masterplan` ("write the masterplan", "revise the masterplan"), `split` ("create tasks for plan N"), `cleanup` ("post-milestone cleanup"); "revise plan N" is a `plan` task
  - `plan`, `masterplan`, `split`: priority `planning_priority` (1000) unless given
  - checked by `pm run` when done: `plan` and `masterplan` must add or change a plan (except "revise …", which may leave it as it is), `split` must leave its plan with tasks; otherwise the task is committed and the run stops
- **Decision:** subject (the question), body (context), options (the recommended one first), status `pending` | `answered` | `irrelevant`. A task linked to a pending decision waits. When the human answers, the task becomes workable again and its worker sees the answer in `pm show`. When a decision is marked irrelevant, the open tasks waiting for it are dropped.
- **Decision log:** answered decisions are the project's decision log (`pm decision list --answered`). Also:
  - kind `rule` (default, lasting, in the log) | `once` (a go-ahead or check, not in the log)
  - `decided_by_task`: set when a worker recorded its own choice (`pm decision record`); empty: the human decided
  - consequences: what changed because of the answer, appended by the tasks that implement it
  - supersede links: a newer answer replaces an older one, wholly or partly (with a note)
  - cite a decision in code, prompts and docs as `decision 32: <subject>`; `pm show d32` finds it
- **Sign-off:** a decision pm makes, per `signoff` in project-manager.toml, before a plan is split into tasks or before work tasks run; its only answer is approve. Shown as `Decision 4 (answered, sign-off): …`. Not for agents to create or answer.
- **Recorded by pm, not set by you:** `created_at` everywhere, `retired_at` of tasks, `implemented_at`, `closed_at`; `created_by_task` (your task, when you create a plan, task or decision as a worker); one **run** per worker run, with result, time, tokens and cost.
- **Queue:** open tasks of open milestones that no pending decision blocks; highest priority first, then by milestone, plan and task id. `pm run` takes the first one.

## Inspect

```sh
pm status        # open and on-hold milestones, the first 10 queued tasks, pending decisions
pm queue         # the whole queue
pm show 7        # task 7 with its plan, its milestone, the plan's other tasks, its decisions and runs
pm show d32      # decision 32 (also `decision 32`); p3: plan 3, m1: milestone 1
pm runs          # worker runs: start, duration, result, turns, tokens, cost; totals per task kind
pm validate      # check tasks.sql; exit 1 with the problems
```

`pm queue` prints one line per task; the priority appears only when it isn't 0:

```
Queue (3):
   +1000  task 1: write the masterplan
   +1000  task 3: create tasks for plan 2
    +700  task 4: stream one table as CSV
```

`pm plans list` (or `pm plan list`): plans with open tasks in open milestones, one per line, highest priority first:

```
  id   prio  subject
   1   1000  masterplan
   3    900  Database schema
   2    700  Export endpoint
```

`pm plan add` prints the new id on stdout, then this list on stderr with the new plan marked: compare it with the others and fix its priority with `pm plan edit`.

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
pm milestone set 1 --status implemented     # or open, on-hold
pm milestone hold 1                         # its tasks leave the queue
pm milestone resume 1
```

Milestones are the human's: a worker never adds or changes one (refused in a worker). A milestone that is contradictory, incomplete or out of date gets a decision (`pm decision add`); the human answers it and edits the milestone.

## Plans

```sh
pm plan add 1 "Backend endpoint" <<'EOF'
`GET /api/export/<table>.csv` streams the rows of one table as CSV.

- Files: `app/api/export.py` (new), `app/api/routes.py`.
- Columns named as in the API, not the database.
EOF
# prints the plan id, e.g. 2, and queues "create tasks for plan 2"

pm plan add 1 "UI button" --priority 400 <<'EOF'
…
EOF

pm plan edit 1 --body - <<'EOF'
The masterplan, divided into parts:

1. Backend endpoint
2. UI button
EOF
pm plan edit 2 --subject "Export endpoint"
pm plan edit 2 --priority 910    # its tasks without their own priority move up
pm plan edit 2 --priority none   # back to 0
```

A new subject or body queues "create tasks for plan N" again, unless one is waiting, and "revise plan M" in each child plan that isn't done; it prints what it queued. A priority change queues nothing. A done plan refuses every edit.

## Tasks

```sh
pm task add 2 "Stream one table as CSV" <<'EOF'
Implement `GET /api/export/<table>.csv` in `app/api/export.py` with the stdlib `csv` module.

Done when: a test exports a table with 3 rows and an empty table.
EOF
# prints the task id, e.g. 5

pm task add 1 "write plan: Export endpoint" --kind plan <<'EOF'
Write the plan for part 1 of the masterplan.
EOF
# a plan-writing task: priority 1000 (planning_priority)

pm task add 2 "Docs for the export endpoint" --priority 690 <<'EOF'
…
EOF

pm task done 5 "2026-10-10, app/api/export.py, tests/test_export.py"
pm task drop 6 "covered by task 5"
pm task priority 6 710      # earlier than the plan's other tasks (plan priority 700)
pm task priority 6 none     # back to the plan's priority
```

A task should fit one worker run: one change you can describe, check and commit.

### Follow-up tasks

Work you find beyond your task is a follow-up task: add it, don't do it, don't leave it in a note.

- A bug or gap your task doesn't cover, a missing test, a part the plan forgot, cleanup your change makes necessary
- Into the plan it belongs to, usually your task's: `pm show <your task>` names it and lists its tasks; don't add one twice
- Body: what, where, done when; written for a worker without your context
- No `--priority` unless it must run earlier or later than its plan's other tasks
- Your task can't be done without it: do it yourself, it's part of your task
- It needs the human's answer first: also `pm decision add` and `pm task-pending-decision <new task> <decision>`

```sh
pm task add 2 "Escape newlines in CSV cells" <<'EOF'
`app/api/export.py` writes cells with newlines unquoted; found in task 5.

Done when: a test exports a cell with a newline and reads it back.
EOF
```

## Decisions

```sh
did=$(pm decision add "Which CSV delimiter?" \
        --body "German Excel opens ';' files directly, ',' needs an import step." \
        --option "semicolon" --option "comma")
pm task-pending-decision 5 "$did"       # task 5 waits until the human answers
```

`--option` is repeatable; put the recommended option first. `--body` takes `-` to read stdin. `decision add` doesn't link tasks: run `pm task-pending-decision <task> <decision>` for every task that waits for the answer, including your own task if it can't go on.

The human answers with `pm decide` (interactive; don't run it yourself), or in a Claude session that runs these for them:

```sh
pm decision list                 # pending decisions: context, options, blocked tasks
pm decision answer 3 1           # option 1, or own text: pm decision answer 3 "tabs"
pm decision irrelevant 3 "<reason>"   # drops the open tasks it blocks
pm decision approve 4            # sign-offs only
```

Record only answers the human gave. A worker never answers decisions: pm refuses `answer`, `approve`, `irrelevant` and `kind` in a worker.

### The decision log

```sh
pm decision list --answered              # the log: lasting answered decisions, newest first, superseded ones left out
pm decision list --all --grep delimiter  # every decision that mentions "delimiter": pending, one-off, superseded too
pm decision show 32                      # one decision: context, options, answer, consequences, tasks, links
pm show d32                              # the same
```

Every task, planning ones included, starts here: grep the log for what the task touches (subjects, files, terms; `--grep` repeats, any word matches). Answered decisions are binding until a newer one supersedes them: a plan follows them, a task implements them, neither asks again what is decided. Do the same before every choice the plan doesn't settle. Cite decisions as `decision 32: <subject>`.

```sh
pm decision list --answered --grep csv --grep delimiter --grep export
```

**Your own choice:** a lasting choice the plan doesn't make (a design, a format, a rule others must follow), that the human doesn't need to make first: record it, answered. The human overrides it by superseding it.

```sh
pm decision record "Number style in prompts" --answer "RAE: 1 234,5; scan flags dotted thousands" \
    --body "Prompts mixed three styles; RAE is what the data uses."
pm decision record "Seen counters" --answer "…" --supersedes 41   # replaces decision 41, a worker's own choice
pm decision supersede 44 28 --note "scope only"                  # partly: decision 44 replaces only the scope of 28
```

A question only the human can answer stays a question (`pm decision add`). You may supersede only a worker's choice; to change a human's answer, ask with `pm decision add` and name the decision. `--once` on `add` or `record`: a one-off go-ahead or check that isn't a lasting rule.

**Consequences:** when `pm show` says your task is the last open one that waited for an answered decision, record what changed because of the answer, before `pm task done`:

```sh
pm decision consequences 32 "RAE number style in all prompts (prompts/*.md); scan.py flags dotted thousands"
```

## Run (for the human)

```sh
pm init              # once per project: project-manager.toml, committed; the database is written on first use
pm run               # work the queue until it is empty, a worker fails or the tree is dirty
pm run --once        # one task
pm run --dry-run     # print the next worker command; change nothing
pm decide            # answer pending decisions: a number, own text, o <answer> = one-off, s = skip, x <reason> = irrelevant; sign-offs: a = approve
pm decision answer 3 1 --once --supersedes 2   # a one-off answer; this one replaces decision 2
pm decision kind 3 rule   # rule: lasting, in the log; once: a one-off
```

Agents don't start `pm run`: a worker is already one task of a run.

## Priorities

- Higher runs first; a task without its own priority gets its plan's; planning tasks (`plan`, `masterplan`, `split`) get 1000.
- Rules for choosing them: `plan_priority_prompt` and `task_priority_prompt` in project-manager.toml, printed at the end of `pm instructions`.
- `pm run` appends both to every worker prompt.

## Recipes

**Write the masterplan** (task "write the masterplan" of plan P): read the milestone (`pm show <task>`) and the code. Write the plan with `pm plan edit P --body -`, divided into parts, and state there that the masterplan's own tasks are not implementation work: each writes the plan for one part. That edit queues "create tasks for plan P". Finish with `pm task done`.

**Revise a plan** (task "revise the masterplan" or "revise plan P"): the milestone or the parent plan (quoted in the task body) changed. If plan P no longer fits, rewrite it with `pm plan edit P --body -`; that queues "create tasks for plan P" and the revisions below it. If it still fits, change nothing. Then close your task.

**Split a plan into tasks** (task "create tasks for plan P"): if P already has tasks, leave the done and dropped ones as they are, drop open ones that no longer fit (`pm task drop`), and add only what the plan asks beyond them. Add one `pm task add P "<subject>"` per task, in the order they should run; for the masterplan, its tasks write plans: `--kind plan`. Priorities: see "Priorities". Raise a decision for each question only the human can answer, and link the tasks that wait for it. Then `pm task done <your task> "<date>, tasks …"`.

**Write a part's plan** (a task of the masterplan): `pm plan list` first, then `pm plan add <milestone> "<part subject>" --priority N` with the full body, N by "Priorities". That queues its "create tasks" task. Then close your task.

**Post-milestone cleanup** (task "post-milestone cleanup"): work through its body for the whole project, not just this milestone. For another milestone that needs a change, or a pending decision that seems obsolete, say so in a new decision for the human. Then close your task.

**Your task needs an answer first:** add the decision, `pm task-pending-decision <your task> <decision>`, stage what's done, stop. `pm run` reports the task as blocked and goes on with the next.

**Finish a task:** `pm task done <task> "<YYYY-MM-DD, files>"` or `pm task drop <task> "<reason>"`. Run the project's checks, stage the changes meant to stay, delete temporary files. Don't commit or push: `pm run` commits.

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
