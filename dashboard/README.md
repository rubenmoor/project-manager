# pm-dashboard

An optional, local, read-only web view of a pm project: milestones, plans, tasks, the queue, decisions and worker runs, live while `pm run` works. It lives in the pm repository but is a separate package, never installed with `pm`. It imports `pm` to read `tasks.sql`, so both always agree on the schema.

## Run

In the project's git repository (the one with `project-manager.toml`):

- `nix run github:<you>/pm#dashboard`, or `nix run .#dashboard` in this repository
- `pipx install .` (pm) then `pipx inject pm ./dashboard --include-apps`
- from a checkout: `PYTHONPATH=<repo>:<repo>/dashboard python3 -m pm_dashboard`
- options: `--root <repo>`, `--port 8642`; serves `http://127.0.0.1:8642/` only

## Views

- **Overview**: the running task, decisions waiting on you, cost, open milestones with progress, queue head
- **Queue**: as `pm run` sees it; below, open tasks it leaves out and why (pending decision, milestone implemented)
- **Milestones**: plans by priority with their tasks; filters by status and kind
- **Task**: body, note, priority and where it comes from, decisions, rows it created, runs, worker log (live while it runs)
- **Decisions**: pending sign-offs, pending questions, closed; a sign-off shows what it approves
- **Runs**: cost per day, per task kind, per milestone; every run
- Keys: `g o`, `g q`, `g m`, `g d`, `g r`; `/` searches subjects (and bodies), a number finds that id

## How it works

- Reads the working copy of `tasks.sql` (a running worker's changes included) through `pm.Project.connect()`, never writes
- Polls `tasks.sql`, `.git/HEAD`, `.git/index`, `.git/logs/HEAD` every 0.5 s; pushes changes over server-sent events
- The running task: the `run` row with result `running` that `pm run` keeps while a worker works
- Worker log: `.git/pm-logs/task-<id>.log`, shown as `pm run` shows it (`pm.Project.read_event`)
- Unreadable `tasks.sql` (mid-merge, hand edit, other schema version): the error, plus the last state that could be read
- Client: Preact + htm and marked, vendored in `pm_dashboard/static/vendor/`, no build step
- Security: binds 127.0.0.1, checks the Host header, CSP without inline scripts; HTML in Markdown bodies is shown as text

## Code

- `pm_dashboard/__init__.py`: server, watcher, JSON API (`/api/state`, `/api/log/<task>?from=N`, `/api/events`)
- `pm_dashboard/static/`: `app.js` (shell, routing, live state), `views.js` (pages), `ui.js` (components, charts), `data.js` (indexes, what pm derives)
