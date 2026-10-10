# pm

Task queue for [Claude Code](https://claude.com/claude-code) workers. A project's work is **milestone → masterplan → plans → tasks** in an SQLite database; `pm run` works the queue one task at a time with `claude -p`, waits out usage limits, and asks you the questions workers raise.

One Python file, standard library only (Python 3.11+), plus `git` and `claude`. Install: `nix run github:<you>/pm`, `pipx install .`, or `python3 pm.py`.

## Use

In the project's git repository:

```sh
pm milestone add   # editor: first line the title, the rest the description (Markdown)
pm run             # work the queue; Ctrl-C stops, `pm run` continues
```

Every command first makes sure of two things. If `project-manager.toml` is missing, it writes the defaults (database file name, worker command, usage-limit margin, log folder, editor, the prompt of the masterplan task), adds the database to `.gitignore` and commits both. If the database is missing, it creates it with the schema (`SCHEMA` in `pm.py`). Otherwise `pm` never commits. Workers commit their own work; nothing is pushed.

Milestones, plans, tasks and decisions are rows with integer ids; plans point to their milestone, tasks to their plan, and `task_decision` links tasks and decisions (many to many). Commands take these ids.

**Queue** (the `queue` view): open tasks of open milestones that no pending decision blocks, by priority (task's, else plan's, else 0; highest first), then milestone, plan and task id.

## Commands

| | |
|---|---|
| `status`, `queue`, `show <task>`, `validate` | Inspect |
| `decide` | Answer pending decisions (number, text, `s` skip, `x <reason>` irrelevant: drops the tasks it blocks) |
| `run [--once] [--dry-run]` | Work the queue until it is empty, a worker fails, or the tree has uncommitted changes; marks a milestone `implemented` once its masterplan isn't a stub and all its tasks are closed |
| `milestone add` | Adds the milestone, a masterplan stub and a plan with one task, "write the masterplan" (goal, scope, exit criteria, plans and tasks; its description is `masterplan_prompt` from the config). In your editor (as `git commit`): first line title, rest description; quit unsaved or save empty to abort |
| `milestone add <title> < description.md` | Non-interactive: the description from stdin |
| `milestone set <milestone>` | `--title`, `--status`, `--description` |
| `masterplan <milestone>` | `--status`, `--goal`, `--scope`, `--exit-criteria` |
| `plan add <milestone> <title>`, `plan priority <plan> <N\|none>` | `--description`, `--priority` |
| `task add <plan> <title> --description …`, `task done\|drop <task> <note>`, `task block <task> <decision>`, `task priority <task> <N\|none>` | `--priority`, `--blocked-by` |
| `decision add <question>` | `--context`, `--option` (recommended first), `--blocks <task>…` |

Global: `--root <repo>`. A text value `-` reads stdin.

## Workers

Each task runs the configured `worker` command (default: `claude -p` with `Bash Read Edit Write Glob Grep` allowed), its prompt being `WORKER_PROMPT` (in `pm.py`) and `pm show <task>`. **The worker has full shell access in the project.** It closes its task with `pm task done|drop`, or blocks it with `pm decision add --blocks`, and commits. Logs: `.git/pm-logs/task-<id>.log` (`log_dir`). On a usage limit, `pm` waits for the reset and retries.
