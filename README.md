# pm

Task queue for [Claude Code](https://claude.com/claude-code) workers. A project's work is **milestones → plans → tasks** in SQLite, kept in git as SQL text; `pm run` works the queue one task at a time with `claude -p`, waits out usage limits; the questions workers raise wait for your answer while it goes on with other tasks.

Python package `pm/` (code in `__init__.py`, the config template `project-manager.toml`, the agent reference `instructions.md`), standard library only (Python 3.11+), plus `git` and `claude`. Install: `nix run github:<you>/pm`, `pipx install .`, or `python3 -m pm` with this repository on `PYTHONPATH`.

## Use

In the project's git repository:

```sh
pm init            # write project-manager.toml, commit it; check and adjust its defaults
pm milestone add   # editor: first line the subject, the rest the body (Markdown)
pm run             # work the queue; Ctrl-C: stop after this task, twice: stop now; `pm run` continues
```

### Writing a milestone

The milestone is the only text a worker gets from you; the masterplan, the plans and the tasks are derived from it, and every worker sees it. So write it for an engineer who knows the code but not your intentions: what should be true afterwards, why, what is fixed, what is out of scope, how to tell it's done. Leave the how to the masterplan. In the editor (`pm milestone add`):

```markdown
Users can export their data as CSV

Users ask for their data outside the app; support exports it by hand today.

## Outcome
- "Export" on the account page downloads one CSV per table the user owns (projects, entries, tags).
- Large accounts (100 000 entries) export in under 10 s without blocking the web server.

## Constraints
- No new dependencies; the stdlib `csv` module is enough.
- Columns are named as in the API, not the database.

## Out of scope
- Import, other formats, scheduled exports.

## Done when
- The export is covered by tests, including an account with no data.
- The API docs describe the endpoint.
```

The first line is the subject, the rest the body. The same without the editor, e.g. from a script:

```sh
pm milestone add "Users can export their data as CSV" < milestone-csv-export.md
```

Questions you can't answer yet don't need to block the milestone: workers turn them into decisions, `pm run` skips the tasks they block, and you answer with `pm decide` in the terminal, or `pm decision answer` (e.g. from a Claude session).

`pm init` writes `project-manager.toml` from the template `pm/project-manager.toml` (database file name, worker command, usage-limit margin, log folder, editor, the five prompts below, the priority settings, the sign-offs) and commits it; run again, it does nothing. Adjust the defaults (e.g. the database file name) before the first other command: that one writes `tasks.sql` with the schema (`SCHEMA` in `pm/__init__.py`) if it is missing. Other commands need the config. Nothing is pushed.

**Storage.** `tasks.sql` is the database: the schema, then one `INSERT` per row, sorted by key. Every command loads it into an in-memory SQLite database; a change rewrites it. So `git diff` shows changed rows, branches that change different rows merge cleanly, and two branches that create the same id conflict on that line (fix the file, then `pm validate`). `pm` commits its own changes (`pm: …`); inside `pm run`, a worker stages its work and `pm run` commits it with `tasks.sql` (`task <id>: <subject>`, the task's note as body). Changes the worker left unstaged fail the task and stop the run. Hand edits are fine; `pm validate` checks them. No migrations: a `tasks.sql` of another schema version is refused.

**Flow.** A new milestone comes with a first plan, the masterplan (body "tbd."), and in it the task "write the masterplan" (`masterplan_prompt`): the worker writes the plan with `pm plan edit`, divided into parts, and states that the masterplan's own tasks each write the plan for one part. So splitting the masterplan into tasks yields one plan-writing task per part, and each of those adds a plan. A new plan, or a plan whose subject or body changes, queues "create tasks for plan N" (`plan_tasks_prompt`): the worker splits it into tasks and decisions. Edits cascade: a changed milestone queues "revise the masterplan" (`revise_masterplan_prompt`), a changed plan "revise plan M" in each plan below it (`revise_plan_prompt`; below: written by one of its tasks, else, for the masterplan, any plan of the milestone); each revision that changes its plan cascades on. Done plans (no open task, nor in the plans below) and closed tasks can't be changed: a change to finished work is a new plan. Once a milestone has no open task, `pm run` queues "post-milestone cleanup" in its first plan (`cleanup_prompt`: bring top-level docs up to date, remove redundant files and unused code, review pending decisions and other milestones); when that is closed and no task is open, the milestone is `implemented` (follow-up tasks of the cleanup run first; no second cleanup).

**Data.** Everything is referenced by its integer id.

- milestone: subject, body, status, `created_at`, `implemented_at`
- plan: belongs to a milestone; subject, body, priority, `created_at`, `created_by_task`
- task: belongs to a plan; subject, body, kind (`work`, `plan`: writes a plan; made by pm: `masterplan`, `split` for "create tasks for plan N", `cleanup`), status, priority, note, `created_at`, `retired_at` (done or dropped), `created_by_task`
- decision: subject, body, options, status, answer or reason, `created_at`, `closed_at`, `created_by_task`, `signoff` (made by pm, see below); `task_decision` links tasks to the decisions they wait for
- run: one worker run of `pm run`: task, start, end, result (`running`, `done`, `blocked`, `failed`, `limit`), reason, exit code, `base_commit` (HEAD at the start), `pid` of `pm run`; from claude's JSON output: model, session, turns, tokens (input, output, cache read, cache creation), cost in USD; while it runs, `running` (at most one, uncommitted); a `pm run` that died leaves it, the next `pm run` marks it `failed` (`interrupted`), or refuses while that process lives
- timestamps in UTC; `created_by_task`: the task whose worker created the row (`PM_TASK`), empty for the human or pm

**Queue** (the `queue` view): open tasks of open milestones that no pending decision blocks, by priority (task's, else plan's, else 0; highest first), then milestone, plan and task id.

**Sign-offs.** `signoff` in project-manager.toml, empty by default: then only the decisions workers raise stop the run. Each value makes `pm run` wait for your approval before the next task of a kind runs:

- `"masterplans"`: "create tasks for plan N" of a masterplan waits for "Sign off the masterplan of milestone M"
- `"plans"`: "create tasks for plan N" of every other plan waits for "Sign off plan N"
- `"task-queue"`: work tasks wait for "Sign off the task queue", one decision for all work tasks without a sign-off; asked when the first of them heads the queue, i.e. after all planning work; work tasks added later need a new one
- pm creates the decision when such a task heads the queue; `pm run` never asks, it goes on with the unblocked tasks and stops when the queue is empty
- the only answer is approve (`pm decide`: `a`, or `pm decision approve <id>`); leave it pending to change plans or tasks first; `pm decision list` shows the plan or the waiting tasks as they are then
- no `x` (irrelevant) for sign-offs

**Priorities.** Set by workers, following rules in project-manager.toml:

- `planning_priority` (1000): planning tasks (kinds `masterplan`, `split`, `plan`) run first, so all plans and tasks exist before implementation starts. After a planning task, `pm run` checks it changed a plan (`plan`, `masterplan`; a "revise …" task may leave it as it is) or left its plan with tasks (`split`), and stops otherwise.
- `plan_priority_prompt`: a new plan's priority by what it affects (database schema 900 … media and assets 200), adjusted after `pm plans list` when it must run before or after another plan. Appended to every worker's prompt.
- `task_priority_prompt`: a task gets its own priority only for a good reason, the plan's ± a multiple of 10. Appended to every worker's prompt.
- Both are also printed at the end of `pm instructions`; empty turns one off.

## Commands

| | |
|---|---|
| `init` | Write and commit the config (if missing) |
| `instructions` | The full reference for agents (`pm/instructions.md`): every command with examples, recipes, errors |
| `status`, `queue`, `plans list`, `show <task>`, `validate` | Inspect |
| `runs [task]` | Worker runs: start, duration, result, turns, tokens, cost; totals per task kind |
| `run [--once] [--dry-run]` | Work the queue until it is empty, a worker fails, or the tree has uncommitted changes |
| `stop` | Have the running `pm run` stop after its current task (SIGUSR1; same as one Ctrl-C) |
| `milestone add`, `plan add <milestone>`, `task add <plan>` | In your editor (as `git commit`): first line subject, rest body (Markdown); quit unsaved or save empty to abort |
| `milestone add <subject>`, `plan add <milestone> <subject>`, `task add <plan> <subject>` | Non-interactive: the body from stdin. `plan add` and `task add` also take `--priority`; `task add` `--kind plan` |
| `milestone set <milestone>` | `--subject`, `--status`, `--body` |
| `plan edit <plan>` | `--subject`, `--body`, `--priority <N\|none>` |
| `task done\|drop <task> <note>`, `task priority <task> <N\|none>` | Close a task, set its priority |
| `decision add <subject>` | `--body`, `--option` (recommended first) |
| `decide` | Answer pending decisions one by one in the terminal (number, text, `s` skip, `x <reason>` irrelevant; sign-offs: `a` approve, `s` skip) |
| `decision list` | Pending decisions: context, options, blocked tasks; sign-offs with what they approve |
| `decision answer <id> <n\|text>`, `decision approve <id>`, `decision irrelevant <id> <reason>` | Close a decision; irrelevant drops the open tasks it blocks; approve: sign-offs only |
| `task-pending-decision <task> <decision>` | The task waits until the decision is answered |

Global: `--root <repo>`. A text value `-` reads stdin.

## Dashboard

`dashboard/` holds `pm-dashboard`, an optional local web view and editor of `tasks.sql`: the running worker and its output, queue, milestones, decisions, what changed since you last looked, runs and cost, live; subjects, bodies and priorities edited in place. A separate package, not installed with `pm`: `nix run .#dashboard` (details: `dashboard/README.md`).

## Workers

Each task runs the configured `worker` command (default: `claude -p` with `Bash Read Edit Write Glob Grep` allowed). The prompt (`WORKER_PROMPT` in `pm/__init__.py`) holds the task with its plan and milestone, and the `pm` commands the worker may use: `plan add`, `plan edit`, `task add`, `decision add`, `task-pending-decision`, `task done|drop`. It points the worker to `pm instructions` for examples of every command. Agents outside `pm run`, e.g. a Claude session asked to plan work, should run `pm instructions` too: say so in the project's `AGENTS.md` or `CLAUDE.md`. **The worker has full shell access in the project.** The default command has claude print JSON (`--output-format stream-json --verbose`): `pm` shows the text and tool calls in the terminal and records model, turns, tokens and cost in the run. Other commands' plain output is shown as it is. Logs, raw: `.git/pm-logs/task-<id>-run-<run>.log` (`log_dir`), `task-<id>.log` links to the latest. On a usage limit, `pm` waits for the reset and retries.

The prompt goes to the worker's stdin, not its command line: a worker's `pkill -f <word in the task>` would otherwise match the worker itself. `{settings}` in the default command adds Claude Code hooks (`pm/guard.py`): one blocks `pkill -f`, `killall` and `kill -1`, nested ones (`bash -c '…'`, `nix develop -c …`) included; a Stop hook sends the worker back once if it ends its turn with the task still open (not done, dropped or waiting for a decision) or a background task running. `claude -p` ends with the turn, and a background job with it, so `{settings}` also turns background tasks off (`CLAUDE_CODE_DISABLE_BACKGROUND_TASKS`) and lets Bash commands run up to 60 minutes in the foreground (`BASH_MAX_TIMEOUT_MS`). The worker runs in its own process group, so Ctrl-C in the terminal doesn't reach it: the first Ctrl-C (or `pm stop`, i.e. SIGUSR1) lets the task finish, then `pm run` exits; a second Ctrl-C, SIGTERM or SIGHUP stops the whole group now.

A failed run: `pm` stashes what the worker left (`tasks.sql` aside; `git stash apply <sha>` gets it back), so the next `pm run` starts clean, and records a summary as the run's reason, shown in the terminal and on the dashboard: exit code or signal, duration, turns, the last tool call, a likely cause, the stash, the log. The next run of the same task gets that summary and the task's stashes in its prompt, with the instruction to inspect them, apply what is still good and recheck what they mark as done; `pm` drops them once the task is done or dropped.
