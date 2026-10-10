# pm

A project manager for [Claude Code](https://claude.com/claude-code) workers. You keep a project's work as **milestone → masterplan → plans → tasks** in an SQLite database inside that project. `pm` computes a task queue from it and works the queue one task at a time with `claude -p`. It waits out Claude's usage limits and asks you about the decisions the workers raise.

One Python file, standard library only (Python 3.11+), plus `git` and the `claude` CLI.

## Install

- **Nix:** `nix run github:<you>/pm -- status`, or add the flake's `packages.default` to your system or dev shell.
- **pip:** `pipx install .` or `pip install .` installs the `pm` command.
- **Plain:** copy `pm.py` anywhere and run `python3 pm.py …`.

## Start

In the git repository of the project you want worked:

```sh
pm milestone add "First milestone" --description "What it achieves, and how you know it's done."
pm sync      # masterplan stub + the two tasks that write the masterplan
pm queue     # what runs next
pm run       # work the queue; Ctrl-C stops, `pm run` continues
```

The first `pm` command in a repository creates the database `tasks.dbsqlite` in its root, with the schema from `pm.py` (`--db` names another file). The first two tasks of each milestone write its masterplan and split it into plans and tasks. Every later task is one `claude -p` run. Workers commit their own work; `pm` never commits or pushes.

## Database

Commit `tasks.dbsqlite` or add it to `.gitignore`, as the project prefers. `pm` doesn't care: it never commits, and its check for uncommitted changes ignores the database either way. The schema is `SCHEMA` in `pm.py`; `PRAGMA user_version` holds its version, and `pm` refuses a database of another version.

| Table | Rows |
|---|---|
| `milestone` | `id`, `title` (short), `status` (`open` \| `implemented`), `description` (Markdown) |
| `masterplan` | One per milestone, missing until `pm sync` writes the stub: `status` (`stub` \| `draft` \| `agreed`), `goal`, `scope` (in and out), `exit_criteria` (checkable) |
| `plan` | Many per milestone: `id`, `title`, `priority` (NULL: 0), `description` |
| `task` | Many per plan: `id`, `title`, `status` (`open` \| `done` \| `dropped`), `priority` (NULL: the plan's), `note` (done: date and files or commit; dropped: the reason), `description` (Markdown: what to do, files, acceptance criteria) |
| `decision` | A question for the human: `id`, `question`, `status` (`pending` \| `answered` \| `irrelevant`), `options` (JSON array, recommended first), `context`, `answer`, `reason`, `closed` |
| `task_decision` | Tasks ↔ decisions, many to many: the task waits for the decision while it is pending |
| `queue` (view) | The queue, below |

IDs: `M001`, `P001`, `T001`, `D0001`; zero-padded, `+1` per new one. Plan IDs count per milestone, task IDs per plan, decision IDs globally. Keys are composite: a plan is `(milestone, id)`, a task `(milestone, plan, id)`, written `M001-P002-T003`.

The rules are constraints of the schema: ID formats, states, non-empty titles, an open task has a description, a decision's fields match its status, every reference exists (foreign keys). Every `pm` command is one transaction; one that would break a rule writes nothing. Hand edits with `sqlite3 tasks.dbsqlite` are fine, but the `sqlite3` shell leaves foreign keys off, so run `pm validate` afterwards.

## Queue

Computed, never stored (the `queue` view): every task with status `open` in a milestone with status `open` that no pending decision blocks. Order: priority, highest first, then full task ID ascending. A task's priority is its own `priority`, else its plan's `priority`, else 0. Positive moves work up, negative moves it down.

## Commands

| Command | Does |
|---|---|
| `pm status` | Open milestones, the first 10 queued tasks, pending decisions |
| `pm queue` | The whole queue with priorities |
| `pm show <task>` | A task with its plan, masterplan, milestone and decisions |
| `pm validate` | Checks the database (integrity, foreign keys, schema version); exit 1 on errors |
| `pm sync` | Writes a masterplan stub for every open milestone without one: plan P001 "write masterplan" with T001 (goal, scope, exit criteria) and T002 (split into plans and tasks, then masterplan status `draft`). Sets `implemented` on an open milestone whose masterplan isn't `stub` and whose tasks are all closed |
| `pm decide` | Asks about each pending decision in the terminal: a number picks an option, other text is the answer, `s` skips, `x <reason>` marks it irrelevant. An irrelevant decision drops the open tasks it blocks |
| `pm run` | Loop: validate → sync → decide (only at a terminal, only if decisions are pending) → work the first queued task → sync. Stops when no milestone is open, the queue is empty, the tree has uncommitted changes apart from the database, the database is invalid, or a worker fails. `--once`: one task. `--dry-run`: print the next worker command and change nothing |
| `pm milestone add <title> [--description <text\|->]` | New open milestone; prints its ID |
| `pm milestone set <Mnnn> [--title …] [--status …] [--description …]` | Set milestone fields |
| `pm masterplan <Mnnn> [--status …] [--goal …] [--scope …] [--exit-criteria …]` | Set masterplan fields |
| `pm plan add <Mnnn> <title> [--description <text\|->] [--priority N]` | New plan; prints its ID |
| `pm plan priority <Mnnn-Pnnn> <N\|none>` | Set or clear a plan's priority |
| `pm task add <Mnnn-Pnnn> <title> --description <text\|-> [--priority N] [--blocked-by D…]` | New task; prints its ID |
| `pm task done <task> <note>`, `pm task drop <task> <reason>` | Close a task |
| `pm task block <task> <Dnnnn>` | The task waits for a decision |
| `pm task priority <task> <N\|none>` | Set or clear a task's priority |
| `pm decision add <question> [--context <text\|->] [--option …]… [--blocks <task>…]` | New pending decision, recommended option first; prints its ID |

Global options: `--root <repo>` (default: the git repository around the current directory), `--db <file>` (default: `tasks.dbsqlite`, relative to the root). A text option given as `-` is read from stdin.

Working a task: `claude -p "<worker prompt>" --permission-mode acceptEdits --allowedTools "Bash Read Edit Write Glob Grep"` in the project root. The output goes to `<git dir>/pm-logs/<task>.log`, inside the git directory, so it is never tracked. The worker prompt holds the worker rules (below) and `pm show <task>`. The result, first match wins:

| Result | Condition | `pm run` |
|---|---|---|
| limit | the log's last 5 lines name a usage limit and when it resets | waits until 5 min after the reset, then runs the same task again |
| failed | exit ≠ 0, database invalid, uncommitted changes (apart from the database), or the task still open and not blocked | stops and shows why |
| done | the task is `done` or `dropped` | next task |
| blocked | the task is open and a pending decision blocks it | next task; `pm decide` unblocks it |

The worker has unrestricted shell access in the project (`Bash`), which it needs to build, test and commit. Run `pm run` only in repositories where that is fine.

## Worker rules

`pm run` puts these rules into every worker's prompt (`WORKER_PROMPT` in `pm.py`), together with `pm show <task>`. The worker also follows the project's own agent instructions (`AGENTS.md`, `CLAUDE.md`, …).

1. Work on the task only. Change milestones, plans, tasks and decisions only through `pm`, never the database directly.
2. Follow-up work: `pm task add` in the same plan, or `pm plan add` and tasks in it for work outside this plan. Priorities only where the default order (plan, then task ID) is wrong.
3. Needs a human answer: `pm decision add "<question>" --context … --option <recommended> --option … --blocks <tasks>`. If the task itself is blocked, include it in `--blocks`, commit what's done, stop.
4. Finished: `pm task done <task> "<YYYY-MM-DD, files or commit>"` or `pm task drop <task> "<reason>"`. Run the project's checks. Commit the work in one commit, message `<task> <title>`. Don't push. Stop.

## Development

```sh
python3 -m unittest discover tests
```

The tests build throwaway git repositories and use a fake `claude` that works its task through `pm` commands.
