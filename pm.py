#!/usr/bin/env python3
"""pm: a project manager for Claude Code workers (README.md). Milestones -> masterplan -> plans -> tasks, plus
decisions for the human, in an SQLite database in the project (tasks.dbsqlite, created by the first command;
the schema is SCHEMA below). The task queue is computed (the `queue` view): open tasks of open milestones that no
pending decision blocks, highest priority first, then by task id. Worked one task at a time by `claude -p`.
Standard library only (Python 3.11+).

    pm status | queue | show <task> | validate | sync | decide | run [--once] [--dry-run]
    pm milestone add|set …   pm masterplan <milestone> …   pm plan add|priority …
    pm task add|done|drop|block|priority …   pm decision add …

Works on the git repository around the current directory. pm never commits or pushes; workers commit their work.
Whether the database is committed or ignored is the project's choice. Worker logs: .git/pm-logs/.
"""
import argparse, contextlib, datetime, json, re, sqlite3, subprocess, sys, time, zoneinfo
from pathlib import Path


DB_FILE = "tasks.dbsqlite"
QID_RE = re.compile(r"^(M\d{3})-(P\d{3})-(T\d{3})$")
PLAN_REF_RE = re.compile(r"^(M\d{3})-(P\d{3})$")
MID_RE = re.compile(r"^M\d{3}$")
MILESTONE_STATES = ("open", "implemented")
MASTER_STATES = ("stub", "draft", "agreed")

SCHEMA_VERSION = 1
SCHEMA = """
CREATE TABLE milestone (
    id          TEXT PRIMARY KEY CHECK (id GLOB 'M[0-9][0-9][0-9]'),
    title       TEXT NOT NULL CHECK (trim(title) <> ''),
    status      TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'implemented')),
    description TEXT NOT NULL DEFAULT ''                   -- Markdown: what it achieves, how you know it's done
) STRICT;

CREATE TABLE masterplan (                                  -- missing until `pm sync` writes the stub
    milestone     TEXT PRIMARY KEY REFERENCES milestone (id),
    status        TEXT NOT NULL DEFAULT 'stub' CHECK (status IN ('stub', 'draft', 'agreed')),
    goal          TEXT NOT NULL DEFAULT '',
    scope         TEXT NOT NULL DEFAULT '',                -- in and out
    exit_criteria TEXT NOT NULL DEFAULT ''                 -- checkable
) STRICT;

CREATE TABLE plan (
    milestone   TEXT NOT NULL REFERENCES milestone (id),
    id          TEXT NOT NULL CHECK (id GLOB 'P[0-9][0-9][0-9]'),
    title       TEXT NOT NULL CHECK (trim(title) <> ''),
    priority    INTEGER,                                   -- NULL: 0
    description TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (milestone, id)
) STRICT;

CREATE TABLE task (
    milestone   TEXT NOT NULL,
    plan        TEXT NOT NULL,
    id          TEXT NOT NULL CHECK (id GLOB 'T[0-9][0-9][0-9]'),
    title       TEXT NOT NULL CHECK (trim(title) <> ''),
    status      TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'done', 'dropped')),
    priority    INTEGER,                                   -- NULL: the plan's
    note        TEXT,                                      -- done: date and files or commit; dropped: the reason
    description TEXT NOT NULL DEFAULT '',                  -- Markdown: what to do, files, acceptance criteria
    PRIMARY KEY (milestone, plan, id),
    FOREIGN KEY (milestone, plan) REFERENCES plan (milestone, id),
    CONSTRAINT "an open task needs a description" CHECK (status <> 'open' OR trim(description) <> '')
) STRICT;

CREATE TABLE decision (                                    -- a question for the human
    id       TEXT PRIMARY KEY CHECK (id GLOB 'D[0-9][0-9][0-9][0-9]'),
    question TEXT NOT NULL CHECK (trim(question) <> ''),
    status   TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'answered', 'irrelevant')),
    options  TEXT NOT NULL DEFAULT '[]' CHECK (json_type(options) = 'array'),  -- JSON, recommended first
    context  TEXT NOT NULL DEFAULT '',
    answer   TEXT,
    reason   TEXT,                                         -- why it is irrelevant
    closed   TEXT,                                         -- YYYY-MM-DD
    CONSTRAINT "pending: no answer, reason or closed; answered: answer and closed; irrelevant: reason and closed"
    CHECK (status = 'pending' AND answer IS NULL AND reason IS NULL AND closed IS NULL
           OR status = 'answered' AND answer IS NOT NULL AND closed IS NOT NULL
           OR status = 'irrelevant' AND reason IS NOT NULL AND closed IS NOT NULL)
) STRICT;

CREATE TABLE task_decision (                               -- the task waits for the decision while it is pending
    milestone TEXT NOT NULL,
    plan      TEXT NOT NULL,
    task      TEXT NOT NULL,
    decision  TEXT NOT NULL REFERENCES decision (id),
    PRIMARY KEY (milestone, plan, task, decision),
    FOREIGN KEY (milestone, plan, task) REFERENCES task (milestone, plan, id)
) STRICT;

CREATE VIEW queue AS
SELECT coalesce(t.priority, p.priority, 0) AS priority, t.milestone || '-' || t.plan || '-' || t.id AS task, t.title
FROM task t
JOIN plan p ON p.milestone = t.milestone AND p.id = t.plan
JOIN milestone m ON m.id = t.milestone
WHERE m.status = 'open' AND t.status = 'open'
  AND NOT EXISTS (SELECT 1 FROM task_decision td JOIN decision d ON d.id = td.decision
                  WHERE d.status = 'pending' AND td.milestone = t.milestone AND td.plan = t.plan AND td.task = t.id)
ORDER BY priority DESC, task;
"""

WORKER_TOOLS = "Bash Read Edit Write Glob Grep"
WORKER_PROMPT = """You are the worker for task {qid}. Follow the project's own instructions for agents \
(AGENTS.md, CLAUDE.md or similar, if there are any), then do task {qid} and nothing else.

Rules:
1. Work on {qid} only. Change milestones, plans, tasks and decisions only through `pm`, never {db} directly.
2. Follow-up work: `pm task add <milestone>-<plan> "<title>" --description "<text>"` in the same plan, or \
`pm plan add <milestone> "<title>" --description "<text>"` and tasks in it for work outside this plan. Set \
priorities (`--priority N`, positive = earlier) only where the default order (plan, then task id) is wrong.
3. Needs a human answer: `pm decision add "<question>" --context "<text>" --option "<recommended>" \
--option "<other>" --blocks <task ids>`. If {qid} itself is blocked, include it in --blocks, commit what \
is done and stop.
4. Finished: `pm task done {qid} "<YYYY-MM-DD, files or commit>"` or `pm task drop {qid} "<reason>"`. Run \
the project's checks for what you changed. Commit your work in one commit, message "{qid} <title>". Don't push. \
Stop.

The task, its plan and its milestone (`pm show {qid}` prints this again):

{show}"""


# --- Claude's usage limit: the message and when it resets ---------------------------------------

LIMIT_WORDS = ("usage limit", "session limit", "rate limit", "hit your limit")
# "resets 11pm (America/Bogota)", "resets 11:30pm", "resets Oct 9, 3am (Europe/Berlin)"
RESET_RE = re.compile(r"resets\s+(?:(?P<mon>[a-z]{3})[a-z]*\.?\s+(?P<day>\d{1,2}),?\s+(?:at\s+)?)?"
                      r"(?P<h>\d{1,2})(?::(?P<m>\d{2}))?\s*(?P<ap>[ap]\.?m\.?)?(?:\s*\((?P<tz>[^)]+)\))?", re.I)
LIMIT_MARGIN = datetime.timedelta(minutes=5)  # resume this long after the reset 


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


def next_id(prefix, ids, width):
    return f"{prefix}{max((int(i[1:]) for i in ids), default=0) + 1:0{width}d}"


def qid_of(t):
    return f"{t['milestone']}-{t['plan']}-{t['id']}"


class Project:
    def __init__(self, root, claude="claude", out=sys.stdout, db=DB_FILE):
        self.root = Path(root)
        self.db = db
        self.db_path = self.root / db
        self.claude = claude
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

    def rows(self, sql, *args):
        with self.connect() as db:
            return [dict(r) for r in db.execute(sql, args)]

    @staticmethod
    def find(db, qid):
        """The task row for a qualified task id."""
        q = QID_RE.match(qid)
        if not q:
            raise FormatError(f"not a task id (Mnnn-Pnnn-Tnnn): {qid}")
        t = db.execute("SELECT * FROM task WHERE milestone = ? AND plan = ? AND id = ?", q.groups()).fetchone()
        if t is None:
            raise FormatError(f"no task {qid}")
        return dict(t)

    @staticmethod
    def plan_key(db, plan_ref):
        r = PLAN_REF_RE.match(plan_ref)
        if not r:
            raise FormatError(f"not a plan (Mnnn-Pnnn): {plan_ref}")
        if not db.execute("SELECT 1 FROM plan WHERE milestone = ? AND id = ?", r.groups()).fetchone():
            raise FormatError(f"no plan {plan_ref}")
        return r.groups()

    @staticmethod
    def check_milestone(db, mid):
        if not db.execute("SELECT 1 FROM milestone WHERE id = ?", (mid,)).fetchone():
            raise FormatError(f"no milestone {mid}")

    @staticmethod
    def block(db, t, did):
        if not db.execute("SELECT 1 FROM decision WHERE id = ?", (did,)).fetchone():
            raise FormatError(f"no decision {did}")
        db.execute("INSERT OR IGNORE INTO task_decision VALUES (?, ?, ?, ?)", (t["milestone"], t["plan"], t["id"], did))

    def pending(self):
        ds = self.rows("SELECT * FROM decision WHERE status = 'pending' ORDER BY id")
        for d in ds:
            d["options"] = json.loads(d["options"])
        return ds

    def blockers(self, db, t, pending=False):
        return [r[0] for r in db.execute(
            "SELECT d.id FROM task_decision td JOIN decision d ON d.id = td.decision WHERE td.milestone = ? "
            "AND td.plan = ? AND td.task = ?" + (" AND d.status = 'pending'" if pending else "") + " ORDER BY d.id",
            (t["milestone"], t["plan"], t["id"]))]

    def queue(self):
        """[(priority, qid, title)]: the `queue` view."""
        return [tuple(r.values()) for r in self.rows("SELECT * FROM queue")]

    # --- git ---------------------------------------------------------------------------------

    def git(self, *args):
        return subprocess.run(["git", *args], cwd=self.root, capture_output=True, text=True, check=True).stdout

    def dirty(self):
        """Uncommitted changes, apart from the database (and its journal), tracked or not."""
        return bool(self.git("status", "--porcelain", "--", ".", f":(top,exclude,glob){self.db}*").strip())

    # --- validate ----------------------------------------------------------------------------

    def validate(self):
        """Every problem in the database, as a list of messages (empty: all good). The schema's constraints
        hold for every write; this catches what a hand edit with foreign keys off, or a broken file, leaves."""
        try:
            with self.connect() as db:
                errors = [r[0] for r in db.execute("PRAGMA integrity_check") if r[0] != "ok"]
                for table, rowid, parent, _ in db.execute("PRAGMA foreign_key_check").fetchall():
                    row = dict(db.execute(f"SELECT * FROM {table} WHERE rowid = ?", (rowid,)).fetchone())
                    key = ", ".join(f"{k} {v}" for k, v in row.items()
                                    if k in ("milestone", "plan", "task", "id", "decision"))
                    errors.append(f"{table} ({key}): no such {parent}")
                return errors
        except FormatError as e:
            return [str(e)]

    # --- sync --------------------------------------------------------------------------------

    @staticmethod
    def stub(db, mid, description):
        """Masterplan stub and plan P001, which writes the masterplan (README.md, sync)."""
        db.execute("INSERT INTO masterplan (milestone, goal) VALUES (?, ?)", (mid, description))
        if db.execute("SELECT 1 FROM plan WHERE milestone = ?", (mid,)).fetchone():
            return
        db.execute("INSERT INTO plan (milestone, id, title, description) VALUES (?, 'P001', 'write masterplan', ?)",
                   (mid, f"Turn milestone {mid} into a masterplan with plans and tasks."))
        db.executemany("INSERT INTO task (milestone, plan, id, title, description) VALUES (?, 'P001', ?, ?, ?)", [
            (mid, "T001", "write goal, scope and exit criteria of the masterplan",
             f"Read milestone {mid} (`pm show {mid}-P001-T001` shows it), the plans and decisions it cites and "
             f"the code it touches. Write the masterplan's goal, scope (in and out) and exit criteria (checkable): "
             f"`pm masterplan {mid} --goal … --scope … --exit-criteria …`. Every question only a human "
             "can answer becomes a decision (`pm decision add`)."),
            (mid, "T002", "split the milestone into plans with tasks",
             f"Split {mid} into plans P002, P003, … (`pm plan add {mid} <title> --description …`), each with tasks "
             f"small enough for one worker run (`pm task add {mid}-Pnnn <title> --description …`). Order them with "
             "priorities where the default (plan, then task id) is wrong. Then "
             f"`pm masterplan {mid} --status draft`.")])

    def sync(self):
        """Stub masterplans of open milestones that have none; close finished milestones. Returns the changes."""
        changes = []
        with self.connect(write=True) as db:
            for mid, description in db.execute(
                    "SELECT id, description FROM milestone WHERE status = 'open' "
                    "AND id NOT IN (SELECT milestone FROM masterplan) ORDER BY id").fetchall():
                self.stub(db, mid, description)
                changes.append(f"{mid} masterplan stub")
            for (mid,) in db.execute(
                    "SELECT m.id FROM milestone m JOIN masterplan mp ON mp.milestone = m.id "
                    "WHERE m.status = 'open' AND mp.status <> 'stub' "
                    "AND EXISTS (SELECT 1 FROM task t WHERE t.milestone = m.id) "
                    "AND NOT EXISTS (SELECT 1 FROM task t WHERE t.milestone = m.id AND t.status = 'open') "
                    "ORDER BY m.id").fetchall():
                db.execute("UPDATE milestone SET status = 'implemented' WHERE id = ?", (mid,))
                changes.append(f"{mid} implemented")
        return changes

    # --- decide ------------------------------------------------------------------------------

    def resolve(self, did, answer=None, irrelevant=None):
        """Close a pending decision. Answered: its tasks unblock by themselves. Irrelevant: the open tasks it
        blocks are dropped."""
        with self.connect(write=True) as db:
            if not db.execute("SELECT 1 FROM decision WHERE id = ? AND status = 'pending'", (did,)).fetchone():
                raise FormatError(f"no pending decision {did}")
            if irrelevant is None:
                db.execute("UPDATE decision SET status = 'answered', answer = ?, closed = ? WHERE id = ?",
                           (answer, today(), did))
                return
            db.execute("UPDATE decision SET status = 'irrelevant', reason = ?, closed = ? WHERE id = ?",
                       (irrelevant, today(), did))
            db.execute("UPDATE task SET status = 'dropped', note = ? WHERE status = 'open' "
                       "AND milestone IN (SELECT id FROM milestone WHERE status = 'open') "
                       "AND EXISTS (SELECT 1 FROM task_decision td WHERE td.decision = ? AND td.milestone = "
                       "task.milestone AND td.plan = task.plan AND td.task = task.id)", (f"{did} irrelevant", did))

    def decide(self, ask=input):
        """Ask about every pending decision; returns how many were closed."""
        closed = 0
        for d in self.pending():
            opts = d["options"]
            blocks = [r["qid"] for r in self.rows(
                "SELECT t.milestone || '-' || t.plan || '-' || t.id AS qid FROM task_decision td JOIN task t "
                "ON t.milestone = td.milestone AND t.plan = td.plan AND t.id = td.task "
                "WHERE td.decision = ? AND t.status = 'open' ORDER BY qid", d["id"])]
            self.say(f"\n{d['id']} {d['question']}")
            if blocks:
                self.say(f"  blocks: {', '.join(blocks)}")
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

    def milestone_add(self, title, description=""):
        with self.connect(write=True) as db:
            mid = next_id("M", [r[0] for r in db.execute("SELECT id FROM milestone")], 3)
            db.execute("INSERT INTO milestone (id, title, description) VALUES (?, ?, ?)",
                       (mid, title, (description or "").strip()))
        return mid

    def milestone_set(self, mid, **fields):
        """Set fields of a milestone (title, status, description); None leaves a field as it is."""
        with self.connect(write=True) as db:
            self.check_milestone(db, mid)
            for k, v in fields.items():
                if v is not None:
                    db.execute(f"UPDATE milestone SET {k} = ? WHERE id = ?", (v.strip(), mid))

    def masterplan_set(self, mid, **fields):
        with self.connect(write=True) as db:
            self.check_milestone(db, mid)
            db.execute("INSERT OR IGNORE INTO masterplan (milestone) VALUES (?)", (mid,))
            for k, v in fields.items():
                if v is not None:
                    db.execute(f"UPDATE masterplan SET {k} = ? WHERE milestone = ?", (v.strip(), mid))

    def plan_add(self, mid, title, description=None, prio=None):
        with self.connect(write=True) as db:
            self.check_milestone(db, mid)
            pid = next_id("P", [r[0] for r in db.execute("SELECT id FROM plan WHERE milestone = ?", (mid,))], 3)
            db.execute("INSERT INTO plan (milestone, id, title, priority, description) VALUES (?, ?, ?, ?, ?)",
                       (mid, pid, title, prio, (description or "").strip()))
        return f"{mid}-{pid}"

    def plan_priority(self, plan_ref, prio):
        with self.connect(write=True) as db:
            mid, pid = self.plan_key(db, plan_ref)
            db.execute("UPDATE plan SET priority = ? WHERE milestone = ? AND id = ?", (prio, mid, pid))

    def task_add(self, plan_ref, title, description, prio=None, blocked_by=()):
        with self.connect(write=True) as db:
            mid, pid = self.plan_key(db, plan_ref)
            tid = next_id("T", [r[0] for r in db.execute(
                "SELECT id FROM task WHERE milestone = ? AND plan = ?", (mid, pid))], 3)
            db.execute("INSERT INTO task (milestone, plan, id, title, priority, description) VALUES (?, ?, ?, ?, ?, ?)",
                       (mid, pid, tid, title, prio, description.strip()))
            for did in blocked_by:
                self.block(db, {"milestone": mid, "plan": pid, "id": tid}, did)
        return f"{plan_ref}-{tid}"

    def task_set(self, qid, **fields):
        """Set fields of a task (status, note, priority); a value None clears it."""
        with self.connect(write=True) as db:
            t = self.find(db, qid)
            for k, v in fields.items():
                db.execute(f"UPDATE task SET {k} = ? WHERE milestone = ? AND plan = ? AND id = ?",
                           (v, t["milestone"], t["plan"], t["id"]))

    def task_block(self, qid, did):
        with self.connect(write=True) as db:
            self.block(db, self.find(db, qid), did)

    def decision_add(self, question, context=None, options=(), blocks=()):
        with self.connect(write=True) as db:
            did = next_id("D", [r[0] for r in db.execute("SELECT id FROM decision")], 4)
            db.execute("INSERT INTO decision (id, question, options, context) VALUES (?, ?, ?, ?)",
                       (did, question, json.dumps(list(options), ensure_ascii=False), (context or "").strip()))
            for qid in blocks:
                self.block(db, self.find(db, qid), did)
        return did

    # --- show --------------------------------------------------------------------------------

    def show(self, qid):
        with self.connect() as db:
            t = self.find(db, qid)
            m = dict(db.execute("SELECT * FROM milestone WHERE id = ?", (t["milestone"],)).fetchone())
            mp = dict(db.execute("SELECT * FROM masterplan WHERE milestone = ?", (m["id"],)).fetchone() or {})
            p = dict(db.execute("SELECT * FROM plan WHERE milestone = ? AND id = ?", (m["id"], t["plan"])).fetchone())
            siblings = db.execute("SELECT id, status, title FROM task WHERE milestone = ? AND plan = ? ORDER BY id",
                                  (m["id"], p["id"])).fetchall()
            ds = db.execute("SELECT d.* FROM task_decision td JOIN decision d ON d.id = td.decision WHERE "
                            "td.milestone = ? AND td.plan = ? AND td.task = ? ORDER BY d.id",
                            (m["id"], p["id"], t["id"])).fetchall()
        prio = t["priority"] if t["priority"] is not None else p["priority"] or 0
        out = [f"Milestone {m['id']} {m['title']} ({m['status']})", m["description"], "",
               f"Masterplan ({mp.get('status', 'none')})"]
        for k in ("goal", "scope", "exit_criteria"):
            if mp.get(k):
                out += [f"{k.replace('_', ' ')}:", mp[k]]
        out += ["", f"Plan {m['id']}-{p['id']} {p['title']} (priority {p['priority'] or 0})"]
        if p["description"]:
            out.append(p["description"])
        for x in siblings:
            out.append(f"  {x['id']} [{x['status']}] {x['title']}" + (" ← this task" if x["id"] == t["id"] else ""))
        out += ["", f"Task {qid}: {t['title']} ({t['status']}, priority {prio})"]
        if t["note"]:
            out.append(f"note: {t['note']}")
        out.append(t["description"])
        for d in ds:
            said = d["answer"] or d["reason"] or ""
            out.append(f"Decision {d['id']} ({d['status']}): {d['question']}" + (f" → {said}" if said else ""))
        return "\n".join(out).rstrip() + "\n"

    # --- run ---------------------------------------------------------------------------------

    def worker_command(self, qid):
        prompt = WORKER_PROMPT.format(qid=qid, db=self.db, show=self.show(qid))
        return [self.claude, "-p", prompt, "--permission-mode", "acceptEdits", "--allowedTools", WORKER_TOOLS]

    @property
    def logs(self):
        """<git dir>/pm-logs: never tracked, so worker logs never make the tree dirty."""
        return Path(self.git("rev-parse", "--absolute-git-dir").strip()) / "pm-logs"

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

    def work(self, qid):
        """Run the worker for qid until no usage limit stops it. Returns ("done" | "blocked" | "failed", message)."""
        cmd, log = self.worker_command(qid), self.logs / f"{qid}.log"
        while True:
            self.say(f"\n== {qid} (log: {log})")
            code = self.run_worker(cmd, log)
            limit = self.limit_line(log)
            if not limit:
                break
            reset = limit_reset(limit)
            if reset is None:
                return "failed", f"usage limit without a readable reset time: {limit}"
            until = max(reset + LIMIT_MARGIN, datetime.datetime.now(reset.tzinfo) + LIMIT_MARGIN)
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
            t = self.find(db, qid)
            blocked = self.blockers(db, t, pending=True)
        if t["status"] != "open":
            return "done", t["status"] + (f": {t['note']}" if t["note"] else "")
        if blocked:
            return "blocked", "waits for " + ", ".join(blocked)
        return "failed", f"exit 0, but {qid} is still open and not blocked\n{tail}"

    def run(self, once=False, dry_run=False, interactive=None, ask=input):
        interactive = sys.stdin.isatty() if interactive is None else interactive
        while True:
            errors = self.validate()
            if errors:
                self.say("the database has errors:\n  " + "\n  ".join(errors))
                return 1
            if dry_run:  # changes nothing: no sync
                q = self.queue()
                if not q:
                    self.say("Queue empty (`pm sync` stubs masterplans for open milestones).")
                else:
                    self.say(" ".join(f"'{a}'" if " " in a else a for a in self.worker_command(q[0][1])))
                return 0
            for change in self.sync():
                self.say(f"sync: {change}")
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
            qid = q[0][1]
            result, message = self.work(qid)
            if result == "failed":
                self.say(f"\n{qid} failed: {message}")
                return 1
            self.say(f"\n{qid}: {result} ({message})")
            for change in self.sync():
                self.say(f"sync: {change}")
            if once:
                return 0

    # --- status ------------------------------------------------------------------------------

    def status(self):
        for m in self.rows(
                "SELECT m.id, m.title, mp.status AS masterplan, "
                "(SELECT count(*) FROM plan p WHERE p.milestone = m.id) AS plans, "
                "(SELECT count(*) FROM task t WHERE t.milestone = m.id) AS tasks, "
                "(SELECT count(*) FROM task t WHERE t.milestone = m.id AND t.status <> 'open') AS closed "
                "FROM milestone m LEFT JOIN masterplan mp ON mp.milestone = m.id WHERE m.status = 'open' ORDER BY m.id"):
            if m["masterplan"] is None:
                self.say(f"{m['id']} {m['title']}: no masterplan yet")
            else:
                self.say(f"{m['id']} {m['title']}: masterplan {m['masterplan']}, {m['plans']} plan(s), "
                         f"{m['closed']}/{m['tasks']} tasks closed")
        self.print_queue(limit=10)
        pending = self.pending()
        self.say(f"\nPending decisions ({len(pending)}):")
        for d in pending:
            self.say(f"  {d['id']} {d['question']}")

    def print_queue(self, limit=None):
        q = self.queue()
        self.say(f"\nQueue ({len(q)}):")
        for prio, qid, title in q[:limit]:
            self.say(f"  {prio:+d} {qid} {title}" if prio else f"     {qid} {title}")
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
    ap.add_argument("--db", default=DB_FILE, help=f"the database, relative to the root (default: {DB_FILE})")
    ap.add_argument("--claude", default="claude", help=argparse.SUPPRESS)  # tests: a fake worker
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name, text in [("status", "open milestones, the queue, pending decisions"), ("queue", "the whole queue"),
                       ("validate", "check the database"), ("sync", "stub masterplans, close milestones"),
                       ("decide", "answer pending decisions in the terminal")]:
        sub.add_parser(name, help=text)
    s = sub.add_parser("show", help="a task with its plan, milestone and decisions")
    s.add_argument("task")
    r = sub.add_parser("run", help="work the queue with claude -p")
    r.add_argument("--once", action="store_true", help="stop after one task")
    r.add_argument("--dry-run", action="store_true", help="print the next worker command; change nothing")

    ms = sub.add_parser("milestone", help="add or change milestones").add_subparsers(dest="action", required=True)
    ma = ms.add_parser("add", help="new open milestone; prints its id")
    ma.add_argument("title")
    ma.add_argument("--description", type=text_arg, help="Markdown, or - for stdin")
    mset = ms.add_parser("set", help="set milestone fields")
    mset.add_argument("milestone")
    mset.add_argument("--title")
    mset.add_argument("--status", choices=MILESTONE_STATES)
    mset.add_argument("--description", type=text_arg)

    mp = sub.add_parser("masterplan", help="set masterplan fields")
    mp.add_argument("milestone")
    mp.add_argument("--status", choices=MASTER_STATES)
    for k in ("goal", "scope", "exit-criteria"):
        mp.add_argument(f"--{k}", type=text_arg)

    p = sub.add_parser("plan", help="add or change plans").add_subparsers(dest="action", required=True)
    pa = p.add_parser("add", help="new plan; prints its id")
    pa.add_argument("milestone")
    pa.add_argument("title")
    pa.add_argument("--description", type=text_arg)
    pa.add_argument("--priority", type=int)
    pp = p.add_parser("priority", help="integer, or none for 0")
    pp.add_argument("plan", help="Mnnn-Pnnn")
    pp.add_argument("priority", type=prio_arg)

    t = sub.add_parser("task", help="add or change tasks").add_subparsers(dest="action", required=True)
    a = t.add_parser("add", help="new task; prints its id")
    a.add_argument("plan", help="Mnnn-Pnnn")
    a.add_argument("title")
    a.add_argument("--description", required=True, type=text_arg, help="Markdown, or - for stdin")
    a.add_argument("--priority", type=int)
    a.add_argument("--blocked-by", nargs="+", default=[], metavar="Dnnnn")
    for verb, text in [("done", "note: date, files or commit"), ("drop", "reason")]:
        x = t.add_parser(verb)
        x.add_argument("task")
        x.add_argument("note", help=text)
    b = t.add_parser("block", help="the task waits for a decision")
    b.add_argument("task")
    b.add_argument("decision")
    tp = t.add_parser("priority", help="integer, or none for the plan's")
    tp.add_argument("task")
    tp.add_argument("priority", type=prio_arg)

    d = sub.add_parser("decision", help="ask the human").add_subparsers(dest="action", required=True)
    da = d.add_parser("add", help="new pending decision; prints its id")
    da.add_argument("question")
    da.add_argument("--context", type=text_arg)
    da.add_argument("--option", action="append", default=[], help="repeat; the recommended one first")
    da.add_argument("--blocks", nargs="+", default=[], metavar="TASK")

    args = ap.parse_args(argv)
    root = args.root or git_root()
    if root is None:
        print("pm: not inside a git repository (or pass --root)")
        return 2
    pm = Project(root=root, claude=args.claude, db=args.db)
    try:
        if args.cmd == "validate":
            errors = pm.validate()
            print("\n".join(errors) if errors else f"{args.db} is valid")
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
        elif args.cmd == "sync":
            changes = pm.sync()
            print("sync: " + (", ".join(changes) if changes else "nothing to do"))
        elif args.cmd == "decide":
            pm.decide()
        elif args.cmd == "run":
            return pm.run(once=args.once, dry_run=args.dry_run)
        elif args.cmd == "milestone":
            if args.action == "add":
                print(pm.milestone_add(args.title, args.description))
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
