# pm

Task queue for [Claude Code](https://claude.com/claude-code) workers. A project's work is **milestone → masterplan → plans → tasks** in an SQLite database; `pm run` works the queue one task at a time with `claude -p`, waits out usage limits, and asks you the questions workers raise.

One Python file, standard library only (Python 3.11+), plus `git` and `claude`. Install: `nix run github:<you>/pm`, `pipx install .`, or `python3 pm.py`.

## Use

In the project's git repository:

```sh
pm milestone add   # editor: first line the title, the rest the description (Markdown)
pm sync      # masterplan stub: tasks that write the masterplan and split it into plans and tasks
pm run       # work the queue; Ctrl-C stops, `pm run` continues
```

The first command writes `project-manager.toml` with the defaults and commits it: database file name, worker command, usage-limit margin, log folder, editor. It creates the database (`tasks.dbsqlite`, schema: `SCHEMA` in `pm.py`) on first use; commit it or ignore it. Otherwise `pm` never commits. Workers commit their own work; nothing is pushed.

**Queue** (the `queue` view): open tasks of open milestones that no pending decision blocks, by priority (task's, else plan's, else 0; highest first), then task ID (`M001-P002-T003`).

## Commands

| | |
|---|---|
| `status`, `queue`, `show <task>`, `validate` | Inspect |
| `sync` | Stub masterplans of new milestones; mark finished milestones `implemented` |
| `decide` | Answer pending decisions (number, text, `s` skip, `x <reason>` irrelevant: drops the tasks it blocks) |
| `run [--once] [--dry-run]` | Work the queue until it is empty, a worker fails, or the tree has uncommitted changes |
| `milestone add` | In your editor (as `git commit`): first line title, rest description; quit unsaved or save empty to abort |
| `milestone add <title> < description.md` | Non-interactive: the description from stdin |
| `milestone set <M>` | `--title`, `--status`, `--description` |
| `masterplan <M>` | `--status`, `--goal`, `--scope`, `--exit-criteria` |
| `plan add <M> <title>`, `plan priority <M-P> <N\|none>` | `--description`, `--priority` |
| `task add <M-P> <title> --description …`, `task done\|drop <task> <note>`, `task block <task> <D>`, `task priority <task> <N\|none>` | `--priority`, `--blocked-by` |
| `decision add <question>` | `--context`, `--option` (recommended first), `--blocks <task>…` |

Global: `--root <repo>`. A text value `-` reads stdin.

## Workers

Each task runs the configured `worker` command (default: `claude -p` with `Bash Read Edit Write Glob Grep` allowed), its prompt being `WORKER_PROMPT` (in `pm.py`) and `pm show <task>`. **The worker has full shell access in the project.** It closes its task with `pm task done|drop`, or blocks it with `pm decision add --blocks`, and commits. Logs: `.git/pm-logs/<task>.log` (`log_dir`). On a usage limit, `pm` waits for the reset and retries.
