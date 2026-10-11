"""pm: a project manager for Claude Code workers (README.md). Milestones -> plans -> tasks, plus decisions for the
human, in SQLite: the project keeps them as SQL text in tasks.sql, tracked by git, which every command loads into an
in-memory database (the schema is SCHEMA below). A new milestone comes with a first plan, the masterplan (body
"tbd."), and a task that writes it; a new or edited plan queues a task that splits it into tasks; a milestone whose
tasks are all closed gets a last one, the post-milestone cleanup, before it is implemented. The queue (the
`queue` view): open tasks of open milestones that no pending decision blocks, highest priority first, then by
milestone, plan and task. Worked one task at a time by `claude -p`. Standard library only (Python 3.11+).

    pm init | status | queue | show <task> | validate | decide | run [--once] [--dry-run] | instructions
    pm milestone add|set …   pm plan add|edit|list …   pm task add|done|drop|priority …
    pm decision add …   pm task-pending-decision <task> <decision>

Works on the git repository around the current directory. `pm init` writes the settings, project-manager.toml (the
template is the file of that name next to this one), and commits it; the first command that uses the database
writes it. pm commits its own changes to tasks.sql; workers commit them with their work. Nothing is pushed.
"""
import argparse, contextlib, datetime, fcntl, json, os, re, sqlite3, subprocess, sys, time, tomllib, zoneinfo
from pathlib import Path


CONFIG_FILE = "project-manager.toml"
CONFIG_TEMPLATE = (Path(__file__).parent / CONFIG_FILE).read_text(encoding="utf-8")
INSTRUCTIONS = Path(__file__).parent / "instructions.md"
EPILOG = "Agents (LLMs): read `pm instructions` first, the full reference with examples for every command."
PROMPT_KEYS = ("masterplan_prompt", "plan_tasks_prompt", "cleanup_prompt")
PRIORITY_KEYS = ("plan_priority_prompt", "task_priority_prompt")
CLEANUP_SUBJECT = "post-milestone cleanup"
PLAN_TASKS_SUBJECT = "create tasks for plan {}"
MILESTONE_STATES = ("open", "implemented")

SCHEMA_VERSION = 7
SCHEMA = """
CREATE TABLE milestone (
    id           INTEGER PRIMARY KEY,
    subject      TEXT NOT NULL CHECK (trim(subject) <> ''),
    status       TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'implemented')),
    body         TEXT NOT NULL DEFAULT '',                 -- Markdown
    created_date TEXT NOT NULL DEFAULT (date('now'))
) STRICT;

CREATE TABLE plan (
    id           INTEGER PRIMARY KEY,
    milestone_id INTEGER NOT NULL REFERENCES milestone (id),
    subject      TEXT NOT NULL CHECK (trim(subject) <> ''),
    priority     INTEGER,                                  -- NULL: 0
    body         TEXT NOT NULL CONSTRAINT "a plan needs a body" CHECK (trim(body) <> '')
) STRICT;

CREATE TABLE task (
    id           INTEGER PRIMARY KEY,
    plan_id      INTEGER NOT NULL REFERENCES plan (id),
    subject      TEXT NOT NULL CHECK (trim(subject) <> ''),
    status       TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'done', 'dropped')),
    priority     INTEGER,                                  -- NULL: the plan's
    note         TEXT,                                     -- done: date, files or commit; dropped: reason
    body         TEXT NOT NULL DEFAULT '',                 -- Markdown
    CONSTRAINT "an open task needs a body" CHECK (status <> 'open' OR trim(body) <> '')
) STRICT;

CREATE TABLE decision (
    id       INTEGER PRIMARY KEY,
    subject  TEXT NOT NULL CHECK (trim(subject) <> ''),
    status   TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'answered', 'irrelevant')),
    options  TEXT NOT NULL DEFAULT '[]' CHECK (json_type(options) = 'array'),  -- recommended first
    body     TEXT NOT NULL DEFAULT '',
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
SELECT coalesce(t.priority, p.priority, 0) AS priority, t.id AS task_id, t.subject
FROM task t
JOIN plan p ON p.id = t.plan_id
JOIN milestone m ON m.id = p.milestone_id
WHERE m.status = 'open' AND t.status = 'open'
  AND NOT EXISTS (SELECT 1 FROM task_decision td JOIN decision d ON d.id = td.decision_id
                  WHERE td.task_id = t.id AND d.status = 'pending')
ORDER BY priority DESC, m.id, p.id, t.id;
"""

WORKER_PROMPT = """You are the worker for task {id}. Follow the project's own instructions for agents (AGENTS.md, \
CLAUDE.md or similar), then do task {id} and nothing else.

{show}
Milestones, plans, tasks and decisions are in {db}; change them only with these commands (a text value - reads stdin):
  pm plan add <milestone id> "<subject>" < body.md     a task to split it into tasks is queued
  pm plan edit <plan id> [--subject …] [--body …] [--priority N]
  pm plans list     plans with open tasks, highest priority first
  pm task add <plan id> "<subject>" [--priority N] < body.md     positive priority: earlier
  pm decision add "<subject>" [--body "<text>"] [--option "<recommended>"] [--option …]   prints its id
  pm task-pending-decision <task id> <decision id>     the task waits until the human answers
  pm show <task id>
Every command with examples, and recipes for the usual tasks: `pm instructions`.

Finish with `pm task done {id} "<YYYY-MM-DD, files>"` or `pm task drop {id} "<reason>"`. Stage (`git add`) the \
changes meant to stay in the project and delete temporary files you created. Don't commit or push: pm commits what \
is staged, together with {db}, as "task {id}: <subject>". If task {id} itself needs an answer first, make it pending \
on the decision, stage what is done and stop."""


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



def init(root, out=sys.stdout):
    """`pm init`: write project-manager.toml (the defaults) if missing and commit it. The database is created by
    the first command that uses it."""
    path = Path(root) / CONFIG_FILE
    if path.exists():
        print(f"pm: already initialized ({CONFIG_FILE})", file=out)
        return
    path.write_text(CONFIG_TEMPLATE, encoding="utf-8")
    pm = Project(root, out=out)
    try:
        pm.git("add", "--", CONFIG_FILE)
        pm.git("commit", "-q", "-m", "pm: init", "--", CONFIG_FILE)
        print(f"pm: wrote and committed {CONFIG_FILE}", file=out)
    except subprocess.CalledProcessError as e:
        print(f"pm: wrote {CONFIG_FILE}; committing failed: {e.stderr.strip()}", file=out)
    print(f"pm: check and adjust the defaults in {CONFIG_FILE} (e.g. the database file name, the worker command)",
          file=out)


def load_config(root):
    path = Path(root) / CONFIG_FILE
    if not path.exists():
        raise FormatError(f"no {CONFIG_FILE}: run `pm init` first")
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
    errors += [f"{k} must be a non-empty string" for k in PROMPT_KEYS
               if not isinstance(config[k], str) or not config[k].strip()]
    errors += [f"{k} must be a string" for k in PRIORITY_KEYS if not isinstance(config[k], str)]
    p = config["planning_priority"]
    if not isinstance(p, int) or isinstance(p, bool):
        errors.append("planning_priority must be an integer")
    if errors:
        raise FormatError(f"{CONFIG_FILE}: " + "; ".join(errors))
    return config


def dump(con):
    """The database as SQL text: schema, then one INSERT per row, sorted by primary key."""
    out = [f"-- pm database. Change it with pm; after a hand edit run `pm validate`.",
           f"PRAGMA user_version = {SCHEMA_VERSION};", ""]
    tables = []
    for kind, name, sql in con.execute("SELECT type, name, sql FROM sqlite_schema WHERE sql IS NOT NULL ORDER BY rowid"):
        out += [sql + ";", ""]
        if kind == "table":
            tables.append(name)
    for t in tables:
        info = con.execute(f"PRAGMA table_info({t})").fetchall()
        cols = [c[1] for c in info]
        key = [c[1] for c in sorted(info, key=lambda c: c[5]) if c[5]]
        rows = con.execute(f"SELECT {', '.join(f'quote({c})' for c in cols)} FROM {t} ORDER BY {', '.join(key)}")
        out += [f"INSERT INTO {t} ({', '.join(cols)}) VALUES ({', '.join(r)});" for r in rows]
        out.append("")
    return "\n".join(out).rstrip() + "\n"


def migrate(old):
    """A version-5 or -6 database (title, description; question, context) copied into the current schema (subject,
    body); closes `old`."""
    con = sqlite3.connect(":memory:", isolation_level=None)
    con.row_factory = sqlite3.Row
    con.executescript(f"BEGIN; {SCHEMA} PRAGMA user_version = {SCHEMA_VERSION};")
    rename = {"title": "subject", "description": "body", "question": "subject", "context": "body"}
    for t in ("milestone", "plan", "task", "decision", "task_decision"):
        for r in old.execute(f"SELECT * FROM {t}").fetchall():
            cols = [rename.get(c, c) for c in r.keys()]
            con.execute(f"INSERT INTO {t} ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})", tuple(r))
    con.execute("COMMIT")
    old.close()
    return con


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
    def lock(self):
        """Exclusive while one pm process changes the database."""
        with open(Path(self.git("rev-parse", "--absolute-git-dir").strip()) / "pm.lock", "w") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            yield

    @contextlib.contextmanager
    def connect(self, write=False):
        """The database file loaded into memory, foreign keys on. write: one transaction under the lock, written
        back to the file if it changed; nothing is written if the block raises. No file yet: written with the schema."""
        with self.lock() if write else contextlib.nullcontext():
            con = sqlite3.connect(":memory:", isolation_level=None)
            try:
                con.row_factory = sqlite3.Row
                text = self.db_path.read_text(encoding="utf-8") if self.db_path.exists() else None
                try:
                    con.executescript(f"BEGIN;\n{text}\nCOMMIT;" if text is not None else
                                      f"BEGIN; {SCHEMA} PRAGMA user_version = {SCHEMA_VERSION}; COMMIT;")
                except sqlite3.Error as e:
                    raise FormatError(f"{self.db}: {e}") from None
                version = con.execute("PRAGMA user_version").fetchone()[0]
                if version in (5, 6):
                    con, version = migrate(con), SCHEMA_VERSION
                if version != SCHEMA_VERSION:
                    raise FormatError(f"{self.db}: schema version {version}, this pm knows {SCHEMA_VERSION}")
                con.execute("PRAGMA foreign_keys = ON")
                if text is None:
                    text = dump(con)
                    self.save(text)
                if not write:
                    yield con
                    return
                con.execute("BEGIN")
                yield con
                if con.in_transaction:
                    con.execute("COMMIT")
                new = dump(con)
                if new != text:
                    self.save(new)
            except sqlite3.IntegrityError as e:
                raise FormatError(f"the change breaks the schema, nothing written: {e}") from None
            except sqlite3.DatabaseError as e:
                raise FormatError(f"{self.db}: {e}") from None
            finally:
                con.close()

    def save(self, text):
        tmp = self.db_path.with_name(self.db_path.name + ".tmp")
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, self.db_path)

    def commit_db(self, message):
        """Commit the database file if it changed; not in a worker, which commits it with its work."""
        if os.environ.get("PM_WORKER") or not self.git("status", "--porcelain", "--", self.db).strip():
            return
        try:
            self.git("add", "--", self.db)
            self.git("commit", "-q", "-m", f"pm: {message}", "--", self.db)
        except subprocess.CalledProcessError as e:
            self.say(f"pm: committing {self.db} failed: {e.stderr.strip()}")

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
        """[(priority, task id, subject)]: the `queue` view."""
        return [tuple(r.values()) for r in self.rows("SELECT * FROM queue")]

    # --- git ---------------------------------------------------------------------------------

    def git(self, *args):
        return subprocess.run(["git", *args], cwd=self.root, capture_output=True, text=True, check=True).stdout

    def dirty(self):
        return bool(self.git("status", "--porcelain").strip())

    # --- validate ----------------------------------------------------------------------------

    def validate(self):
        """Every problem in the database file (empty: all good): SQL errors, broken rules, missing references."""
        try:
            with self.connect() as db:
                errors = []
                for table, rowid, parent, _ in db.execute("PRAGMA foreign_key_check").fetchall():
                    row = dict(db.execute(f"SELECT * FROM {table} WHERE rowid = ?", (rowid,)).fetchone())
                    key = ", ".join(f"{k} {v}" for k, v in row.items() if k == "id" or k.endswith("_id"))
                    errors.append(f"{table} ({key}): no such {parent}")
                return errors
        except FormatError as e:
            return [str(e)]

    # --- milestones --------------------------------------------------------------------------

    def close_milestones(self):
        """For each open milestone without an open task: queue the cleanup task in its first plan, or, if its last
        task is that task, mark it implemented. Returns (implemented ids, ids that got the cleanup task)."""
        closed, cleanup = [], []
        with self.connect(write=True) as db:
            for (mid,) in db.execute(
                    "SELECT m.id FROM milestone m WHERE m.status = 'open' "
                    "AND NOT EXISTS (SELECT 1 FROM task t JOIN plan p ON p.id = t.plan_id "
                    "WHERE p.milestone_id = m.id AND t.status = 'open') "
                    "ORDER BY m.id").fetchall():
                last = db.execute("SELECT t.subject FROM task t JOIN plan p ON p.id = t.plan_id "
                                  "WHERE p.milestone_id = ? ORDER BY t.id DESC LIMIT 1", (mid,)).fetchone()
                pid = db.execute("SELECT min(id) FROM plan WHERE milestone_id = ?", (mid,)).fetchone()[0]
                if pid is not None and (last is None or last[0] != CLEANUP_SUBJECT):
                    db.execute("INSERT INTO task (plan_id, subject, body) VALUES (?, ?, ?)",
                               (pid, CLEANUP_SUBJECT, self.prompt("cleanup_prompt")))
                    cleanup.append(mid)
                else:
                    db.execute("UPDATE milestone SET status = 'implemented' WHERE id = ?", (mid,))
                    closed.append(mid)
        return closed, cleanup

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
            self.say(f"\nDecision {d['id']}: {d['subject']}")
            if blocks:
                self.say(f"  blocks task(s) {', '.join(blocks)}")
            if d["body"]:
                self.say("  " + d["body"].replace("\n", "\n  "))
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

    def require(self, table, rid):
        with self.connect() as db:
            self.get(db, table, rid)

    def subject_and_body(self, what, subject):
        """For `pm <what> add`: with a subject, the body from stdin; without, both from the editor (first
        line: subject, the rest: body). None if the editor was quit without saving or saved empty."""
        if subject is not None:
            return subject, sys.stdin.read()
        if not sys.stdin.isatty():
            raise FormatError(f"`pm {what} add` without a subject needs a terminal; give the subject and pipe the "
                              "body")
        text = self.edit(f"PM_{what.upper()}")
        if text is None or not text.strip():
            return None
        subject, _, body = text.strip().partition("\n")
        return subject.strip(), body.strip()

    def prompt(self, key, **values):
        text = self.config[key]
        for k, v in values.items():
            text = text.replace("{" + k + "}", str(v))
        return text.strip()

    def milestone_add(self, subject, body=""):
        """A new milestone with its first plan, the masterplan (a stub), and the task that writes it."""
        with self.connect(write=True) as db:
            mid = db.execute("INSERT INTO milestone (subject, body) VALUES (?, ?)",
                             (subject, (body or "").strip())).lastrowid
            pid = db.execute("INSERT INTO plan (milestone_id, subject, priority, body) "
                             "VALUES (?, 'masterplan', ?, 'tbd.')", (mid, self.config["planning_priority"])).lastrowid
            db.execute("INSERT INTO task (plan_id, subject, body) VALUES (?, 'write the masterplan', ?)",
                       (pid, self.prompt("masterplan_prompt", milestone_id=mid, milestone_subject=subject, plan_id=pid)))
        return mid

    def milestone_set(self, mid, **fields):
        """Set fields of a milestone (subject, status, body); None leaves a field as it is."""
        with self.connect(write=True) as db:
            self.get(db, "milestone", mid)
            for k, v in fields.items():
                if v is not None:
                    db.execute(f"UPDATE milestone SET {k} = ? WHERE id = ?", (v.strip(), mid))

    def queue_plan_tasks(self, db, pid):
        """Queue the task that splits plan pid into tasks, unless one is still open."""
        p = self.get(db, "plan", pid)
        subject = PLAN_TASKS_SUBJECT.format(pid)
        if not db.execute("SELECT 1 FROM task WHERE plan_id = ? AND subject = ? AND status = 'open'",
                          (pid, subject)).fetchone():
            db.execute("INSERT INTO task (plan_id, subject, priority, body) VALUES (?, ?, ?, ?)",
                       (pid, subject, self.config["planning_priority"],
                        self.prompt("plan_tasks_prompt", plan_id=pid, plan_subject=p["subject"])))

    def plan_add(self, mid, subject, body, prio=None):
        with self.connect(write=True) as db:
            self.get(db, "milestone", mid)
            pid = db.execute("INSERT INTO plan (milestone_id, subject, priority, body) VALUES (?, ?, ?, ?)",
                             (mid, subject, prio, body.strip())).lastrowid
            self.queue_plan_tasks(db, pid)
        return pid

    def plan_edit(self, pid, subject=None, body=None, prio=None):
        """Change a plan; a new subject or body queues the task that splits it into tasks again.
        prio "none" clears the priority."""
        with self.connect(write=True) as db:
            self.get(db, "plan", pid)
            if prio is not None:
                db.execute("UPDATE plan SET priority = ? WHERE id = ?", (None if prio == "none" else prio, pid))
            for k, v in (("subject", subject), ("body", body)):
                if v is not None:
                    db.execute(f"UPDATE plan SET {k} = ? WHERE id = ?", (v.strip(), pid))
            if subject is not None or body is not None:
                self.queue_plan_tasks(db, pid)

    def task_add(self, pid, subject, body, prio=None):
        with self.connect(write=True) as db:
            self.get(db, "plan", pid)
            return db.execute("INSERT INTO task (plan_id, subject, priority, body) VALUES (?, ?, ?, ?)",
                              (pid, subject, prio, body.strip())).lastrowid

    def task_set(self, tid, **fields):
        """Set fields of a task (status, note, priority); a value None clears it."""
        with self.connect(write=True) as db:
            self.get(db, "task", tid)
            for k, v in fields.items():
                db.execute(f"UPDATE task SET {k} = ? WHERE id = ?", (v, tid))

    def task_pending_decision(self, tid, did):
        with self.connect(write=True) as db:
            self.block(db, tid, did)

    def decision_add(self, subject, body=None, options=()):
        with self.connect(write=True) as db:
            return db.execute("INSERT INTO decision (subject, options, body) VALUES (?, ?, ?)",
                              (subject, json.dumps(list(options), ensure_ascii=False), (body or "").strip())).lastrowid

    # --- show --------------------------------------------------------------------------------

    def show(self, tid):
        """The task with its plan and milestone: the context a worker gets."""
        with self.connect() as db:
            t = self.get(db, "task", tid)
            p = self.get(db, "plan", t["plan_id"])
            m = self.get(db, "milestone", p["milestone_id"])
            plans = db.execute("SELECT id, subject FROM plan WHERE milestone_id = ? ORDER BY id", (m["id"],)).fetchall()
            siblings = db.execute("SELECT id, status, subject FROM task WHERE plan_id = ? ORDER BY id", (p["id"],)).fetchall()
            ds = db.execute("SELECT d.* FROM task_decision td JOIN decision d ON d.id = td.decision_id "
                            "WHERE td.task_id = ? ORDER BY d.id", (tid,)).fetchall()
        prio = t["priority"] if t["priority"] is not None else p["priority"] or 0
        out = [f"# Milestone {m['id']}: {m['subject']} ({m['status']}, created {m['created_date']})", "", m["body"],
               "", "Plans: " + ", ".join(f"{x['id']} {x['subject']}" for x in plans), "",
               f"# Plan {p['id']}: {p['subject']} (priority {p['priority'] or 0})", "", p["body"], ""]
        out += [f"- task {x['id']} [{x['status']}] {x['subject']}" + (" ← this task" if x["id"] == tid else "")
                for x in siblings] + [""]
        out += [f"# Task {tid}: {t['subject']} ({t['status']}, priority {prio})", ""]
        if t["note"]:
            out.append(f"note: {t['note']}")
        out += [t["body"], ""]
        for d in ds:
            said = d["answer"] or d["reason"] or ""
            out.append(f"Decision {d['id']} ({d['status']}): {d['subject']}" + (f" → {said}" if said else ""))
        return "\n".join(out).rstrip() + "\n"

    # --- run ---------------------------------------------------------------------------------

    def plans(self):
        """[(priority, plan id, subject)] of plans with an open task in an open milestone, highest priority first."""
        return [tuple(r.values()) for r in self.rows(
            "SELECT coalesce(p.priority, 0) AS priority, p.id, p.subject FROM plan p "
            "JOIN milestone m ON m.id = p.milestone_id WHERE m.status = 'open' "
            "AND EXISTS (SELECT 1 FROM task t WHERE t.plan_id = p.id AND t.status = 'open') "
            "ORDER BY priority DESC, m.id, p.id")]

    def priority_prompts(self, tid):
        """The priority instructions for task tid: for plans in a masterplan's tasks (they write plans), for tasks
        in "create tasks for plan N"."""
        with self.connect() as db:
            t = self.get(db, "task", tid)
            first = db.execute("SELECT min(q.id) FROM plan p JOIN plan q ON q.milestone_id = p.milestone_id "
                               "WHERE p.id = ?", (t["plan_id"],)).fetchone()[0]
        keys = (["plan_priority_prompt"] if t["plan_id"] == first else []) + \
               (["task_priority_prompt"] if t["subject"] == PLAN_TASKS_SUBJECT.format(t["plan_id"]) else [])
        return [text for k in keys if (text := self.prompt(k))]

    def worker_command(self, tid):
        show = "\n".join([self.show(tid), *self.priority_prompts(tid)]).rstrip() + "\n"
        prompt = WORKER_PROMPT.format(id=tid, db=self.db, show=show)
        return [a.replace("{prompt}", prompt) for a in self.config["worker"]]

    @property
    def logs(self):
        """log_dir; relative: inside the git dir, so never tracked."""
        return Path(self.git("rev-parse", "--absolute-git-dir").strip()) / self.config["log_dir"]

    def run_worker(self, cmd, log):
        """Run the worker, copying its output to the terminal and the log. Returns the exit code."""
        log.parent.mkdir(parents=True, exist_ok=True)
        with open(log, "w", encoding="utf-8") as f:
            with subprocess.Popen(cmd, cwd=self.root, env=os.environ | {"PM_WORKER": "1"}, stdout=subprocess.PIPE,
                                  stderr=subprocess.STDOUT, text=True, bufsize=1) as proc:
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
        with self.connect() as db:
            t = self.get(db, "task", tid)
            blocked = [str(r[0]) for r in db.execute(
                "SELECT d.id FROM task_decision td JOIN decision d ON d.id = td.decision_id "
                "WHERE td.task_id = ? AND d.status = 'pending' ORDER BY d.id", (tid,))]
        if t["status"] != "open":
            result = "done", t["status"] + (f": {t['note']}" if t["note"] else "")
        elif blocked:
            result = "blocked", "waits for decision(s) " + ", ".join(blocked)
        else:
            return "failed", f"exit 0, but task {tid} is still open and not blocked\n{tail}"
        try:
            self.commit_task(tid, t["subject"], result[1])
        except subprocess.CalledProcessError as e:
            return "failed", f"git commit failed:\n{(e.stdout or '') + (e.stderr or '')}".rstrip()
        if left := self.git("status", "--porcelain").rstrip():
            return "failed", f"committed task {tid}; the worker left changes that are not staged:\n{left}"
        return result

    def commit_task(self, tid, subject, body):
        """Commit what the worker staged, with the database, as "task <id>: <subject>"."""
        self.git("add", "--", self.db)
        if subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=self.root).returncode:
            self.git("commit", "-q", "-m", f"task {tid}: {subject}", "-m", body)

    def close_and_commit(self):
        closed, cleanup = self.close_milestones()
        done = [f"milestone {mid}: {CLEANUP_SUBJECT} queued" for mid in cleanup] + \
               [f"milestone {mid} implemented" for mid in closed]
        for line in done:
            self.say(line)
        if done:
            self.commit_db(", ".join(done))

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
            self.close_and_commit()
            if interactive and self.pending() and self.decide(ask):
                self.commit_db("decisions answered")
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
            self.close_and_commit()
            if once:
                return 0

    # --- status ------------------------------------------------------------------------------

    def status(self):
        for m in self.rows(
                "SELECT m.id, m.subject, m.created_date, "
                "(SELECT count(*) FROM plan p WHERE p.milestone_id = m.id) AS plans, "
                "(SELECT count(*) FROM task t JOIN plan p ON p.id = t.plan_id WHERE p.milestone_id = m.id) AS tasks, "
                "(SELECT count(*) FROM task t JOIN plan p ON p.id = t.plan_id "
                "WHERE p.milestone_id = m.id AND t.status <> 'open') AS closed "
                "FROM milestone m WHERE m.status = 'open' ORDER BY m.id"):
            self.say(f"Milestone {m['id']}: {m['subject']} (created {m['created_date']}): {m['plans']} plan(s), "
                     f"{m['closed']}/{m['tasks']} tasks closed")
        self.print_queue(limit=10)
        pending = self.pending()
        self.say(f"\nPending decisions ({len(pending)}):")
        for d in pending:
            self.say(f"  {d['id']}: {d['subject']}")

    def print_plans(self, new=None, file=None):
        """`pm plan list`: id, priority, subject; `new` marked."""
        lines = ["  id   prio  subject"] + [f"{pid:4d}  {prio:5d}  {subject}" + ("   ← new" if pid == new else "")
                                          for prio, pid, subject in self.plans()]
        print("\n".join(lines), file=file or self.out, flush=True)

    def print_queue(self, limit=None):
        q = self.queue()
        self.say(f"\nQueue ({len(q)}):")
        for prio, tid, subject in q[:limit]:
            self.say(f"  {prio:+6d}  task {tid}: {subject}" if prio else f"          task {tid}: {subject}")
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
    ap = argparse.ArgumentParser(prog="pm", description=__doc__, epilog=EPILOG,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", help="the project's git repository (default: the one around the current directory)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name, text in [("init", f"write {CONFIG_FILE} and the database (missing ones only) and commit them"),
                       ("status", "open milestones, the queue, pending decisions"), ("queue", "the whole queue"),
                       ("validate", "check the database"),
                       ("decide", "answer pending decisions in the terminal"),
                       ("instructions", "the full reference for agents: every command with examples, recipes")]:
        sub.add_parser(name, help=text)
    s = sub.add_parser("show", help="a task with its plan, milestone and decisions")
    s.add_argument("task", type=int)
    r = sub.add_parser("run", help="work the queue with the worker command (claude -p)")
    r.add_argument("--once", action="store_true", help="stop after one task")
    r.add_argument("--dry-run", action="store_true", help="print the next worker command; change nothing")

    ms = sub.add_parser("milestone", help="add or change milestones").add_subparsers(dest="action", required=True)
    ma = ms.add_parser("add", help="new open milestone; prints its id. Without a subject: write it in your editor "
                       "(first line: subject, the rest: body); with a subject: the body is read from stdin")
    ma.add_argument("subject", nargs="?")
    mset = ms.add_parser("set", help="set milestone fields")
    mset.add_argument("milestone", type=int)
    mset.add_argument("--subject")
    mset.add_argument("--status", choices=MILESTONE_STATES)
    mset.add_argument("--body", type=text_arg)

    p = sub.add_parser("plan", aliases=["plans"], help="add, change or list plans").add_subparsers(
        dest="action", required=True)
    pa = p.add_parser("add", help="new plan; prints its id and queues the task that splits it into tasks")
    pa.add_argument("milestone", type=int)
    pa.add_argument("subject", nargs="?", help="then the body (Markdown) is read from stdin; without: editor")
    pa.add_argument("--priority", type=int)
    pe = p.add_parser("edit", help="change a plan; a new subject or body queues the task that splits it again")
    pe.add_argument("plan", type=int)
    pe.add_argument("--subject")
    pe.add_argument("--body", type=text_arg)
    pe.add_argument("--priority", type=lambda v: v if v == "none" else int(v), help="integer, or none for 0")
    p.add_parser("list", help="plans with open tasks in open milestones, one per line: priority, id, subject; highest "
                  "priority first")

    t = sub.add_parser("task", help="add or change tasks").add_subparsers(dest="action", required=True)
    a = t.add_parser("add", help="new task; prints its id")
    a.add_argument("plan", type=int)
    a.add_argument("subject", nargs="?", help="then the body (Markdown) is read from stdin; without: editor")
    a.add_argument("--priority", type=int)
    for verb, text in [("done", "note: date, files or commit"), ("drop", "reason")]:
        x = t.add_parser(verb)
        x.add_argument("task", type=int)
        x.add_argument("note", help=text)
    tp = t.add_parser("priority", help="integer, or none for the plan's")
    tp.add_argument("task", type=int)
    tp.add_argument("priority", type=prio_arg)

    d = sub.add_parser("decision", help="ask the human").add_subparsers(dest="action", required=True)
    da = d.add_parser("add", help="new pending decision; prints its id")
    da.add_argument("subject", help="the question")
    da.add_argument("--body", type=text_arg, help="context for the human")
    da.add_argument("--option", action="append", default=[], help="repeat; the recommended one first")
    tpd = sub.add_parser("task-pending-decision", help="the task waits until the decision is answered")
    tpd.add_argument("task", type=int)
    tpd.add_argument("decision", type=int)

    args = ap.parse_args(argv)
    if args.cmd == "instructions":
        print(INSTRUCTIONS.read_text(encoding="utf-8"), end="")
        root = args.root or git_root()
        try:
            pm = Project(root, load_config(root)) if root else Project(".")
        except FormatError:
            pm = Project(".")
        texts = [t for k in PRIORITY_KEYS if (t := pm.prompt(k))]
        if texts:
            print("\n## Priorities (project-manager.toml)\n\n" + "\n\n".join(texts))
        return 0
    root = args.root or git_root()
    if root is None:
        print("pm: not inside a git repository (or pass --root)")
        return 2
    try:
        if args.cmd == "init":
            init(root)
            return 0
        pm = Project(root, load_config(root))
        if args.cmd == "validate":
            errors = pm.validate()
            print("\n".join(errors) if errors else f"{pm.db} is valid")
            return 1 if errors else 0
        if args.cmd != "run" and (errors := pm.validate()):
            print("the database has errors (`pm validate`):\n  " + "\n  ".join(errors))
            return 1
        msg = None
        if args.cmd == "status":
            pm.status()
        elif args.cmd == "queue":
            pm.print_queue()
        elif args.cmd == "show":
            print(pm.show(args.task), end="")
        elif args.cmd == "decide":
            msg = pm.decide() and "decisions answered"
        elif args.cmd == "run":
            return pm.run(once=args.once, dry_run=args.dry_run)
        elif args.cmd == "milestone":
            if args.action == "add":
                if not (entry := pm.subject_and_body("milestone", args.subject)):
                    print("pm: aborted, no milestone added")
                    return 1
                mid = pm.milestone_add(*entry)
                print(mid)
                msg = f"milestone {mid} added: {entry[0]}"
            else:
                pm.milestone_set(args.milestone, subject=args.subject, status=args.status, body=args.body)
                msg = f"milestone {args.milestone} changed"
        elif args.cmd in ("plan", "plans"):
            if args.action == "list":
                pm.print_plans()
            elif args.action == "add":
                pm.require("milestone", args.milestone)
                if not (entry := pm.subject_and_body("plan", args.subject)):
                    print("pm: aborted, no plan added")
                    return 1
                pid = pm.plan_add(args.milestone, *entry, args.priority)
                print(pid)
                pm.print_plans(new=pid, file=sys.stderr)
                print(f"Compare plan {pid} with these plans. If it must clearly run before or after one of them, "
                      f"change its priority: pm plan edit {pid} --priority N", file=sys.stderr)
                msg = f"plan {pid} added: {entry[0]}"
            else:
                pm.plan_edit(args.plan, args.subject, args.body, args.priority)
                msg = f"plan {args.plan} changed"
        elif args.cmd == "task":
            if args.action == "add":
                pm.require("plan", args.plan)
                if not (entry := pm.subject_and_body("task", args.subject)):
                    print("pm: aborted, no task added")
                    return 1
                tid = pm.task_add(args.plan, *entry, args.priority)
                print(tid)
                msg = f"task {tid} added: {entry[0]}"
            elif args.action in ("done", "drop"):
                pm.task_set(args.task, status="done" if args.action == "done" else "dropped", note=args.note)
                msg = f"task {args.task} {args.action}"
            else:
                pm.task_set(args.task, priority=args.priority)
                msg = f"task {args.task} priority"
        elif args.cmd == "decision":
            did = pm.decision_add(args.subject, args.body, args.option)
            print(did)
            msg = f"decision {did} added: {args.subject}"
        elif args.cmd == "task-pending-decision":
            pm.task_pending_decision(args.task, args.decision)
            msg = f"task {args.task} waits for decision {args.decision}"
        if msg:
            pm.commit_db(msg)
        return 0
    except FormatError as e:
        print(f"pm: {e}")
        return 1
    except BrokenPipeError:  # `pm queue | head`
        return 0
    except KeyboardInterrupt:
        print("\nStopped. `pm run` continues with the same task.")
        return 130

