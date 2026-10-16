# pm-dashboard

An optional, local web view and editor for a pm project: milestones, plans, tasks, the queue, decisions and worker runs, live while `pm run` works. It lives in the pm repository but is a separate package, never installed with `pm`. It imports `pm` to read and change `tasks.sql`, so both always agree on the schema and the rules.

## Run

In the project's git repository (the one with `project-manager.toml`):

- `nix run github:<you>/pm#dashboard`, or `nix run .#dashboard` in this repository
- `pipx install .` (pm) then `pipx inject pm ./dashboard --include-apps`
- from a checkout: `PYTHONPATH=<repo>:<repo>/dashboard python3 -m pm_dashboard`
- options: `--root <repo>`, `--port 8642`; serves `http://127.0.0.1:8642/` only; port taken: tries the next, up to 20, prints the URL used

## Views

- **Live** (home while pm runs): left, one timeline: queue above (next nearest), the running task between two rules, done and dropped tasks below (newest first); a click selects a task (outline); right, the selected task's task page (below), with a link to it
  - running task (or what pm run is doing) pinned in the middle; queue and done/dropped scroll on their own; queue opens at "next"
  - "blocked by decisions" (queue header, off by default, remembered in the browser): open tasks waiting for pending decisions, in purple, where they would queue once unblocked, with the decisions they wait for
  - failed last run: a red "failed" pill in the queue; with no worker running, "pm run stopped: task N failed" and the likely cause
  - crashed `pm run` (e.g. disk full): "pm not running", the task it was on and its error (also on the overview, and a notification); its stale `running` run shows as "crashed"
- **Overview**: running task, decisions waiting on you, cost, open and on-hold milestones with progress, queue head; without milestones a welcome screen with one button
- **Queue**: as `pm run` sees it; open tasks it leaves out and why
- **Tasks**: every task. On top: pending decisions by how many open tasks answering each frees (it is their only blocker), the top one highlighted, each listing its waiting tasks; then executive interventions (exec tasks, no plan; add one there); then by milestone (queue index) and plan, done plans folded; open, closed or all
- **Milestones**: open and on-hold cards grouped by queue index (lowest first), completed ones in their own column on the right (newest first); with progress and the number of pending decisions its open tasks wait for, linked to exactly those (`#/decisions?milestone=N`)
- **Milestone**, **plans**: plans by priority with their tasks; filters by status and kind; change a milestone's queue index; put a milestone on hold or resume it (its plans and open tasks show "on hold")
- **Task**: queue position or what blocks it, note, priority, decisions; after a failed run: pm's summary; the body last, at the bottom
  - done or dropped: rows its commit touched (Affected: tasks created first; tasks, plans, decisions, milestones: new, edited, done, dropped, answered, waits for …; each styled by kind; says why when empty), files changed, the full diff (syntax highlighted)
  - runs, worker log (live while it runs, following its end)
  - worker log always shown: a box 20 lines high, opened at its end, following it while scrolled there; scroll up for earlier lines (`?log` scrolls to it)
  - diff starts hidden; "show" shows it in full
- **Decisions**: pending sign-offs and questions by milestone, the ones holding up the most important work first; closed ones: the decision log (as `pm decision list --answered`) or all; tags for one-offs, workers' own choices, superseded ones; a decision's page shows consequences and supersede links; a pending one is resolved there (below)
- **Reports**: on top a button per kind, `pm summary request` and `pm report request` (exec tasks, priority 10000); while one is open its button is disabled and links to it; then the custom report form, `pm report ask` (open questions listed with their task); below every report of every kind, newest first
  - custom report: the question quoted, then the one-paragraph answer
  - executive summary: reads the previous one and reports the delta
  - resource use report: per category (criteria below its name) tasks, time, tokens, cost and shares, bars by time
- **Runs**: cost per day, per task kind, per milestone; done and dropped tasks per cost bracket (total over their runs); every run
- Status line (bottom): Claude account usage of the 5-hour window and the week (%, reset time), tokens and cost of pm's workers in each window; a marker in each bar shows how much of the window has passed: bar green while usage is behind it (budget left at the reset), orange once ahead (runs out before the reset)
- Milestone implemented: a message with fireworks (no sound), shown once the tab is visible again; stays until "Yay!"; remembered per project in the browser
- Notifications (`notify` in the top bar): a new pending decision, a failed run, `pm run` stopped or done
- Ids: every task, plan and milestone shown with its id first (`t12`, `p3`, `m1`), as workers and Claude refer to them
- Search: a number finds that id

## Editing

Text is edited in place, through pm's own methods; each change is a commit (`pm: … (dashboard)`).

- Subjects and bodies of milestones, plans and tasks: rendered; Enter or a click shows the Markdown source (CodeMirror, marked "Markdown"); Esc or leaving it saves
- Priorities: click the number; empty clears it
- Tasks: done or drop with a note; add milestones (with a queue index, default 0) and tasks (plans come from workers)
- Pending decisions, on their page, as `pm decision answer|approve|irrelevant`: pick an option or write the answer (Markdown, edited like a body; Esc renders it), one-off, supersedes; Answer or Ctrl-Enter. Sign-offs: Approve. "& next" (Ctrl-Shift-Enter): then the next pending decision in the Decisions list order, or the list when none is left. Irrelevant: a reason; drops the open tasks waiting for it. Drafts survive navigating away (session storage)
- After adding a milestone: add another, or start `pm run` in the terminal (the page says whether it runs)
- Edits cascade as in pm: a changed milestone queues "revise the masterplan of milestone N: …"; a changed plan queues "create tasks for plan N: …" and "revise plan M: …" for each plan below it
- Implemented milestones, done plans (no open task, nor in the plans below them) and closed tasks are read-only; pm refuses changes to them
- While a worker runs: the running task can't be changed; other changes are allowed and committed with the running task, not on their own (pm's rules)
- A text edit is refused if the row changed since editing started (a worker or another window): copy your text, then edit the new version
- pm's checks apply: e.g. an open task needs a body

## How it works

- Reads the working copy of `tasks.sql` (a running worker's changes included) through `pm.Project.connect()`
- Polls `tasks.sql`, `.git/HEAD`, `.git/index`, `.git/logs/HEAD` every 0.5 s; pushes changes over server-sent events; the browser refetches the snapshot
- The running task: the `run` row with result `running` that `pm run` keeps while a worker works, and only while `pm run` holds its lock; else it is stale and the error comes from `pm.Project.run_crash`
- Between tasks: a running task already done (its worker finishing), then `pm run` committing and picking the next task or waiting for a usage limit: told apart by the run row and `pm run`'s lock (`pm.Project.run_pid`); shown with a spinner; `pm run` alive with an empty queue: "pm run waits for tasks"
- Worker log: `.git/pm-logs/task-<id>-run-<run>.log` (`task-<id>.log`: the latest), shown as `pm run` shows it (`pm.Project.read_event`); `pm run` flushes it per line
- Usage limits: the newest `rate_limit_event` in the worker logs (claude's stream-json); as of the last worker's API call, so stale between runs; usage outside pm counts in the % but not in the token totals
- A task's commit: the one `pm run` made, `task <id>: <subject>`
- Unreadable `tasks.sql` (mid-merge, hand edit): the error, plus the last state that could be read
- Security: binds 127.0.0.1; checks Host (and Origin for writes); writes need a JSON body; CSP without inline scripts; HTML in Markdown is shown as text

## Code

- `pm_dashboard/__init__.py`: server, watcher, JSON API (`/api/state`, `/api/log/<task>?from=N`, `/api/events`, `/api/task/<id>/commit`, `/api/usage`, `POST /api/edit`)
- `pm_dashboard/edit.py`: writes and the run rules; history from git
- `pm_dashboard/static/`: `app.js` (shell, routing, live state), `views.js` (pages), `live.js` (run view), `editor.js` (in-place editing), `add.js` (forms, welcome), `affected.js` (rows a task's commit touched), `tasks.js`, `reports.js`, `diff.js`, `notify.js`, `usage.js`, `ui.js` (components, charts), `data.js` (indexes, what pm derives)
- `pm_dashboard/static/vendor/`: Preact + htm 3.1.1, marked 12.0.2, diff2html 3.4.48 (slim, with highlight.js), CodeMirror 6 bundled from `vendor-src/codemirror.js`:
  `npm i esbuild codemirror@6.0.1 @codemirror/lang-markdown@6.2.5 && npx esbuild vendor-src/codemirror.js --bundle --format=esm --minify --outfile=…/vendor/codemirror.js`
