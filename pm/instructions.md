# pm: instructions for agents

`pm` keeps a project's work as **milestones → plans → tasks**, plus **decisions** that only the human can make. Everything lives in `tasks.sql` (SQL text, tracked by git) and is read and changed only through `pm` commands. `pm run` works the queue one task at a time with a `claude -p` worker; as a worker you are one task of that run (environment `PM_TASK=<your task>`). A work task right after a done work task of the same plan may continue that task's Claude session (warm hand-off): what you learned of the code still holds, but only the task in your newest prompt is yours.

## Rules

1. **Subject on the command line, body on stdin.** Without a subject, `pm milestone|plan|task add` opens an editor and fails without a terminal. Use a heredoc with a quoted delimiter (`<<'EOF'`), so `$`, backticks and `\` in Markdown stay as written. `--body -` reads stdin too.
2. **Creating commands print the new integer id on stdout**, nothing else: `tid=$(pm task add 4 "…" <<'EOF' … EOF)`.
3. **IDs are integers, unique per kind:** `pm show 12` is task 12, `pm plan edit 3` is plan 3; `pm show` also takes `d32` (decision), `p3`, `m1`, `s2` (summary), `r2` (report).
4. **`tasks.sql` is pm's:** query it with pm (`pm show`, `pm queue`, `pm decision list`), never read, grep or edit the file; in a worker, pm's hooks block reading it. Edit it by hand only when the human asks, then run `pm validate`.
5. **References:** a milestone, plan, task or decision always by id and full subject, e.g. `plan 2: Backend endpoint`: plain in a subject, in backticks in a body, note or decision text. Renaming an item updates its references in open tasks that aren't running and in pending decisions.
6. **Bodies are Markdown**, for an engineer who knows the code but not the intentions: what, why, which files, how to tell it's done.
7. **Intros are for a human who reads nothing else:** every plan and decision needs one (`--intro`; pm refuses `plan add`, `decision add` and `decision record` without, and a worker's `plan edit --body` of a plan that has none). One paragraph, 2 to 4 sentences, in plain English for an interested outsider who knows neither the code nor how the work is organised: what this is about and why it matters. No ids, file names, code or project-management words (task, plan, milestone, sign-off, worker, pm). A decision's intro says what has to be chosen, between which options, and what changes with the answer.
8. **Closed is final:** an implemented milestone, a done plan, a closed (done or dropped) task and a running task's subject and body can't be changed. Add a new milestone, plan or task instead.
9. **The human's, refused in a worker:** adding or changing milestones (`pm milestone add|set|hold|resume`); answering decisions (`pm decision answer|approve|irrelevant|kind`); exec tasks (`pm task add --exec`). A worker that needs one of these raises a decision. Agents never start `pm run`.
10. **Commits:** outside `pm run`, each `pm` command commits `tasks.sql` itself (`pm: …`). In a worker: stage (`git add`) the changes meant to stay, delete temporary files, don't commit; `pm run` commits what is staged with `tasks.sql` as `task <id>: <subject>`. Unstaged leftovers: of a task waiting for a decision, stashed for its next run; of a closed task, committed on a branch with a decision to look into it. Never push.

## Model

- **Milestone:** subject, body, status `open` | `on-hold` | `implemented` (set by pm), queue index (default 0). On hold: its tasks stay out of the queue until resumed; a running task finishes.
- **Plan:** belongs to a milestone; subject, intro, body, priority. The milestone's first plan is its **masterplan**. A plan's parent is the plan of the task that wrote it, else the masterplan. **Done:** it has tasks, none open, and every child plan is done; the masterplan, when every plan of its milestone is.
- **Task:** belongs to a plan (an `exec` task: none); subject, body (required while open), kind, status `open` | `done` | `dropped`, note (done: date and files; dropped: the reason), priority (default: the plan's, else 0), web (see "Web access").
- **Task kinds:**
  - `work` (default)
  - `plan`: writes a plan (`--kind plan`)
  - pm's own (see "What pm queues"): `masterplan`, `split`, `revise`, `cleanup`, `investigate`
  - `exec`: the human's direct intervention; no plan, so no milestone; queued as if at milestone queue index 0; no task-queue sign-off; if it fails, dropped, not investigated
- **Decision:** subject (the question), intro (what is to be decided, plain English), body (context), options (the recommended one first), status `pending` | `answered` | `irrelevant`, kind `rule` (default: lasting, in the log) | `once` (a go-ahead or check). A task linked to a pending decision waits; answered, its worker sees the answer in `pm show`; irrelevant, the open tasks waiting for it are dropped. Also: `decided_by_task` (a worker's own choice, `pm decision record`; empty: the human's), consequences (what changed because of the answer), supersede links (a newer answer replaces an older one, wholly or partly).
- **Sign-off:** a decision pm makes itself, per `signoff` in project-manager.toml: before a masterplan or plan is split into tasks, before work tasks run, or before a web task gets the web. Its only answer is approve; shown as `Decision 4 (answered, sign-off): …`. Not for agents to create or answer.
- **Queue:** open tasks of open milestones, and open `exec` tasks, that no pending decision blocks; by milestone queue index (lowest first), then highest priority first, then milestone, plan and task id. `pm run` takes the first.
- **Recorded by pm:** `created_at`, `retired_at`, `implemented_at`, `closed_at`; `created_by_task` (your task, for what you create as a worker); one run per worker run, with result, time, tokens and cost.

## What pm queues

pm adds these tasks itself; their bodies are the prompts of the same name in project-manager.toml. A queued one that isn't running yet gets the new text instead of a second task (a running one may have read the old text).

| Trigger | Task (kind) | Plan |
|---|---|---|
| `pm milestone add` | "write the masterplan of milestone N: …" (`masterplan`, priority 1000) | masterplan, body "tbd." |
| plan added, or its subject or body changed | "create tasks for plan N: …" (`split`, priority 1000) | that plan |
| milestone's subject or body changed, no masterplan task waiting | "revise the masterplan of milestone N: …" (`revise`) | masterplan |
| plan's subject or body changed | "revise plan M: …" (`revise`), parent quoted | each child plan that isn't done |
| a milestone's last open task closed | "post-milestone cleanup of milestone N: …" (`cleanup`); closed too: milestone implemented | masterplan |
| a task failed | "investigate the failure of task N: …" (`investigate`), above the failed task | the failed task's |
| `pm summary request` | "write an executive summary" (`exec`, priority 10000) | none |
| `pm report request` | "write a resource use report" (`exec`, priority 10000) | none |
| `pm report ask "<question>"` | "answer: <question>" (`exec`, priority 10000), a custom report | none |

A priority change queues nothing. When done, pm checks: `plan` and `masterplan` tasks added or changed a plan (`revise` may leave it as it is), `split` left its plan with tasks; otherwise the task fails.

## Inspect

```sh
pm status            # open and on-hold milestones, the first 10 queued tasks, pending decisions
pm queue             # the whole queue
pm show 7            # task 7 with its plan, milestone, the plan's other tasks, its decisions and runs
pm milestone list    # every milestone: id, queue index, status, closed/all tasks, subject
pm milestone show 1  # body, plans with their open tasks (= pm show m1)
pm plan list         # plans with open tasks in open milestones, by queue index, then highest priority
pm runs              # worker runs: start, duration, result, turns, tokens, cost; totals per task kind
pm validate          # check tasks.sql; exit 1 with the problems
```

`pm queue` shows a priority only when it isn't 0:

```
Queue (3):
   +1000  task 1: write the masterplan of milestone 1: CSV export
   +1000  task 3: create tasks for plan 2: Backend endpoint
    +700  task 4: stream one table as CSV
```

`pm show <task>` is what a worker gets in its prompt: the milestone, plan and task as Markdown, then its decisions, e.g. `Decision 1 (answered): Which delimiter? → semicolon`.

## Milestones (the human's)

```sh
pm milestone add "Users can export their data as CSV" [--queue N] <<'EOF'
"Export" on the account page downloads one CSV per table the user owns.

- No new dependencies.
- No import, no other formats.
EOF
pm milestone set 1 [--subject …] [--body -] [--queue N]
pm milestone hold 1 ; pm milestone resume 1
```

A worker that finds a milestone contradictory, incomplete or out of date raises a decision; the human answers it and edits the milestone.

## Plans

```sh
pm plan add 1 "Backend endpoint" --priority 700 \
    --intro "People can download their data as a spreadsheet file, one table at a time. Today they can only look at it on screen." <<'EOF'
`GET /api/export/<table>.csv` streams the rows of one table as CSV.

- Files: `app/api/export.py` (new), `app/api/routes.py`.
- Columns named as in the API, not the database.
EOF
pm plan edit 2 --body - <<'EOF'
…
EOF
pm plan edit 2 --subject "Export endpoint"
pm plan edit 2 --intro "…"       # queues nothing
pm plan edit 2 --priority 910    # its tasks without their own priority move with it; none: back to 0
```

`pm plan add` prints the id on stdout and `pm plan list` on stderr with the new plan marked: compare and fix its priority. An edit prints what it queued.

## Tasks

```sh
pm task add 2 "Stream one table as CSV" <<'EOF'
Implement `GET /api/export/<table>.csv` in `app/api/export.py` with the stdlib `csv` module.

Done when: a test exports a table with 3 rows and an empty table.
EOF
pm task add 1 "write plan: Export endpoint" --kind plan <<'EOF'
Write the plan for part 1 of the masterplan.
EOF
pm task add 2 "Docs for the export endpoint" --priority 690 <<'EOF'
…
EOF
pm task add 2 "Untangle the cache invalidation" --model opus <<'EOF'   # needs deep reasoning
…
EOF
pm task set 6 [--subject …] [--body -] [--priority N|none] [--web|--no-web] [--model opus|none]   # open, not running
pm task priority 6 710          # none: back to the plan's
pm task done 5 "2026-10-10, app/api/export.py, tests/test_export.py"
pm task drop 6 "covered by task 5"
```

A task fits one worker run: one change you can describe, check and commit.

**Model:** pm picks the worker's model by task kind (`[models]` in the config: usually Sonnet for work, Opus for planning and investigations) and switches a task to Opus after a failed run. When splitting a plan, mark a work task that needs deep reasoning (subtle design, hard bugs, wide refactors) `--model opus`; leave the rest to pm.

**Web access:** workers have no WebSearch or WebFetch, except for a task marked web (`--web`). Mark only a task that needs it, e.g. "verify the sourced figures"; its worker gets both tools once the human approves the sign-off (if `"web"` is in `signoff`). Your own task needs the web: `pm task set <your task> --web`, then stop without closing it, even when pm reminds you it is open; the next run has the tools. Don't ask for web access with a decision, and don't edit settings to grant it.

### Follow-up tasks

Work you find beyond your task is a follow-up task: add it, don't do it, don't leave it in a note.

- A bug or gap your task doesn't cover, a missing test, a part the plan forgot, cleanup your change makes necessary
- Your task can't be done without it: it's part of your task, do it
- Into the plan it belongs to, usually your task's; `pm show <your task>` lists the plan's tasks: don't add one twice
- Body: what, where, done when; for a worker without your context
- No `--priority` unless it must run earlier or later than its plan's other tasks
- It needs the human's answer first: also `pm decision add` and `pm task-pending-decision <new task> <decision>`

```sh
pm task add 2 "Escape newlines in CSV cells" <<'EOF'
`app/api/export.py` writes cells with newlines unquoted; found in task 5.

Done when: a test exports a cell with a newline and reads it back.
EOF
```

## Decisions

### Ask the human

```sh
did=$(pm decision add "Which CSV delimiter?" \
        --intro "Exported files can separate their values with semicolons or with commas. Semicolons open directly in German Excel; commas are the international standard but need an import step there." \
        --body "German Excel opens ';' files directly, ',' needs an import step." \
        --option "semicolon" --option "comma")
pm task-pending-decision 5 "$did"       # task 5 waits until the human answers
```

Put the recommended option first. `decision add` links no task: run `pm task-pending-decision` for every task that waits for the answer, your own included. `--once`: a one-off go-ahead or check, not a lasting rule.

### The decision log

The answered `rule` decisions that nothing fully supersedes. Every task, planning ones included, starts here: grep it for what the task touches (subjects, files, terms; `--grep` repeats, any word matches), and again before every choice the plan doesn't settle. Answered decisions are binding until a newer one supersedes them: a plan follows them, a task implements them, neither asks again. Cite them as `decision 32: <subject>`, in code, prompts and docs too.

```sh
pm decision list --answered --grep csv --grep delimiter   # the log, newest first
pm decision list --all --grep delimiter                   # pending, one-off, superseded, irrelevant too
pm decision show 32                                       # = pm show d32
```

**Your own choice:** a lasting choice the plan doesn't make (a design, a format, a rule others must follow) that the human needn't make first: record it, answered. The human overrides it by superseding it. A question only the human can answer stays a question (`pm decision add`). You may supersede only a worker's choice; to change a human's answer, ask with `pm decision add` and name the decision.

```sh
pm decision record "Number style in prompts" --answer "RAE: 1 234,5; scan flags dotted thousands" \
    --intro "Numbers in the questions are now written the Spanish way, with a space between thousands and a decimal comma, because that is how the source data writes them." \
    --body "Prompts mixed three styles; RAE is what the data uses."
pm decision record "Seen counters" --answer "…" --supersedes 41   # replaces decision 41, a worker's own choice
pm decision supersede 44 28 --note "scope only"                  # decision 44 replaces only the scope of 28
```

**Consequences:** when `pm show` says your task is the last open one that waited for a decision, record what changed because of the answer, before `pm task done`:

```sh
pm decision consequences 32 "RAE number style in all prompts (prompts/*.md); scan.py flags dotted thousands"
```

## Priorities

- Higher runs first, within the same milestone queue index (a lower queue index runs before any priority)
- A task without its own priority gets its plan's
- Planning (`plan`, `masterplan`, `split`): `planning_priority`, 1000, unless given
- Rules for choosing them: `plan_priority_prompt` and `task_priority_prompt` in project-manager.toml, appended to every worker prompt and printed at the end of `pm instructions`

## Recipes

Each pm task's body says what to do; these are the mechanics.

- **Write the masterplan** (plan P): read the milestone (`pm show <task>`) and the code; `pm plan edit P --intro "…" --body -`, divided into parts, stating that the masterplan's own tasks each write the plan for one part. That queues "create tasks for plan P: …".
- **Revise a plan** (P): if it no longer fits its milestone or parent plan, `pm plan edit P --body -`; that queues its split and the revisions below it. If it still fits, change nothing.
- **Split a plan into tasks** (P): keep the done and dropped tasks, drop open ones that no longer fit, add one `pm task add P …` per missing task, in the order they should run (`--model opus` for one that needs deep reasoning); the masterplan's tasks write plans (`--kind plan`). Raise a decision for each question only the human can answer, and link the tasks that wait for it. Note: `"<date>, tasks …"`.
- **Write a part's plan** (a task of the masterplan): `pm plan list` first, then `pm plan add <milestone> "<part>" --priority N --intro "…"` with the full body.
- **Post-milestone cleanup:** work through the body for the whole project. For another milestone that needs a change, or a pending decision that seems obsolete: a new decision for the human.
- **Executive summary** (`exec`): `pm summary show` is the previous one; store the new one with `pm summary add < summary.md` (only in an exec task).
- **Resource use report** (`exec`): `pm report tasks` lists the done tasks; store with `pm report add < report.json`, `{"categories": [{"name": "…", "criteria": "…", "tasks": [3, 7]}]}`; refused unless every done task is in exactly one category. pm sums each category's time, tokens and cost.
- **Custom report** (`exec`): the owner's question or request, in the task body; answer in one plain paragraph, store with `pm report answer < answer.md`. `pm show c2`.
- **Your task needs an answer first:** `pm decision add`, `pm task-pending-decision <your task> <decision>`, stage what's done, stop. `pm run` goes on with the next task.
- **Finish:** run the project's checks, stage the changes meant to stay, delete temporary files, then `pm task done <task> "<YYYY-MM-DD, files>"` or `pm task drop <task> "<reason>"`. A worker run ends with its task closed or waiting for a decision (web: see "Web access"); otherwise pm keeps it going once.

## For the human

Agents run these only for the human, in the human's own session.

```sh
pm init              # once per project: project-manager.toml, committed
pm run               # work the queue until it is empty, Claude needs the human or the tree is dirty
pm run --once        # one task;  --dry-run: print the next worker command
pm stop              # the running `pm run` stops after its current task
pm decide            # answer pending decisions interactively (don't run it as an agent)
pm decision list                       # pending decisions: context, options, blocked tasks
pm decision answer 3 1                 # option 1; or own text: pm decision answer 3 "tabs"
pm decision answer 3 1 --once --supersedes 2
pm decision irrelevant 3 "<reason>"    # drops the open tasks it blocks
pm decision approve 4                  # sign-offs only
pm decision kind 3 rule                # rule: lasting, in the log; once: a one-off
pm task add --exec "<subject>" [--priority N] < body.md
pm summary request ; pm report request # queue an executive summary / resource use report; show, list
pm report ask "<question>"             # queue a custom one-paragraph report
```

Settings in `project-manager.toml`: `[models]` (the worker's model per task kind, `escalate` after a failure), `warm_handoff` (tasks per resumed Claude session, 0: off).

Record only answers the human gave.

## Errors

| Message | Meaning |
|---|---|
| `no plan 9`, `no task 9`, `no milestone 9`, `no decision 9` | No such id; look it up with `pm status`, `pm queue` or `pm show` |
| `the change breaks the schema, nothing written: … a plan needs a body` (or `an open task needs a body`) | Empty body: stdin was empty or not piped |
| `` `pm task add` without a subject needs a terminal `` | Give the subject on the command line |
| `task 6 is running; its subject and body can't be changed`, `task 6 is done; a closed task can't be changed` | Add a new task instead |
| `decision 3 is not pending` | Answered or marked irrelevant already |
| `the database has errors (pm validate)` | `tasks.sql` was edited into an invalid state; `pm validate` lists the problems |
| `no project-manager.toml: run pm init first` | The project has no pm yet; that's the human's call |
