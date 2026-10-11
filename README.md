# pm

Task queue for [Claude Code](https://claude.com/claude-code) workers. A project's work is **milestones → plans → tasks** in SQLite, kept in git as SQL text; `pm run` works the queue one task at a time with `claude -p`, waits out usage limits, and asks you the questions workers raise.

Python package `pm/` (code in `__init__.py`, the config template `project-manager.toml`, the agent reference `instructions.md`), standard library only (Python 3.11+), plus `git` and `claude`. Install: `nix run github:<you>/pm`, `pipx install .`, or `python3 -m pm` with this repository on `PYTHONPATH`.

## Use

In the project's git repository:

```sh
pm init            # write project-manager.toml and tasks.sql, commit them
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

`pm init` writes `project-manager.toml` from the template `pm/project-manager.toml` (database file name, worker command, usage-limit margin, log folder, editor, the three prompts below, the priority settings) and `tasks.sql` with the schema (`SCHEMA` in `pm/__init__.py`), and commits them; run again, it adds only what is missing. Other commands need both files. Nothing is pushed.

**Storage.** `tasks.sql` is the database: the schema, then one `INSERT` per row, sorted by key. Every command loads it into an in-memory SQLite database; a change rewrites it. So `git diff` shows changed rows, branches that change different rows merge cleanly, and two branches that create the same id conflict on that line (fix the file, then `pm validate`). `pm` commits its own changes (`pm: …`); inside `pm run`, workers commit theirs together with their work. Hand edits are fine; `pm validate` checks them. A `tasks.sql` from before the rename to subject/body (title/description, question/context; schema version 5 or 6) is migrated in memory and rewritten by the next change.

**Flow.** A new milestone comes with a first plan, the masterplan (body "tbd."), and in it the task "write the masterplan" (`masterplan_prompt`): the worker writes the plan with `pm plan edit`, divided into parts, and states that the masterplan's own tasks each write the plan for one part. So splitting the masterplan into tasks yields one plan-writing task per part, and each of those adds a plan. A new plan, or a plan whose subject or body changes, queues "create tasks for plan N" (`plan_tasks_prompt`): the worker splits it into tasks and decisions. Once a milestone has no open task, `pm run` queues "post-milestone cleanup" in its first plan (`cleanup_prompt`: bring top-level docs up to date, remove redundant files and unused code, review pending decisions and other milestones); when that is closed too, the milestone is `implemented`.

**Data.** A milestone has a subject, a body, a status and a created date; plans belong to a milestone, tasks to a plan, and `task_decision` links tasks to the decisions they wait for. Everything is referenced by its integer id.

**Queue** (the `queue` view): open tasks of open milestones that no pending decision blocks, by priority (task's, else plan's, else 0; highest first), then milestone, plan and task id.

**Priorities.** Set by workers, following rules in project-manager.toml:

- `planning_priority` (1000): the masterplan's tasks and every "create tasks for plan N" run first, so all plans and tasks exist before implementation starts.
- `plan_priority_prompt`: a new plan's priority by what it affects (database schema 900 … media and assets 200), adjusted after `pm plans list` when it must run before or after another plan. Appended to the prompt of the masterplan's tasks.
- `task_priority_prompt`: a task gets its own priority only for a good reason, the plan's ± a multiple of 10. Appended to the prompt of "create tasks for plan N".
- Both are also printed at the end of `pm instructions`; empty turns one off.

## Commands

| | |
|---|---|
| `init` | Write and commit the config and the database (missing ones only) |
| `instructions` | The full reference for agents (`pm/instructions.md`): every command with examples, recipes, errors |
| `status`, `queue`, `plans list`, `show <task>`, `validate` | Inspect |
| `decide` | Answer pending decisions (number, text, `s` skip, `x <reason>` irrelevant: drops the tasks it blocks) |
| `run [--once] [--dry-run]` | Work the queue until it is empty, a worker fails, or the tree has uncommitted changes |
| `milestone add`, `plan add <milestone>`, `task add <plan>` | In your editor (as `git commit`): first line subject, rest body (Markdown); quit unsaved or save empty to abort |
| `milestone add <subject>`, `plan add <milestone> <subject>`, `task add <plan> <subject>` | Non-interactive: the body from stdin. `plan add` and `task add` also take `--priority` |
| `milestone set <milestone>` | `--subject`, `--status`, `--body` |
| `plan edit <plan>` | `--subject`, `--body`, `--priority <N\|none>` |
| `task done\|drop <task> <note>`, `task priority <task> <N\|none>` | Close a task, set its priority |
| `decision add <subject>` | `--body`, `--option` (recommended first) |
| `task-pending-decision <task> <decision>` | The task waits until the decision is answered |

Global: `--root <repo>`. A text value `-` reads stdin.

## Workers

Each task runs the configured `worker` command (default: `claude -p` with `Bash Read Edit Write Glob Grep` allowed). The prompt (`WORKER_PROMPT` in `pm/__init__.py`) holds the task with its plan and milestone, and the `pm` commands the worker may use: `plan add`, `plan edit`, `task add`, `decision add`, `task-pending-decision`, `task done|drop`. It points the worker to `pm instructions` for examples of every command. Agents outside `pm run`, e.g. a Claude session asked to plan work, should run `pm instructions` too: say so in the project's `AGENTS.md` or `CLAUDE.md`. **The worker has full shell access in the project.** Logs: `.git/pm-logs/task-<id>.log` (`log_dir`). On a usage limit, `pm` waits for the reset and retries.
