# pm

A project manager for [Claude Code](https://claude.com/claude-code) workers. You write a project's work down as **milestone → masterplan → plans → tasks**, one TOML file per milestone. `pm` computes a task queue from it and works the queue one task at a time with `claude -p`. It waits out Claude's usage limits and asks you about the decisions the workers raise.

One Python file, standard library only (Python 3.11+), plus `git` and the `claude` CLI.

## Install

- **Nix:** `nix run github:<you>/pm -- status`, or add the flake's `packages.default` to your system or dev shell.
- **pip:** `pipx install .` or `pip install .` installs the `pm` command.
- **Plain:** copy `pm.py` anywhere and run `python3 pm.py …`.

## Start

In a git repository, write `plans/M001.toml`:

```toml
id = "M001"
title = "First milestone"
status = "open"
goal = '''
What this milestone achieves, and how you know it's done.
'''
```

Commit it, then:

```sh
pm sync      # masterplan stub + the two tasks that write the masterplan
pm queue     # what runs next
pm run       # work the queue; Ctrl-C stops, `pm run` continues
```

The first two tasks of each milestone write its masterplan and split it into plans and tasks. Every later task is one `claude -p` run. Workers commit their own work; `pm` never pushes.

## Files

All in `plans/` of the project (`--dir` for another folder), TOML. `pm` writes them in a fixed key order and keeps only the first comment line; hand edits are fine, `pm validate` checks them.

| File | Content |
|---|---|
| `M001.toml`, `M002.toml`, … | One milestone each: the milestone, its masterplan, its plans, its tasks |
| `pm-decisions.toml` | Questions for the human: pending, answered, irrelevant |

IDs: `M001`, `P001`, `T001`, `D0001`; zero-padded, `+1` per new one, never reused. Plan IDs count per milestone, task IDs per plan, decision IDs globally. A task's full ID is `M001-P002-T003`.

## Milestone file `Mnnn.toml`

```toml
id = "M002"                      # = the file name
title = "…"
status = "open"                  # open | implemented
goal = '''
…
'''

[masterplan]                     # missing until `pm sync` writes the stub
status = "stub"                  # stub | draft | agreed
goal = '''…'''
scope = '''…'''                  # in and out
exit_criteria = '''…'''          # checkable

[[plans]]
id = "P002"
title = "…"
priority = 1                     # optional integer, default 0
goal = '''…'''                   # optional

[[tasks]]
id = "T001"
plan = "P002"                    # required: the plan this task belongs to
subject = "…"                    # one line
status = "open"                  # open | done | dropped
priority = -1                    # optional integer; default: the plan's priority
blocked_by = ["D0003"]           # optional: decisions it waits for
note = "2026-10-10, src/x.py"    # done: date and files or commit; dropped: the reason
details = '''
What to do, files, acceptance criteria. Markdown is fine.
'''
```
Rules: no other keys; plan IDs unique per milestone; `(plan, id)` unique per milestone; `plan` names a `[[plans]]` entry; an open task has `details`; every `blocked_by` entry exists in `pm-decisions.toml`.

Text fields are TOML multi-line literal strings (`'''…'''`): Markdown goes in as-is, with no escaping. Only `'''` inside a text doesn't fit; `pm` then writes an escaped one-line string.

## Decisions `pm-decisions.toml`

```toml
[[decisions]]
id = "D0003"
question = "…"
status = "pending"               # pending | answered | irrelevant
options = ["recommended", "other"]   # optional
context = '''…'''                # optional
answer = "…"                     # answered only
reason = "…"                     # irrelevant only
closed = "2026-10-10"            # answered and irrelevant
```
A decision blocks the tasks that list it in `blocked_by` while it is pending.

## Queue

Computed, never stored: every task with `status = "open"` in a milestone with `status = "open"` that no pending decision blocks. Order: priority, highest first, then full task ID ascending. A task's priority is its own `priority`, else its plan's `priority`, else 0. Positive moves work up, negative moves it down.

## Commands

| Command | Does |
|---|---|
| `pm status` | Open milestones, the first 10 queued tasks, pending decisions |
| `pm queue` | The whole queue with priorities |
| `pm show <task>` | A task with its plan, masterplan, milestone and decisions |
| `pm validate` | Checks every file in the plans folder; exit 1 on errors |
| `pm sync` | Writes a masterplan stub for every open milestone without `[masterplan]`: plan P001 "write masterplan" with T001 (goal, scope, exit criteria) and T002 (split into plans and tasks, then `status = "draft"`). Sets `status = "implemented"` on an open milestone whose masterplan isn't `stub` and whose tasks are all closed |
| `pm decide` | Asks about each pending decision in the terminal: a number picks an option, other text is the answer, `s` skips, `x <reason>` marks it irrelevant. An irrelevant decision drops the open tasks it blocks |
| `pm run` | Loop: validate → sync → decide (only at a terminal, only if decisions are pending) → work the first queued task → sync. Stops when no milestone is open, the queue is empty, the tree is dirty, the plans folder is invalid, or a worker fails. `--once`: one task. `--dry-run`: print the next worker command and change nothing |
| `pm task add <Mnnn-Pnnn> <subject> --details <text\|-> [--priority N] [--blocked-by D…]` | New task; prints its ID |
| `pm task done <task> <note>`, `pm task drop <task> <reason>` | Close a task |
| `pm task block <task> <Dnnnn>` | The task waits for a decision |
| `pm task priority <task> <N\|none>`, `pm plan priority <Mnnn-Pnnn> <N\|none>` | Set or clear a priority |
| `pm plan add <Mnnn> <title> [--goal <text\|->] [--priority N]` | New plan; prints its ID |
| `pm decision add <question> [--context <text\|->] [--option …]… [--blocks <task>…]` | New pending decision, recommended option first; prints its ID |
| `pm masterplan <Mnnn> [--status …] [--goal …] [--scope …] [--exit-criteria …]` | Set masterplan fields |

Global options: `--root <repo>` (default: the git repository around the current directory), `--dir <folder>` (default: `plans`). A text option given as `-` is read from stdin.

The edit commands check the plans folder after the change and write nothing if it would become invalid. `sync`, `decide` and `run` commit the plans folder as `pm: …`; the edit commands don't commit. Nothing is pushed.

Working a task: `claude -p "<worker prompt>" --permission-mode acceptEdits --allowedTools "Bash Read Edit Write Glob Grep"` in the project root. The output goes to `<git dir>/pm-logs/<task>.log`, inside the git directory, so it is never tracked. The worker prompt holds the worker rules (below) and `pm show <task>`. The result, first match wins:

| Result | Condition | `pm run` |
|---|---|---|
| limit | the log's last 5 lines name a usage limit and when it resets | waits until 5 min after the reset, then runs the same task again |
| failed | exit ≠ 0, plans folder invalid, uncommitted changes, or the task still open and not blocked | stops and shows why; nothing changes |
| done | the task is `done` or `dropped` | next task |
| blocked | the task is open and a pending decision blocks it | next task; `pm decide` unblocks it |

The worker has unrestricted shell access in the project (`Bash`), which it needs to build, test and commit. Run `pm run` only in repositories where that is fine.

## Worker rules

`pm run` puts these rules into every worker's prompt (`WORKER_PROMPT` in `pm.py`), together with `pm show <task>`. The worker also follows the project's own agent instructions (`AGENTS.md`, `CLAUDE.md`, …).

1. Work on the task only. Change the TOML files only through `pm` (prose fields may be edited by hand).
2. Follow-up work: `pm task add` in the same plan, or `pm plan add` and tasks in it for work outside this plan. Priorities only where the default order (plan, then task ID) is wrong.
3. Needs a human answer: `pm decision add "<question>" --context … --option <recommended> --option … --blocks <tasks>`. If the task itself is blocked, include it in `--blocks`, commit what's done, stop.
4. Finished: `pm task done <task> "<YYYY-MM-DD, files or commit>"` or `pm task drop <task> "<reason>"`. Run `pm validate` and the project's checks. Commit everything in one commit, message `<task> <subject>`. Don't push. Stop.

## Development

```sh
python3 -m unittest discover tests
```

The tests build throwaway git repositories and use a fake `claude` that works its task through `pm` commands.
