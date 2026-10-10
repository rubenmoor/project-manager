# pm

Task queue for [Claude Code](https://claude.com/claude-code) workers. A project's work is **milestone → masterplan → plans → tasks** in an SQLite database; `pm run` works the queue one task at a time with `claude -p`, waits out usage limits, and asks you the questions workers raise.

One Python file, standard library only (Python 3.11+), plus `git` and `claude`. Install: `nix run github:<you>/pm`, `pipx install .`, or `python3 pm.py`.

## Use

In the project's git repository:

```sh
pm milestone add "First milestone" --description "What it achieves, how you know it's done."
pm sync      # masterplan stub: tasks that write the masterplan and split it into plans and tasks
pm run       # work the queue; Ctrl-C stops, `pm run` continues
```

The first command creates `tasks.dbsqlite` (schema: `SCHEMA` in `pm.py`). Commit it or ignore it; `pm` never commits. Workers commit their own work; nothing is pushed.

**Queue** (the `queue` view): open tasks of open milestones that no pending decision blocks, by priority (task's, else plan's, else 0; highest first), then task ID (`M001-P002-T003`).

## Commands

| | |
|---|---|
| `status`, `queue`, `show <task>`, `validate` | Inspect |
| `sync` | Stub masterplans of new milestones; mark finished milestones `implemented` |
| `decide` | Answer pending decisions (number, text, `s` skip, `x <reason>` irrelevant: drops the tasks it blocks) |
| `run [--once] [--dry-run]` | Work the queue until it is empty, a worker fails, or the tree has uncommitted changes |
| `milestone add <title>`, `milestone set <M>` | `--title`, `--status`, `--description` |
| `masterplan <M>` | `--status`, `--goal`, `--scope`, `--exit-criteria` |
| `plan add <M> <title>`, `plan priority <M-P> <N\|none>` | `--description`, `--priority` |
| `task add <M-P> <title> --description …`, `task done\|drop <task> <note>`, `task block <task> <D>`, `task priority <task> <N\|none>` | `--priority`, `--blocked-by` |
| `decision add <question>` | `--context`, `--option` (recommended first), `--blocks <task>…` |

Global: `--root <repo>`, `--db <file>`. A text value `-` reads stdin.

## Workers

Each task runs `claude -p` with `WORKER_PROMPT` (in `pm.py`) and `pm show <task>`, with `Bash Read Edit Write Glob Grep` allowed. **The worker has full shell access in the project.** It closes its task with `pm task done|drop`, or blocks it with `pm decision add --blocks`, and commits. Logs: `.git/pm-logs/<task>.log`. On a usage limit, `pm` waits for the reset and retries.
