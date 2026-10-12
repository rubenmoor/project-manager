"""pm: a project manager for Claude Code workers (README.md). Milestones -> plans -> tasks, plus decisions for the
human, in SQLite: the project keeps them as SQL text in tasks.sql, tracked by git, which every command loads into an
in-memory database (the schema is SCHEMA below). A new milestone comes with a first plan, the masterplan (body
"tbd."), and a task that writes it; a new or edited plan queues a task that splits it into tasks; edits cascade: a
changed milestone queues the revision of its masterplan, a changed plan that of the plans below it and of its open
tasks; implemented milestones, done plans and closed tasks can't be changed; a milestone whose tasks are all closed
gets a last one, the post-milestone cleanup, before it is implemented; a milestone on hold keeps its tasks out of
the queue until it is resumed. The queue (the `queue` view): open tasks of
open milestones that no pending decision blocks, highest priority first, then by milestone, plan and task.
Worked one task at a time by `claude -p`. Standard library only (Python 3.11+).

    pm init | status | queue | show <task> | validate | decide | run [--once] [--dry-run] | instructions
    pm milestone add|set|hold|resume …   pm plan add|edit|list …   pm task add|done|drop|priority …
    pm decision add|list|answer|approve|irrelevant …   pm task-pending-decision <task> <decision>

Works on the git repository around the current directory. `pm init` writes the settings, project-manager.toml (the
template is the file of that name next to this one), and commits it; the first command that uses the database
writes it. pm commits its own changes to tasks.sql; workers commit them with their work. Nothing is pushed.
"""
import argparse, collections, contextlib, datetime, fcntl, json, os, re, shlex, signal, sqlite3, subprocess, sys, \
    time, tomllib, zoneinfo
from pathlib import Path


VERSION = 1  # bump by one with every commit (AGENTS.md)
CONFIG_FILE = "project-manager.toml"
CONFIG_TEMPLATE = (Path(__file__).parent / CONFIG_FILE).read_text(encoding="utf-8")
INSTRUCTIONS = Path(__file__).parent / "instructions.md"
GUARD = Path(__file__).parent / "guard.py"
# In {settings}: `claude -p` ends with the turn, so a background job dies unwatched; long checks run in the foreground.
WORKER_ENV = {"CLAUDE_CODE_DISABLE_BACKGROUND_TASKS": "1", "BASH_MAX_TIMEOUT_MS": "3600000"}
EPILOG = "Agents (LLMs): read `pm instructions` first, the full reference with examples for every command."
PROMPT_KEYS = ("masterplan_prompt", "plan_tasks_prompt", "revise_masterplan_prompt", "revise_plan_prompt",
               "cleanup_prompt")
PRIORITY_KEYS = ("plan_priority_prompt", "task_priority_prompt")
CLEANUP_SUBJECT = "post-milestone cleanup"
PLAN_TASKS_SUBJECT = "create tasks for plan {}"
REVISE_MASTERPLAN_SUBJECT = "revise the masterplan"
REVISE_PLAN_SUBJECT = "revise plan {}"
# Dedupes queued planning tasks: a running one may have read the old text already, so it doesn't count.
NOT_RUNNING = "AND id NOT IN (SELECT task_id FROM run WHERE result = 'running')"
MILESTONE_STATES = ("open", "on-hold", "implemented")
# work: the default; plan: writes a plan; masterplan, split ("create tasks for plan N"), cleanup: made by pm only
TASK_KINDS = ("work", "plan", "masterplan", "split", "cleanup")
PLANNING_KINDS = ("plan", "masterplan", "split")
# `signoff` in project-manager.toml → decision.signoff; a sign-off's only answer is SIGNOFF_ANSWER
SIGNOFF_LEVELS = {"masterplans": "masterplan", "plans": "plan", "task-queue": "task-queue"}
SIGNOFF_ANSWER = "approve"

SCHEMA_VERSION = 12
# Timestamps: UTC, "2026-10-11T16:30:00Z". created_by_task: the task whose worker created the row; NULL: the human or pm.
SCHEMA = """
CREATE TABLE milestone (
    id             INTEGER PRIMARY KEY,
    subject        TEXT NOT NULL CHECK (trim(subject) <> ''),
    status         TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'on-hold', 'implemented')),
    body           TEXT NOT NULL DEFAULT '',               -- Markdown
    created_at     TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    implemented_at TEXT,
    CONSTRAINT "implemented_at is set exactly when implemented" CHECK ((status = 'implemented') = (implemented_at IS NOT NULL))
) STRICT;

CREATE TABLE plan (
    id              INTEGER PRIMARY KEY,
    milestone_id    INTEGER NOT NULL REFERENCES milestone (id),
    subject         TEXT NOT NULL CHECK (trim(subject) <> ''),
    priority        INTEGER,                               -- NULL: 0
    body            TEXT NOT NULL CONSTRAINT "a plan needs a body" CHECK (trim(body) <> ''),
    created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    created_by_task INTEGER REFERENCES task (id)
) STRICT;

CREATE TABLE task (
    id              INTEGER PRIMARY KEY,
    plan_id         INTEGER NOT NULL REFERENCES plan (id),
    subject         TEXT NOT NULL CHECK (trim(subject) <> ''),
    kind            TEXT NOT NULL DEFAULT 'work' CHECK (kind IN ('work', 'plan', 'masterplan', 'split', 'cleanup')),
    status          TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'done', 'dropped')),
    priority        INTEGER,                               -- NULL: the plan's
    note            TEXT,                                  -- done: date, files; dropped: reason
    body            TEXT NOT NULL DEFAULT '',              -- Markdown
    created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    retired_at      TEXT,                                  -- done or dropped
    created_by_task INTEGER REFERENCES task (id),
    CONSTRAINT "an open task needs a body" CHECK (status <> 'open' OR trim(body) <> ''),
    CONSTRAINT "retired_at is set exactly when done or dropped" CHECK ((status = 'open') = (retired_at IS NULL))
) STRICT;

CREATE TABLE decision (
    id              INTEGER PRIMARY KEY,
    subject         TEXT NOT NULL CHECK (trim(subject) <> ''),
    status          TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'answered', 'irrelevant')),
    options         TEXT NOT NULL DEFAULT '[]' CHECK (json_type(options) = 'array'),  -- recommended first
    body            TEXT NOT NULL DEFAULT '',
    answer          TEXT,
    reason          TEXT,
    created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    closed_at       TEXT,
    created_by_task INTEGER REFERENCES task (id),
    signoff         TEXT CHECK (signoff IN ('masterplan', 'plan', 'task-queue')),  -- made by pm; NULL: a question
    CONSTRAINT "a sign-off is only approved"
    CHECK (signoff IS NULL OR status <> 'irrelevant' AND coalesce(answer, 'approve') = 'approve'),
    CONSTRAINT "pending: no answer, reason or closed_at; answered: answer and closed_at; irrelevant: reason and closed_at"
    CHECK (status = 'pending' AND answer IS NULL AND reason IS NULL AND closed_at IS NULL
           OR status = 'answered' AND answer IS NOT NULL AND closed_at IS NOT NULL
           OR status = 'irrelevant' AND reason IS NOT NULL AND closed_at IS NOT NULL)
) STRICT;

CREATE TABLE run (                                         -- one worker run of `pm run`
    id                    INTEGER PRIMARY KEY,
    task_id               INTEGER NOT NULL REFERENCES task (id),
    started_at            TEXT NOT NULL,
    ended_at              TEXT,                            -- NULL: running
    result                TEXT NOT NULL CHECK (result IN ('running', 'done', 'blocked', 'failed', 'limit')),
    reason                TEXT,                            -- failed: why; limit: the message
    exit_code             INTEGER,
    base_commit           TEXT,                            -- HEAD when the run started
    model                 TEXT,                            -- from here on: the worker's JSON output, if any
    session_id            TEXT,
    turns                 INTEGER,
    input_tokens          INTEGER,
    output_tokens         INTEGER,
    cache_read_tokens     INTEGER,
    cache_creation_tokens INTEGER,
    cost_usd              REAL,
    pid                   INTEGER,                         -- the `pm run` process
    CONSTRAINT "ended_at is set exactly when not running" CHECK ((result = 'running') = (ended_at IS NULL))
) STRICT;

CREATE TABLE task_decision (                               -- the task waits while the decision is pending
    task_id     INTEGER NOT NULL REFERENCES task (id),
    decision_id INTEGER NOT NULL REFERENCES decision (id),
    PRIMARY KEY (task_id, decision_id)
) STRICT;

CREATE INDEX plan_milestone ON plan (milestone_id);
CREATE INDEX task_plan ON task (plan_id);
CREATE INDEX task_decision_decision ON task_decision (decision_id);
CREATE INDEX run_task ON run (task_id);
CREATE UNIQUE INDEX run_running ON run (result) WHERE result = 'running';  -- at most one

CREATE VIEW queue AS
SELECT coalesce(t.priority, p.priority, 0) AS priority, t.id AS task_id, t.subject, t.kind
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
  pm task add <plan id> "<subject>" [--kind plan] [--priority N] < body.md     --kind plan: the task writes a plan
  pm decision add "<subject>" [--body "<text>"] [--option "<recommended>"] [--option …]   prints its id
  pm task-pending-decision <task id> <decision id>     the task waits until the human answers
  pm show <task id>
Every command with examples, and recipes for the usual tasks: `pm instructions`.

Stop processes you started by PID (`kill $!`), never with `pkill -f` or `killall`: the pattern can match your own \
process. Run long checks in the foreground (a Bash call with a timeout): your session ends with your turn, \
and nothing wakes you later.

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


def now():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def creator():
    """The task whose worker runs this command (PM_TASK, set by `pm run`), or None."""
    return int(os.environ["PM_TASK"]) if os.environ.get("PM_TASK", "").isdigit() else None


def stamp(ts):
    """"2026-10-11 16:30" (UTC) from a timestamp."""
    return ts[:16].replace("T", " ") if ts else ""


def seconds(start, end):
    return int((datetime.datetime.fromisoformat(end or now()) - datetime.datetime.fromisoformat(start)).total_seconds())


def duration(secs):
    """"4m12s", "2h05m"."""
    secs = int(secs)
    return f"{secs // 3600}h{secs // 60 % 60:02d}m" if secs >= 3600 else f"{secs // 60}m{secs % 60:02d}s"



def exit_signal(code):
    """The signal that ended a process with this exit code (negative: Popen's; 129-159: a shell's 128 + n), or None."""
    n = -code if code is not None and code < 0 else code - 128 if code is not None and 128 < code < 160 else None
    try:
        return signal.Signals(n) if n else None
    except ValueError:
        return None


def exit_text(code):
    """"killed by SIGTERM (exit 143)", "exit 1"."""
    sig = exit_signal(code)
    return f"killed by {sig.name} (exit {code})" if sig else f"exit {code}"


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
    if not isinstance(w, list) or not w or not all(isinstance(a, str) for a in w):
        errors.append("worker must be a non-empty list of strings")
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
    s = config["signoff"]
    if not isinstance(s, list) or not all(isinstance(x, str) and x in SIGNOFF_LEVELS for x in s):
        errors.append("signoff must be a list of " + ", ".join(f'"{x}"' for x in SIGNOFF_LEVELS))
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



class Project:
    def __init__(self, root, config=None, out=sys.stdout):
        self.root = Path(root)
        self.config = config or tomllib.loads(CONFIG_TEMPLATE)
        self.db = self.config["database"]
        self.db_path = self.root / self.db
        self.out = out
        self.stopping = False       # `pm run` stops after the current task

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
        """[(priority, task id, subject, kind)]: the `queue` view."""
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
        """For each open milestone without an open task: queue the cleanup task in its first plan, or, if it had one
        already, mark it implemented. Returns (implemented ids, ids that got the cleanup task)."""
        closed, cleanup = [], []
        with self.connect(write=True) as db:
            for (mid,) in db.execute(
                    "SELECT m.id FROM milestone m WHERE m.status = 'open' "
                    "AND NOT EXISTS (SELECT 1 FROM task t JOIN plan p ON p.id = t.plan_id "
                    "WHERE p.milestone_id = m.id AND t.status = 'open') "
                    "ORDER BY m.id").fetchall():
                done = db.execute("SELECT 1 FROM task t JOIN plan p ON p.id = t.plan_id "
                                  "WHERE p.milestone_id = ? AND t.kind = 'cleanup'", (mid,)).fetchone()
                pid = db.execute("SELECT min(id) FROM plan WHERE milestone_id = ?", (mid,)).fetchone()[0]
                if pid is not None and not done:
                    db.execute("INSERT INTO task (plan_id, subject, kind, body, created_by_task) "
                               "VALUES (?, ?, 'cleanup', ?, ?)", (pid, CLEANUP_SUBJECT, self.prompt("cleanup_prompt"),
                                                                 creator()))
                    cleanup.append(mid)
                else:
                    db.execute("UPDATE milestone SET status = 'implemented', implemented_at = ? WHERE id = ?",
                               (now(), mid))
                    closed.append(mid)
        return closed, cleanup

    # --- decisions ---------------------------------------------------------------------------

    def resolve(self, did, answer=None, irrelevant=None):
        """Close a pending decision. Answered: its tasks unblock by themselves. Irrelevant: the open tasks it
        blocks are dropped."""
        with self.connect(write=True) as db:
            d = self.get(db, "decision", did)
            if d["status"] != "pending":
                raise FormatError(f"decision {did} is not pending")
            if d["signoff"] and (irrelevant is not None or answer != SIGNOFF_ANSWER):
                raise FormatError(f"decision {did} is a sign-off: its only answer is {SIGNOFF_ANSWER!r}")
            if irrelevant is None:
                db.execute("UPDATE decision SET status = 'answered', answer = ?, closed_at = ? WHERE id = ?",
                           (answer, now(), did))
                return
            db.execute("UPDATE decision SET status = 'irrelevant', reason = ?, closed_at = ? WHERE id = ?",
                       (irrelevant, now(), did))
            db.execute("UPDATE task SET status = 'dropped', note = ?, retired_at = ? WHERE status = 'open' "
                       "AND id IN (SELECT task_id FROM task_decision WHERE decision_id = ?)",
                       (f"decision {did} irrelevant", now(), did))

    def decisions(self):
        """`pm decision list`: every pending decision with what it blocks, its context and options."""
        return self.decisions_of(self.pending()) or "No pending decisions.\n"

    def decisions_of(self, ds):
        out = []
        for d in ds:
            out.append(f"Decision {d['id']}{' (sign-off)' if d['signoff'] else ''}: {d['subject']}")
            if d["signoff"]:
                out.append("  " + self.signoff_view(d["id"], d["signoff"]).replace("\n", "\n  "))
                out.append(f"  pm decision approve {d['id']}\n")
                continue
            blocks = [str(r["id"]) for r in self.rows(
                "SELECT t.id FROM task_decision td JOIN task t ON t.id = td.task_id "
                "WHERE td.decision_id = ? AND t.status = 'open' ORDER BY t.id", d["id"])]
            if blocks:
                out.append(f"  blocks task(s) {', '.join(blocks)}")
            if d["body"]:
                out.append("  " + d["body"].replace("\n", "\n  "))
            for n, o in enumerate(d["options"], 1):
                out.append(f"  {n}) {o}{'  (recommended)' if n == 1 else ''}")
            out.append("")
        return "\n".join(out)

    def decide(self, ask=input):
        """`pm decide`: ask about every pending decision in the terminal; returns how many were closed."""
        closed = 0
        for d in self.pending():
            self.say("\n" + self.decisions_of([d]).rstrip("\n"))
            if d["signoff"]:
                if ask(f"Answer [a = {SIGNOFF_ANSWER}, s = skip]: ").strip().lower() in ("a", SIGNOFF_ANSWER):
                    self.resolve(d["id"], answer=SIGNOFF_ANSWER)
                    closed += 1
                continue
            hint = f"1-{len(d['options'])}, " if d["options"] else ""
            reply = ask(f"Answer [{hint}your own text, s = skip, x <reason> = irrelevant]: ").strip()
            if not reply or reply.lower() == "s":
                continue
            if reply.lower() == "x" or reply.lower().startswith("x "):
                self.resolve(d["id"], irrelevant=reply[1:].strip() or "no reason given")
            else:
                self.answer(d["id"], reply)
            closed += 1
        return closed

    def answer(self, did, reply):
        """Answer decision did: an option's number or own text. Returns the answer."""
        with self.connect() as db:
            opts = json.loads(self.get(db, "decision", did)["options"])
        if reply.isdigit() and 1 <= int(reply) <= len(opts):
            reply = opts[int(reply) - 1]
        self.resolve(did, answer=reply)
        return reply

    # --- sign-offs ---------------------------------------------------------------------------

    def signoff_level(self, db, t):
        """The sign-off task t needs under `signoff` in project-manager.toml, or None."""
        if t["kind"] == "split":
            master = db.execute("SELECT 1 FROM task WHERE plan_id = ? AND kind = 'masterplan'", (t["plan_id"],))
            level = "masterplan" if master.fetchone() else "plan"
        else:
            level = "task-queue" if t["kind"] == "work" else None
        return level if level in [SIGNOFF_LEVELS[x] for x in self.config["signoff"]] else None

    @staticmethod
    def signed_off(db, tid, level):
        return db.execute("SELECT 1 FROM task_decision td JOIN decision d ON d.id = td.decision_id "
                          "WHERE td.task_id = ? AND d.signoff = ? AND d.status = 'answered'", (tid, level)).fetchone()

    def needs_signoff(self, tid):
        """The sign-off task tid needs and doesn't have yet, or None."""
        with self.connect() as db:
            level = self.signoff_level(db, self.get(db, "task", tid))
            return level if level and not self.signed_off(db, tid, level) else None

    def request_signoff(self, tid):
        """Make task tid wait for its sign-off. Plan or masterplan: a new decision. Task queue: the pending one, else a
        new one, for every open work task of open milestones without a sign-off. Returns the decision id."""
        with self.connect(write=True) as db:
            t = self.get(db, "task", tid)
            level = self.signoff_level(db, t)
            did = None
            if level == "task-queue":
                did = db.execute("SELECT min(id) FROM decision WHERE signoff = 'task-queue' AND status = 'pending'"
                                 ).fetchone()[0]
                tids = [r[0] for r in db.execute(
                    "SELECT t.id FROM task t JOIN plan p ON p.id = t.plan_id JOIN milestone m ON m.id = p.milestone_id "
                    "WHERE m.status = 'open' AND t.status = 'open' AND t.kind = 'work' ORDER BY t.id").fetchall()
                        if not self.signed_off(db, r[0], level)]
                subject = "Sign off the task queue"
            else:
                p = self.get(db, "plan", t["plan_id"])
                tids = [tid]
                subject = (f"Sign off the masterplan of milestone {p['milestone_id']}" if level == "masterplan"
                           else f"Sign off plan {p['id']}: {p['subject']}")
            if did is None:
                did = db.execute("INSERT INTO decision (subject, options, signoff) VALUES (?, ?, ?)",
                                 (subject, json.dumps([SIGNOFF_ANSWER]), level)).lastrowid
            for x in tids:
                self.block(db, x, did)
        return did

    def signoff_view(self, did, level):
        """What a pending sign-off approves, as it is now: the plan, or the waiting tasks by plan."""
        ts = self.rows("SELECT t.id, t.subject, p.id AS plan_id, p.subject AS plan_subject, p.body AS plan_body "
                       "FROM task_decision td JOIN task t ON t.id = td.task_id JOIN plan p ON p.id = t.plan_id "
                       "WHERE td.decision_id = ? AND t.status = 'open' ORDER BY p.id, t.id", did)
        if not ts:
            return "No open task waits for it."
        if level != "task-queue":
            return "\n\n".join(f"# Plan {t['plan_id']}: {t['plan_subject']}\n\n{t['plan_body']}" for t in ts)
        out, plan = [], None
        for t in ts:
            if t["plan_id"] != plan:
                plan = t["plan_id"]
                out.append(f"Plan {plan}: {t['plan_subject']}")
            out.append(f"  task {t['id']}: {t['subject']}")
        return "\n".join(out)

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
            pid = db.execute("INSERT INTO plan (milestone_id, subject, priority, body, created_by_task) "
                             "VALUES (?, 'masterplan', ?, 'tbd.', ?)",
                             (mid, self.config["planning_priority"], creator())).lastrowid
            db.execute("INSERT INTO task (plan_id, subject, kind, body, created_by_task) "
                       "VALUES (?, 'write the masterplan', 'masterplan', ?, ?)",
                       (pid, self.prompt("masterplan_prompt", milestone_id=mid, milestone_subject=subject, plan_id=pid),
                        creator()))
        return mid

    def milestone_set(self, mid, **fields):
        """Set fields of a milestone (subject, status, body); None leaves a field as it is. A changed subject or body
        queues the revision of the masterplan; refused once the milestone is implemented or every plan of it is done.
        Returns the queued tasks: [(id, subject)]."""
        with self.connect(write=True) as db:
            m = self.get(db, "milestone", mid)
            changed = any(fields.get(k) is not None and fields[k].strip() != m[k] for k in ("subject", "body"))
            master = self.masterplan(db, mid)
            if changed and (m["status"] == "implemented" or master and self.plan_done(db, master)):
                why = "implemented" if m["status"] == "implemented" else "done: every plan of it is done"
                raise FormatError(f"milestone {mid} is {why}; its subject and body can't be changed. "
                                  f"Add a new milestone instead: pm milestone add")
            if fields.get("status") == "on-hold" and m["status"] == "implemented":
                raise FormatError(f"milestone {mid} is implemented; it can't be put on hold")
            for k, v in fields.items():
                if k == "status" and v is not None:
                    db.execute("UPDATE milestone SET status = ?, implemented_at = CASE WHEN ? = 'implemented' "
                               "THEN coalesce(implemented_at, ?) END WHERE id = ?", (v, v, now(), mid))
                elif v is not None:
                    db.execute(f"UPDATE milestone SET {k} = ? WHERE id = ?", (v.strip(), mid))
            return [q for q in [self.queue_revision(db, master)] if q] if changed and master else []

    # --- plan tree ---------------------------------------------------------------------------
    # The masterplan is the plan with the task "write the masterplan". A plan's parent: the plan of the task that wrote
    # it, if in the same milestone; else the masterplan. Done: has tasks, none open, and every child plan is done.

    @staticmethod
    def masterplan(db, mid):
        return db.execute("SELECT min(t.plan_id) FROM task t JOIN plan p ON p.id = t.plan_id "
                          "WHERE p.milestone_id = ? AND t.kind = 'masterplan'", (mid,)).fetchone()[0]

    def child_plans(self, db, pid):
        mid = self.get(db, "plan", pid)["milestone_id"]
        master = self.masterplan(db, mid)
        rows = db.execute("SELECT p.id, CASE WHEN q.milestone_id = p.milestone_id AND q.id <> p.id THEN q.id END "
                          "FROM plan p LEFT JOIN task t ON t.id = p.created_by_task LEFT JOIN plan q ON q.id = t.plan_id "
                          "WHERE p.milestone_id = ? AND p.id <> ? ORDER BY p.id", (mid, master or 0)).fetchall()
        return [c for c, parent in rows if (parent or master) == pid and c != pid]

    def plan_done(self, db, pid, seen=None):
        seen = set() if seen is None else seen
        seen.add(pid)
        if not db.execute("SELECT 1 FROM task WHERE plan_id = ?", (pid,)).fetchone() or \
                db.execute("SELECT 1 FROM task WHERE plan_id = ? AND status = 'open'", (pid,)).fetchone():
            return False
        return all(self.plan_done(db, c, seen) for c in self.child_plans(db, pid) if c not in seen)

    def done_plans(self):
        """Ids of the done plans."""
        with self.connect() as db:
            return [pid for (pid,) in db.execute("SELECT id FROM plan ORDER BY id").fetchall() if self.plan_done(db, pid)]

    def check_plan_open(self, db, pid):
        if self.plan_done(db, pid):
            mid = self.get(db, "plan", pid)["milestone_id"]
            raise FormatError(f"plan {pid} is done (no open task, nor in the plans below it) and can't be changed; "
                              f"add a new plan instead: pm plan add {mid}")

    def queue_revision(self, db, pid, parent=None):
        """Queue the task that brings plan pid in line with its changed milestone (the masterplan) or parent plan,
        unless pid is done. A waiting one gets the new parent text. Returns (id, subject) of a new task, else None."""
        if self.plan_done(db, pid):
            return None
        if parent is None:
            subject, body = REVISE_MASTERPLAN_SUBJECT, self.prompt("revise_masterplan_prompt", plan_id=pid)
            if db.execute("SELECT 1 FROM task WHERE plan_id = ? AND kind = 'masterplan' AND status = 'open' "
                          + NOT_RUNNING, (pid,)).fetchone():
                return None
        else:
            q = self.get(db, "plan", parent)
            subject = REVISE_PLAN_SUBJECT.format(pid)
            body = (self.prompt("revise_plan_prompt", plan_id=pid, parent_plan_id=parent)
                    + f"\n\n# Plan {parent}: {q['subject']}\n\n{q['body']}")
            if old := db.execute("SELECT id FROM task WHERE plan_id = ? AND subject = ? AND status = 'open' "
                                 + NOT_RUNNING, (pid, subject)).fetchone():
                db.execute("UPDATE task SET body = ? WHERE id = ?", (body, old[0]))
                return None
        tid = db.execute("INSERT INTO task (plan_id, subject, kind, priority, body, created_by_task) "
                         "VALUES (?, ?, ?, ?, ?, ?)", (pid, subject, "plan" if parent else "masterplan",
                                                       self.config["planning_priority"], body, creator())).lastrowid
        return tid, subject

    def queue_plan_tasks(self, db, pid):
        """Queue the task that splits plan pid into tasks, unless one is waiting. Returns (id, subject) or None."""
        p = self.get(db, "plan", pid)
        if db.execute("SELECT 1 FROM task WHERE plan_id = ? AND kind = 'split' AND status = 'open' " + NOT_RUNNING,
                      (pid,)).fetchone():
            return None
        subject = PLAN_TASKS_SUBJECT.format(pid)
        return db.execute("INSERT INTO task (plan_id, subject, kind, priority, body, created_by_task) "
                          "VALUES (?, ?, 'split', ?, ?, ?)",
                          (pid, subject, self.config["planning_priority"],
                           self.prompt("plan_tasks_prompt", plan_id=pid, plan_subject=p["subject"]), creator())
                          ).lastrowid, subject

    def plan_add(self, mid, subject, body, prio=None):
        with self.connect(write=True) as db:
            self.get(db, "milestone", mid)
            pid = db.execute("INSERT INTO plan (milestone_id, subject, priority, body, created_by_task) "
                             "VALUES (?, ?, ?, ?, ?)", (mid, subject, prio, body.strip(), creator())).lastrowid
            self.queue_plan_tasks(db, pid)
        return pid

    def plan_edit(self, pid, subject=None, body=None, prio=None):
        """Change a plan that isn't done; a changed subject or body queues the task that splits it into tasks again and
        the revision of each child plan. prio "none" clears the priority. Returns the queued tasks: [(id, subject)]."""
        with self.connect(write=True) as db:
            p = self.get(db, "plan", pid)
            self.check_plan_open(db, pid)
            if prio is not None:
                db.execute("UPDATE plan SET priority = ? WHERE id = ?", (None if prio == "none" else prio, pid))
            changed = False
            for k, v in (("subject", subject), ("body", body)):
                if v is not None and v.strip() != p[k]:
                    db.execute(f"UPDATE plan SET {k} = ? WHERE id = ?", (v.strip(), pid))
                    changed = True
            if not changed:
                return []
            queued = [self.queue_plan_tasks(db, pid)] + [self.queue_revision(db, c, pid) for c in self.child_plans(db, pid)]
            return [q for q in queued if q]

    def task_add(self, pid, subject, body, prio=None, kind="work"):
        """A new task; a plan-writing one gets planning_priority unless prio is given."""
        if prio is None and kind in PLANNING_KINDS:
            prio = self.config["planning_priority"]
        with self.connect(write=True) as db:
            self.check_plan_open(db, pid)
            return db.execute("INSERT INTO task (plan_id, subject, kind, priority, body, created_by_task) "
                              "VALUES (?, ?, ?, ?, ?, ?)", (pid, subject, kind, prio, body.strip(), creator())).lastrowid

    def task_set(self, tid, **fields):
        """Set fields of an open task (status, note, priority); a value None clears it."""
        with self.connect(write=True) as db:
            if (t := self.get(db, "task", tid))["status"] != "open":
                raise FormatError(f"task {tid} is {t['status']}; a closed task can't be changed")
            for k, v in fields.items():
                if k == "status":
                    db.execute("UPDATE task SET status = ?, retired_at = CASE WHEN ? <> 'open' THEN ? END WHERE id = ?",
                               (v, v, now(), tid))
                else:
                    db.execute(f"UPDATE task SET {k} = ? WHERE id = ?", (v, tid))

    def task_pending_decision(self, tid, did):
        with self.connect(write=True) as db:
            self.block(db, tid, did)

    def decision_add(self, subject, body=None, options=()):
        with self.connect(write=True) as db:
            return db.execute("INSERT INTO decision (subject, options, body, created_by_task) VALUES (?, ?, ?, ?)",
                              (subject, json.dumps(list(options), ensure_ascii=False), (body or "").strip(),
                               creator())).lastrowid

    # --- show --------------------------------------------------------------------------------

    def show(self, tid):
        """The task with its plan and milestone: the context a worker gets."""
        with self.connect() as db:
            t = self.get(db, "task", tid)
            p = self.get(db, "plan", t["plan_id"])
            m = self.get(db, "milestone", p["milestone_id"])
            plans = db.execute("SELECT id, subject FROM plan WHERE milestone_id = ? ORDER BY id", (m["id"],)).fetchall()
            siblings = db.execute("SELECT id, status, kind, subject FROM task WHERE plan_id = ? ORDER BY id",
                                  (p["id"],)).fetchall()
            ds = db.execute("SELECT d.* FROM task_decision td JOIN decision d ON d.id = td.decision_id "
                            "WHERE td.task_id = ? ORDER BY d.id", (tid,)).fetchall()
            runs = db.execute("SELECT * FROM run WHERE task_id = ? ORDER BY id", (tid,)).fetchall()
        prio = t["priority"] if t["priority"] is not None else p["priority"] or 0
        by = lambda r: f", by task {r['created_by_task']}" if r["created_by_task"] else ""
        implemented = f", implemented {stamp(m['implemented_at'])}" if m["implemented_at"] else ""
        out = [f"# Milestone {m['id']}: {m['subject']} ({m['status']}, created {stamp(m['created_at'])}{implemented})",
               "", m["body"], "", "Plans: " + ", ".join(f"{x['id']} {x['subject']}" for x in plans), "",
               f"# Plan {p['id']}: {p['subject']} (priority {p['priority'] or 0}, created {stamp(p['created_at'])}{by(p)})",
               "", p["body"], ""]
        out += [f"- task {x['id']} [{x['status']}{'' if x['kind'] == 'work' else ', ' + x['kind']}] {x['subject']}"
                + (" ← this task" if x["id"] == tid else "") for x in siblings] + [""]
        retired = f", retired {stamp(t['retired_at'])}" if t["retired_at"] else ""
        out += [f"# Task {tid}: {t['subject']} ({t['status']}, kind {t['kind']}, priority {prio}, "
                f"created {stamp(t['created_at'])}{retired}{by(t)})", ""]
        if t["note"]:
            out.append(f"note: {t['note']}")
        out += [t["body"], ""]
        for d in ds:
            said = d["answer"] or d["reason"] or ""
            out.append(f"Decision {d['id']} ({d['status']}{', sign-off' if d['signoff'] else ''}): {d['subject']}"
                       + (f" → {said}" if said else ""))
        if runs:
            out += ["", "Runs:"] + ["  " + self.run_line(r) for r in runs]
        return "\n".join(out).rstrip() + "\n"

    @staticmethod
    def run_line(r):
        tokens = sum(r[k] or 0 for k in ("input_tokens", "output_tokens", "cache_read_tokens", "cache_creation_tokens"))
        parts = [f"run {r['id']}", stamp(r["started_at"]), duration(seconds(r["started_at"], r["ended_at"])),
                 r["result"]]
        if r["turns"] is not None:
            parts.append(f"{r['turns']} turns")
        if tokens:
            parts.append(f"{tokens / 1e6:.2f}M tokens" if tokens >= 1e5 else f"{tokens / 1e3:.0f}k tokens")
        if r["cost_usd"] is not None:
            parts.append(f"${r['cost_usd']:.2f}")
        return ", ".join(parts) + (f": {r['reason'].splitlines()[0][:120]}" if r["reason"] else "")

    # --- run ---------------------------------------------------------------------------------

    def plans(self):
        """[(priority, plan id, subject)] of plans with an open task in an open milestone, highest priority first."""
        return [tuple(r.values()) for r in self.rows(
            "SELECT coalesce(p.priority, 0) AS priority, p.id, p.subject FROM plan p "
            "JOIN milestone m ON m.id = p.milestone_id WHERE m.status = 'open' "
            "AND EXISTS (SELECT 1 FROM task t WHERE t.plan_id = p.id AND t.status = 'open') "
            "ORDER BY priority DESC, m.id, p.id")]

    def priority_prompts(self):
        return [text for k in PRIORITY_KEYS if (text := self.prompt(k))]

    def worker_command(self, tid):
        """(command, prompt): the prompt goes to the worker's stdin; None if the command holds it ("{prompt}")."""
        show = "\n".join([self.show(tid), self.earlier_failure(tid), *self.priority_prompts()]).rstrip() + "\n"
        prompt = WORKER_PROMPT.format(id=tid, db=self.db, show=show)
        hook = {"type": "command", "command": shlex.join([sys.executable, str(GUARD)])}
        settings = json.dumps({"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [hook]}], "Stop": [{"hooks": [hook]}]},
                               "env": WORKER_ENV})
        w = self.config["worker"]
        cmd = [a.replace("{prompt}", prompt).replace("{settings}", settings) for a in w]
        return cmd, None if any("{prompt}" in a for a in w) else prompt

    def worker_warnings(self):
        w = self.config["worker"]
        out = []
        if any("{prompt}" in a for a in w):
            out.append('the worker command holds the prompt ("{prompt}"), so `pkill -f <word in the task>` matches the '
                       'worker itself; drop "{prompt}" and pm passes the prompt on stdin')
        if Path(w[0]).name == "claude" and not any("{settings}" in a for a in w):
            out.append('add "--settings", "{settings}" to the worker command: a hook that blocks `pkill -f` and '
                       '`killall`')
        return [f"pm: {CONFIG_FILE}: {x} (see `worker` in pm's template, pm/{CONFIG_FILE})" for x in out]

    @property
    def logs(self):
        """log_dir; relative: inside the git dir, so never tracked."""
        return Path(self.git("rev-parse", "--absolute-git-dir").strip()) / self.config["log_dir"]

    def run_worker(self, cmd, prompt, log, tid, info):
        """Run the worker in its own session, writing its output to the log and a readable form of it to the
        terminal. Returns the exit code; `info` gets what its JSON output (`--output-format stream-json`) says about
        the run (model, tokens, cost, the last tool call) and the last lines shown. Interrupted (Ctrl-C, SIGTERM,
        SIGHUP): the worker's process group gets SIGTERM."""
        log.parent.mkdir(parents=True, exist_ok=True)
        info["shown"] = shown_lines = collections.deque(maxlen=15)
        def stop(sig, _):
            raise KeyboardInterrupt(signal.Signals(sig).name)
        old = {sig: signal.signal(sig, stop) for sig in (signal.SIGTERM, signal.SIGHUP)}
        try:
            with open(log, "w", encoding="utf-8") as f, subprocess.Popen(
                    cmd, cwd=self.root, env=os.environ | {"PM_WORKER": "1", "PM_TASK": str(tid)},
                    stdin=subprocess.DEVNULL if prompt is None else subprocess.PIPE, stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT, text=True, bufsize=1, start_new_session=True) as proc:
                try:
                    if prompt is not None:
                        with contextlib.suppress(BrokenPipeError):
                            proc.stdin.write(prompt)
                        with contextlib.suppress(BrokenPipeError):
                            proc.stdin.close()
                    for line in proc.stdout:
                        f.write(line)
                        f.flush()                   # read live by pm-dashboard
                        shown = self.read_event(line, info)
                        if shown is not None:
                            shown_lines.extend(shown.rstrip("\n").split("\n"))
                            self.out.write(shown)
                            self.out.flush()
                    return proc.wait()
                except BaseException:
                    with contextlib.suppress(ProcessLookupError, PermissionError):
                        os.killpg(proc.pid, signal.SIGTERM)
                    raise
        finally:
            for sig, handler in old.items():
                signal.signal(sig, handler)

    @staticmethod
    def read_event(line, info):
        """A line of the worker's output: plain text is shown as it is; a JSON event of claude's stream-json adds to
        `info` and is shown as text, tool calls or a summary (None: nothing to show)."""
        try:
            e = json.loads(line)
        except ValueError:
            return line
        if not isinstance(e, dict):
            return line
        if e.get("type") == "system" and e.get("subtype") == "init":
            info["model"] = e.get("model")
        elif e.get("type") == "assistant":
            out = []
            for c in (e.get("message") or {}).get("content") or []:
                if c.get("type") == "text" and c.get("text", "").strip():
                    out.append(c["text"].strip())
                    info["last_text"] = c["text"].strip()
                elif c.get("type") == "tool_use":
                    i = c.get("input") or {}
                    arg = i.get("command") or i.get("file_path") or i.get("pattern") or json.dumps(i)[:100]
                    first = str(arg).splitlines()[0] if str(arg).strip() else ""
                    out.append(f"  → {c.get('name')}: {first[:150]}")
                    info["last_tool"] = f"{c.get('name')}: {first[:300]}"
                    info["last_arg"] = str(arg)[:4000]
            return "".join(o + "\n" for o in out) or None
        elif e.get("type") == "result":
            u = e.get("usage") or {}
            info.update(session_id=e.get("session_id"), turns=e.get("num_turns"), cost_usd=e.get("total_cost_usd"),
                        input_tokens=u.get("input_tokens"), output_tokens=u.get("output_tokens"),
                        cache_read_tokens=u.get("cache_read_input_tokens"),
                        cache_creation_tokens=u.get("cache_creation_input_tokens"), result=e.get("result"))
            cost = f", ${e['total_cost_usd']:.2f}" if e.get("total_cost_usd") is not None else ""
            return f"[{e.get('subtype', 'result')}: {e.get('num_turns')} turns{cost}]\n"
        return None

    @staticmethod
    def limit_line(log, info):
        """The usage-limit message: the worker's result, or one of the log's last 5 lines; None if there is none."""
        tail = log.read_text(encoding="utf-8", errors="replace").splitlines()[-5:]
        for l in [info.get("result") or ""] + list(reversed(tail)):
            if any(w in l.lower() for w in LIMIT_WORDS) and "resets " in l.lower():
                return l
        return None

    def wait_until(self, when):
        """Sleep until `when` by the wall clock, so a suspended machine still wakes up on time."""
        while not self.stopping and (left := (when - datetime.datetime.now(when.tzinfo)).total_seconds()) > 0:
            time.sleep(min(left, 60))

    def head(self):
        try:
            return self.git("rev-parse", "HEAD").strip()
        except subprocess.CalledProcessError:
            return None

    def start_run(self, tid):
        """The run row of the task now running (result 'running'); returns its id."""
        with self.connect(write=True) as db:
            return db.execute("INSERT INTO run (task_id, started_at, result, base_commit, pid) "
                              "VALUES (?, ?, 'running', ?, ?)", (tid, now(), self.head(), os.getpid())).lastrowid

    def record_run(self, rid, code, info, result, reason=None):
        cols = ("model", "session_id", "turns", "input_tokens", "output_tokens", "cache_read_tokens",
                "cache_creation_tokens", "cost_usd")
        with self.connect(write=True) as db:
            db.execute(f"UPDATE run SET ended_at = ?, result = ?, reason = ?, exit_code = ?, "
                       f"{', '.join(c + ' = ?' for c in cols)} WHERE id = ?",
                       (now(), result, reason, code, *(info.get(c) for c in cols), rid))

    def close_stale_run(self):
        """A 'running' run whose `pm run` is gone becomes failed and is committed; one whose process lives is an
        error: one `pm run` per repository."""
        r = (self.rows("SELECT * FROM run WHERE result = 'running'") or [None])[0]
        if r is None:
            return
        with contextlib.suppress(ProcessLookupError, TypeError):
            if r["pid"] != os.getpid():
                os.kill(r["pid"], 0)
                raise FormatError(f"`pm run` (pid {r['pid']}) is working task {r['task_id']}, run {r['id']}")
        with self.connect(write=True) as db:
            db.execute("UPDATE run SET ended_at = ?, result = 'failed', reason = 'interrupted' WHERE id = ?",
                       (now(), r["id"]))
        self.commit_db(f"run {r['id']} (task {r['task_id']}) interrupted")

    def work(self, tid):
        """Run the worker for a task until no usage limit stops it; record each run and commit the task.
        Returns ("done" | "blocked" | "failed", message)."""
        cmd, prompt = self.worker_command(tid)
        before = self.planning_snapshot()
        while True:
            started, rid = now(), self.start_run(tid)
            log = self.run_log(tid, rid)
            self.say(f"\n== task {tid}, run {rid} (log: {log})")
            info = {}
            try:
                code = self.run_worker(cmd, prompt, log, tid, info)
            except KeyboardInterrupt as e:
                self.failed(tid, rid, f"interrupted ({e.args[0] if e.args else 'Ctrl-C'})", None, info, started, log)
                raise
            limit = self.limit_line(log, info)
            if not limit:
                break
            reset = limit_reset(limit)
            if reset is None:
                return self.failed(tid, rid, f"usage limit without a readable reset time: {limit.strip()}", code, info,
                                   started, log)
            self.record_run(rid, code, info, "limit", limit.strip())
            margin = datetime.timedelta(minutes=self.config["limit_margin_minutes"])
            until = max(reset + margin, datetime.datetime.now(reset.tzinfo) + margin)
            self.say(f"Usage limit ({limit.strip()}). Waiting until {until:%Y-%m-%d %H:%M %Z}, then the same task again.")
            self.wait_until(until)
            if self.stopping:
                return "limit", "stopped while waiting for the usage limit's reset"
        if errors := self.validate():
            return "failed", "the database is no longer valid, the run is not recorded:\n  " + "\n  ".join(errors)
        if code != 0:
            return self.failed(tid, rid, exit_text(code), code, info, started, log)
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
            return self.failed(tid, rid, "exit 0, but the task is still open and not blocked", code, info, started, log,
                               cause=f"the worker ended without `pm task done|drop {tid}` or a pending decision"
                               + (f"; its last message: {info['last_text'].splitlines()[-1][:200]}"
                                  if info.get("last_text") else ""))
        problem = None
        if left := [l for l in self.git("status", "--porcelain").splitlines() if l[1] != " " and l[3:] != self.db]:
            problem = "the worker left changes that are not staged:\n" + "\n".join(left)
        elif t["status"] == "done" and (missing := self.planning_missing(t, before)):
            problem = f"task {tid} is done, but it {missing}"
        self.record_run(rid, code, info, "failed" if problem else result[0], problem)
        try:
            self.commit_task(tid, t["subject"], result[1])
        except subprocess.CalledProcessError as e:
            return "failed", f"git commit failed:\n{(e.stdout or '') + (e.stderr or '')}".rstrip()
        if problem and (stash := self.stash_leftovers(f"pm: task {tid} run {rid} left")):
            problem += "\n" + stash
        if t["status"] != "open" and (dropped := self.drop_task_stashes(tid)):
            self.say(f"dropped the stash(es) of task {tid}'s failed runs: {', '.join(dropped)}")
        return ("failed", f"committed task {tid}; {problem}") if problem else result

    def run_log(self, tid, rid):
        """The run's log, task-<id>-run-<run>.log; task-<id>.log links to the task's latest."""
        log = self.logs / f"task-{tid}-run-{rid}.log"
        latest, tmp = self.logs / f"task-{tid}.log", self.logs / f".task-{tid}.log.tmp"
        log.parent.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(OSError):
            tmp.unlink(missing_ok=True)
            tmp.symlink_to(log.name)
            os.replace(tmp, latest)
        return log

    def failed(self, tid, rid, what, code=None, info=None, started=None, log=None, cause=None):
        """A failed run: stash what the worker left (the database aside), record the run with a summary of what
        happened, commit the database alone. Returns ("failed", the summary and the last lines shown)."""
        info = info or {}
        head = [what]
        if started:
            head.append(f"after {duration(seconds(started, None))}")
        if info.get("turns") is not None:
            head.append(f"{info['turns']} turns")
        if info.get("model") and "result" not in info:
            head.append("no result")
        lines = [", ".join(head)]
        if info.get("last_tool"):
            lines.append(f"last tool call: {info['last_tool']}")
        if cause is None and exit_signal(code):
            cause = ("the worker's own kill command matched its process"
                     if re.search(r"\b(pkill|killall|kill)\b", info.get("last_arg") or "")
                     else "killed from outside (out of memory, Ctrl-C, another process)")
        if cause:
            lines.append(f"likely cause: {cause}")
        if stash := self.stash_leftovers(f"pm: task {tid} run {rid} failed"):
            lines.append(stash)
        if log:
            lines.append(f"log: {log}")
        summary = "\n".join(lines)
        self.record_run(rid, code, info, "failed", summary)
        try:
            self.git("commit", "-q", "-m", f"pm: task {tid} failed", "--", self.db)
        except subprocess.CalledProcessError as e:
            summary += f"\ncommitting {self.db} failed: {e.stderr.strip()}"
        if shown := info.get("shown"):
            summary += "\nlast output:\n" + "\n".join("  " + l for l in shown)
        elif log and log.exists():
            summary += "\nlast lines of the log:\n" + "\n".join(
                "  " + l[:300] for l in log.read_text(encoding="utf-8", errors="replace").splitlines()[-15:])
        return "failed", summary

    def task_stashes(self, tid):
        """[(ref, sha)] of the stashes failed runs of task tid left (stash_leftovers), newest first."""
        try:
            lines = self.git("stash", "list", "--format=%gd%x00%H%x00%s").splitlines()
        except subprocess.CalledProcessError:
            return []
        return [(ref, sha) for ref, sha, subject in (l.split("\0", 2) for l in lines)
                if re.search(rf"pm: task {tid} run \d+ failed$", subject)]

    def drop_task_stashes(self, tid):
        """Drop task tid's stashes (its work is committed or dropped); returns their short shas."""
        dropped = []
        for ref, sha in reversed(self.task_stashes(tid)):       # oldest first: the newer refs keep their index
            with contextlib.suppress(subprocess.CalledProcessError):
                self.git("stash", "drop", "-q", ref)
                dropped.append(sha[:7])
        return dropped

    def earlier_failure(self, tid):
        """For the worker's prompt: the last failed run's summary, if the task's last run failed, and the stashes
        failed runs left. Empty if neither."""
        last = (self.rows("SELECT id, result, reason FROM run WHERE task_id = ? AND result NOT IN ('running', 'limit') "
                          "ORDER BY id DESC LIMIT 1", tid) or [None])[0]
        stashes = self.task_stashes(tid)
        out = []
        if last and last["result"] == "failed":
            out += [f"# Run {last['id']} of this task failed", "",
                    *[f"    {l}" for l in (last["reason"] or "no reason recorded").splitlines()], ""]
        if stashes:
            out.append("Failed runs of this task left their work in git stashes, newest first:")
            for _, sha in stashes:
                try:
                    files = self.git("stash", "show", "--name-only", "--include-untracked", sha).split()
                except subprocess.CalledProcessError:
                    files = []
                more = f" … and {len(files) - 15} more" if len(files) > 15 else ""
                out.append(f"  {sha[:7]}: {', '.join(files[:15])}{more}")
            out += ["Before you start, look at each (`git stash show -p --include-untracked <sha>`), apply what is "
                    "still good (`git stash apply <sha>`) and fix or undo the rest. Treat what it marks as done or "
                    "checked as unchecked until your own check passes. Don't drop the stashes: pm drops them when the "
                    "task is done or dropped.", ""]
        return "\n".join(out)

    def stash_leftovers(self, message):
        """Stash the working tree's changes, the database aside, so the next `pm run` starts clean. Returns a line
        saying what was stashed and how to get it back, or None if nothing was left."""
        files = [l[3:] for l in self.git("status", "--porcelain").splitlines() if l[3:] != self.db]
        if not files:
            return None
        n = f"{len(files)} file{'s' if len(files) > 1 else ''}"
        try:
            self.git("stash", "push", "-q", "--include-untracked", "-m", message, "--", ".", f":(exclude){self.db}")
            sha = self.git("rev-parse", "--short", "stash@{0}").strip()
        except subprocess.CalledProcessError as e:
            return f"left behind: {n}, stashing failed: {e.stderr.strip()}"
        return f"left behind: {n}, stashed as {sha} ({message}); get them back with `git stash apply {sha}`"

    def planning_snapshot(self):
        """The plans: to tell whether a plan-writing task changed them."""
        return self.rows("SELECT * FROM plan ORDER BY id")

    def planning_missing(self, t, before):
        """What a done task of a planning kind failed to do, or None."""
        revision = t["subject"] in (REVISE_MASTERPLAN_SUBJECT, REVISE_PLAN_SUBJECT.format(t["plan_id"]))
        if t["kind"] in ("plan", "masterplan") and not revision and self.planning_snapshot() == before:
            return "added or changed no plan"
        if t["kind"] == "split" and not self.rows("SELECT 1 FROM task WHERE plan_id = ? AND kind IN ('work', 'plan')",
                                                  t["plan_id"]):
            return "left its plan without tasks"
        return None

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

    def run(self, once=False, dry_run=False):
        """Work the queue. The first Ctrl-C (SIGINT) or a SIGUSR1 (`pm stop`): stop after the current task; a second
        Ctrl-C, SIGTERM or SIGHUP: stop now."""
        def stop_later(sig, _):
            if sig == signal.SIGINT and self.stopping:
                raise KeyboardInterrupt("SIGINT")
            if not self.stopping:
                self.stopping = True
                os.write(2, b"\n== stopping after this task (Ctrl-C again: stop now)\n")  # print() may be mid-write
        old = {sig: signal.signal(sig, stop_later) for sig in (signal.SIGINT, signal.SIGUSR1)}
        try:
            return self.work_queue(once, dry_run)
        finally:
            for sig, handler in old.items():
                signal.signal(sig, handler)

    def work_queue(self, once, dry_run):
        for w in self.worker_warnings():
            self.say(w)
        while True:
            errors = self.validate()
            if errors:
                self.say("the database has errors:\n  " + "\n  ".join(errors))
                return 1
            if dry_run:
                q = self.queue()
                if not q:
                    self.say("Queue empty.")
                elif level := self.needs_signoff(q[0][1]):
                    self.say(f"task {q[0][1]} needs a sign-off ({level}); `pm run` would request it")
                else:
                    cmd, prompt = self.worker_command(q[0][1])
                    self.say(shlex.join(cmd) + ("" if prompt is None else f"\n\nstdin:\n{prompt}"))
                return 0
            self.close_stale_run()
            self.close_and_commit()
            if not self.rows("SELECT 1 FROM milestone WHERE status = 'open'"):
                held = [r["id"] for r in self.rows("SELECT id FROM milestone WHERE status = 'on-hold' ORDER BY id")]
                self.say("No open milestone (`pm milestone add` starts one)." + (
                    f" On hold: milestone(s) {', '.join(map(str, held))} (`pm milestone resume <id>`)." if held else ""))
                return 0
            q = self.queue()
            if not q:
                n = len(self.pending())
                self.say(f"Queue empty; {n} pending decision(s)." + (" Answer them with `pm decide`." if n else ""))
                return 0
            if level := self.needs_signoff(q[0][1]):
                did = self.request_signoff(q[0][1])
                self.say(f"task {q[0][1]} waits for sign-off: decision {did} ({level})")
                self.commit_db(f"sign-off requested: decision {did}")
                continue
            if self.dirty():
                self.say(self.dirty_message())
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
            if self.stopping:
                self.say("Stopped as asked; `pm run` continues with the queue.")
                return 0

    def stop(self):
        """Ask the running `pm run` to stop after its current task."""
        r = (self.rows("SELECT * FROM run WHERE result = 'running'") or [None])[0]
        try:
            os.kill(r["pid"], signal.SIGUSR1)
        except (TypeError, ProcessLookupError):
            raise FormatError("no `pm run` is working a task; between tasks or waiting for a usage limit, Ctrl-C "
                              "or `kill <pid>` stops it with nothing lost") from None
        self.say(f"`pm run` (pid {r['pid']}) stops after task {r['task_id']}")

    def dirty_message(self):
        files = self.git("status", "--porcelain").splitlines()
        out = [f"The working tree has {len(files)} uncommitted change(s); commit or stash them first:"]
        out += ["  " + l for l in files[:10]] + ([f"  … and {len(files) - 10} more"] if len(files) > 10 else [])
        last = (self.rows("SELECT * FROM run WHERE ended_at IS NOT NULL ORDER BY id DESC LIMIT 1") or [None])[0]
        if last and last["result"] == "failed":
            out.append(f"The last run, {last['id']} of task {last['task_id']}, failed; they may be its leftovers.")
        out += ["  keep them aside:  git stash push --include-untracked", "  drop them:        git restore -SW . && git clean -fd"]
        return "\n".join(out)

    # --- status ------------------------------------------------------------------------------

    def status(self):
        for r in self.rows("SELECT * FROM run WHERE result = 'running'"):
            self.say(f"Running: task {r['task_id']} (run {r['id']}, pid {r['pid']}) since {stamp(r['started_at'])}, "
                     f"{duration(seconds(r['started_at'], None))}")
        for m in self.rows(
                "SELECT m.id, m.subject, m.status, m.created_at, "
                "(SELECT count(*) FROM plan p WHERE p.milestone_id = m.id) AS plans, "
                "(SELECT count(*) FROM task t JOIN plan p ON p.id = t.plan_id WHERE p.milestone_id = m.id) AS tasks, "
                "(SELECT count(*) FROM task t JOIN plan p ON p.id = t.plan_id "
                "WHERE p.milestone_id = m.id AND t.status <> 'open') AS closed, "
                "(SELECT count(*) FROM run r JOIN task t ON t.id = r.task_id JOIN plan p ON p.id = t.plan_id "
                "WHERE p.milestone_id = m.id) AS runs, "
                "(SELECT count(*) FROM run r JOIN task t ON t.id = r.task_id JOIN plan p ON p.id = t.plan_id "
                "WHERE p.milestone_id = m.id AND r.result = 'failed') AS failed, "
                "(SELECT total(r.cost_usd) FROM run r JOIN task t ON t.id = r.task_id JOIN plan p ON p.id = t.plan_id "
                "WHERE p.milestone_id = m.id) AS cost "
                "FROM milestone m WHERE m.status <> 'implemented' ORDER BY m.id"):
            held = ", on hold" if m["status"] == "on-hold" else ""
            self.say(f"Milestone {m['id']}: {m['subject']} (created {stamp(m['created_at'])}{held}): {m['plans']} plan(s), "
                     f"{m['closed']}/{m['tasks']} tasks closed, {m['runs']} run(s) ({m['failed']} failed), "
                     f"${m['cost']:.2f}")
        self.print_queue(limit=10)
        pending = self.pending()
        self.say(f"\nPending decisions ({len(pending)}):")
        for d in pending:
            self.say(f"  {d['id']}: {d['subject']}")

    def print_runs(self, tid=None):
        """`pm runs`: every worker run, or a task's; then the totals per task kind."""
        where, args = ("WHERE r.task_id = ?", (tid,)) if tid else ("", ())
        runs = self.rows(f"SELECT r.*, t.kind FROM run r JOIN task t ON t.id = r.task_id {where} ORDER BY r.id", *args)
        for r in runs:
            self.say(f"task {r['task_id']:>4}  " + self.run_line(r))
        totals = {}
        for r in runs:
            n, secs, cost = totals.get(r["kind"], (0, 0, 0.0))
            secs += seconds(r["started_at"], r["ended_at"])
            totals[r["kind"]] = (n + 1, secs, cost + (r["cost_usd"] or 0))
        if totals:
            self.say("\nPer kind: " + "; ".join(f"{k} {n} run(s), {duration(s)}, ${c:.2f}"
                                                  for k, (n, s, c) in sorted(totals.items())))
        else:
            self.say("No runs yet.")

    def print_plans(self, new=None, file=None):
        """`pm plan list`: id, priority, subject; `new` marked."""
        lines = ["  id   prio  subject"] + [f"{pid:4d}  {prio:5d}  {subject}" + ("   ← new" if pid == new else "")
                                          for prio, pid, subject in self.plans()]
        print("\n".join(lines), file=file or self.out, flush=True)

    def print_queue(self, limit=None):
        q = self.queue()
        self.say(f"\nQueue ({len(q)}):")
        for prio, tid, subject, kind in q[:limit]:
            subject += "" if kind == "work" else f" ({kind})"
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


def print_queued(queued):
    for tid, subject in queued:
        print(f"queued task {tid}: {subject}")


def main(argv=None):
    ap = argparse.ArgumentParser(prog="pm", description=__doc__, epilog=EPILOG,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--version", action="version", version=f"pm {VERSION}")
    ap.add_argument("--root", help="the project's git repository (default: the one around the current directory)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name, text in [("init", f"write {CONFIG_FILE} and the database (missing ones only) and commit them"),
                       ("status", "open and on-hold milestones, the queue, pending decisions"), ("queue", "the whole queue"),
                       ("validate", "check the database"),
                       ("decide", "answer pending decisions in the terminal, one by one"),
                       ("instructions", "the full reference for agents: every command with examples, recipes")]:
        sub.add_parser(name, help=text)
    s = sub.add_parser("show", help="a task with its plan, milestone, decisions and runs")
    s.add_argument("task", type=int)
    rs = sub.add_parser("runs", help="worker runs (time, result, tokens, cost) and totals per task kind")
    rs.add_argument("task", type=int, nargs="?", help="only this task's runs")
    r = sub.add_parser("run", help="work the queue with the worker command (claude -p)")
    r.add_argument("--once", action="store_true", help="stop after one task")
    r.add_argument("--dry-run", action="store_true", help="print the next worker command; change nothing")
    sub.add_parser("stop", help="have the running `pm run` stop after its current task")

    ms = sub.add_parser("milestone", help="add or change milestones").add_subparsers(dest="action", required=True)
    ma = ms.add_parser("add", help="new open milestone; prints its id. Without a subject: write it in your editor "
                       "(first line: subject, the rest: body); with a subject: the body is read from stdin")
    ma.add_argument("subject", nargs="?")
    mset = ms.add_parser("set", help="set milestone fields; a new subject or body queues the revision of the masterplan")
    mset.add_argument("milestone", type=int)
    mset.add_argument("--subject")
    mset.add_argument("--status", choices=MILESTONE_STATES)
    mset.add_argument("--body", type=text_arg)
    for name, text in [("hold", "put a milestone on hold: its plans and tasks leave the queue; a running task finishes"),
                       ("resume", "resume a milestone on hold: its tasks are queued again")]:
        ms.add_parser(name, help=text).add_argument("milestone", type=int)

    p = sub.add_parser("plan", aliases=["plans"], help="add, change or list plans").add_subparsers(
        dest="action", required=True)
    pa = p.add_parser("add", help="new plan; prints its id and queues the task that splits it into tasks")
    pa.add_argument("milestone", type=int)
    pa.add_argument("subject", nargs="?", help="then the body (Markdown) is read from stdin; without: editor")
    pa.add_argument("--priority", type=int)
    pe = p.add_parser("edit", help="change a plan that isn't done; a new subject or body queues the task that splits it "
                      "again and the revision of the plans below it")
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
    a.add_argument("--priority", type=int, help="default: the plan's; for --kind plan: planning_priority")
    a.add_argument("--kind", choices=("work", "plan"), default="work", help="plan: the task writes a plan")
    for verb, text in [("done", "note: date, files or commit"), ("drop", "reason")]:
        x = t.add_parser(verb)
        x.add_argument("task", type=int)
        x.add_argument("note", help=text)
    tp = t.add_parser("priority", help="integer, or none for the plan's")
    tp.add_argument("task", type=int)
    tp.add_argument("priority", type=prio_arg)

    d = sub.add_parser("decision", aliases=["decisions"], help="ask the human; the human's answers").add_subparsers(
        dest="action", required=True)
    da = d.add_parser("add", help="new pending decision; prints its id")
    da.add_argument("subject", help="the question")
    da.add_argument("--body", type=text_arg, help="context for the human")
    da.add_argument("--option", action="append", default=[], help="repeat; the recommended one first")
    d.add_parser("list", help="pending decisions with their context, options and the tasks they block")
    dn = d.add_parser("answer", help="the human's answer; its tasks become workable")
    dn.add_argument("decision", type=int)
    dn.add_argument("answer", help="an option's number, or own text")
    dap = d.add_parser("approve", help="approve a sign-off")
    dap.add_argument("decision", type=int)
    di = d.add_parser("irrelevant", help="close without answer; drops the open tasks it blocks")
    di.add_argument("decision", type=int)
    di.add_argument("reason")
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
        if texts := pm.priority_prompts():
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
        elif args.cmd == "runs":
            pm.print_runs(args.task)
        elif args.cmd == "decide":
            msg = pm.decide() and "decisions answered"
        elif args.cmd == "run":
            return pm.run(once=args.once, dry_run=args.dry_run)
        elif args.cmd == "stop":
            pm.stop()
        elif args.cmd == "milestone":
            if args.action == "add":
                if not (entry := pm.subject_and_body("milestone", args.subject)):
                    print("pm: aborted, no milestone added")
                    return 1
                mid = pm.milestone_add(*entry)
                print(mid)
                msg = f"milestone {mid} added: {entry[0]}"
            elif args.action in ("hold", "resume"):
                if args.action == "resume" and pm.rows("SELECT 1 FROM milestone WHERE id = ? AND status <> 'on-hold'",
                                                       args.milestone):
                    raise FormatError(f"milestone {args.milestone} is not on hold")
                pm.milestone_set(args.milestone, status="on-hold" if args.action == "hold" else "open")
                msg = f"milestone {args.milestone} " + ("on hold" if args.action == "hold" else "resumed")
            else:
                print_queued(pm.milestone_set(args.milestone, subject=args.subject, status=args.status,
                                              body=args.body))
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
                print_queued(pm.plan_edit(args.plan, args.subject, args.body, args.priority))
                msg = f"plan {args.plan} changed"
        elif args.cmd == "task":
            if args.action == "add":
                pm.require("plan", args.plan)
                if not (entry := pm.subject_and_body("task", args.subject)):
                    print("pm: aborted, no task added")
                    return 1
                tid = pm.task_add(args.plan, *entry, args.priority, args.kind)
                print(tid)
                msg = f"task {tid} added: {entry[0]}"
            elif args.action in ("done", "drop"):
                pm.task_set(args.task, status="done" if args.action == "done" else "dropped", note=args.note)
                msg = f"task {args.task} {args.action}"
            else:
                pm.task_set(args.task, priority=args.priority)
                msg = f"task {args.task} priority"
        elif args.cmd in ("decision", "decisions"):
            if args.action == "list":
                print(pm.decisions(), end="")
            elif args.action == "answer":
                msg = f"decision {args.decision} answered: {pm.answer(args.decision, args.answer)}"
            elif args.action == "approve":
                if not pm.rows("SELECT 1 FROM decision WHERE id = ? AND signoff IS NOT NULL", args.decision):
                    raise FormatError(f"decision {args.decision} is not a sign-off; use `pm decision answer`")
                pm.resolve(args.decision, answer=SIGNOFF_ANSWER)
                msg = f"decision {args.decision} approved"
            elif args.action == "irrelevant":
                pm.resolve(args.decision, irrelevant=args.reason)
                msg = f"decision {args.decision} irrelevant: {args.reason}"
            else:
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

