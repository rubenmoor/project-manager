# pm

Task queue for [Claude Code](https://claude.com/claude-code) workers. A project's work is **milestones → plans → tasks** in an SQLite database; `pm run` works the queue one task at a time with `claude -p`, waits out usage limits, and asks you the questions workers raise.

One Python file, standard library only (Python 3.11+), plus `git` and `claude`. Install: `nix run github:<you>/pm`, `pipx install .`, or `python3 pm.py`.

## Use

In the project's git repository:

```sh
pm milestone add   # editor: first line the title, the rest the description (Markdown)
pm run             # work the queue; Ctrl-C stops, `pm run` continues
```

Every command first makes sure of two things. If `project-manager.toml` is missing, it writes the defaults (database file name, worker command, usage-limit margin, log folder, editor, the two prompts below), adds the database to `.gitignore` and commits both. If the database is missing, it creates it with the schema (`SCHEMA` in `pm.py`). Otherwise `pm` never commits. Workers commit their own work; nothing is pushed.

**Flow.** A new milestone queues the task "write the masterplan" (`masterplan_prompt`): the worker writes the milestone's first plan. A new plan, or a plan whose title or description changes, queues "create tasks for plan N" (`plan_tasks_prompt`): the worker splits it into tasks and decisions. A milestone is `implemented` once it has a plan and no open task.

**Data.** A milestone has a title, a description, a status and a created date; plans belong to a milestone, tasks to a plan (the masterplan task only to its milestone), and `task_decision` links tasks to the decisions they wait for. Everything is referenced by its integer id.

**Queue** (the `queue` view): open tasks of open milestones that no pending decision blocks, by priority (task's, else plan's, else 0; highest first), then milestone, plan and task id.

## Commands

| | |
|---|---|
| `status`, `queue`, `show <task>`, `validate` | Inspect |
| `decide` | Answer pending decisions (number, text, `s` skip, `x <reason>` irrelevant: drops the tasks it blocks) |
| `run [--once] [--dry-run]` | Work the queue until it is empty, a worker fails, or the tree has uncommitted changes |
| `milestone add` | In your editor (as `git commit`): first line title, rest description; quit unsaved or save empty to abort |
| `milestone add <title> < description.md` | Non-interactive: the description from stdin |
| `milestone set <milestone>` | `--title`, `--status`, `--description` |
| `plan add <milestone> <title> --description …` | `--priority` |
| `plan edit <plan>` | `--title`, `--description`, `--priority <N\|none>` |
| `task add <plan> <title> --description …`, `task done\|drop <task> <note>`, `task priority <task> <N\|none>` | `--priority` |
| `decision add <question>` | `--context`, `--option` (recommended first) |
| `task-pending-decision <task> <decision>` | The task waits until the decision is answered |

Global: `--root <repo>`. A text value `-` reads stdin.

## Workers

Each task runs the configured `worker` command (default: `claude -p` with `Bash Read Edit Write Glob Grep` allowed). The prompt (`WORKER_PROMPT` in `pm.py`) holds the task with its plan and milestone, and the `pm` commands the worker may use: `plan add`, `plan edit`, `task add`, `decision add`, `task-pending-decision`, `task done|drop`. **The worker has full shell access in the project.** Logs: `.git/pm-logs/task-<id>.log` (`log_dir`). On a usage limit, `pm` waits for the reset and retries.
