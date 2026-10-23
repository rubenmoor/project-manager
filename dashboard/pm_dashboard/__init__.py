"""pm-dashboard: a local web view of a pm project's tasks.sql, and an editor for it (README.md). Optional: not part of the `pm`
package, but it imports it, so both read the database the same way. Standard library only (Python 3.11+).

    pm-dashboard [<repo> …] [--port 8642]

Shows several projects: the repositories given (default: the one around the current directory) and the ones added in
the dashboard, saved in ~/.config/pm-dashboard/projects. Serves the browser client (static/) on 127.0.0.1, a project's
at /p/<name>/, and a JSON API:
    /api/projects        every project: name, root, error, and what notifications and the switcher need
    POST /api/projects   {"op": "add", "path": …} or {"op": "remove", "name": …}
    /api/usage           the account's usage limits, from the newest rate_limit_event in every project's worker logs
    /api/poll?project=<name>  polled by the client: every project's summary, and that project's state version (it
                         changes with its tasks.sql, git or running log) and running logs' sizes
  per project, under /p/<name>/:
    api/state            every table, the queue, git HEAD and changed files; or {"error": …}
    api/log/<task>       the task's latest worker log (?run=R: run R's) as readable lines, from byte ?from=N on
    api/task/<id>/commit the commit pm made for the task, with its patch and the rows it changed
    POST api/edit        {"op": …}: add or change milestones, plans, tasks, resolve decisions, request an
                         executive summary, a resource use report or a custom report through pm (edit.py)
"""
import argparse, datetime, errno, json, mimetypes, os, subprocess, sys, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlsplit

PM_MIN_VERSION = 82     # the pm whose Project this version uses (log_ids, run_pid, run_crash, approve, milestone_hold,
                        # summary_request, exec tasks, open_summary, report_request, report_rows, report_ask, WORKTREE_NAME,
                        # task_requires, shown_queue)
try:
    import pm
except ImportError:
    sys.exit("pm-dashboard: the `pm` package is not importable; put the pm repository on PYTHONPATH or install it")
if getattr(pm, "VERSION", 0) < PM_MIN_VERSION:
    sys.exit(f"pm-dashboard: needs pm {PM_MIN_VERSION} or newer, but imported pm {getattr(pm, 'VERSION', '?')} from "
             f"{pm.__file__}: an older pm on PYTHONPATH? (`echo $PYTHONPATH`)")
from pm_dashboard import edit

STATIC = Path(__file__).parent / "static"
TABLES = ("milestone", "plan", "task", "decision", "task_decision", "task_prerequisite_tasks", "decision_supersedes", "run", "summary",
          "resource_use_report", "custom_report")
POLL = 0.5
DIRTY_POLL = 5
CSP = ("default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; "
       "base-uri 'none'; form-action 'none'; frame-ancestors 'none'")


class Watcher:
    """One project: loads it (again after an error), polls tasks.sql, git and the running worker's log; wakes the event
    streams on a change."""

    def __init__(self, name, root, cond, saved):
        self.name, self.root, self.saved = name, root, saved
        self.project = None
        self.cond = cond                        # shared by every project
        self.stopped = False
        self.summary = {}
        self.version = 0
        self.log = None                         # ((task id, size), …) of the running workers' logs
        self.state = None
        self.last_good = None
        self.sig = None
        self.dirty, self.dirty_at = None, 0.0
        self.alive = None                       # the pid of `pm run`, while it lives

    def signature(self):
        git = self.project.git_dir
        out = []
        for p in (self.project.db_path, git / "HEAD", git / "index", git / "logs" / "HEAD"):
            try:
                s = p.stat()
                out.append((s.st_mtime_ns, s.st_size))
            except FileNotFoundError:
                out.append(None)
        return out

    def running_log(self, state):
        """((task id, log size), …) of the running workers: one per work tree."""
        runs = (state or {}).get("tables", {}).get("run", [])
        out = []
        for r in runs:
            if r["result"] == "running":
                try:
                    out.append((r["task_id"], (self.project.logs / f"task-{r['task_id']}.log").stat().st_size))
                except FileNotFoundError:
                    out.append((r["task_id"], 0))
        return tuple(out) or None

    def load(self):
        try:
            self.project = pm.Project(self.root, pm.load_config(self.root))
        except (pm.FormatError, OSError) as e:
            if self.state != (state := {"error": f"{self.root}: {e}", "root": self.root, "last_good": None}):
                self.state, self.summary = state, {"error": state["error"]}
                with self.cond:
                    self.version += 1
                    self.cond.notify_all()

    def loop(self):
        while not self.stopped:
            if not self.project:
                self.load()
                if not self.project:
                    time.sleep(DIRTY_POLL)
                    continue
            try:
                if time.monotonic() - self.dirty_at > DIRTY_POLL:    # edits in the trunk change no git file
                    self.dirty_at, self.dirty = time.monotonic(), self.project.git("status", "--porcelain")
                sig = self.signature() + [self.dirty]
                changed = sig != self.sig
                state = read_state(self.project, self.last_good) if changed else self.state
                log, alive = self.running_log(state), self.project.run_pid()
                if changed or log != self.log or alive != self.alive:
                    state = state | {"pm_run": alive, "pm_crash": None if alive else self.project.run_crash()}  # a new dict
                    if "error" not in state:
                        self.last_good = state
                    self.state, self.sig, self.log, self.alive = state, sig, log, alive
                    self.summary = summary(state)
                    with self.cond:
                        self.version += 1
                        self.cond.notify_all()
            except Exception as e:  # keep watching whatever happens; the error shows in the client
                self.state = {"error": f"pm-dashboard: {e}", "root": self.root, "last_good": self.last_good}
                self.sig = None                         # read again next time
            time.sleep(POLL)


def summary(state):
    """What the project switcher and notifications need of a project's state."""
    good = state if "tables" in state else state.get("last_good") or {}
    t = good.get("tables") or {}
    running = [r["task_id"] for r in t.get("run", ()) if r["result"] == "running"] if state.get("pm_run") else []
    return {"error": state.get("error"),
            "running": running,
            "pending": [{"id": d["id"], "subject": d["subject"], "signoff": bool(d.get("signoff"))}
                        for d in t.get("decision", ()) if d["status"] == "pending"],
            "failed": [{"id": r["id"], "task": r["task_id"], "reason": (r.get("reason") or "").split("\n")[0]}
                       for r in t.get("run", ()) if r["result"] == "failed"][-10:],
            "crash": state.get("pm_crash"),
            "queue": len(good.get("queue") or ())}


class Projects:
    """The projects shown, in order: the command line's, then the saved ones (added in the dashboard)."""

    def __init__(self, path):
        self.path = path
        self.cond = threading.Condition()
        self.watchers = {}
        self.version = 0                        # the list changed
        self.lock = threading.Lock()

    def saved(self):
        try:
            return [l.strip() for l in self.path.read_text(encoding="utf-8").splitlines() if l.strip()]
        except FileNotFoundError:
            return []

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text("".join(f"{w.root}\n" for w in self.watchers.values() if w.saved), encoding="utf-8")
        os.replace(tmp, self.path)

    def add(self, path, saved):
        """Watch the git repository at or around path; its name, or a ValueError."""
        p = Path(path).expanduser()
        if not p.is_absolute():
            raise ValueError("give an absolute path")
        if not p.is_dir():
            raise ValueError(f"no such folder: {p}")
        r = subprocess.run(["git", "-C", str(p), "rev-parse", "--show-toplevel"], capture_output=True, text=True)
        if r.returncode:
            raise ValueError(f"not in a git repository: {p}")
        root = str(Path(r.stdout.strip()).resolve())
        with self.lock:
            for w in self.watchers.values():
                if w.root == root:
                    if saved and not w.saved:
                        w.saved = True
                        self.save()
                    return w.name
            if saved and not (Path(root) / pm.CONFIG_FILE).exists():
                raise ValueError(f"no {pm.CONFIG_FILE} in {root}: run `pm init` there first")
            base = Path(root).name
            name = next(n for n in (base, f"{Path(root).parent.name}-{base}", *(f"{base}-{i}" for i in range(2, 1000)))
                        if n not in self.watchers)
            w = self.watchers[name] = Watcher(name, root, self.cond, saved)
            if saved:
                self.save()
            self.changed()
        threading.Thread(target=w.loop, daemon=True).start()
        return name

    def remove(self, name):
        with self.lock:
            w = self.watchers.pop(name, None)
            if not w:
                raise ValueError(f"no project {name!r}")
            w.stopped = True
            if w.saved:
                self.save()
            self.changed()

    def changed(self):
        with self.cond:
            self.version += 1
            self.cond.notify_all()

    def list(self):
        return [{"name": w.name, "root": w.root, "saved": w.saved, **w.summary} for w in list(self.watchers.values())]


def read_state(project, last_good=None):
    """Everything the client shows, from the working copy of tasks.sql (a running worker's changes included)."""
    head = git_head(project)
    dirty = [l[3:] for l in project.git("status", "--porcelain").splitlines()]
    base = {"root": str(project.root), "db": project.db, "head": head, "dirty": dirty, "read_at": pm.now(),
            "schema_version": pm.SCHEMA_VERSION, "planning_priority": project.config.get("planning_priority"),
            "handoff_context": pm.HANDOFF_CONTEXT, "models": project.config.get("models"),
            "worktrees": project.config.get("worktrees") or {}, "holds": {}}
    if not project.db_path.exists():               # written by the first pm command, e.g. adding a milestone
        return base | {"tables": {t: [] for t in TABLES}, "queue": [], "shown_queue": [], "head_signoff": None, "done_plans": [], "errors": []}
    try:
        with project.connect() as db:
            tables = {t: [dict(r) for r in db.execute(f"SELECT * FROM {t} ORDER BY rowid")] for t in TABLES}
            queue = [r["task_id"] for r in db.execute("SELECT task_id FROM queue")]
            in_log = project.log_ids(db)
        shown_queue = [r[1] for r in project.shown_queue()]
        plan_prio = {p["id"]: p["priority"] for p in tables["plan"]}
        picks = project.picks_model()
        for t in tables["task"]:
            t["effective_priority"] = t["priority"] if t["priority"] is not None else plan_prio.get(t["plan_id"]) or 0
            if picks and t["status"] == "open":
                t["model_pick"] = project.model_for(t["id"])    # [model, "set" | "escalated" | "kind"]
        for d in tables["decision"]:
            d["options"] = json.loads(d["options"])
            d["in_log"] = d["id"] in in_log
            if d.get("signoff") and d["status"] == "pending":
                d["signoff_view"] = project.signoff_view(d["id"], d["signoff"])
        head_signoff = project.needs_signoff(queue[0]) if queue else None
        done_plans = project.done_plans()
        open_summary, open_report = project.open_summary(), project.open_report()
        for r in tables["resource_use_report"]:
            r["categories"] = project.report_rows(r["id"])
        errors = project.validate()
        holds = project.planning_holds(project.queue())
    except pm.FormatError as e:
        return base | {"error": str(e), "last_good": last_good}
    return base | {"tables": tables, "queue": queue, "shown_queue": shown_queue, "head_signoff": head_signoff, "done_plans": done_plans,
                   "errors": errors, "open_summary": open_summary,
                   "open_report": open_report, "holds": holds}


def usage(projects):
    """The last rate_limit_event claude's stream-json wrote to a worker log of any project: how much of the 5-hour and
    the weekly limit the account had used then. Only `pm run` workers write logs, so it is as of their last API call."""
    logs = set()
    for p in projects:
        try:
            logs |= {f.resolve() for f in p.logs.glob("task-*.log")}
        except OSError:
            pass
    week = time.time() - 8 * 86400
    for f in sorted((f for f in logs if f.is_file()), key=lambda f: f.stat().st_mtime, reverse=True):
        st = f.stat()
        if st.st_mtime < week:
            break
        with open(f, "rb") as fh:
            fh.seek(max(0, st.st_size - (1 << 20)))
            tail = fh.read().decode("utf-8", "replace").splitlines()
        for line in reversed(tail):
            if '"rate_limit_event"' not in line:
                continue
            try:
                info = json.loads(pm.unstamp(line)[1])["rate_limit_info"]
            except (ValueError, KeyError, TypeError):
                continue
            windows = info.get("unifiedWindows") or {}
            if not windows and info.get("rateLimitType"):
                windows = {info["rateLimitType"]: {"utilization": info.get("utilization"), "resetsAt": info.get("resetsAt")}}
            return {"event": {"status": info.get("status"), "windows": windows, "overage": info.get("isUsingOverage"),
                              "at": datetime.datetime.fromtimestamp(st.st_mtime, datetime.UTC).isoformat()},
                    "burn": burn(logs)}
    return {"event": None}


def burn(logs, window=3600):
    """How fast the weekly limit filled over the last hour, from the rate_limit_events in the worker logs: the rise of
    its utilization since the last event at or before an hour ago (else the first one since), per hour up to now.
    None while fewer than 15 minutes are covered. Sustainable is 1 / 168 per hour."""
    now = time.time()
    points = []
    for f in logs:
        try:
            st = f.stat()
            if st.st_mtime < now - 3 * window:
                continue
            with open(f, "rb") as fh:
                fh.seek(max(0, st.st_size - (8 << 20)))
                tail = fh.read().decode("utf-8", "replace").splitlines()
        except OSError:
            continue
        for line in tail:
            if '"rate_limit_event"' not in line:
                continue
            at, rest = pm.unstamp(line)
            try:
                w = (json.loads(rest)["rate_limit_info"].get("unifiedWindows") or {})["seven_day"]
                t = datetime.datetime.strptime(at, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=datetime.UTC).timestamp()
                points.append((t, float(w["utilization"]), w.get("resetsAt")))
            except (ValueError, KeyError, TypeError):
                continue
    points.sort()
    if not points or points[-1][0] < now - window:
        return {"rate": 0.0, "since": None}
    points = [p for p in points if p[2] == points[-1][2]]       # not across a weekly reset
    before = [p for p in points if p[0] <= now - window]
    base = before[-1] if before else points[0]
    if now - base[0] < window / 4:
        return {"rate": None, "since": base[0]}
    return {"rate": (points[-1][1] - base[1]) * 3600 / (now - base[0]), "since": base[0]}


def git_head(project):
    try:
        commit, date, subject = project.git("log", "-1", "--format=%H%x00%cI%x00%s").strip().split("\0")
        branch = project.git("rev-parse", "--abbrev-ref", "HEAD").strip()
        return {"commit": commit, "date": date, "subject": subject, "branch": branch}
    except (subprocess.CalledProcessError, ValueError):
        return None


def read_log(project, tid, start=0, rid=None):
    """The worker log of task tid (its latest run, or run rid) from byte `start`, as pm shows it in the terminal:
    ([[UTC timestamp or None, line]], end offset), the timestamp on an event's first line only. Logs from before per-run
    logs: task-<id>.log is a file, not a link."""
    path = project.logs / f"task-{tid}.log"
    if rid is not None and (project.logs / f"task-{tid}-run-{rid}.log").exists():
        path = project.logs / f"task-{tid}-run-{rid}.log"
    if not path.exists():
        return [], 0
    with open(path, "rb") as f:
        f.seek(start)
        data = f.read()
    cut = data.rfind(b"\n") + 1                # whole lines only; the rest comes next time
    lines = []
    info = {}
    for raw in data[:cut].decode("utf-8", errors="replace").splitlines(keepends=True):
        shown = pm.Project.read_event(raw, info)
        if shown:
            stamp = pm.unstamp(raw)[0]
            lines += [[stamp if i == 0 else None, l] for i, l in enumerate(shown.rstrip("\n").split("\n"))]
    return lines, start + cut


class Handler(BaseHTTPRequestHandler):
    server_version = "pm-dashboard"
    projects: Projects = None
    allowed_hosts = ()
    write_lock = threading.Lock()

    def log_message(self, fmt, *args):
        pass

    def send(self, code, body, ctype="application/json; charset=utf-8", cache=False, headers=()):
        if not isinstance(body, bytes):
            body = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Content-Security-Policy", CSP)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", "max-age=3600" if cache else "no-store")
        for k, v in headers:
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def split(self, path):
        """/p/<name>/<rest> → (its Watcher or None, "/<rest>"); other paths → (None, path)."""
        if not path.startswith("/p/"):
            return None, path
        name, _, rest = path[3:].partition("/")
        return self.projects.watchers.get(unquote(name)), "/" + rest

    def do_POST(self):
        host = self.headers.get("Host")
        if host not in self.allowed_hosts or self.headers.get("Origin") not in (f"http://{host}", f"https://{host}"):
            return self.send(403, {"error": "unknown host or origin"})
        w, path = self.split(self.path)
        if path not in ("/api/edit", "/api/projects") or (path == "/api/edit") != bool(w) \
                or not (self.headers.get("Content-Type") or "").startswith("application/json"):
            return self.send(404, {"error": "not found"})
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = -1
        if not 0 <= n <= 1_000_000:
            return self.send(413 if n > 0 else 400, {"error": "bad or too large Content-Length"})
        try:
            op = json.loads(self.rfile.read(n))
            if not isinstance(op, dict):
                raise ValueError("not an object")
        except ValueError as e:
            return self.send(400, {"error": f"bad JSON: {e}"})
        if path == "/api/projects":
            try:
                if op.get("op") == "add" and isinstance(op.get("path"), str):
                    return self.send(200, {"name": self.projects.add(op["path"], saved=True)})
                if op.get("op") == "remove" and isinstance(op.get("name"), str):
                    self.projects.remove(op["name"])
                    return self.send(200, {})
                return self.send(400, {"error": "unknown op"})
            except (ValueError, OSError) as e:
                return self.send(400, {"error": str(e)})
        if not w.project:
            return self.send(400, {"error": w.state["error"]})
        with self.write_lock:
            try:
                result = edit.edit(w.project, op)
            except edit.EditError as e:
                return self.send(400, {"error": str(e)})
        self.send(200, result)

    def do_GET(self):
        if self.headers.get("Host") not in self.allowed_hosts:  # DNS rebinding
            return self.send(403, {"error": "unknown host"})
        url = urlsplit(self.path)
        query = parse_qs(url.query)
        w, path = self.split(url.path)
        if url.path == "/api/projects":
            return self.send(200, self.projects.list())
        if url.path == "/api/usage":
            return self.send(200, usage([x.project for x in list(self.projects.watchers.values()) if x.project]))
        if url.path == "/api/poll":
            return self.poll(self.projects.watchers.get(query.get("project", [""])[0]))
        if url.path == "/":
            first = next(iter(self.projects.watchers), None)
            if first:
                return self.send(302, b"", "text/plain", headers=[("Location", f"/p/{quote(first)}/")])
            path = "/index.html"                # no project yet: the client offers to add one
        elif url.path.startswith("/p/"):
            if not w:
                return self.send(302, b"", "text/plain", headers=[("Location", "/")])
            if path == "/" and not url.path.endswith("/"):
                return self.send(302, b"", "text/plain", headers=[("Location", url.path + "/")])
            if path == "/api/state":
                return self.send(200, w.state)
            if path.startswith("/api/") and not w.project:
                return self.send(400, {"error": w.state["error"]})
            if path.startswith("/api/task/") and path.endswith("/commit"):
                try:
                    tid = int(path.split("/")[3])
                except ValueError:
                    return self.send(400, {"error": "bad task id"})
                return self.send(200, edit.task_commit(w.project, tid))
            if path.startswith("/api/log/"):
                try:
                    tid, start = int(path.rsplit("/", 1)[1]), int(query.get("from", ["0"])[0])
                    rid = int(query["run"][0]) if "run" in query else None
                except ValueError:
                    return self.send(400, {"error": "bad task id, offset or run"})
                lines, end = read_log(w.project, tid, start, rid)
                return self.send(200, {"task": tid, "lines": lines, "next": end})
            if path != "/":
                return self.send(404, {"error": "not found"})
            path = "/index.html"
        f = (STATIC / path.lstrip("/")).resolve()
        if STATIC.resolve() not in f.parents or not f.is_file():
            return self.send(404, {"error": "not found"})
        ctype = mimetypes.guess_type(f.name)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype.endswith("javascript"):
            ctype += "; charset=utf-8"
        self.send(200, f.read_bytes(), ctype, cache="/vendor/" in path)

    def poll(self, w):
        """Polled by every tab about once a second (a held connection per tab, like server-sent events, runs into the
        browser's ~6 connections per host): the projects' summaries, project w's state version and running logs."""
        ps = self.projects
        msg = {"pm": pm.VERSION, "projects": ps.list()}
        if w:
            msg |= {"version": w.version, "log": [{"task": t, "size": n} for t, n in w.log or ()]}
        self.send(200, msg)


def config_dir():
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "pm-dashboard"


def main(argv=None):
    ap = argparse.ArgumentParser(prog="pm-dashboard", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("roots", nargs="*", metavar="repo",
                    help="projects' git repositories (default: the one around the current directory), shown with the saved ones")
    ap.add_argument("--root", action="append", default=[], help=argparse.SUPPRESS)     # older form of `repo`
    ap.add_argument("--port", type=int, default=8642)
    ap.add_argument("--allowed-host", action="append", default=[], metavar="HOST",
                    help="also accept this Host header, e.g. project-manager-dashboard.localhost (repeatable)")
    args = ap.parse_args(argv)
    projects = Projects(config_dir() / "projects")
    roots = args.roots + args.root
    if not roots and (here := pm.git_root()):
        roots = [here]
    for root in roots:
        try:
            projects.add(str(Path(root).resolve()), saved=False)
        except ValueError as e:
            print(f"pm-dashboard: {e}")
            return 1
    for root in projects.saved():
        try:
            projects.add(root, saved=True)
        except ValueError as e:
            print(f"pm-dashboard: {projects.path}: {e}")
    Handler.projects = projects
    server = None
    for port in range(args.port, args.port + 20):
        try:
            server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
            break
        except OSError as e:
            if e.errno != errno.EADDRINUSE:
                print(f"pm-dashboard: port {port}: {e.strerror}")
                return 1
    if not server:
        print(f"pm-dashboard: ports {args.port}-{port} are all in use")
        return 1
    Handler.allowed_hosts = {f"{h}:{port}" for h in ("127.0.0.1", "localhost")} \
        | set(args.allowed_host) | {f"{h}:{port}" for h in args.allowed_host if ":" not in h}
    server.daemon_threads = True
    for w in list(projects.watchers.values()):
        print(f"pm-dashboard: {w.root} on http://127.0.0.1:{port}/p/{quote(w.name)}/", flush=True)
    if not projects.watchers:
        print(f"pm-dashboard: no project yet; add one on http://127.0.0.1:{port}/", flush=True)
    print("pm-dashboard: Ctrl-C stops", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0
