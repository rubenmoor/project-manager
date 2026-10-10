# pm

Task queue for [Claude Code](https://claude.com/claude-code) workers. A project's work is **milestones → plans → tasks** in SQLite, kept in git as SQL text; `pm run` works the queue one task at a time with `claude -p`, waits out usage limits, and asks you the questions workers raise.

Python package `pm/` (code in `__init__.py`, the config template `project-manager.toml`), standard library only (Python 3.11+), plus `git` and `claude`. Install: `nix run github:<you>/pm`, `pipx install .`, or `python3 -m pm` with this repository on `PYTHONPATH`.

## Use

In the project's git repository:

```sh
pm init            # write project-manager.toml and tasks.sql, commit them
pm milestone add   # editor: first line the title, the rest the description (Markdown)
pm run             # work the queue; Ctrl-C stops, `pm run` continues
```

`pm init` writes `project-manager.toml` from the template `pm/project-manager.toml` (database file name, worker command, usage-limit margin, log folder, editor, the two prompts below) and `tasks.sql` with the schema (`SCHEMA` in `pm/__init__.py`), and commits them; run again, it adds only what is missing. Other commands need both files. Nothing is pushed.

**Storage.** `tasks.sql` is the database: the schema, then one `INSERT` per row, sorted by key. Every command loads it into an in-memory SQLite database; a change rewrites it. So `git diff` shows changed rows, branches that change different rows merge cleanly, and two branches that create the same id conflict on that line (fix the file, then `pm validate`). `pm` commits its own changes (`pm: …`); inside `pm run`, workers commit theirs together with their work. Hand edits are fine; `pm validate` checks them.

**Flow.** A new milestone comes with a first plan, the masterplan (description "tbd."), and in it the task "write the masterplan" (`masterplan_prompt`): the worker writes the plan with `pm plan edit`, divided into parts, and states that the masterplan's own tasks each write the plan for one part. So splitting the masterplan into tasks yields one plan-writing task per part, and each of those adds a plan. A new plan, or a plan whose title or description changes, queues "create tasks for plan N" (`plan_tasks_prompt`): the worker splits it into tasks and decisions. A milestone is `implemented` once it has no open task.

**Data.** A milestone has a title, a description, a status and a created date; plans belong to a milestone, tasks to a plan, and `task_decision` links tasks to the decisions they wait for. Everything is referenced by its integer id.

**Queue** (the `queue` view): open tasks of open milestones that no pending decision blocks, by priority (task's, else plan's, else 0; highest first), then milestone, plan and task id.

## Commands

| | |
|---|---|
| `init` | Write and commit the config and the database (missing ones only) |
| `status`, `queue`, `show <task>`, `validate` | Inspect |
| `decide` | Answer pending decisions (number, text, `s` skip, `x <reason>` irrelevant: drops the tasks it blocks) |
| `run [--once] [--dry-run]` | Work the queue until it is empty, a worker fails, or the tree has uncommitted changes |
| `milestone add`, `plan add <milestone>`, `task add <plan>` | In your editor (as `git commit`): first line title, rest description (Markdown); quit unsaved or save empty to abort |
| `milestone add <title>`, `plan add <milestone> <title>`, `task add <plan> <title>` | Non-interactive: the description from stdin. `plan add` and `task add` also take `--priority` |
| `milestone set <milestone>` | `--title`, `--status`, `--description` |
| `plan edit <plan>` | `--title`, `--description`, `--priority <N\|none>` |
| `task done\|drop <task> <note>`, `task priority <task> <N\|none>` | Close a task, set its priority |
| `decision add <question>` | `--context`, `--option` (recommended first) |
| `task-pending-decision <task> <decision>` | The task waits until the decision is answered |

Global: `--root <repo>`. A text value `-` reads stdin.

## Workers

Each task runs the configured `worker` command (default: `claude -p` with `Bash Read Edit Write Glob Grep` allowed). The prompt (`WORKER_PROMPT` in `pm/__init__.py`) holds the task with its plan and milestone, and the `pm` commands the worker may use: `plan add`, `plan edit`, `task add`, `decision add`, `task-pending-decision`, `task done|drop`. **The worker has full shell access in the project.** Logs: `.git/pm-logs/task-<id>.log` (`log_dir`). On a usage limit, `pm` waits for the reset and retries.
