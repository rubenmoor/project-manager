#!/usr/bin/env python3
"""pm: a project manager for Claude Code workers (README.md). Milestones -> masterplan -> plans -> tasks, plus
decisions for the human, in an SQLite database in the project (tasks.dbsqlite, created by the first command;
the schema is SCHEMA below). The task queue is computed (the `queue` view): open tasks of open milestones that no
pending decision blocks, highest priority first, then by milestone, plan and task. Worked one task at a time by `claude -p`.
Standard library only (Python 3.11+).

    pm status | queue | show <task> | validate | decide | run [--once] [--dry-run]
    pm milestone add|set …   pm masterplan <milestone> …   pm plan add|priority …
    pm task add|done|drop|block|priority …   pm decision add …

Works on the git repository around the current directory. Settings: project-manager.toml, written with defaults
and committed by the first command. Otherwise pm never commits; workers commit their work. Nothing is pushed.
Whether the database is committed or ignored is the project's choice.
"""
import argparse, contextlib, datetime, json, os, re, sqlite3, subprocess, sys, time, tomllib, zoneinfo
from pathlib import Path


CONFIG_FILE = "project-manager.toml"
CONFIG_TEMPLATE = """# pm configuration. Delete this file to get the defaults back.

# SQLite database, relative to the project root.
database = "tasks.dbsqlite"

# Command that works one task. "{prompt}" is replaced by the worker prompt.
worker = ["claude", "-p", "{prompt}", "--permission-mode", "acceptEdits",
          "--allowedTools", "Bash Read Edit Write Glob Grep"]

# Minutes to wait after a usage limit resets before retrying the task.
limit_margin_minutes = 5

# Worker logs. A relative path is inside the git directory, so it is never tracked.
log_dir = "pm-logs"

# Editor for `pm milestone add`. Empty: the editor git uses.
editor = ""
"""
MILESTONE_STATES = ("open", "implemented")
MASTER_STATES = ("stub", "draft", "agreed")

SCHEMA_VERSION = 2
SCHEMA = """
CREATE TABLE milestone (
    id          INTEGER PRIMARY KEY,
    title       TEXT NOT NULL CHECK (trim(title) <> ''),
    status      TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'implemented')),
    description TEXT NOT NULL DEFAULT ''                   -- Markdown
) STRICT;

CREATE TABLE masterplan (
    milestone_id  INTEGER PRIMARY KEY REFERENCES milestone (id),
    status        TEXT NOT NULL DEFAULT 'stub' CHECK (status IN ('stub', 'draft', 'agreed')),
    goal          TEXT NOT NULL DEFAULT '',
    scope         TEXT NOT NULL DEFAULT '',
    exit_criteria TEXT NOT NULL DEFAULT ''
) STRICT;

CREATE TABLE plan (
    id           INTEGER PRIMARY KEY,
    milestone_id INTEGER NOT NULL REFERENCES milestone (id),
    title        TEXT NOT NULL CHECK (trim(title) <> ''),
    priority     INTEGER,                                  -- NULL: 0
    description  TEXT NOT NULL DEFAULT ''
) STRICT;

CREATE TABLE task (
    id          INTEGER PRIMARY KEY,
    plan_id     INTEGER NOT NULL REFERENCES plan (id),
    title       TEXT NOT NULL CHECK (trim(title) <> ''),
    status      TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'done', 'dropped')),
    priority    INTEGER,                                   -- NULL: the plan's
    note        TEXT,                                      -- done: date, files or commit; dropped: reason
    description TEXT NOT NULL DEFAULT '',                  -- Markdown
    CONSTRAINT "an open task needs a description" CHECK (status <> 'open' OR trim(description) <> '')
) STRICT;

CREATE TABLE decision (
    id       INTEGER PRIMARY KEY,
    question TEXT NOT NULL CHECK (trim(question) <> ''),
    status   TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'answered', 'irrelevant')),
    options  TEXT NOT NULL DEFAULT '[]' CHECK (json_type(options) = 'array'),  -- recommended first
    context  TEXT NOT NULL DEFAULT '',
    answer   TEXT,
    reason   TEXT,
    closed   TEXT,
    CONSTRAINT "pending: no answer, reason or closed; answered: answer and closed; irrelevant: reason and closed"
    CHECK (status = 'pending' AND answer IS NULL AND reason IS NULL AND closed IS NULL
           OR status = 'answered' AND answer IS NOT NULL AND closed IS NOT NULL
           OR status = 'irrelevant' AND reason IS NOT NULL AND closed IS NOT NULL)
) STRICT;

CREATE TABLE task_decision (                               -- the task waits while the decision is pending
    task_id     INTEGER NOT NULL REFERENCES task (id),
    decision_id INTEGER NOT NULL REFERENCES decision (id),
    PRIMARY KEY (task_id, decision_id)
) STRICT;

CREATE INDEX plan_milestone ON plan (milestone_id);
CREATE INDEX task_plan ON task (plan_id);
CREATE INDEX task_decision_decision ON task_decision (decision_id);

CREATE VIEW queue AS
SELECT coalesce(t.priority, p.priority, 0) AS priority, t.id AS task_id, t.title
FROM task t
JOIN plan p ON p.id = t.plan_id
JOIN milestone m ON m.id = p.milestone_id
WHERE m.status = 'open' AND t.status = 'open'
  AND NOT EXISTS (SELECT 1 FROM task_decision td JOIN decision d ON d.id = td.decision_id
                  WHERE td.task_id = t.id AND d.status = 'pending')
ORDER BY priority DESC, m.id, p.id, t.id;
"""

WORKER_PROMPT = """You are the worker for task {id}. Follow the project's own instructions for agents \
(AGENTS.md, CLAUDE.md or similar, if there are any), then do task {id} and nothing else.

Rules:
1. Work on task {id} only. Change milestones, plans, tasks and decisions only through `pm`, never {db} directly.
2. Follow-up work: `pm task add <plan id> "<title>" --description "<text>"` in the same plan, or \
`pm plan add <milestone id> "<title>" --description "<text>"` and tasks in it for work outside this plan. Set \
priorities (`--priority N`, positive = earlier) only where the default order (plan, then task) is wrong.
3. Needs a human answer: `pm decision add "<question>" --context "<text>" --option "<recommended>" \
--option "<other>" --blocks <task ids>`. If task {id} itself is blocked, include it in --blocks, commit what \
is done and stop.
4. Finished: `pm task done {id} "<YYYY-MM-DD, files or commit>"` or `pm task drop {id} "<reason>"`. Run \
the project's checks for what you changed. Commit your work in one commit, message "task {id}: <title>". \
Don't push. Stop.

The task, its plan and its milestone (`pm show {id}` prints this again):

{show}"""


# --- Claude's usage limit: the message and when it resets ---------------------------------------

LIMIT_WORDS = ("usage limit", "session limit", "rate limit", "hit your limit")
# "resets 11pm (America/Bogota)", "resets 11:30pm", "resets Oct 9, 3am (Europe/Berlin)"
RESET_RE = re.compile(r"resets\s+(?:(?P<mon>[a-z]{3})[a-z]*\.?\s+(?P<day>\d{1,2}),?\s+(?:at\s+)?)?"
                      r"(?P<h>\d{1,2})(?::(?P<m>\d{2}))?\s*(?P<ap>[ap]\.?m\.?)?(?:\s*\((?P<tz>[^)]+)\))?", re.I)


def limit_reset(msg, now=None):
    """When Claude's usage limit in `msg` resets (an aware datetime), or None if it doesn't say."""
    m = RESET_RE.search(str(msg))
    if not m:
        return None
    tz = None
    if m["tz"]:
        try:
            tz = zoneinfo.ZoneInfo(m["tz"].strip())
        except Exception:  # noqa: BLE001 — unknown zone or no tz data: assume local time
            tz = None
    tz = tz or datetime.datetime.now().astimezone().tzinfo
    now = (now or datetime.datetime.now(datetime.timezone.utc)).astimezone(tz)
    hour, minute = int(m["h"]), int(m["m"] or 0)
    if m["ap"]:
        hour = hour % 12 + (12 if m["ap"][0].lower() == "p" else 0)
    if hour > 23 or minute > 59:
        return None
    try:
        if m["mon"]:
            month = datetime.datetime.strptime(m["mon"][:3].title(), "%b").month
            reset = now.replace(month=month, day=int(m["day"]), hour=hour, minute=minute, second=0, microsecond=0)
            if reset < now - datetime.timedelta(days=1):  # "resets Jan 2" said in December
                reset = reset.replace(year=now.year + 1)
        else:
            reset = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if reset < now - datetime.timedelta(hours=1):  # "resets 2am" said in the evening
                reset += datetime.timedelta(days=1)
    except ValueError:
        return None
    return reset


class FormatError(Exception):
    """The database breaks the schema, or a command names something that doesn't exist."""


def today():
    return datetime.date.today().isoformat()



def load_config(root, out=sys.stderr):
    """project-manager.toml in root. If missing: written from CONFIG_TEMPLATE, the database added to
    .gitignore, both committed."""
    path = Path(root) / CONFIG_FILE
    if not path.exists():
        path.write_text(CONFIG_TEMPLATE, encoding="utf-8")
        ignore, line = Path(root) / ".gitignore", f"/{tomllib.loads(CONFIG_TEMPLATE)['database']}*"
        text = ignore.read_text(encoding="utf-8") if ignore.exists() else ""
        if line not in text.splitlines():
            ignore.write_text(text + ("\n" if text and not text.endswith("\n") else "") + line + "\n", encoding="utf-8")
        files = [CONFIG_FILE, ".gitignore"]
        try:
            subprocess.run(["git", "add", "--", *files], cwd=root, capture_output=True, check=True)
            subprocess.run(["git", "commit", "-q", "-m", f"pm: add {CONFIG_FILE}, ignore the database", "--", *files],
                           cwd=root, capture_output=True, check=True)
            print(f"pm: wrote {CONFIG_FILE} with the defaults, added the database to .gitignore, committed both",
                  file=out)
        except subprocess.CalledProcessError as e:
            print(f"pm: wrote {CONFIG_FILE} and .gitignore; committing them failed: "
                  f"{(e.stderr or b'').decode().strip()}", file=out)
    try:
        config = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as e:
        raise FormatError(f"{CONFIG_FILE}: {e}") from None
    defaults = tomllib.loads(CONFIG_TEMPLATE)
    errors = [f"unknown setting {k!r}" for k in config.keys() - defaults.keys()]
    config = defaults | config
    if not isinstance(config["database"], str) or not config["database"].strip():
        errors.append("database must be a file name")
    w = config["worker"]
    if not isinstance(w, list) or not w or not all(isinstance(a, str) for a in w) or not any("{prompt}" in a for a in w):
        errors.append('worker must be a list of strings, one of them containing "{prompt}"')
    m = config["limit_margin_minutes"]
    if not isinstance(m, int) or isinstance(m, bool) or m < 0:
        errors.append("limit_margin_minutes must be an integer >= 0")
    errors += [f"{k} must be a string" for k in ("log_dir", "editor") if not isinstance(config[k], str)]
    if errors:
        raise FormatError(f"{CONFIG_FILE}: " + "; ".join(errors))
    return config


class Project:
    def __init__(self, root, config=None, out=sys.stdout):
        self.root = Path(root)
        self.config = config or tomllib.loads(CONFIG_TEMPLATE)
        self.db = self.config["database"]
        self.db_path = self.root / self.db
        self.out = out

    def say(self, *args):
        print(*args, file=self.out, flush=True)

    # --- database ----------------------------------------------------------------------------

    @contextlib.contextmanager
    def connect(self, write=False):
        """A connection with foreign keys on; write: one transaction, rolled back if the block raises.
        The first connection creates the database."""
        con = sqlite3.connect(self.db_path, isolation_level=None)
        try:
            con.row_factory = sqlite3.Row
            con.execute("PRAGMA foreign_keys = ON")
            version = con.execute("PRAGMA user_version").fetchone()[0]
            if version == 0 and not con.execute("SELECT 1 FROM sqlite_schema").fetchone():
                con.executescript(f"BEGIN; {SCHEMA} PRAGMA user_version = {SCHEMA_VERSION}; COMMIT;")
                version = SCHEMA_VERSION
            if version != SCHEMA_VERSION:
                raise FormatError(f"{self.db}: schema version {version}, this pm knows {SCHEMA_VERSION}")
            if write:
                con.execute("BEGIN IMMEDIATE")
            yield con
            if con.in_transaction:
                con.execute("COMMIT")
        except sqlite3.IntegrityError as e:
            raise FormatError(f"the change breaks the schema, nothing written: {e}") from None
        except sqlite3.DatabaseError as e:
            raise FormatError(f"{self.db}: {e}") from None
        finally:
            con.close()  # an open transaction is rolled back

    def init_db(self):
        """Create the database with the schema if it doesn't exist."""
        if not self.db_path.exists():
            with self.connect():
                pass

    def rows(self, sql, *args):
        with self.connect() as db:
            return [dict(r) for r in db.execute(sql, args)]

    @staticmethod
    def get(db, table, rid):
        """The row `rid` of `table`, as a dict."""
        r = db.execute(f"SELECT * FROM {table} WHERE id = ?", (rid,)).fetchone()
        if r is None:
            raise FormatError(f"no {table} {rid}")
        return dict(r)

    @classmethod
    def block(cls, db, task_id, decision_id):
        cls.get(db, "task", task_id)
        cls.get(db, "decision", decision_id)
        db.execute("INSERT OR IGNORE INTO task_decision VALUES (?, ?)", (task_id, decision_id))

    def pending(self):
        ds = self.rows("SELECT * FROM decision WHERE status = 'pending' ORDER BY id")
        for d in ds:
            d["options"] = json.loads(d["options"])
        return ds

    def queue(self):
        """[(priority, task id, title)]: the `queue` view."""
        return [tuple(r.values()) for r in self.rows("SELECT * FROM queue")]

    # --- git ---------------------------------------------------------------------------------

    def git(self, *args):
        return subprocess.run(["git", *args], cwd=self.root, capture_output=True, text=True, check=True).stdout

    def dirty(self):
        """Uncommitted changes, apart from the database (and its journal), tracked or not."""
        return bool(self.git("status", "--porcelain", "--", ".", f":(top,exclude,glob){self.db}*").strip())

    # --- validate ----------------------------------------------------------------------------

    def validate(self):
        """Every problem in the database (empty: all good): what a hand edit with foreign keys off, or a broken
        file, leaves."""
        try:
            with self.connect() as db:
                errors = [r[0] for r in db.execute("PRAGMA integrity_check") if r[0] != "ok"]
                for table, rowid, parent, _ in db.execute("PRAGMA foreign_key_check").fetchall():
                    row = dict(db.execute(f"SELECT * FROM {table} WHERE rowid = ?", (rowid,)).fetchone())
                    key = ", ".join(f"{k} {v}" for k, v in row.items() if k == "id" or k.endswith("_id"))
                    errors.append(f"{table} ({key}): no such {parent}")
                return errors
        except FormatError as e:
            return [str(e)]

    # --- milestones --------------------------------------------------------------------------

    def close_milestones(self):
        """Mark open milestones implemented whose masterplan isn't a stub and whose tasks are all closed."""
        closed = []
        with self.connect(write=True) as db:
            for (mid,) in db.execute(
                    "SELECT m.id FROM milestone m JOIN masterplan mp ON mp.milestone_id = m.id "
                    "WHERE m.status = 'open' AND mp.status <> 'stub' "
                    "AND NOT EXISTS (SELECT 1 FROM task t JOIN plan p ON p.id = t.plan_id "
                    "WHERE p.milestone_id = m.id AND t.status = 'open') ORDER BY m.id").fetchall():
                db.execute("UPDATE milestone SET status = 'implemented' WHERE id = ?", (mid,))
                closed.append(mid)
        return closed

    # --- decide ------------------------------------------------------------------------------

    def resolve(self, did, answer=None, irrelevant=None):
        """Close a pending decision. Answered: its tasks unblock by themselves. Irrelevant: the open tasks it
        blocks are dropped."""
        with self.connect(write=True) as db:
            if self.get(db, "decision", did)["status"] != "pending":
                raise FormatError(f"decision {did} is not pending")
            if irrelevant is None:
                db.execute("UPDATE decision SET status = 'answered', answer = ?, closed = ? WHERE id = ?",
                           (answer, today(), did))
                return
            db.execute("UPDATE decision SET status = 'irrelevant', reason = ?, closed = ? WHERE id = ?",
                       (irrelevant, today(), did))
            db.execute("UPDATE task SET status = 'dropped', note = ? WHERE status = 'open' "
                       "AND id IN (SELECT task_id FROM task_decision WHERE decision_id = ?)",
                       (f"decision {did} irrelevant", did))

    def decide(self, ask=input):
        """Ask about every pending decision; returns how many were closed."""
        closed = 0
        for d in self.pending():
            opts = d["options"]
            blocks = [str(r["id"]) for r in self.rows(
                "SELECT t.id FROM task_decision td JOIN task t ON t.id = td.task_id "
                "WHERE td.decision_id = ? AND t.status = 'open' ORDER BY t.id", d["id"])]
            self.say(f"\nDecision {d['id']}: {d['question']}")
            if blocks:
                self.say(f"  blocks task(s) {', '.join(blocks)}")
            if d["context"]:
                self.say("  " + d["context"].replace("\n", "\n  "))
            for n, o in enumerate(opts, 1):
                self.say(f"  {n}) {o}{'  (recommended)' if n == 1 else ''}")
            hint = f"1-{len(opts)}, " if opts else ""
            reply = ask(f"Answer [{hint}your own text, s = skip, x <reason> = irrelevant]: ").strip()
            if not reply or reply.lower() == "s":
                continue
            if reply.lower() == "x" or reply.lower().startswith("x "):
                self.resolve(d["id"], irrelevant=reply[1:].strip() or "no reason given")
            elif reply.isdigit() and 1 <= int(reply) <= len(opts):
                self.resolve(d["id"], answer=opts[int(reply) - 1])
            else:
                self.resolve(d["id"], answer=reply)
            closed += 1
        return closed

    # --- edit commands -----------------------------------------------------------------------

    def edit(self, name, template=""):
        """Open the user's editor (as `git commit` does) on <git dir>/<name> holding `template`. Returns the saved
        text, or None if the editor failed or the file was not saved. No comment lines: '#' starts Markdown headings."""
        path = Path(self.git("rev-parse", "--absolute-git-dir").strip()) / name
        path.write_text(template, encoding="utf-8")
        before = path.stat().st_mtime_ns
        editor = self.config["editor"]
        if not editor:
            try:
                editor = self.git("var", "GIT_EDITOR").strip()
            except subprocess.CalledProcessError:
                editor = os.environ.get("VISUAL") or os.environ.get("EDITOR") or "vi"
        try:
            if subprocess.run(["sh", "-c", f'{editor} "$@"', editor, str(path)], cwd=self.root).returncode != 0:
                return None
            text = path.read_text(encoding="utf-8")
            if path.stat().st_mtime_ns == before and text == template:
                return None
        finally:
            path.unlink(missing_ok=True)
        return text

    def milestone_edit(self):
        """`pm milestone add` without a title: the first line written in the editor is the title, the rest the
        description. Returns the new milestone id, or None if aborted (not saved, or nothing written)."""
        text = self.edit("PM_MILESTONE")
        if text is None or not text.strip():
            return None
        title, _, description = text.strip().partition("\n")
        return self.milestone_add(title.strip(), description.strip())

    def milestone_add(self, title, description=""):
        """A new milestone with a masterplan stub and a plan whose one task writes the masterplan."""
        with self.connect(write=True) as db:
            mid = db.execute("INSERT INTO milestone (title, description) VALUES (?, ?)",
                             (title, (description or "").strip())).lastrowid
            db.execute("INSERT INTO masterplan (milestone_id) VALUES (?)", (mid,))
            pid = db.execute("INSERT INTO plan (milestone_id, title) VALUES (?, 'masterplan')", (mid,)).lastrowid
            db.execute("INSERT INTO task (plan_id, title, description) VALUES (?, ?, ?)", (
                pid, f"write the masterplan for milestone {mid}: {title}",
                f"Read milestone {mid} and the code it touches. Write the masterplan's goal, scope (in and out) and "
                f"exit criteria (checkable): `pm masterplan {mid} --goal … --scope … --exit-criteria …`. Split the "
                f"work into plans (`pm plan add {mid} <title> --description …`), each with tasks small enough for "
                "one worker run (`pm task add <plan id> <title> --description …`); priorities only where the "
                "default order (plan, then task) is wrong. Questions only a human can answer become decisions "
                f"(`pm decision add`). Then `pm masterplan {mid} --status draft`."))
        return mid

    def milestone_set(self, mid, **fields):
        """Set fields of a milestone (title, status, description); None leaves a field as it is."""
        with self.connect(write=True) as db:
            self.get(db, "milestone", mid)
            for k, v in fields.items():
                if v is not None:
                    db.execute(f"UPDATE milestone SET {k} = ? WHERE id = ?", (v.strip(), mid))

    def masterplan_set(self, mid, **fields):
        with self.connect(write=True) as db:
            self.get(db, "milestone", mid)
            db.execute("INSERT OR IGNORE INTO masterplan (milestone_id) VALUES (?)", (mid,))
            for k, v in fields.items():
                if v is not None:
                    db.execute(f"UPDATE masterplan SET {k} = ? WHERE milestone_id = ?", (v.strip(), mid))

    def plan_add(self, mid, title, description=None, prio=None):
        with self.connect(write=True) as db:
            self.get(db, "milestone", mid)
            return db.execute("INSERT INTO plan (milestone_id, title, priority, description) VALUES (?, ?, ?, ?)",
                              (mid, title, prio, (description or "").strip())).lastrowid

    def plan_priority(self, pid, prio):
        with self.connect(write=True) as db:
            self.get(db, "plan", pid)
            db.execute("UPDATE plan SET priority = ? WHERE id = ?", (prio, pid))

    def task_add(self, pid, title, description, prio=None, blocked_by=()):
        with self.connect(write=True) as db:
            self.get(db, "plan", pid)
            tid = db.execute("INSERT INTO task (plan_id, title, priority, description) VALUES (?, ?, ?, ?)",
                             (pid, title, prio, description.strip())).lastrowid
            for did in blocked_by:
                self.block(db, tid, did)
        return tid

    def task_set(self, tid, **fields):
        """Set fields of a task (status, note, priority); a value None clears it."""
        with self.connect(write=True) as db:
            self.get(db, "task", tid)
            for k, v in fields.items():
                db.execute(f"UPDATE task SET {k} = ? WHERE id = ?", (v, tid))

    def task_block(self, tid, did):
        with self.connect(write=True) as db:
            self.block(db, tid, did)

    def decision_add(self, question, context=None, options=(), blocks=()):
        with self.connect(write=True) as db:
            did = db.execute("INSERT INTO decision (question, options, context) VALUES (?, ?, ?)",
                             (question, json.dumps(list(options), ensure_ascii=False), (context or "").strip())).lastrowid
            for tid in blocks:
                self.block(db, tid, did)
        return did

    # --- show --------------------------------------------------------------------------------

    def show(self, tid):
        with self.connect() as db:
            t = self.get(db, "task", tid)
            p = self.get(db, "plan", t["plan_id"])
            m = self.get(db, "milestone", p["milestone_id"])
            mp = dict(db.execute("SELECT * FROM masterplan WHERE milestone_id = ?", (m["id"],)).fetchone() or {})
            siblings = db.execute("SELECT id, status, title FROM task WHERE plan_id = ? ORDER BY id", (p["id"],)).fetchall()
            ds = db.execute("SELECT d.* FROM task_decision td JOIN decision d ON d.id = td.decision_id "
                            "WHERE td.task_id = ? ORDER BY d.id", (tid,)).fetchall()
        prio = t["priority"] if t["priority"] is not None else p["priority"] or 0
        out = [f"Milestone {m['id']}: {m['title']} ({m['status']})", m["description"], "",
               f"Masterplan ({mp.get('status', 'none')})"]
        for k in ("goal", "scope", "exit_criteria"):
            if mp.get(k):
                out += [f"{k.replace('_', ' ')}:", mp[k]]
        out += ["", f"Plan {p['id']}: {p['title']} (priority {p['priority'] or 0})"]
        if p["description"]:
            out.append(p["description"])
        for x in siblings:
            out.append(f"  task {x['id']} [{x['status']}] {x['title']}" + (" ← this task" if x["id"] == tid else ""))
        out += ["", f"Task {tid}: {t['title']} ({t['status']}, priority {prio})"]
        if t["note"]:
            out.append(f"note: {t['note']}")
        out.append(t["description"])
        for d in ds:
            said = d["answer"] or d["reason"] or ""
            out.append(f"Decision {d['id']} ({d['status']}): {d['question']}" + (f" → {said}" if said else ""))
        return "\n".join(out).rstrip() + "\n"

    # --- run ---------------------------------------------------------------------------------

    def worker_command(self, tid):
        prompt = WORKER_PROMPT.format(id=tid, db=self.db, show=self.show(tid))
        return [a.replace("{prompt}", prompt) for a in self.config["worker"]]

    @property
    def logs(self):
        """log_dir; relative: inside the git dir, so never tracked."""
        return Path(self.git("rev-parse", "--absolute-git-dir").strip()) / self.config["log_dir"]

    def run_worker(self, cmd, log):
        """Run the worker, copying its output to the terminal and the log. Returns the exit code."""
        log.parent.mkdir(parents=True, exist_ok=True)
        with open(log, "w", encoding="utf-8") as f:
            with subprocess.Popen(cmd, cwd=self.root, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                  text=True, bufsize=1) as proc:
                for line in proc.stdout:
                    f.write(line)
                    self.out.write(line)
                return proc.wait()

    @staticmethod
    def limit_line(log):
        """The usage-limit line among the log's last 5 lines, or None."""
        tail = log.read_text(encoding="utf-8", errors="replace").splitlines()[-5:]
        return next((l for l in reversed(tail) if any(w in l.lower() for w in LIMIT_WORDS)
                     and "resets " in l.lower()), None)

    def wait_until(self, when):
        """Sleep until `when` by the wall clock, so a suspended machine still wakes up on time."""
        while (left := (when - datetime.datetime.now(when.tzinfo)).total_seconds()) > 0:
            time.sleep(min(left, 60))

    def work(self, tid):
        """Run the worker for a task until no usage limit stops it. Returns ("done" | "blocked" | "failed", message)."""
        cmd, log = self.worker_command(tid), self.logs / f"task-{tid}.log"
        while True:
            self.say(f"\n== task {tid} (log: {log})")
            code = self.run_worker(cmd, log)
            limit = self.limit_line(log)
            if not limit:
                break
            reset = limit_reset(limit)
            if reset is None:
                return "failed", f"usage limit without a readable reset time: {limit}"
            margin = datetime.timedelta(minutes=self.config["limit_margin_minutes"])
            until = max(reset + margin, datetime.datetime.now(reset.tzinfo) + margin)
            self.say(f"Usage limit ({limit.strip()}). Waiting until {until:%Y-%m-%d %H:%M %Z}, then the same task again.")
            self.wait_until(until)
        tail = "\n".join(log.read_text(encoding="utf-8", errors="replace").splitlines()[-30:])
        if code != 0:
            return "failed", f"exit {code}\n{tail}"
        errors = self.validate()
        if errors:
            return "failed", "the database is no longer valid:\n  " + "\n  ".join(errors)
        if self.dirty():
            return "failed", "the worker left uncommitted changes (git status)"
        with self.connect() as db:
            t = self.get(db, "task", tid)
            blocked = [str(r[0]) for r in db.execute(
                "SELECT d.id FROM task_decision td JOIN decision d ON d.id = td.decision_id "
                "WHERE td.task_id = ? AND d.status = 'pending' ORDER BY d.id", (tid,))]
        if t["status"] != "open":
            return "done", t["status"] + (f": {t['note']}" if t["note"] else "")
        if blocked:
            return "blocked", "waits for decision(s) " + ", ".join(blocked)
        return "failed", f"exit 0, but task {tid} is still open and not blocked\n{tail}"

    def run(self, once=False, dry_run=False, interactive=None, ask=input):
        interactive = sys.stdin.isatty() if interactive is None else interactive
        while True:
            errors = self.validate()
            if errors:
                self.say("the database has errors:\n  " + "\n  ".join(errors))
                return 1
            if dry_run:
                q = self.queue()
                if not q:
                    self.say("Queue empty.")
                else:
                    self.say(" ".join(f"'{a}'" if " " in a else a for a in self.worker_command(q[0][1])))
                return 0
            for mid in self.close_milestones():
                self.say(f"milestone {mid} implemented")
            if interactive and self.pending():
                self.decide(ask)
            if not self.rows("SELECT 1 FROM milestone WHERE status = 'open'"):
                self.say("No open milestone (`pm milestone add` starts one).")
                return 0
            q = self.queue()
            if not q:
                n = len(self.pending())
                self.say(f"Queue empty; {n} pending decision(s)." + (" Answer them with `pm decide`." if n else ""))
                return 0
            if self.dirty():
                self.say("The working tree has uncommitted changes; commit or stash them first.")
                return 1
            tid = q[0][1]
            result, message = self.work(tid)
            if result == "failed":
                self.say(f"\ntask {tid} failed: {message}")
                return 1
            self.say(f"\ntask {tid}: {result} ({message})")
            for mid in self.close_milestones():
                self.say(f"milestone {mid} implemented")
            if once:
                return 0

    # --- status ------------------------------------------------------------------------------

    def status(self):
        for m in self.rows(
                "SELECT m.id, m.title, mp.status AS masterplan, "
                "(SELECT count(*) FROM plan p WHERE p.milestone_id = m.id) AS plans, "
                "(SELECT count(*) FROM task t JOIN plan p ON p.id = t.plan_id WHERE p.milestone_id = m.id) AS tasks, "
                "(SELECT count(*) FROM task t JOIN plan p ON p.id = t.plan_id "
                "WHERE p.milestone_id = m.id AND t.status <> 'open') AS closed "
                "FROM milestone m LEFT JOIN masterplan mp ON mp.milestone_id = m.id "
                "WHERE m.status = 'open' ORDER BY m.id"):
            self.say(f"Milestone {m['id']}: {m['title']}: masterplan {m['masterplan'] or 'none'}, {m['plans']} plan(s), "
                     f"{m['closed']}/{m['tasks']} tasks closed")
        self.print_queue(limit=10)
        pending = self.pending()
        self.say(f"\nPending decisions ({len(pending)}):")
        for d in pending:
            self.say(f"  {d['id']}: {d['question']}")

    def print_queue(self, limit=None):
        q = self.queue()
        self.say(f"\nQueue ({len(q)}):")
        for prio, tid, title in q[:limit]:
            self.say(f"  {prio:+4d}  task {tid}: {title}" if prio else f"        task {tid}: {title}")
        if limit and len(q) > limit:
            self.say(f"  … {len(q) - limit} more (`pm queue`)")


def git_root():
    r = subprocess.run(["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 else None


def text_arg(v):
    """A text option: '-' reads stdin."""
    return sys.stdin.read() if v == "-" else v


def prio_arg(v):
    return None if v == "none" else int(v)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="pm", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", help="the project's git repository (default: the one around the current directory)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name, text in [("status", "open milestones, the queue, pending decisions"), ("queue", "the whole queue"),
                       ("validate", "check the database"),
                       ("decide", "answer pending decisions in the terminal")]:
        sub.add_parser(name, help=text)
    s = sub.add_parser("show", help="a task with its plan, milestone and decisions")
    s.add_argument("task", type=int)
    r = sub.add_parser("run", help="work the queue with the worker command (claude -p)")
    r.add_argument("--once", action="store_true", help="stop after one task")
    r.add_argument("--dry-run", action="store_true", help="print the next worker command; change nothing")

    ms = sub.add_parser("milestone", help="add or change milestones").add_subparsers(dest="action", required=True)
    ma = ms.add_parser("add", help="new open milestone; prints its id. Without a title: write it in your editor "
                       "(first line: title, the rest: description); with a title: the description is read from stdin")
    ma.add_argument("title", nargs="?")
    mset = ms.add_parser("set", help="set milestone fields")
    mset.add_argument("milestone", type=int)
    mset.add_argument("--title")
    mset.add_argument("--status", choices=MILESTONE_STATES)
    mset.add_argument("--description", type=text_arg)

    mp = sub.add_parser("masterplan", help="set masterplan fields")
    mp.add_argument("milestone", type=int)
    mp.add_argument("--status", choices=MASTER_STATES)
    for k in ("goal", "scope", "exit-criteria"):
        mp.add_argument(f"--{k}", type=text_arg)

    p = sub.add_parser("plan", help="add or change plans").add_subparsers(dest="action", required=True)
    pa = p.add_parser("add", help="new plan; prints its id")
    pa.add_argument("milestone", type=int)
    pa.add_argument("title")
    pa.add_argument("--description", type=text_arg)
    pa.add_argument("--priority", type=int)
    pp = p.add_parser("priority", help="integer, or none for 0")
    pp.add_argument("plan", type=int)
    pp.add_argument("priority", type=prio_arg)

    t = sub.add_parser("task", help="add or change tasks").add_subparsers(dest="action", required=True)
    a = t.add_parser("add", help="new task; prints its id")
    a.add_argument("plan", type=int)
    a.add_argument("title")
    a.add_argument("--description", required=True, type=text_arg, help="Markdown, or - for stdin")
    a.add_argument("--priority", type=int)
    a.add_argument("--blocked-by", nargs="+", type=int, default=[], metavar="DECISION")
    for verb, text in [("done", "note: date, files or commit"), ("drop", "reason")]:
        x = t.add_parser(verb)
        x.add_argument("task", type=int)
        x.add_argument("note", help=text)
    b = t.add_parser("block", help="the task waits for a decision")
    b.add_argument("task", type=int)
    b.add_argument("decision", type=int)
    tp = t.add_parser("priority", help="integer, or none for the plan's")
    tp.add_argument("task", type=int)
    tp.add_argument("priority", type=prio_arg)

    d = sub.add_parser("decision", help="ask the human").add_subparsers(dest="action", required=True)
    da = d.add_parser("add", help="new pending decision; prints its id")
    da.add_argument("question")
    da.add_argument("--context", type=text_arg)
    da.add_argument("--option", action="append", default=[], help="repeat; the recommended one first")
    da.add_argument("--blocks", nargs="+", type=int, default=[], metavar="TASK")

    args = ap.parse_args(argv)
    root = args.root or git_root()
    if root is None:
        print("pm: not inside a git repository (or pass --root)")
        return 2
    try:
        pm = Project(root, load_config(root))
        pm.init_db()
        if args.cmd == "validate":
            errors = pm.validate()
            print("\n".join(errors) if errors else f"{pm.db} is valid")
            return 1 if errors else 0
        if args.cmd != "run" and (errors := pm.validate()):
            print("the database has errors (`pm validate`):\n  " + "\n  ".join(errors))
            return 1
        if args.cmd == "status":
            pm.status()
        elif args.cmd == "queue":
            pm.print_queue()
        elif args.cmd == "show":
            print(pm.show(args.task), end="")
        elif args.cmd == "decide":
            pm.decide()
        elif args.cmd == "run":
            return pm.run(once=args.once, dry_run=args.dry_run)
        elif args.cmd == "milestone":
            if args.action == "add":
                if args.title is None:
                    mid = pm.milestone_edit()
                    if mid is None:
                        print("pm: aborted, no milestone added")
                        return 1
                else:
                    mid = pm.milestone_add(args.title, sys.stdin.read())
                print(mid)
            else:
                pm.milestone_set(args.milestone, title=args.title, status=args.status, description=args.description)
        elif args.cmd == "masterplan":
            pm.masterplan_set(args.milestone, status=args.status, goal=args.goal, scope=args.scope,
                              exit_criteria=args.exit_criteria)
        elif args.cmd == "plan":
            if args.action == "add":
                print(pm.plan_add(args.milestone, args.title, args.description, args.priority))
            else:
                pm.plan_priority(args.plan, args.priority)
        elif args.cmd == "task":
            if args.action == "add":
                print(pm.task_add(args.plan, args.title, args.description, args.priority, args.blocked_by))
            elif args.action == "done":
                pm.task_set(args.task, status="done", note=args.note)
            elif args.action == "drop":
                pm.task_set(args.task, status="dropped", note=args.note)
            elif args.action == "block":
                pm.task_block(args.task, args.decision)
            else:
                pm.task_set(args.task, priority=args.priority)
        elif args.cmd == "decision":
            print(pm.decision_add(args.question, args.context, args.option, args.blocks))
        return 0
    except FormatError as e:
        print(f"pm: {e}")
        return 1
    except BrokenPipeError:  # `pm queue | head`
        return 0
    except KeyboardInterrupt:
        print("\nStopped. `pm run` continues with the same task.")
        return 130


if __name__ == "__main__":
    sys.exit(main())
