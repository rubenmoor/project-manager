# pm

Task queue for [Claude Code](https://claude.com/claude-code) workers. A project's work is **milestones → plans → tasks** in SQLite, kept in git as SQL text; `pm run` works the queue one task at a time with `claude -p`, waits out usage limits, and asks you the questions workers raise.

Python package `pm/` (code in `__init__.py`, the config template `project-manager.toml`, the agent reference `instructions.md`), standard library only (Python 3.11+), plus `git` and `claude`. Install: `nix run github:<you>/pm`, `pipx install .`, or `python3 -m pm` with this repository on `PYTHONPATH`.

## Use

In the project's git repository:

```sh
pm init            # write project-manager.toml, commit it; check and adjust its defaults
pm milestone add   # editor: first line the subject, the rest the body (Markdown)
pm run             # work the queue; Ctrl-C stops, `pm run` continues
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

Questions you can't answer yet don't need to block the milestone: workers turn them into decisions, and `pm run` asks you (`pm decide`).

`pm init` writes `project-manager.toml` from the template `pm/project-manager.toml` (database file name, worker command, usage-limit margin, log folder, editor, the three prompts below, the priority settings, the sign-offs) and commits it; run again, it does nothing. Adjust the defaults (e.g. the database file name) before the first other command: that one writes `tasks.sql` with the schema (`SCHEMA` in `pm/__init__.py`) if it is missing. Other commands need the config. Nothing is pushed.

**Storage.** `tasks.sql` is the database: the schema, then one `INSERT` per row, sorted by key. Every command loads it into an in-memory SQLite database; a change rewrites it. So `git diff` shows changed rows, branches that change different rows merge cleanly, and two branches that create the same id conflict on that line (fix the file, then `pm validate`). `pm` commits its own changes (`pm: …`); inside `pm run`, a worker stages its work and `pm run` commits it with `tasks.sql` (`task <id>: <subject>`, the task's note as body). Changes the worker left unstaged fail the task and stop the run. Hand edits are fine; `pm validate` checks them. No migrations: a `tasks.sql` of another schema version is refused.

**Flow.** A new milestone comes with a first plan, the masterplan (body "tbd."), and in it the task "write the masterplan" (`masterplan_prompt`): the worker writes the plan with `pm plan edit`, divided into parts, and states that the masterplan's own tasks each write the plan for one part. So splitting the masterplan into tasks yields one plan-writing task per part, and each of those adds a plan. A new plan, or a plan whose subject or body changes, queues "create tasks for plan N" (`plan_tasks_prompt`): the worker splits it into tasks and decisions. Once a milestone has no open task, `pm run` queues "post-milestone cleanup" in its first plan (`cleanup_prompt`: bring top-level docs up to date, remove redundant files and unused code, review pending decisions and other milestones); when that is closed and no task is open, the milestone is `implemented` (follow-up tasks of the cleanup run first; no second cleanup).

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
- pm creates the decision when such a task heads the queue; interactive `pm run` asks it at once, otherwise the run stops when the queue is empty
- the only answer is approve (`pm decide`: `a`); skip it to stop and change plans or tasks first; `pm decide` shows the plan or the waiting tasks as they are then
- no `x` (irrelevant) for sign-offs

**Priorities.** Set by workers, following rules in project-manager.toml:

- `planning_priority` (1000): planning tasks (kinds `masterplan`, `split`, `plan`) run first, so all plans and tasks exist before implementation starts. After a planning task, `pm run` checks it changed a plan (`plan`, `masterplan`) or left its plan with tasks (`split`), and stops otherwise.
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
| `decide` | Answer pending decisions (number, text, `s` skip, `x <reason>` irrelevant: drops the tasks it blocks; sign-offs: `a` approve, `s` skip) |
| `run [--once] [--dry-run]` | Work the queue until it is empty, a worker fails, or the tree has uncommitted changes |
| `milestone add`, `plan add <milestone>`, `task add <plan>` | In your editor (as `git commit`): first line subject, rest body (Markdown); quit unsaved or save empty to abort |
| `milestone add <subject>`, `plan add <milestone> <subject>`, `task add <plan> <subject>` | Non-interactive: the body from stdin. `plan add` and `task add` also take `--priority`; `task add` `--kind plan` |
| `milestone set <milestone>` | `--subject`, `--status`, `--body` |
| `plan edit <plan>` | `--subject`, `--body`, `--priority <N\|none>` |
| `task done\|drop <task> <note>`, `task priority <task> <N\|none>` | Close a task, set its priority |
| `decision add <subject>` | `--body`, `--option` (recommended first) |
| `task-pending-decision <task> <decision>` | The task waits until the decision is answered |

Global: `--root <repo>`. A text value `-` reads stdin.

## Workers

Each task runs the configured `worker` command (default: `claude -p` with `Bash Read Edit Write Glob Grep` allowed). The prompt (`WORKER_PROMPT` in `pm/__init__.py`) holds the task with its plan and milestone, and the `pm` commands the worker may use: `plan add`, `plan edit`, `task add`, `decision add`, `task-pending-decision`, `task done|drop`. It points the worker to `pm instructions` for examples of every command. Agents outside `pm run`, e.g. a Claude session asked to plan work, should run `pm instructions` too: say so in the project's `AGENTS.md` or `CLAUDE.md`. **The worker has full shell access in the project.** The default command has claude print JSON (`--output-format stream-json --verbose`): `pm` shows the text and tool calls in the terminal and records model, turns, tokens and cost in the run. Other commands' plain output is shown as it is. Logs, raw: `.git/pm-logs/task-<id>.log` (`log_dir`). On a usage limit, `pm` waits for the reset and retries.
