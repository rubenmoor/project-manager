# pm

Task queue for [Claude Code](https://claude.com/claude-code) workers. A project's work is **milestones → plans → tasks** in SQLite, kept in git as SQL text; `pm run` works the queue one task at a time with `claude -p` (one per work tree: the work trees named in the config run their tasks in parallel, the trunk runs yours and pm's planning), waits out usage limits; the questions workers raise wait for your answer while it goes on with other tasks.

Python package `pm/` (code in `__init__.py`, the config template `project-manager.toml`, the agent reference `instructions.md`), standard library only (Python 3.11+), plus `git` and `claude`. Install: `nix run github:<you>/pm`, `pipx install .`, or `python3 -m pm` with this repository on `PYTHONPATH`.

## Use

In the project's git repository:

```sh
pm init            # write project-manager.toml, commit it; check and adjust its defaults
pm milestone add   # editor: first line the subject, the rest the body (Markdown)
pm run             # work the queue, then wait for new tasks; Ctrl-C: stop after the running task(s), twice: stop now; `pm run` continues
```

### Writing a milestone

The milestone is the only text a worker gets from you; the masterplan, the plans and the tasks are derived from it. A description of what you want, and a few constraints as bullet points; no fixed structure. Leave the how to the masterplan. In the editor (`pm milestone add`):

```markdown
Users can export their data as CSV

"Export" on the account page downloads one CSV per table the user owns; support exports it by hand today.

- No new dependencies; the stdlib `csv` module is enough.
- Columns are named as in the API, not the database.
- No import, no other formats.
```

The first line is the subject, the rest the body. The same without the editor, e.g. from a script:

```sh
pm milestone add "Users can export their data as CSV" < milestone-csv-export.md
```

Questions you can't answer yet don't need to block the milestone: workers turn them into decisions, `pm run` skips the tasks they block, and you answer with `pm decide` in the terminal, `pm decision answer` (e.g. from a Claude session), or on a decision's page in `pm-dashboard`. Answered decisions are the project's decision log: `pm decision list --answered`, with what each answer changed (consequences), which newer answer replaced it (supersede), and the lasting choices workers recorded on their own (`pm decision record`), which you override by superseding them.

`pm init` writes `project-manager.toml` from the template `pm/project-manager.toml` (database file name, worker command, usage-limit margin, log folder, editor, the five prompts below, the priority settings, the sign-offs) and commits it; run again, it does nothing. Adjust the defaults (e.g. the database file name) before the first other command: that one writes `tasks.sql` with the schema (`SCHEMA` in `pm/__init__.py`) if it is missing. Other commands need the config. Nothing is pushed.

**Storage.** `tasks.sql` is the database: the schema, then one `INSERT` per row, sorted by key. Every command loads it into an in-memory SQLite database; a change rewrites it. So `git diff` shows changed rows, branches that change different rows merge cleanly, and two branches that create the same id conflict on that line (fix the file, then `pm validate`). `pm` commits its own changes (`pm: …`); inside `pm run`, a worker stages its work and `pm run` commits it with `tasks.sql` (`task <id>: <subject>`, the task's note as body). Changes the worker left unstaged: of a task waiting for a decision, stashed (`pm: task <id> run <run> left`) for the task's next run; of a closed task, committed on a branch `pm/task-<id>-run-<run>-left`, the tree cleaned, and a pending decision to investigate them. The run goes on. Hand edits are fine; `pm validate` checks them. **Migrations.** `tasks.sql` carries its schema version (`PRAGMA user_version`). A file of an older version is migrated on the next command: the steps in `MIGRATIONS` (`pm/__init__.py`) run in order, the rows are copied into the current schema, and pm writes and commits the file (`pm: tasks.sql: schema 12 -> 13`). A newer file, or one without a migration path, is refused.

**Flow.** A new milestone comes with a first plan, the masterplan (body "tbd."), and in it the task "write the masterplan of milestone N: …" (`masterplan_prompt`): the worker writes the plan with `pm plan edit`, divided into parts, and states that the masterplan's own tasks each write the plan for one part. So splitting the masterplan into tasks yields one plan-writing task per part, and each of those adds a plan. A new plan, or a plan whose subject or body changes, queues "create tasks for plan N: …" (`plan_tasks_prompt`): the worker splits it into tasks and decisions. Edits cascade: a changed milestone queues "revise the masterplan of milestone N: …" (`revise_masterplan_prompt`), a changed plan "revise plan M: …" in each plan below it (`revise_plan_prompt`; below: written by one of its tasks, else, for the masterplan, any plan of the milestone); each revision that changes its plan cascades on. Implemented milestones (subject, body), done plans (no open task, nor in the plans below) and closed tasks can't be changed: a change to finished work is a new milestone or plan. Once a milestone has no open task, `pm run` queues "post-milestone cleanup of milestone N: …" in its first plan (`cleanup_prompt`: bring top-level docs up to date, remove redundant files and unused code, review pending decisions and other milestones); when that is closed and no task is open, the milestone is `implemented` (follow-up tasks of the cleanup run first; no second cleanup).

**Data.** Everything is referenced by its integer id.

- milestone: subject, body, status (`open`, `on-hold`, `implemented`), `queue_index` (default 0), `created_at`, `implemented_at`
- plan: belongs to a milestone; subject, body, priority, `created_at`, `created_by_task`
- task: belongs to a plan, except kind `exec`; subject, body, kind (`work`, `plan`: writes a plan; `exec`: an executive intervention by the human, no plan or milestone, queued as if at queue index 0, by priority; made by pm: `masterplan`, `split` for "create tasks for plan N: …", `cleanup`, `revise` for "revise the masterplan of …" and "revise plan N: …", `investigate` for "investigate the failure of task N: …", `merge` for "merge the trunk and work tree X"), status, priority, note, `created_at`, `retired_at` (done or dropped), `created_by_task`, `web`, `model` (NULL: by kind, `[models]`), `worktree` (`[worktrees]`; NULL: the trunk, for exec, planning and cleanup tasks)
- summary: an executive summary (Markdown), the task that wrote it, `head_commit` (where the next one's delta starts), `created_at`
- resource_use_report: the task that wrote it, `head_commit`, `created_at`; `resource_use_category`: its mutually exclusive categories (name, one sentence of criteria); `resource_use_task`: each task done when it was written, in one category
- custom_report: the human's question, its task, the one-paragraph answer (empty until answered), `created_at`, `answered_at`
- decision: subject, body, options, status, answer or reason, `created_at`, `closed_at`, `created_by_task`, `signoff` (made by pm, see below), `kind` (`rule` | `once`), `decided_by_task` (a worker's own choice), `consequences`; `task_decision` links tasks to the decisions they wait for; `decision_supersedes` links a decision to the ones it replaces (note: only partly)
- run: one worker run of `pm run`: task, start, end, result (`running`, `done`, `blocked`, `failed`, `limit`), reason, exit code, `base_commit` (HEAD at the start), `worktree` (the task's then; empty: the trunk), `pid` of `pm run`; from claude's JSON output: model, session, `continues_run` (warm hand-off: the run whose session it resumed), turns, tokens (input, output, cache read, cache creation), cost in USD; while it runs, `running` (at most one per work tree, uncommitted); a `pm run` that died leaves it, the next `pm run` marks it `failed` (`interrupted`), or refuses while that process lives
- timestamps in UTC; `created_by_task`: the task whose worker created the row (`PM_TASK`), empty for the human or pm

**Queue** (the `queue` view): open tasks of open milestones (not on hold) that no pending decision blocks, by the milestone's queue index (lowest first), then by priority (task's, else plan's, else 0; highest first), then milestone, plan and task id.

**Sign-offs.** `signoff` in project-manager.toml, `["web"]` by default: then only the decisions workers raise and web access stop the run. Each value makes `pm run` wait for your approval before the next task of a kind runs:

- `"masterplans"`: "create tasks for plan N: …" of a masterplan waits for "Sign off the masterplan of milestone M: …"
- `"plans"`: "create tasks for plan N: …" of every other plan waits for "Sign off plan N: …"
- `"task-queue"`: work tasks wait for "Sign off the task queue", one decision for all work tasks without a sign-off; asked when the first of them heads the queue, i.e. after all planning work; work tasks added later need a new one
- `"web"`: a task marked web waits for "Grant task N: … web access (WebSearch, WebFetch)"; without `"web"` in the list, marked tasks get the tools unasked
- pm creates the decision when such a task heads the queue; `pm run` never asks, it goes on with the unblocked tasks and waits when the queue is empty
- the only answer is approve (`pm decide`: `a`, or `pm decision approve <id>`); leave it pending to change plans or tasks first; `pm decision list` shows the plan or the waiting tasks as they are then
- no `x` (irrelevant) for sign-offs

**Priorities.** Set by workers, following rules in project-manager.toml:

- `planning_priority` (1000): planning tasks (kinds `masterplan`, `split`, `plan`) run first within a milestone queue index, so all plans and tasks exist before implementation starts; milestones with a higher queue index wait, planning included. After a planning task, `pm run` checks it changed a plan (`plan`, `masterplan`; a "revise …" task may leave it as it is) or left its plan with tasks (`split`); otherwise the task fails (reopened).
- `plan_priority_prompt`: a new plan's priority by what it affects (database schema 900 … media and assets 200), adjusted after `pm plans list` when it must run before or after another plan. Appended to every worker's prompt.
- `task_priority_prompt`: a task gets its own priority only for a good reason, the plan's ± a multiple of 10. Appended to every worker's prompt.
- Both are also printed at the end of `pm instructions`; empty turns one off.

## Commands

| | |
|---|---|
| `init` | Write and commit the config (if missing) |
| `instructions` | The full reference for agents (`pm/instructions.md`): every command with examples, recipes, errors |
| `status`, `queue`, `milestone list`, `milestone show <id>`, `plans list`, `show <task\|d32\|p3\|m1>`, `validate` | Inspect; `show` also takes `decision 32` |
| `runs [task]` | Worker runs: start, duration, result, turns, tokens, cost; totals per task kind |
| `run [--once] [--dry-run]` | Work the queue; when it is empty, wait for tasks (`--once`: exit). Stops when Claude needs you or the tree has uncommitted changes |
| `stop` | Have the running `pm run` stop after its running task(s) (SIGUSR1; same as one Ctrl-C) |
| `milestone add`, `plan add <milestone>`, `task add <plan>` | In your editor (as `git commit`): first line subject, rest body (Markdown); quit unsaved or save empty to abort |
| `milestone add <subject>`, `plan add <milestone> <subject>`, `task add <plan> <subject>` | Non-interactive: the body from stdin. `plan add` requires `--intro` (plain English for an outsider, no pm terms); `plan add` and `task add` also take `--priority`; `task add` `--kind plan`, `--web`, `--worktree <name>` (required); `task add --exec <subject>`: an exec task, no plan |
| `milestone add … --queue <N>`, `milestone set <milestone>` | `--queue <N>`: queue index, lowest first (default 0); `set` also `--subject`, `--status`, `--body`. `milestone add\|set\|hold\|resume` is refused in a worker |
| `milestone hold <milestone>`, `milestone resume <milestone>` | Keep its plans and tasks out of the queue (a running task finishes), and back |
| `plan edit <plan>` | `--subject`, `--body`, `--intro` (queues nothing), `--priority <N\|none>`; a worker's `--body` on a plan without an intro needs `--intro` too |
| `task set <task>` | `--subject`, `--body`, `--priority <N\|none>`, `--web`/`--no-web`, `--model`, `--worktree <name>`; open tasks that aren't running |
| `task done\|drop <task> <note>`, `task priority <task> <N\|none>` | Close a task, set its priority |
| `decision add <subject>` | `--intro` (required: what has to be decided, plain English for an outsider), `--body`, `--option` (recommended first) |
| `decide` | Answer pending decisions one by one in the terminal (number, text, `s` skip, `x <reason>` irrelevant; sign-offs: `a` approve, `s` skip) |
| `decision list` | Pending decisions: context, options, blocked tasks; sign-offs with what they approve. `--answered`: the decision log (answered `rule` decisions nothing fully supersedes, newest first); `--all`: every decision; `--grep <text>` |
| `decision show <id>` | One decision: context, options, answer, consequences, tasks, supersede links (also `pm show d<id>`) |
| `decision record <subject> --answer …` | A decision nobody asked, added answered; `--intro` (required), `--body`, `--once`, `--supersedes <id>`, `--consequences`. In a worker: its own choice (`decided_by_task`) |
| `summary request`, `summary show [id]`, `summary list` | Queue an executive summary (an exec task, priority 10000; it reads the previous summary and reports the delta); show the newest or one; list them |
| `report request`, `report show [id]`, `report list` | Queue a resource use report (an exec task, priority 10000; two passes over all done tasks and their diffs: draw up mutually exclusive categories with criteria, then put each task in one; pm sums time, tokens and cost per category); show the newest or one; list them (with custom reports) |
| `report ask <question>`, `report answer` | A custom report: queue an exec task (priority 10000) that answers the question in one plain paragraph; in its worker, store the answer (stdin); `pm show c2` |
| `decision supersede <new> <old>`, `decision consequences <id> <text>`, `decision kind <id> rule\|once` | Link a replaced decision (`--note`: only partly); append what changed because of an answer; lasting rule or one-off |
| `decision answer <id> <n\|text>`, `decision approve <id>`, `decision irrelevant <id> <reason>` | Close a decision (`answer`: `--once`, `--supersedes <id>`); irrelevant drops the open tasks it blocks; approve: sign-offs only. Refused in a worker |
| `task-pending-decision <task> <decision>` | The task waits until the decision is answered |

Global: `--root <repo>`. A text value `-` reads stdin.

## Dashboard

`dashboard/` holds `pm-dashboard`, an optional local web view and editor of `tasks.sql`: the running worker and its output, queue, milestones, decisions, reports (executive summaries, resource use reports and custom one-paragraph reports; a button or form queues each), runs and cost, live; subjects, bodies and priorities edited in place; pending decisions answered, approved or closed. A separate package, not installed with `pm`: `nix run .#dashboard` (details: `dashboard/README.md`).

## Workers

Each task runs the configured `worker` command (default: `claude -p` with `Bash Read Edit Write Glob Grep` allowed). Web access only per task: a task marked web (`--web`) gets `WebSearch` and `WebFetch` in `{settings}`, after its sign-off (`"web"` in `signoff`). The prompt (`WORKER_PROMPT` in `pm/__init__.py`) holds the task with its plan and milestone, and the `pm` commands the worker may use: `plan add`, `plan edit`, `task add`, `task set`, `decision add`, `task-pending-decision`, `task done|drop`. It points the worker to `pm instructions` for examples of every command. Agents outside `pm run`, e.g. a Claude session asked to plan work, should run `pm instructions` too: say so in the project's `AGENTS.md` or `CLAUDE.md`. The prompt also has every worker look up the answered decisions on what its task touches first (`pm decision list --answered --grep …`). A project's `AGENTS.md`, for example:

```markdown
- Work is planned with pm: run `pm instructions` first.
- Decisions are pm's decision log: before planning or changing something, `pm decision list --answered --grep <word>`; cite as `decision 32: <subject>`.

## Outside pm workers

- Skip this section if `PM_TASK` is set (you are a pm worker).
- Milestones are mine; edit them with pm when we answer decisions: `pm decision list`, `pm milestone set <id> --body - <<'EOF'`, then `pm decision answer <id> …`.
```

Sections for the human's own sessions only: check `PM_TASK` (`pm run` sets it in every worker), and pm enforces it anyway: `milestone add|set|hold|resume` and `decision answer|approve|irrelevant` are refused in a worker. These checks, like the guard hook below, keep an honest worker on track; they are no security boundary: a worker with a shell can unset `PM_TASK`.

**The worker has full shell access in the project.** The default command has claude print JSON (`--output-format stream-json --verbose`): `pm` shows the text and tool calls in the terminal, each with the local time it got it, and records model, turns, tokens and cost in the run. Other commands' plain output is shown as it is. Logs, raw, each line prefixed with its UTC time and a tab: `.git/pm-logs/task-<id>-run-<run>.log` (`log_dir`), `task-<id>.log` links to the latest. On a usage limit, `pm` waits for the reset and retries.

The prompt goes to the worker's stdin, not its command line: a worker's `pkill -f <word in the task>` would otherwise match the worker itself. `{settings}` in the default command adds Claude Code hooks (`pm/guard.py`): one blocks `pkill -f`, `killall` and `kill -1`, nested ones (`bash -c '…'`, `nix develop -c …`) included, and commands that read or grep `tasks.sql`; a Stop hook sends the worker back once if it ends its turn with the task still open (not done, dropped or waiting for a decision) or a background task running. `claude -p` ends with the turn, and a background job with it, so `{settings}` also turns background tasks off (`CLAUDE_CODE_DISABLE_BACKGROUND_TASKS`) and lets Bash commands run up to 60 minutes in the foreground (`BASH_MAX_TIMEOUT_MS`). The worker runs in its own process group, so Ctrl-C in the terminal doesn't reach it: the first Ctrl-C (or `pm stop`, i.e. SIGUSR1) lets the task finish, then `pm run` exits; a second Ctrl-C, SIGTERM or SIGHUP stops the whole group now.

Model per task (`[models]` in the config, passed as `claude --model`): a task's own model (`pm task add|set --model`, the dashboard's task page) first; then `escalate` (Opus) for a task with a failed run or an investigation; then its kind's (Opus for masterplan, plan, split, revise, investigate); then `default` (Sonnet). Workers splitting a plan mark tasks that need deep reasoning `--model opus`. Only when the worker is claude and its command names no `--model`; `""` leaves the choice to claude. The Runs page compares cost and failures per model and kind.

Warm hand-off (`warm_handoff`, default 3): a work task right after a done work task of the same plan and work tree resumes that run's Claude session (`claude --resume`), so the worker keeps what it learned of the code. Only within 5 minutes of the previous run's end (`HANDOFF_MINUTES`, while the prompt cache is warm), at most `warm_handoff` tasks per session, only with claude's stream-json. Still one run, one commit and one cost record per task; the run shows "continues run N". A resume that fails before any tool call is recorded as a failed run and the task runs fresh.

One `pm run` per repository: it holds a lock (`.git/pm-run.lock`, with its pid) while it lives; `pm stop` signals the pid in it, also between tasks or while waiting for a usage limit. A `pm run` that crashes writes the error into its lock file; `pm status` and the dashboard show it ("pm not running"). A `running` run left by a `pm run` that died is recorded as failed by the next one, with that error as reason (else "interrupted"). While a worker runs in the trunk, other pm commands and the dashboard don't commit `tasks.sql`; their changes land in the task's commit.

**Work trees.** The project's own checkout, the trunk, is the executive one: your milestones, your direct edits (e.g. AGENTS.md), exec tasks (reports included) and pm's planning and cleanup tasks run there. Every other task runs in one of the git work trees named in `[worktrees]` in the config (`name = "what belongs there"`, e.g. `app` and `questions` for a trivia project); tasks of different work trees run in parallel. pm errors out while `[worktrees]` is empty.

- Assignment: `pm task add` needs `--worktree <name>` unless `--exec`; workers pick it by the `[worktrees]` descriptions (their prompt lists them); `pm task set --worktree`, the dashboard's task page and add form: a dropdown; investigations get the failed task's; pm never guesses
- Priority orders tasks only within one work tree: tasks of different work trees run side by side, whatever their priorities
- Old tasks without a work tree: with one `[worktrees]` entry, `pm run` puts them there; with several, it lists them and stops until you `pm task set` them
- Scheduling: `pm run` starts the first queued task of the trunk and of every work tree that runs nothing, within the lowest milestone queue index in the queue, up to `max_parallel` (default 4; 1: one at a time); within one work tree, the queue order holds
- Work tree `questions`: kept at `worktree_dir`/questions (default `../<repo>-worktrees`, outside the project) on branch `pm/wt/questions`, made on first use; before each task pm merges the trunk's HEAD into it; the worker runs there; pm commits what it staged (`task <id>: <subject>`)
- Merge into the trunk: one at a time, by `pm run` itself, so no contention; only while no trunk task runs and nothing is staged in the trunk (git refuses a merge over staged changes; reported once); `pm: merge work tree questions: task N`, `--no-ff`, with `tasks.sql`
- A conflict, either way: the merge is aborted, so the trunk is never half-merged; pm queues a `merge` task in that work tree (the plan of its last task, priority + 2, `merge_prompt`): its worker merges the trunk in and resolves; the work tree runs nothing else until that is done, then merges cleanly
- A trunk task waits while the trunk has uncommitted changes (yours); work trees go on
- `tasks.sql`: one, the trunk's; workers in a work tree use it through `PM_ROOT` (their copy is stale), writes are serialized by pm's lock; committed only in the trunk, so a branch never conflicts on it
- Failed runs, stashes, investigations, leftovers branches work as in the trunk (stashes are shared by all work trees); a crashed `pm run`'s work trees: their changes stashed for the task's next run
- Usage limit: a worker that hits it waits as before; no other task starts until the reset. A Claude error that needs you: no new task starts, `pm run` exits once the running ones end
- Output: lines of a work tree's worker are prefixed `[questions]`; the dashboard stacks the running tasks, each with its work tree

`tasks.sql` is loaded once per process, again only when the file changed. Each commit of it adds a full copy to `.git/objects`; past 50 MB of loose objects, `pm` packs them in the background (`git repack -d`), where a copy is a delta to the one before. Workers don't read it (megabytes of SQL): `pm run` and `pm init` add `/tasks.sql` to `.ignore` (ripgrep and Claude Code's Grep skip it), `{settings}` denies Read of it, and the guard hook blocks Bash commands that read it or search it (`cat`, `grep … tasks.sql`, `grep -r`, `git grep`, `rg -u`) unless they exclude it. Its schema must be pm's (`SCHEMA`; a hand edit or a merge that changes it is refused), and only what a dump holds runs: no `ATTACH`, no `PRAGMA` besides `user_version`.

A failed run: `pm` stashes what the worker left (`tasks.sql` aside; `git stash apply <sha>` gets it back), so the next `pm run` starts clean, and records a summary as the run's reason, shown in the terminal and on the dashboard: exit code or signal, duration, turns, the last tool call, a likely cause, the stash, the log. The next run of the same task gets that summary and the task's stashes in its prompt, with the instruction to inspect them, apply what is still good and recheck what they mark as done; `pm` drops them once the task is done or dropped. A run that exits non-zero after `pm task done`: with everything staged it counts as done; otherwise its work is stashed and the task reopened. Usage limits are looked for only in failed runs.

A failed task doesn't stop `pm run` and isn't retried as it is: its leftovers are in its stash, and pm queues "investigate the failure of task N: …" (kind `investigate`, `investigate_prompt`, the failure's summary below) in its plan, priority one above it. The investigation finds the cause and, by it, adds a fix task between the two (a bug in a script), changes or drops the task, or raises a decision for the human; only then does the task run again, with its stash. A task that fails again after 3 investigations, or a failed investigation, stops `pm run`.

Claude's own errors aren't the task's, so pm handles them itself, from the worker's result ("API Error: …") or, without one, its last log lines:
- usage limit: wait for its reset (plus `limit_margin_minutes`), then the same task again
- passing (overloaded, 5xx, 429, connection error, timeout, network errors, a limit without a readable reset): the same task again, at once, then after 1, 2, 5, 10, 20, 30, then every 60 minutes (`RETRY_MINUTES`)
- needs you (invalid API key, /login, credit balance, unknown model, a worker command that can't start, a failure before any tool call that matches nothing): `pm run` stops, the task stays as it is
- `pm run` also stops on an invalid database, a failed `git commit` or stash, a dirty tree before a task
