# pm-dashboard

An optional, local web view and editor for a pm project: milestones, plans, tasks, the queue, decisions and worker runs, live while `pm run` works. It lives in the pm repository but is a separate package, never installed with `pm`. It imports `pm` to read and change `tasks.sql`, so both always agree on the schema and the rules.

## Run

In the project's git repository (the one with `project-manager.toml`):

- `nix run github:<you>/pm#dashboard`, or `nix run .#dashboard` in this repository
- `pipx install .` (pm) then `pipx inject pm ./dashboard --include-apps`
- from a checkout: `PYTHONPATH=<repo>:<repo>/dashboard python3 -m pm_dashboard`
- options: `--root <repo>`, `--port 8642`; serves `http://127.0.0.1:8642/` only

## Views

- **Live** (home while pm runs): left, one timeline: queue above (next nearest), the running task between two rules, done and dropped tasks below (newest first); a click (or `j`/`k`) selects a task (outline); right, the selected task, with a link to its page:
  - running: its output, live
  - open: queue position or what blocks it, body, decisions, earlier runs; after a failed run: pm's summary and the run's output
  - failed last run: a red "failed" pill in the queue; with no worker running, "pm run stopped: task N failed" and the likely cause
  - done or dropped: note, runs, files changed, the output of its last run
- **Overview**: running task, decisions waiting on you, cost, open and on-hold milestones with progress, queue head; without milestones a welcome screen with one button
- **Queue**: as `pm run` sees it; open tasks it leaves out and why
- **Milestones**: cards grouped by queue index (lowest first), with progress and the number of pending decisions its open tasks wait for, linked to exactly those (`#/decisions?milestone=N`)
- **Milestone**, **plans**: plans by priority with their tasks; filters by status and kind; change a milestone's queue index; put a milestone on hold or resume it (its plans and open tasks show "on hold")
- **Task**: body, note, priority, decisions, rows it created, runs, the diff of its commit (syntax highlighted), worker log
- **Decisions**: pending sign-offs and questions by milestone, the ones holding up the most important work first; closed ones: the decision log (as `pm decision list --answered`) or all; tags for one-offs, workers' own choices, superseded ones; a decision's page shows consequences and supersede links
- **Since you looked**: rows changed since the commit you marked as seen (uncommitted ones included): decisions answered, choices workers recorded without asking, plan bodies as diffs, new and closed tasks, runs
- **Runs**: cost per day, per task kind, per milestone; every run
- Status line (bottom): Claude account usage of the 5-hour window and the week (%, reset time), tokens and cost of pm's workers in each window
- Notifications (`notify` in the top bar): a new pending decision, a failed run, `pm run` stopped or done
- Ids: every task, plan and milestone shown with its id first (`t12`, `p3`, `m1`), as workers and Claude refer to them
- Keys: `g` then `l o q m d s r` to go; `/` search (a number finds that id); `i` edit; Esc save

## Editing

Text is edited in place, through pm's own methods; each change is a commit (`pm: … (dashboard)`).

- Subjects and bodies of milestones, plans and tasks: rendered; `i`, Enter or a click shows the Markdown source (CodeMirror, marked "Markdown"); Esc or leaving it saves
- Priorities: click the number; empty clears it
- Tasks: done or drop with a note; add milestones and tasks (plans come from workers)
- After adding a milestone: add another, or start `pm run` in the terminal (the page says whether it runs)
- Edits cascade as in pm: a changed milestone queues "revise the masterplan of milestone N: …"; a changed plan queues "create tasks for plan N: …" and "revise plan M: …" for each plan below it
- Implemented milestones, done plans (no open task, nor in the plans below them) and closed tasks are read-only; pm refuses changes to them
- While a worker runs: the running task can't be changed; other changes are allowed and committed with the running task, not on their own
- pm's checks apply: e.g. an open task needs a body

## How it works

- Reads the working copy of `tasks.sql` (a running worker's changes included) through `pm.Project.connect()`
- Polls `tasks.sql`, `.git/HEAD`, `.git/index`, `.git/logs/HEAD` every 0.5 s; pushes changes over server-sent events; the browser refetches the snapshot
- The running task: the `run` row with result `running` that `pm run` keeps while a worker works
- Between tasks: a running task already done (its worker finishing), then `pm run` committing and picking the next task or waiting for a usage limit: told apart by the run row and whether its `pid` lives; shown with a spinner
- Worker log: `.git/pm-logs/task-<id>-run-<run>.log` (`task-<id>.log`: the latest), shown as `pm run` shows it (`pm.Project.read_event`); `pm run` flushes it per line
- Usage limits: the newest `rate_limit_event` in the worker logs (claude's stream-json); as of the last worker's API call, so stale between runs; usage outside pm counts in the % but not in the token totals
- A task's commit: the one `pm run` made, `task <id>: <subject>`
- Unreadable `tasks.sql` (mid-merge, hand edit): the error, plus the last state that could be read
- Security: binds 127.0.0.1; checks Host (and Origin for writes); writes need a JSON body; CSP without inline scripts; HTML in Markdown is shown as text

## Code

- `pm_dashboard/__init__.py`: server, watcher, JSON API (`/api/state`, `/api/log/<task>?from=N`, `/api/events`, `/api/since?commit=C`, `/api/task/<id>/commit`, `/api/usage`, `POST /api/edit`)
- `pm_dashboard/edit.py`: writes and the run rules; history from git
- `pm_dashboard/static/`: `app.js` (shell, routing, live state), `views.js` (pages), `live.js` (run view), `editor.js` (in-place editing), `add.js` (forms, welcome), `since.js`, `diff.js`, `notify.js`, `usage.js`, `ui.js` (components, charts), `data.js` (indexes, what pm derives)
- `pm_dashboard/static/vendor/`: Preact + htm 3.1.1, marked 12.0.2, diff2html 3.4.48 (slim, with highlight.js), CodeMirror 6 bundled from `vendor-src/codemirror.js`:
  `npm i esbuild codemirror@6.0.1 @codemirror/lang-markdown@6.2.5 && npx esbuild vendor-src/codemirror.js --bundle --format=esm --minify --outfile=…/vendor/codemirror.js`
