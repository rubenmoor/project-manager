"""pm-dashboard: a local web view of a pm project's tasks.sql, and an editor for it (README.md). Optional: not part of the `pm`
package, but it imports it, so both read the database the same way. Standard library only (Python 3.11+).

    pm-dashboard [--root <repo>] [--port 8642]

Serves the browser client (static/) and a JSON API on 127.0.0.1:
    /api/state           every table, the queue, git HEAD and changed files; or {"error": …}
    /api/log/<task>      the task's latest worker log (?run=R: run R's) as readable lines, from byte ?from=N on
    /api/events          server-sent events: "state" when tasks.sql or git changes, "log" when the running log grows
    /api/since?commit=C  rows changed between commit C and the working copy
    /api/task/<id>/commit  the commit pm made for the task, with its patch
    POST /api/edit       {"op": …}: add or change milestones, plans, tasks through pm (edit.py)
"""
import argparse, json, mimetypes, os, subprocess, sys, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

try:
    import pm
except ImportError:
    sys.exit("pm-dashboard: the `pm` package is not importable; put the pm repository on PYTHONPATH or install it")
from pm_dashboard import edit

STATIC = Path(__file__).parent / "static"
TABLES = ("milestone", "plan", "task", "decision", "task_decision", "run")
POLL = 0.5
CSP = ("default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; "
       "base-uri 'none'; form-action 'none'; frame-ancestors 'none'")


class Watcher:
    """Polls tasks.sql, git and the running worker's log; wakes the event streams on a change."""

    def __init__(self, project):
        self.project = project
        self.cond = threading.Condition()
        self.version = 0
        self.log = None                         # (task id, size) of the running worker's log
        self.state = None
        self.last_good = None
        self.sig = None

    def signature(self):
        git = Path(self.project.git("rev-parse", "--absolute-git-dir").strip())
        out = []
        for p in (self.project.db_path, git / "HEAD", git / "index", git / "logs" / "HEAD"):
            try:
                s = p.stat()
                out.append((s.st_mtime_ns, s.st_size))
            except FileNotFoundError:
                out.append(None)
        return out

    def running_log(self):
        runs = (self.state or {}).get("tables", {}).get("run", [])
        r = next((r for r in runs if r["result"] == "running"), None)
        if r is None:
            return None
        try:
            return r["task_id"], (self.project.logs / f"task-{r['task_id']}.log").stat().st_size
        except FileNotFoundError:
            return r["task_id"], 0

    def loop(self):
        while True:
            try:
                sig = self.signature()
                changed = sig != self.sig
                if changed:
                    self.sig = sig
                    self.state = read_state(self.project, self.last_good)
                    if "error" not in self.state:
                        self.last_good = self.state
                log = self.running_log()
                if changed or log != self.log:
                    self.log = log
                    with self.cond:
                        self.version += 1
                        self.cond.notify_all()
            except Exception as e:  # keep watching whatever happens; the error shows in the client
                self.state = {"error": f"pm-dashboard: {e}", "last_good": self.last_good}
            time.sleep(POLL)


def read_state(project, last_good=None):
    """Everything the client shows, from the working copy of tasks.sql (a running worker's changes included)."""
    head = git_head(project)
    dirty = [l[3:] for l in project.git("status", "--porcelain").splitlines()]
    base = {"root": str(project.root), "db": project.db, "head": head, "dirty": dirty, "read_at": pm.now(),
            "schema_version": pm.SCHEMA_VERSION, "planning_priority": project.config.get("planning_priority")}
    if not project.db_path.exists():               # written by the first pm command, e.g. adding a milestone
        return base | {"tables": {t: [] for t in TABLES}, "queue": [], "head_signoff": None, "errors": []}
    try:
        with project.connect() as db:
            tables = {t: [dict(r) for r in db.execute(f"SELECT * FROM {t} ORDER BY rowid")] for t in TABLES}
            queue = [r["task_id"] for r in db.execute("SELECT task_id FROM queue")]
        for d in tables["decision"]:
            d["options"] = json.loads(d["options"])
            if d.get("signoff") and d["status"] == "pending":
                d["signoff_view"] = project.signoff_view(d["id"], d["signoff"])
        head_signoff = project.needs_signoff(queue[0]) if queue and hasattr(project, "needs_signoff") else None
        errors = project.validate()
    except pm.FormatError as e:
        return base | {"error": str(e), "last_good": last_good}
    return base | {"tables": tables, "queue": queue, "head_signoff": head_signoff, "errors": errors}


def git_head(project):
    try:
        commit, date, subject = project.git("log", "-1", "--format=%H%x00%cI%x00%s").strip().split("\0")
        branch = project.git("rev-parse", "--abbrev-ref", "HEAD").strip()
        return {"commit": commit, "date": date, "subject": subject, "branch": branch}
    except (subprocess.CalledProcessError, ValueError):
        return None


def read_log(project, tid, start=0, rid=None):
    """The worker log of task tid (its latest run, or run rid) from byte `start`, as pm shows it in the terminal:
    (lines, end offset). Logs from before per-run logs: task-<id>.log is a file, not a link."""
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
            lines += shown.rstrip("\n").split("\n")
    return lines, start + cut


class Handler(BaseHTTPRequestHandler):
    server_version = "pm-dashboard"
    watcher: Watcher = None
    allowed_hosts = ()
    write_lock = threading.Lock()

    def log_message(self, fmt, *args):
        pass

    def send(self, code, body, ctype="application/json; charset=utf-8", cache=False):
        if not isinstance(body, bytes):
            body = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Content-Security-Policy", CSP)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", "max-age=3600" if cache else "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        host = self.headers.get("Host")
        if host not in self.allowed_hosts or self.headers.get("Origin") != f"http://{host}":
            return self.send(403, {"error": "unknown host or origin"})
        if self.path != "/api/edit" or not (self.headers.get("Content-Type") or "").startswith("application/json"):
            return self.send(404, {"error": "not found"})
        n = int(self.headers.get("Content-Length") or 0)
        if n > 1_000_000:
            return self.send(413, {"error": "too large"})
        try:
            op = json.loads(self.rfile.read(n))
            if not isinstance(op, dict):
                raise ValueError("not an object")
        except ValueError as e:
            return self.send(400, {"error": f"bad JSON: {e}"})
        with self.write_lock:
            try:
                result = edit.edit(self.watcher.project, op)
            except edit.EditError as e:
                return self.send(400, {"error": str(e)})
        self.send(200, result)

    def do_GET(self):
        if self.headers.get("Host") not in self.allowed_hosts:  # DNS rebinding
            return self.send(403, {"error": "unknown host"})
        url = urlsplit(self.path)
        path, query = url.path, parse_qs(url.query)
        w = self.watcher
        if path == "/api/state":
            return self.send(200, w.state)
        if path == "/api/since":
            commit = query.get("commit", [""])[0]
            if not commit.isalnum():
                return self.send(400, {"error": "bad commit"})
            return self.send(200, edit.since(w.project, commit))
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
        if path == "/api/events":
            return self.events()
        if path == "/":
            path = "/index.html"
        f = (STATIC / path.lstrip("/")).resolve()
        if STATIC.resolve() not in f.parents or not f.is_file():
            return self.send(404, {"error": "not found"})
        ctype = mimetypes.guess_type(f.name)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype.endswith("javascript"):
            ctype += "; charset=utf-8"
        self.send(200, f.read_bytes(), ctype, cache="/vendor/" in path)

    def events(self):
        w = self.watcher
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        seen = -1
        try:
            while True:
                with w.cond:
                    w.cond.wait_for(lambda: w.version != seen, timeout=15)
                    version, log = w.version, w.log
                if version == seen:
                    self.wfile.write(b": keepalive\n\n")
                else:
                    seen = version
                    msg = {"version": version, "log": log and {"task": log[0], "size": log[1]}}
                    self.wfile.write(f"event: state\ndata: {json.dumps(msg)}\n\n".encode())
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass


def main(argv=None):
    ap = argparse.ArgumentParser(prog="pm-dashboard", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", help="the project's git repository (default: the one around the current directory)")
    ap.add_argument("--port", type=int, default=8642)
    args = ap.parse_args(argv)
    root = args.root or pm.git_root()
    if not root:
        print("pm-dashboard: not in a git repository (or pass --root)")
        return 1
    try:
        project = pm.Project(root, pm.load_config(root))
    except pm.FormatError as e:
        print(f"pm-dashboard: {e}")
        return 1
    watcher = Watcher(project)
    watcher.state = read_state(project)
    threading.Thread(target=watcher.loop, daemon=True).start()
    Handler.watcher = watcher
    Handler.allowed_hosts = {f"{h}:{args.port}" for h in ("127.0.0.1", "localhost")}
    try:
        server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    except OSError as e:
        print(f"pm-dashboard: port {args.port}: {e.strerror}")
        return 1
    server.daemon_threads = True
    print(f"pm-dashboard: {root} on http://127.0.0.1:{args.port}/ (Ctrl-C stops)", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0
