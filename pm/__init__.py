"""pm: a project manager for Claude Code workers (README.md). Milestones -> plans -> tasks, plus decisions for the
human, in SQLite: the project keeps them as SQL text in tasks.sql, tracked by git, which every command loads into an
in-memory database (the schema is SCHEMA below). A new milestone comes with a first plan, the masterplan (body
"tbd."), and a task that writes it; a new or edited plan queues a task that splits it into tasks; edits cascade: a
changed milestone queues the revision of its masterplan, a changed plan that of the plans below it and of its open
tasks; implemented milestones, done plans and closed tasks can't be changed; a milestone whose tasks are all closed
gets a last one, the post-milestone cleanup, before it is implemented; a milestone on hold keeps its tasks out of
the queue until it is resumed. An exec task (kind exec) is the human's direct intervention: no plan, so no milestone,
queued as if at milestone queue index 0; one is the executive summary (`pm summary request`, SUMMARY_PRIORITY): its
worker reads the previous summary and stores the new one in the summary table. The queue (the `queue` view): open
tasks of open milestones, and open exec tasks, that no pending decision blocks, by the milestone's queue index (lowest
first; exec: 0), then highest priority first, then by milestone, plan and task.
Worked one task at a time by `claude -p`. Standard library only (Python 3.11+).

    pm init | status | queue | show <task|d32|p3|m1> | validate | decide | run [--once] [--dry-run] | instructions
    pm milestone add|set|hold|resume|list|show …   pm plan add|edit|list …   pm task add|set|done|drop|priority …
    pm decision add|list|show|answer|approve|irrelevant|record|supersede|consequences|kind …
    pm summary request|add|show|list
    pm task-pending-decision <task> <decision>

Works on the git repository around the current directory. `pm init` writes the settings, project-manager.toml (the
template is the file of that name next to this one), and commits it; the first command that uses the database
writes it. pm commits its own changes to tasks.sql; workers commit them with their work. Nothing is pushed.
"""
import argparse, collections, contextlib, datetime, fcntl, functools, json, os, re, shlex, signal, sqlite3, \
    subprocess, sys, threading, time, tomllib, traceback, zoneinfo
from pathlib import Path


VERSION = 41  # bump by one with every commit (AGENTS.md)
CONFIG_FILE = "project-manager.toml"
CONFIG_TEMPLATE = (Path(__file__).parent / CONFIG_FILE).read_text(encoding="utf-8")
INSTRUCTIONS = Path(__file__).parent / "instructions.md"
GUARD = Path(__file__).parent / "guard.py"
# In {settings}: `claude -p` ends with the turn, so a background job dies unwatched; long checks run in the foreground.
# Loose objects (MB) that make pm pack them: each commit adds a full copy of the database; git's own `gc --auto` waits
# for ~6700 objects, gigabytes at a few MB per copy.
REPACK_LOOSE_MB = 50
REPACK = ("import fcntl, subprocess, sys\nf = open(sys.argv[1], 'w')\ntry:\n    fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)\n"
          "except OSError:\n    sys.exit()\nsubprocess.run(['git', 'repack', '-d', '-q'])")
WORKER_ENV = {"CLAUDE_CODE_DISABLE_BACKGROUND_TASKS": "1", "BASH_MAX_TIMEOUT_MS": "3600000"}
EPILOG = "Agents (LLMs): read `pm instructions` first, the full reference with examples for every command."
PROMPT_KEYS = ("masterplan_prompt", "plan_tasks_prompt", "revise_masterplan_prompt", "revise_plan_prompt",
               "cleanup_prompt", "investigate_prompt", "summary_prompt")
PRIORITY_KEYS = ("plan_priority_prompt", "task_priority_prompt")
# Items are referred to by id and full subject, `ref()`: plain in a subject, in backticks in a body.
CLEANUP_SUBJECT = "post-milestone cleanup of {}"
PLAN_TASKS_SUBJECT = "create tasks for {}"
WRITE_MASTERPLAN_SUBJECT = "write the masterplan of {}"
REVISE_MASTERPLAN_SUBJECT = "revise the masterplan of {}"
REVISE_PLAN_SUBJECT = "revise {}"
INVESTIGATE_SUBJECT = "investigate the failure of {}"
SUMMARY_SUBJECT = "write an executive summary"
SUMMARY_PRIORITY = 10000
# Dedupes queued planning tasks: a running one may have read the old text already, so it doesn't count.
NOT_RUNNING = "AND id NOT IN (SELECT task_id FROM run WHERE result = 'running')"
# work: the default; plan: writes a plan; made by pm only: masterplan, split ("create tasks for plan N: …"), cleanup,
# revise ("revise the masterplan of milestone N: …", "revise plan N: …"), investigate (a failed task); made by the
# human only: exec (an executive intervention, the only kind without a plan)
TASK_KINDS = ("work", "plan", "masterplan", "split", "cleanup", "revise", "investigate", "exec")
PLANNING_KINDS = ("plan", "masterplan", "split")
# `signoff` in project-manager.toml → decision.signoff; a sign-off's only answer is SIGNOFF_ANSWER
SIGNOFF_LEVELS = {"masterplans": "masterplan", "plans": "plan", "task-queue": "task-queue", "web": "web"}
# A task marked web gets these, once signed off (or if "web" isn't in `signoff`)
WEB_TOOLS = ["WebSearch", "WebFetch"]
SIGNOFF_ANSWER = "approve"
# The decision log: answered lasting questions that nothing fully supersedes.
IN_LOG = ("status = 'answered' AND signoff IS NULL AND kind = 'rule' AND id NOT IN (SELECT superseded_id FROM "
          "decision_supersedes s JOIN decision n ON n.id = s.decision_id WHERE s.note = '' AND n.status = 'answered')")

SCHEMA_VERSION = 20
# A change to SCHEMA raises SCHEMA_VERSION and adds MIGRATIONS[old version]: SQL (or a function of the connection)
# run on a database of that version, after which the rows are copied into SCHEMA by column name (Project.migrate).
# Columns and tables SCHEMA dropped are left behind, new ones get their defaults; so only renames, moved data and new
# columns without a default need a step, else "".
def revise_kind(con):
    """14 -> 15: revisions of plans, told apart by their subject before, get the kind revise."""
    con.execute("PRAGMA ignore_check_constraints = ON")
    con.execute("UPDATE task SET kind = 'revise' WHERE kind IN ('plan', 'masterplan') "
                "AND (subject LIKE 'revise the masterplan of %' OR subject LIKE 'revise plan %')")


def drop_summary_kind(con):
    """18 -> 19: summary tasks are work tasks without a plan."""
    con.execute("PRAGMA ignore_check_constraints = ON")
    con.execute("UPDATE task SET kind = 'work' WHERE kind = 'summary'")


def exec_kind(con):
    """19 -> 20: tasks without a plan get the kind exec."""
    con.execute("PRAGMA ignore_check_constraints = ON")
    con.execute("UPDATE task SET kind = 'exec' WHERE plan_id IS NULL")


MIGRATIONS = {12: "", 13: "", 14: revise_kind, 15: "", 16: "", 17: "", 18: drop_summary_kind, 19: exec_kind}
# Timestamps: UTC, "2026-10-11T16:30:00Z". created_by_task: the task whose worker created the row; NULL: the human or pm.
SCHEMA = """
CREATE TABLE milestone (
    id             INTEGER PRIMARY KEY,
    subject        TEXT NOT NULL CHECK (trim(subject) <> ''),
    status         TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'on-hold', 'implemented')),
    body           TEXT NOT NULL DEFAULT '',               -- Markdown
    created_at     TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    implemented_at TEXT,
    queue_index    INTEGER NOT NULL DEFAULT 0,             -- its tasks are queued lowest index first, then by priority
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
    plan_id         INTEGER REFERENCES plan (id),          -- NULL: exec, and only exec
    subject         TEXT NOT NULL CHECK (trim(subject) <> ''),
    kind            TEXT NOT NULL DEFAULT 'work' CHECK (kind IN ('work', 'plan', 'masterplan', 'split', 'cleanup', 'revise', 'investigate', 'exec')),
    status          TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'done', 'dropped')),
    priority        INTEGER,                               -- NULL: the plan's
    note            TEXT,                                  -- done: date, files; dropped: reason
    body            TEXT NOT NULL DEFAULT '',              -- Markdown
    created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    retired_at      TEXT,                                  -- done or dropped
    created_by_task INTEGER REFERENCES task (id),
    web             INTEGER NOT NULL DEFAULT 0 CHECK (web IN (0, 1)),  -- 1: its worker gets WebSearch and WebFetch
    CONSTRAINT "an open task needs a body" CHECK (status <> 'open' OR trim(body) <> ''),
    CONSTRAINT "retired_at is set exactly when done or dropped" CHECK ((status = 'open') = (retired_at IS NULL)),
    CONSTRAINT "an exec task has no plan, every other task has one" CHECK ((kind = 'exec') = (plan_id IS NULL))
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
    signoff         TEXT CHECK (signoff IN ('masterplan', 'plan', 'task-queue', 'web')),  -- made by pm; NULL: a question
    kind            TEXT NOT NULL DEFAULT 'rule' CHECK (kind IN ('rule', 'once')),  -- once: a go-ahead, not in the log
    decided_by_task INTEGER REFERENCES task (id),          -- a worker's own choice (`pm decision record`); NULL: the human
    consequences    TEXT NOT NULL DEFAULT '',              -- what changed because of the answer; appended
    CONSTRAINT "a sign-off is only approved"
    CHECK (signoff IS NULL OR status <> 'irrelevant' AND coalesce(answer, 'approve') = 'approve'),
    CONSTRAINT "only an answered question has a decider and consequences"
    CHECK (status = 'answered' AND signoff IS NULL OR decided_by_task IS NULL AND consequences = ''),
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

CREATE TABLE decision_supersedes (                         -- decision_id's answer replaces superseded_id's
    decision_id   INTEGER NOT NULL REFERENCES decision (id),
    superseded_id INTEGER NOT NULL REFERENCES decision (id),
    note          TEXT NOT NULL DEFAULT '',                -- '': all of it; else partly, e.g. "scope only"
    PRIMARY KEY (decision_id, superseded_id),
    CHECK (decision_id <> superseded_id)
) STRICT;

CREATE TABLE summary (                                     -- an executive summary, written by a summary task
    id          INTEGER PRIMARY KEY,
    task_id     INTEGER NOT NULL REFERENCES task (id),
    body        TEXT NOT NULL CHECK (trim(body) <> ''),     -- Markdown
    head_commit TEXT,                                      -- git HEAD when written: the next summary's delta starts here
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
) STRICT;

CREATE INDEX plan_milestone ON plan (milestone_id);
CREATE INDEX task_plan ON task (plan_id);
CREATE INDEX task_decision_decision ON task_decision (decision_id);
CREATE INDEX decision_superseded ON decision_supersedes (superseded_id);
CREATE INDEX run_task ON run (task_id);
CREATE UNIQUE INDEX run_running ON run (result) WHERE result = 'running';  -- at most one

CREATE VIEW queue AS
SELECT coalesce(t.priority, p.priority, 0) AS priority, t.id AS task_id, t.subject, t.kind
FROM task t
LEFT JOIN plan p ON p.id = t.plan_id
LEFT JOIN milestone m ON m.id = p.milestone_id
WHERE (m.status = 'open' OR t.kind = 'exec') AND t.status = 'open'
  AND NOT EXISTS (SELECT 1 FROM task_decision td JOIN decision d ON d.id = td.decision_id
                  WHERE td.task_id = t.id AND d.status = 'pending')
ORDER BY coalesce(m.queue_index, 0), priority DESC, m.id, p.id, t.id;  -- exec: queue index 0
"""

WORKER_PROMPT = """You are the worker for task {id}. Follow the project's own instructions for agents (AGENTS.md, \
CLAUDE.md or similar), then do task {id} and nothing else.

{show}
Before you start, look up what the project decided about what task {id} touches (its subjects, files, terms): \
`pm decision list --answered --grep <word> [--grep <word> …]`. Those answers are binding, unless a newer one \
supersedes them; follow them, and cite one where you rely on it, as `decision <id>: <subject>`.

Refer to a milestone, plan, task or decision by id and full subject, `plan 12: <subject>`: plain in a subject, \
in backticks in a body, a note or a decision's text.

Work you find beyond task {id} (a bug, a missing test, a part the plan forgot, cleanup your change makes necessary) \
is a follow-up task, not part of yours: `pm task add <plan id> "<subject>" < body.md`, in the plan it belongs to \
(usually task {id}'s, above), with a body that lets another worker do it without your context; don't add one the \
plan already lists.

Milestones, plans, tasks and decisions are in {db}; change them only with these commands (a text value - reads stdin):
  pm plan add <milestone id> "<subject>" < body.md     a task to split it into tasks is queued
  pm plan edit <plan id> [--subject …] [--body …] [--priority N]
  pm plans list     plans with open tasks, by milestone queue index, then highest priority first
  pm milestone list     every milestone; pm milestone show <milestone id>
  pm task add <plan id> "<subject>" [--kind plan] [--priority N] [--web] < body.md     --kind plan: the task writes a plan;
      --web: its worker gets WebSearch and WebFetch (after the human's sign-off)
  pm task set <task id> [--subject …] [--body …] [--priority N] [--web|--no-web]     an open task that isn't
      running
  pm decision add "<subject>" [--body "<text>"] [--option "<recommended>"] [--option …]   prints its id
  pm task-pending-decision <task id> <decision id>     the task waits until the human answers
  pm decision list --answered [--grep <word> …]     the decision log: what was decided about X
  pm decision record "<subject>" --answer "<choice>" --body "<why>"   a lasting choice of yours (design, a rule)
  pm decision consequences <decision id> "<text>"     what changed because of an answer
  pm show <task id>     also d<decision id>, p<plan id>, m<milestone id>
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


def option_lines(options):
    """A decision's options, numbered, the recommended one first."""
    return [f"  {n}) {o}{'  (recommended)' if n == 1 else ''}" for n, o in enumerate(options, 1)]


def ref(kind, id, subject, md=False):
    """An item as `<kind> <id>: <subject>`; md: in backticks, for a body."""
    text = f"{kind} {id}: {subject}"
    return f"`{text}`" if md else text


# Claude's errors, in the worker's result ("API Error: …") or, without one, its last log lines. Needs the human: the run
# stops. Passing: the same task again, now, then after RETRY_MINUTES each.
CLAUDE_FATAL = ("invalid api key", "please run /login", "oauth token has expired", "authentication_error",
                "credit balance is too low", "not_found_error", "permission_error")
CLAUDE_PASSING = ("overloaded", "connection error", "timed out", "api error: 5", "api error: 429", "rate_limit_error")
NETWORK_ERRORS = ("econnreset", "econnrefused", "etimedout", "eai_again", "enotfound", "socket hang up", "fetch failed")
RETRY_MINUTES = (0, 1, 2, 5, 10, 20, 30, 60)
# A task that fails again after this many investigations stops `pm run`: else a fix that doesn't fix loops.
MAX_INVESTIGATIONS = 3


def claude_trouble(info, tail):
    """("fatal" | "transient", line) if Claude, not the task, made the run fail; else None. A failure before any
    tool call that matches nothing counts as fatal: pm can't tell it from a broken setup."""
    lines = [l.strip() for l in ([info["result"]] if info.get("result") else reversed(tail)) if l and l.strip()]
    for l in lines:
        low = l.lower()
        if any(w in low for w in CLAUDE_FATAL):
            return "fatal", l[:300]
        if "api error" in low and any(w in low for w in CLAUDE_PASSING) or any(w in low for w in NETWORK_ERRORS):
            return "transient", l[:300]
    if not info.get("last_tool"):
        return "fatal", "failed before any tool call: " + (lines[0][:300] if lines else "no output")
    return None


def creator():
    """The task whose worker runs this command (PM_TASK, set by `pm run`), or None."""
    return int(os.environ["PM_TASK"]) if os.environ.get("PM_TASK", "").isdigit() else None


def human_only(what):
    if creator() is not None:
        raise FormatError(f"a worker doesn't {what}; only the human does")


def matches(d, texts):
    """Decision d mentions one of texts (any case) in its subject, body, options, answer, reason or consequences."""
    hay = "\n".join(str(d[k] or "") for k in ("subject", "body", "options", "answer", "reason", "consequences")).casefold()
    return not texts or any(t.casefold() in hay for t in texts)


def stamp(ts):
    """"2026-10-11 16:30" (UTC) from a timestamp."""
    return ts[:16].replace("T", " ") if ts else ""


def parse_crash(lock):
    """The run lock's content: the pid; after a crash also its time and the error."""
    pid, at, error = (lock.decode(errors="replace").split("\n", 2) + ["", ""])[:3]
    return {"pid": int(pid) if pid.isdigit() else None, "at": at, "error": error.strip()} if at else None


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


def tail_lines(path, n, chunk=1 << 16):
    """The last n lines of a file, read from its end."""
    with open(path, "rb") as f:
        f.seek(0, os.SEEK_END)
        f.seek(max(0, f.tell() - chunk))
        return f.read().decode("utf-8", errors="replace").splitlines()[-n:]


def init(root, out=sys.stdout):
    """`pm init`: write project-manager.toml (the defaults) if missing and commit it. The database is created by
    the first command that uses it."""
    path = Path(root) / CONFIG_FILE
    if path.exists():
        print(f"pm: already initialized ({CONFIG_FILE})", file=out)
        return
    path.write_text(CONFIG_TEMPLATE, encoding="utf-8")
    pm = Project(root, out=out)
    pm.ignore_db()
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


def primary_key(con, table):
    info = con.execute(f"PRAGMA table_info({table})").fetchall()
    return [c[1] for c in sorted(info, key=lambda c: c[5]) if c[5]]


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
        cols = [c[1] for c in con.execute(f"PRAGMA table_info({t})")]
        key = primary_key(con, t)
        rows = con.execute(f"SELECT {', '.join(f'quote({c})' for c in cols)} FROM {t} ORDER BY {', '.join(key)}")
        out += [f"INSERT INTO {t} ({', '.join(cols)}) VALUES ({', '.join(r)});" for r in rows]
        out.append("")
    return "\n".join(out).rstrip() + "\n"


def running_run(db):
    """The run row of the worker running now, or None."""
    r = db.execute("SELECT * FROM run WHERE result = 'running'").fetchone()
    return dict(r) if r else None


def memory():
    con = sqlite3.connect(":memory:", isolation_level=None, check_same_thread=False)
    con.row_factory = sqlite3.Row
    return con


def allow_dump_sql(action, arg1, *_):
    """Authorizer while a database file runs: no ATTACH (it writes files), no PRAGMA but user_version."""
    if action in (sqlite3.SQLITE_ATTACH, sqlite3.SQLITE_DETACH) or (
            action == sqlite3.SQLITE_PRAGMA and arg1 != "user_version"):
        return sqlite3.SQLITE_DENY
    return sqlite3.SQLITE_OK


def load_sql(text):
    """SQL text (a database file, or SCHEMA) in a new in-memory database, in one transaction."""
    con = memory()
    con.set_authorizer(allow_dump_sql)
    try:
        con.executescript(f"BEGIN;\n{text}\nCOMMIT;")
    except BaseException:
        con.close()
        raise
    con.set_authorizer(None)
    return con


@functools.cache
def schema_sql():
    con = load_sql(SCHEMA)
    try:
        return {(r[0], r[1]): r[2] for r in con.execute("SELECT type, name, sql FROM sqlite_schema WHERE sql IS NOT NULL")}
    finally:
        con.close()


def schema_difference(con):
    """How the schema of a loaded database file differs from SCHEMA, or None."""
    have = {(r[0], r[1]): r[2] for r in con.execute("SELECT type, name, sql FROM sqlite_schema WHERE sql IS NOT NULL")}
    want = schema_sql()
    diff = sorted({k for k in have.keys() | want.keys() if have.get(k) != want.get(k)})
    return "schema differs from pm's in " + ", ".join(f"{t} {n}" for t, n in diff) if diff else None


class Project:
    def __init__(self, root, config=None, out=sys.stdout):
        self.root = Path(root)
        self.config = config or tomllib.loads(CONFIG_TEMPLATE)
        self.db = self.config["database"]
        self.db_path = self.root / self.db
        self.out = out
        self.stopping = False       # `pm run` stops after the current task
        self.crash = None           # run_crash() as `pm run` started
        self.cache = None           # (file_key(), the loaded database)
        self.mutex = threading.RLock()
        self.locked = False

    def say(self, *args):
        print(*args, file=self.out, flush=True)

    # --- database ----------------------------------------------------------------------------

    @functools.cached_property
    def git_dir(self):
        return Path(self.git("rev-parse", "--absolute-git-dir").strip())

    @contextlib.contextmanager
    def lock(self):
        """Exclusive while one pm process changes the database."""
        with open(self.git_dir / "pm.lock", "w") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            self.locked = True
            try:
                yield
            finally:
                self.locked = False

    def file_key(self):
        try:
            st = self.db_path.stat()
        except FileNotFoundError:
            return None
        return st.st_ino, st.st_mtime_ns, st.st_size

    @contextlib.contextmanager
    def connect(self, write=False):
        """The database file loaded into memory, foreign keys on; loaded once per process and again only when the
        file changes. write: one transaction under the lock, written back to the file if it changed; rolled back and
        nothing written if the block raises. No file yet: written with the schema."""
        with self.mutex, self.lock() if write else contextlib.nullcontext():
            con = self.loaded()
            before = con.total_changes
            if not con.in_transaction:          # else: nested in a write, which sees its own changes
                con.execute(f"PRAGMA query_only = {'OFF' if write else 'ON'}")
            if write:
                con.execute("BEGIN")
            try:
                yield con
                if con.in_transaction:
                    con.execute("COMMIT")
            except BaseException as e:
                if con.in_transaction:
                    con.execute("ROLLBACK")
                if isinstance(e, sqlite3.IntegrityError):
                    raise FormatError(f"the change breaks the schema, nothing written: {e}") from None
                if isinstance(e, sqlite3.DatabaseError):
                    raise FormatError(f"{self.db}: {e}") from None
                raise
            if write and con.total_changes != before:
                try:
                    self.save(dump(con))
                except BaseException:
                    self.forget()
                    raise
                self.cache = (self.file_key(), con)

    def forget(self):
        if self.cache:
            self.cache[1].close()
        self.cache = None

    def loaded(self):
        """The cached connection, (re)loaded if the file changed since."""
        key = self.file_key()
        if self.cache and self.cache[0] == key and key is not None:
            return self.cache[1]
        self.forget()
        text = self.db_path.read_text(encoding="utf-8") if key else None
        try:
            con = load_sql(text) if text is not None else load_sql(f"{SCHEMA} PRAGMA user_version = {SCHEMA_VERSION};")
        except sqlite3.Error as e:
            if "not authorized" in str(e):
                raise FormatError(f"{self.db} holds a statement a dump doesn't (ATTACH or a PRAGMA besides "
                                  "user_version); pm doesn't run it. Undo the hand edit") from None
            raise FormatError(f"{self.db}: {e}") from None
        version = con.execute("PRAGMA user_version").fetchone()[0]
        if version != SCHEMA_VERSION:
            con = self.migrate(con, version)
            with contextlib.nullcontext() if self.locked else self.lock():
                if self.db_path.read_text(encoding="utf-8") != text:
                    con.close()
                    raise FormatError(f"{self.db} changed while migrating, try again")
                self.save(dump(con))
                self.commit_db(f"{self.db}: schema {version} -> {SCHEMA_VERSION}", running_run(con))
            self.say(f"pm: migrated {self.db} from schema {version} to {SCHEMA_VERSION}")
        elif problem := schema_difference(con):
            con.close()
            raise FormatError(f"{self.db}: {problem}; the schema is pm's (SCHEMA in pm/__init__.py), "
                              f"undo the hand edit or merge")
        elif text is None:
            self.save(dump(con))
        con.execute("PRAGMA foreign_keys = ON")
        self.cache = (self.file_key(), con)
        return con

    def migrate(self, old, version):
        """`old` (schema `version`) brought to SCHEMA_VERSION: MIGRATIONS in order, then the rows of the tables and
        columns SCHEMA still has copied into a fresh SCHEMA database, whose constraints check them."""
        if version > SCHEMA_VERSION:
            raise FormatError(f"{self.db}: schema version {version}, this pm knows up to {SCHEMA_VERSION}: update pm")
        missing = [v for v in range(version, SCHEMA_VERSION) if v not in MIGRATIONS]
        if missing:
            raise FormatError(f"{self.db}: schema version {version}, this pm knows {SCHEMA_VERSION} "
                              f"and has no migration from {missing[0]}")
        try:
            for v in range(version, SCHEMA_VERSION):
                step = MIGRATIONS[v]
                step(old) if callable(step) else old.executescript(step)
            con = load_sql(f"{SCHEMA} PRAGMA user_version = {SCHEMA_VERSION};")
            con.execute("BEGIN")
            for (t,) in con.execute("SELECT name FROM sqlite_schema WHERE type = 'table' ORDER BY rowid").fetchall():
                have = {c[1] for c in old.execute(f"PRAGMA table_info({t})")}
                cols = [c[1] for c in con.execute(f"PRAGMA table_info({t})") if c[1] in have]
                if cols:
                    con.executemany(f"INSERT INTO {t} ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                                    old.execute(f"SELECT {', '.join(cols)} FROM {t}").fetchall())
            con.execute("COMMIT")
        except sqlite3.Error as e:
            raise FormatError(f"{self.db}: migrating from schema {version} failed, nothing written: {e}") from None
        finally:
            old.close()
        return con

    def save(self, text):
        tmp = self.db_path.with_name(self.db_path.name + ".tmp")
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, self.db_path)

    def commit_db(self, message, running=...):
        """Commit the database file if it changed. Not in a worker, nor while one runs: `pm run` commits the file with
        the task. Returns False if git failed."""
        if os.environ.get("PM_WORKER") or not self.git("status", "--porcelain", "--", self.db).strip():
            return True
        if running is ...:
            with self.connect() as db:
                running = running_run(db)
        if running:
            self.say(f"pm: task {running['task_id']} is running; {self.db} is committed with it")
            return True
        try:
            self.git("add", "--", self.db)
            self.git("commit", "-q", "-m", f"pm: {message}", "--", self.db)
            self.gc()
            return True
        except subprocess.CalledProcessError as e:
            self.say(f"pm: committing {self.db} failed: {e.stderr.strip()}")
            return False

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

    def gc(self):
        """Packs loose objects, in the background, once they exceed REPACK_LOOSE_MB: packed, a copy of the database
        is a delta to the one before."""
        try:
            info = dict(l.split(": ", 1) for l in self.git("count-objects", "-v").splitlines())
            if int(info["size"]) > REPACK_LOOSE_MB * 1024:
                subprocess.Popen([sys.executable, "-I", "-c", REPACK, str(self.git_dir / "pm-repack.lock")],
                                 cwd=self.root, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL, start_new_session=True)
        except (subprocess.CalledProcessError, OSError, KeyError, ValueError):
            pass

    def ignore_db(self):
        """`/<database>` in .ignore, committed: ripgrep (and Claude Code's Grep) skip it in searches."""
        path, line = self.root / ".ignore", f"/{self.db}"
        text = path.read_text(encoding="utf-8") if path.exists() else ""
        if line in text.splitlines():
            return
        path.write_text(text + ("" if not text or text.endswith("\n") else "\n") + line + "\n", encoding="utf-8")
        try:
            self.git("add", "--", ".ignore")
            self.git("commit", "-q", "-m", f"pm: .ignore {self.db}, so searches skip it", "--", ".ignore")
        except subprocess.CalledProcessError as e:
            self.say(f"pm: committing .ignore failed: {e.stderr.strip()}")

    def dirty(self):
        """Changes besides the database (pm commits that itself)."""
        return any(l[3:] != self.db for l in self.git("status", "--porcelain").splitlines())

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
                    m = self.get(db, "milestone", mid)
                    db.execute("INSERT INTO task (plan_id, subject, kind, body, created_by_task) "
                               "VALUES (?, ?, 'cleanup', ?, ?)",
                               (pid, CLEANUP_SUBJECT.format(ref("milestone", mid, m["subject"])),
                                self.prompt("cleanup_prompt", milestone=ref("milestone", mid, m["subject"], True)),
                                creator()))
                    cleanup.append(mid)
                else:
                    db.execute("UPDATE milestone SET status = 'implemented', implemented_at = ? WHERE id = ?",
                               (now(), mid))
                    closed.append(mid)
        return closed, cleanup

    # --- decisions ---------------------------------------------------------------------------

    def resolve(self, did, answer=None, irrelevant=None, kind=None, supersedes=()):
        """Close a pending decision (the human only). Answered: its tasks unblock by themselves. Irrelevant: the open
        tasks it blocks are dropped."""
        human_only("answer, approve or close decisions")
        with self.connect(write=True) as db:
            d = self.get(db, "decision", did)
            if d["status"] != "pending":
                raise FormatError(f"decision {did} is not pending")
            if d["signoff"] and (irrelevant is not None or answer != SIGNOFF_ANSWER):
                raise FormatError(f"decision {did} is a sign-off: its only answer is {SIGNOFF_ANSWER!r}")
            if irrelevant is None:
                db.execute("UPDATE decision SET status = 'answered', answer = ?, closed_at = ?, kind = ? WHERE id = ?",
                           (answer, now(), kind or d["kind"], did))
                for old in supersedes:
                    self.link_supersede(db, did, old)
                return
            db.execute("UPDATE decision SET status = 'irrelevant', reason = ?, closed_at = ? WHERE id = ?",
                       (irrelevant, now(), did))
            db.execute("UPDATE task SET status = 'dropped', note = ?, retired_at = ? WHERE status = 'open' "
                       "AND id IN (SELECT task_id FROM task_decision WHERE decision_id = ?)",
                       (f"decision {did} irrelevant", now(), did))

    def decisions(self, answered=False, everything=False, grep=None):
        """`pm decision list`: every pending decision with what it blocks, its context and options. answered: the
        log, newest first: answered lasting questions (kind rule) that nothing fully supersedes. everything: all."""
        if not answered and not everything:
            ds = [d for d in self.pending() if matches(d, grep)]
            return self.decisions_of(ds) or ("No pending decisions" + (" matching " + " or ".join(map(repr, grep)) if grep else "") + ".\n")
        where = "" if everything else "WHERE " + IN_LOG
        ds = [d for d in self.rows(f"SELECT * FROM decision {where} ORDER BY coalesce(closed_at, created_at) DESC, "
                                   "id DESC") if matches(d, grep)]
        return "\n".join(self.log_entry(d) for d in ds) or (
            "No decisions" + (" matching " + " or ".join(map(repr, grep)) if grep else "") + ".\n")

    @staticmethod
    def log_ids(db):
        """Ids of the decisions in the log (`pm decision list --answered`)."""
        return {r[0] for r in db.execute(f"SELECT id FROM decision WHERE {IN_LOG}")}

    def log_entry(self, d):
        """A decision in a few lines: header, answer, consequences, supersede links."""
        out = [f"Decision {d['id']} ({self.decision_tags(d)}): {d['subject']}"]
        if d["answer"] or d["reason"]:
            out.append(f"  → {d['answer'] or d['reason']}".replace("\n", "\n    "))
        if d["consequences"]:
            out.append("  consequences: " + d["consequences"].replace("\n", "\n    "))
        out += ["  " + x for x in self.supersede_lines(d["id"])]
        return "\n".join(out) + "\n"

    @staticmethod
    def decision_tags(d):
        """"rule, answered 2026-10-12 by task 7", "pending, sign-off", …"""
        tags = [d["status"]] if d["signoff"] else [d["kind"], d["status"]]
        if d["closed_at"]:
            tags[-1] += " " + d["closed_at"][:10]
        if d["decided_by_task"]:
            tags[-1] += f" by task {d['decided_by_task']}"
        return ", ".join(tags + ["sign-off"] * bool(d["signoff"]))

    def supersede_lines(self, did):
        """["supersedes decision 28: …", "partly superseded by decision 40: … (scope only)"]."""
        def line(verb, r):
            return (f"{'partly ' if r['note'] else ''}{verb} decision {r['id']}: {r['subject']}"
                    + (f" ({r['note']})" if r["note"] else "") + ("" if r["status"] == "answered" else f" [{r['status']}]"))
        return ([line("supersedes", r) for r in self.rows(
                    "SELECT d.id, d.subject, d.status, s.note FROM decision_supersedes s "
                    "JOIN decision d ON d.id = s.superseded_id WHERE s.decision_id = ? ORDER BY d.id", did)]
                + [line("superseded by", r) for r in self.rows(
                    "SELECT d.id, d.subject, d.status, s.note FROM decision_supersedes s "
                    "JOIN decision d ON d.id = s.decision_id WHERE s.superseded_id = ? ORDER BY d.id", did)])

    def decision_show(self, did):
        """`pm decision show`: everything about one decision."""
        with self.connect() as db:
            d = self.get(db, "decision", did)
            tasks = db.execute("SELECT t.id, t.status, t.subject FROM task_decision td JOIN task t ON t.id = td.task_id "
                               "WHERE td.decision_id = ? ORDER BY t.id", (did,)).fetchall()
        by = f", by task {d['created_by_task']}" if d["created_by_task"] else ""
        out = [f"# Decision {did}: {d['subject']}", "", f"{self.decision_tags(d)}; created {stamp(d['created_at'])}{by}"]
        out += self.supersede_lines(did)
        if d["body"]:
            out += ["", d["body"]]
        if d["signoff"] and d["status"] == "pending":
            out += ["", self.signoff_view(did, d["signoff"])]
        if opts := json.loads(d["options"]):
            out += ["", "Options:"] + option_lines(opts)
        if d["answer"]:
            out += ["", f"Answer ({stamp(d['closed_at'])}" + (f", task {d['decided_by_task']}'s own choice" if
                                                             d["decided_by_task"] else "") + f"): {d['answer']}"]
        elif d["reason"]:
            out += ["", f"Irrelevant ({stamp(d['closed_at'])}): {d['reason']}"]
        if d["consequences"]:
            out += ["", "Consequences:", d["consequences"]]
        if tasks:
            out += ["", "Tasks:"] + [f"  task {t['id']} [{t['status']}] {t['subject']}" for t in tasks]
        return "\n".join(out).rstrip() + "\n"

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
            out += option_lines(d["options"])
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
            reply = ask(f"Answer [{hint}your own text, o <answer> = one-off (not in the log), s = skip, "
                        f"x <reason> = irrelevant]: ").strip()
            if not reply or reply.lower() == "s":
                continue
            if reply.lower() == "x" or reply.lower().startswith("x "):
                self.resolve(d["id"], irrelevant=reply[1:].strip() or "no reason given")
            elif reply.lower().startswith("o ") and reply[2:].strip():
                self.answer(d["id"], reply[2:].strip(), kind="once")
            else:
                self.answer(d["id"], reply)
            closed += 1
        return closed

    def approve(self, did):
        with self.connect() as db:
            if not self.get(db, "decision", did)["signoff"]:
                raise FormatError(f"decision {did} is not a sign-off; answer it (pm decision answer)")
        self.resolve(did, answer=SIGNOFF_ANSWER)

    def answer(self, did, reply, kind=None, supersedes=()):
        """Answer decision did: an option's number or own text. Returns the answer."""
        with self.connect() as db:
            d = self.get(db, "decision", did)
            opts = json.loads(d["options"])
        if d["signoff"] and reply != SIGNOFF_ANSWER:
            raise FormatError(f"decision {did} is a sign-off: approve it (pm decision approve {did})")
        if reply.isdigit() and 1 <= int(reply) <= len(opts):
            reply = opts[int(reply) - 1]
        self.resolve(did, answer=reply, kind=kind, supersedes=supersedes)
        return reply

    def record(self, subject, answer, body=None, kind="rule", supersedes=(), consequences=None):
        """`pm decision record`: a decision nobody asked, added answered: the human's, or in a worker its own choice
        (decided_by_task). Returns its id."""
        with self.connect(write=True) as db:
            did = db.execute("INSERT INTO decision (subject, body, status, answer, closed_at, created_by_task, kind, "
                             "decided_by_task) VALUES (?, ?, 'answered', ?, ?, ?, ?, ?)",
                             (subject, (body or "").strip(), answer.strip(), now(), creator(), kind,
                              creator())).lastrowid
            for old in supersedes:
                self.link_supersede(db, did, old)
            if consequences:
                self.add_consequences(db, did, consequences)
            return did

    def supersede(self, new, old, note=""):
        with self.connect(write=True) as db:
            self.link_supersede(db, new, old, note)

    def link_supersede(self, db, new, old, note=""):
        """Decision new's answer replaces old's (partly, when note says what). Both answered questions; in a worker,
        old must be a worker's own choice: a human's answer is superseded only by the human."""
        n, o = self.get(db, "decision", new), self.get(db, "decision", old)
        for d in (n, o):
            if d["signoff"] or d["status"] != "answered":
                raise FormatError(f"decision {d['id']} is {'a sign-off' if d['signoff'] else d['status']}: "
                                  "only answered questions supersede or are superseded")
        if new == old or db.execute(
                "WITH RECURSIVE up(id) AS (SELECT ? UNION SELECT s.superseded_id FROM decision_supersedes s "
                "JOIN up ON s.decision_id = up.id) SELECT 1 FROM up WHERE id = ?", (old, new)).fetchone():
            raise FormatError(f"decision {old} already supersedes decision {new}" if new != old else
                              f"decision {new} can't supersede itself")
        if creator() is not None and o["decided_by_task"] is None:
            raise FormatError(f"decision {old} is the human's: a worker doesn't supersede it. Ask with "
                              f"`pm decision add` (mention decision {old}) and wait for the answer")
        db.execute("INSERT OR REPLACE INTO decision_supersedes VALUES (?, ?, ?)", (new, old, (note or "").strip()))

    def consequences(self, did, text):
        with self.connect(write=True) as db:
            self.add_consequences(db, did, text)

    def add_consequences(self, db, did, text):
        """Append what changed because of decision did's answer, dated, with the task that wrote it."""
        d = self.get(db, "decision", did)
        if d["signoff"] or d["status"] != "answered":
            raise FormatError(f"decision {did} is {'a sign-off' if d['signoff'] else d['status']}: consequences "
                              "belong to answered questions")
        if not (text := (text or "").strip()):
            raise FormatError("no consequences given")
        head = now()[:10] + (f", task {creator()}" if creator() else "")
        db.execute("UPDATE decision SET consequences = ? WHERE id = ?",
                   ("\n\n".join(x for x in (d["consequences"], f"{head}: {text}") if x), did))

    def set_kind(self, did, kind):
        human_only("change a decision's kind")
        with self.connect(write=True) as db:
            if self.get(db, "decision", did)["signoff"]:
                raise FormatError(f"decision {did} is a sign-off; it has no kind")
            db.execute("UPDATE decision SET kind = ? WHERE id = ?", (kind, did))

    # --- sign-offs ---------------------------------------------------------------------------

    def signoff_levels(self, db, t):
        """The sign-offs task t needs under `signoff` in project-manager.toml."""
        if t["kind"] == "split":
            master = db.execute("SELECT 1 FROM task WHERE plan_id = ? AND kind = 'masterplan'", (t["plan_id"],))
            levels = ["masterplan" if master.fetchone() else "plan"]
        else:
            levels = ["task-queue"] if t["kind"] == "work" else []
        levels += ["web"] * bool(t["web"])
        return [x for x in levels if x in [SIGNOFF_LEVELS[c] for c in self.config["signoff"]]]

    def missing_signoff(self, db, t):
        return next((x for x in self.signoff_levels(db, t) if not self.signed_off(db, t["id"], x)), None)

    @staticmethod
    def signed_off(db, tid, level):
        return db.execute("SELECT 1 FROM task_decision td JOIN decision d ON d.id = td.decision_id "
                          "WHERE td.task_id = ? AND d.signoff = ? AND d.status = 'answered'", (tid, level)).fetchone()

    def needs_signoff(self, tid):
        """The sign-off task tid needs and doesn't have yet, or None."""
        with self.connect() as db:
            return self.missing_signoff(db, self.get(db, "task", tid))

    def web_granted(self, tid):
        """Task tid is marked web and needs no web sign-off, or has it."""
        with self.connect() as db:
            t = self.get(db, "task", tid)
            return bool(t["web"]) and ("web" not in self.signoff_levels(db, t) or bool(self.signed_off(db, tid, "web")))

    def request_signoff(self, tid):
        """Make task tid wait for its next sign-off. Plan, masterplan or web: a new decision. Task queue: the pending
        one, else a new one, for every open work task of open milestones without a sign-off. Returns the decision id."""
        with self.connect(write=True) as db:
            t = self.get(db, "task", tid)
            level = self.missing_signoff(db, t)
            did = None
            if level == "task-queue":
                did = db.execute("SELECT min(id) FROM decision WHERE signoff = 'task-queue' AND status = 'pending'"
                                 ).fetchone()[0]
                tids = [r[0] for r in db.execute(
                    "SELECT t.id FROM task t JOIN plan p ON p.id = t.plan_id JOIN milestone m ON m.id = p.milestone_id "
                    "WHERE m.status = 'open' AND t.status = 'open' AND t.kind = 'work' ORDER BY t.id").fetchall()
                        if not self.signed_off(db, r[0], level)]
                subject = "Sign off the task queue"
            elif level == "web":
                tids = [tid]
                subject = f"Grant {ref('task', tid, t['subject'])} web access (WebSearch, WebFetch)"
            else:
                p = self.get(db, "plan", t["plan_id"])
                tids = [tid]
                m = self.get(db, "milestone", p["milestone_id"])
                subject = (f"Sign off the masterplan of {ref('milestone', m['id'], m['subject'])}"
                           if level == "masterplan" else f"Sign off {ref('plan', p['id'], p['subject'])}")
            if did is None:
                did = db.execute("INSERT INTO decision (subject, options, signoff) VALUES (?, ?, ?)",
                                 (subject, json.dumps([SIGNOFF_ANSWER]), level)).lastrowid
            for x in tids:
                self.block(db, x, did)
        return did

    def signoff_view(self, did, level):
        """What a pending sign-off approves, as it is now: the plan, or the waiting tasks by plan."""
        ts = self.rows("SELECT t.id, t.subject, t.body, p.id AS plan_id, p.subject AS plan_subject, p.body AS plan_body "
                       "FROM task_decision td JOIN task t ON t.id = td.task_id JOIN plan p ON p.id = t.plan_id "
                       "WHERE td.decision_id = ? AND t.status = 'open' ORDER BY p.id, t.id", did)
        if not ts:
            return "No open task waits for it."
        if level == "web":
            return "\n\n".join(f"# Task {t['id']}: {t['subject']}\n\n{t['body']}" for t in ts)
        if level != "task-queue":
            return "\n\n".join(f"# Plan {t['plan_id']}: {t['plan_subject']}\n\n{t['plan_body']}" for t in ts)
        out, plan = [], None
        for t in ts:
            if t["plan_id"] != plan:
                plan = t["plan_id"]
                out.append(f"Plan {plan}: {t['plan_subject']}")
            out.append(f"  task {t['id']}: {t['subject']}")
        return "\n".join(out)

    # --- executive summaries -------------------------------------------------------------------

    def summary_request(self):
        """Queue an exec task that writes an executive summary, unless one is open. Returns (its id, newly queued)."""
        if (old := self.open_summary()) is not None:
            return old, False
        return self.task_add(None, SUMMARY_SUBJECT, self.prompt("summary_prompt"), SUMMARY_PRIORITY, "exec"), True

    def open_summary(self):
        """The open exec task that writes an executive summary, or None."""
        rows = self.rows("SELECT id FROM task WHERE kind = 'exec' AND status = 'open' AND subject = ? ORDER BY id",
                         SUMMARY_SUBJECT)
        return rows[0]["id"] if rows else None

    def summary_add(self, body):
        """Store the summary the running exec task wrote. Returns its id."""
        tid = creator()
        with self.connect(write=True) as db:
            if tid is None or (t := self.get(db, "task", tid))["kind"] != "exec" or t["status"] != "open":
                raise FormatError("only the worker of an open exec task adds a summary (`pm summary request`)")
            if not body.strip():
                raise FormatError("the summary is empty")
            return db.execute("INSERT INTO summary (task_id, body, head_commit) VALUES (?, ?, ?)",
                              (tid, body.strip(), self.head())).lastrowid

    def summary_show(self, sid=None):
        """Summary sid, or the newest; with the commit its delta starts from for the next one."""
        rows = self.rows("SELECT * FROM summary WHERE id = ?", sid) if sid else self.rows(
            "SELECT * FROM summary ORDER BY id DESC LIMIT 1")
        if not rows:
            if sid:
                raise FormatError(f"no summary {sid}")
            return "No executive summary yet.\n"
        s = rows[0]
        at = f", at commit {s['head_commit'][:12]}" if s["head_commit"] else ""
        return f"# Executive summary {s['id']} ({stamp(s['created_at'])}, by task {s['task_id']}{at})\n\n{s['body']}\n"

    def summaries(self):
        """`pm summary list`: id, date, commit, first line; newest first."""
        rows = self.rows("SELECT * FROM summary ORDER BY id DESC")
        return "".join(f"{s['id']:4d}  {stamp(s['created_at'])}  {(s['head_commit'] or '')[:7]:<7}  "
                       f"{s['body'].splitlines()[0].lstrip('# ')[:80]}\n" for s in rows) or "No executive summary yet.\n"

    # --- edit commands -----------------------------------------------------------------------

    def edit(self, name, template=""):
        """Open the user's editor (as `git commit` does) on <git dir>/<name> holding `template`. Returns the saved
        text, or None if the editor failed or the file was not saved. No comment lines: '#' starts Markdown headings."""
        path = self.git_dir / name
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
            if sys.stdin.isatty():
                print(f"pm: the body of the {what}, then Ctrl-D (or pipe it: … < body.md):", file=sys.stderr)
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

    def milestone_add(self, subject, body="", queue_index=0):
        """A new milestone with its first plan, the masterplan (a stub), and the task that writes it."""
        human_only("add or change milestones (raise a decision instead: pm decision add)")
        with self.connect(write=True) as db:
            mid = db.execute("INSERT INTO milestone (subject, body, queue_index) VALUES (?, ?, ?)",
                             (subject, (body or "").strip(), queue_index or 0)).lastrowid
            pid = db.execute("INSERT INTO plan (milestone_id, subject, priority, body, created_by_task) "
                             "VALUES (?, 'masterplan', ?, 'tbd.', ?)",
                             (mid, self.config["planning_priority"], creator())).lastrowid
            db.execute("INSERT INTO task (plan_id, subject, kind, body, created_by_task) "
                       "VALUES (?, ?, 'masterplan', ?, ?)",
                       (pid, WRITE_MASTERPLAN_SUBJECT.format(ref("milestone", mid, subject)),
                        self.prompt("masterplan_prompt", milestone_id=mid, milestone_subject=subject, plan_id=pid,
                                    milestone=ref("milestone", mid, subject, True),
                                    plan=ref("plan", pid, "masterplan", True)),
                        creator()))
        return mid

    def milestone_hold(self, mid, hold):
        """Put a milestone on hold, or resume one on hold."""
        if not hold and self.rows("SELECT 1 FROM milestone WHERE id = ? AND status <> 'on-hold'", mid):
            raise FormatError(f"milestone {mid} is not on hold")
        self.milestone_set(mid, status="on-hold" if hold else "open")

    def milestone_set(self, mid, **fields):
        """Set fields of a milestone (subject, status, body, queue_index); None leaves a field as it is. A changed subject or body
        queues the revision of the masterplan; refused once the milestone is implemented or every plan of it is done.
        Returns the queued tasks: [(id, subject)]."""
        human_only("add or change milestones (raise a decision instead: pm decision add)")
        with self.connect(write=True) as db:
            m = self.get(db, "milestone", mid)
            changed = any(fields.get(k) is not None and fields[k].strip() != m[k] for k in ("subject", "body"))
            master = self.masterplan(db, mid)
            if changed and (m["status"] == "implemented" or master and self.plan_done(db, master)):
                why = "implemented" if m["status"] == "implemented" else "done: every plan of it is done"
                raise FormatError(f"milestone {mid} is {why}; its subject and body can't be changed. "
                                  f"Add a new milestone instead: pm milestone add")
            status = fields.get("status")
            if status is not None and status != m["status"]:
                if m["status"] == "implemented":
                    raise FormatError(f"milestone {mid} is implemented; its status can't change. "
                                      f"Add a new milestone instead: pm milestone add")
                if status not in ("open", "on-hold"):
                    raise FormatError(f"a milestone is implemented by pm, once its tasks are closed; "
                                      f"set open or on-hold")
            for k, v in fields.items():
                if k == "status" and v is not None:
                    db.execute("UPDATE milestone SET status = ?, implemented_at = CASE WHEN ? = 'implemented' "
                               "THEN coalesce(implemented_at, ?) END WHERE id = ?", (v, v, now(), mid))
                elif v is not None:
                    db.execute(f"UPDATE milestone SET {k} = ? WHERE id = ?", (v.strip() if isinstance(v, str) else v, mid))
            if fields.get("subject") is not None:
                self.rename_refs(db, "milestone", mid, m["subject"], fields["subject"].strip())
            return [q for q in [self.queue_revision(db, master)] if q] if changed and master else []

    # --- plan tree ---------------------------------------------------------------------------
    # The masterplan is the plan with the task "write the masterplan of …". A plan's parent: the plan of the task that wrote
    # it, if in the same milestone; else the masterplan. Done: has tasks, none open, and every child plan is done.

    @staticmethod
    def masterplan(db, mid):
        return db.execute("SELECT min(t.plan_id) FROM task t JOIN plan p ON p.id = t.plan_id "
                          "WHERE p.milestone_id = ? AND t.kind = 'masterplan'", (mid,)).fetchone()[0]

    def plan_children(self, db, mid):
        """{plan id: [ids of the plans below it]} of milestone mid."""
        master = self.masterplan(db, mid)
        rows = db.execute("SELECT p.id, CASE WHEN q.milestone_id = p.milestone_id AND q.id <> p.id THEN q.id END "
                          "FROM plan p LEFT JOIN task t ON t.id = p.created_by_task LEFT JOIN plan q ON q.id = t.plan_id "
                          "WHERE p.milestone_id = ? AND p.id <> ? ORDER BY p.id", (mid, master or 0)).fetchall()
        children = {}
        for c, parent in rows:
            if (parent or master) not in (None, c):
                children.setdefault(parent or master, []).append(c)
        return children

    def child_plans(self, db, pid):
        return self.plan_children(db, self.get(db, "plan", pid)["milestone_id"]).get(pid, [])

    def plan_done(self, db, pid, children=None, memo=None):
        """Plan pid has tasks, none open, and every plan below it is done. children: plan_children of its milestone;
        memo: answers so far (a cycle counts as done)."""
        if children is None:
            children = self.plan_children(db, self.get(db, "plan", pid)["milestone_id"])
        memo = {} if memo is None else memo
        if pid not in memo:
            memo[pid] = True
            n, open_ = db.execute("SELECT count(*), coalesce(sum(status = 'open'), 0) FROM task WHERE plan_id = ?",
                                  (pid,)).fetchone()
            memo[pid] = bool(n) and not open_ and all(self.plan_done(db, c, children, memo) for c in children.get(pid, []))
        return memo[pid]

    def done_plans(self):
        """Ids of the done plans."""
        with self.connect() as db:
            out = []
            for (mid,) in db.execute("SELECT id FROM milestone ORDER BY id").fetchall():
                children, memo = self.plan_children(db, mid), {}
                out += [pid for (pid,) in db.execute("SELECT id FROM plan WHERE milestone_id = ?", (mid,)).fetchall()
                        if self.plan_done(db, pid, children, memo)]
            return sorted(out)

    def check_plan_open(self, db, pid):
        if self.plan_done(db, pid):
            mid = self.get(db, "plan", pid)["milestone_id"]
            raise FormatError(f"plan {pid} is done (no open task, nor in the plans below it) and can't be changed; "
                              f"add a new plan instead: pm plan add {mid}")

    @staticmethod
    def rename_refs(db, kind, rid, old, new):
        """A renamed item: its references in open tasks that aren't running and in pending decisions follow."""
        old, new = ref(kind, rid, old), ref(kind, rid, new)
        if old == new:
            return
        db.execute("UPDATE task SET subject = replace(subject, ?1, ?2), body = replace(body, ?1, ?2) "
                   "WHERE status = 'open' " + NOT_RUNNING, (old, new))
        db.execute("UPDATE decision SET subject = replace(subject, ?1, ?2), body = replace(body, ?1, ?2) "
                   "WHERE status = 'pending'", (old, new))

    def queue_revision(self, db, pid, parent=None):
        """Queue the task that brings plan pid in line with its changed milestone (the masterplan) or parent plan,
        unless pid is done. A waiting one gets the new parent text. Returns (id, subject) of a new task, else None."""
        if self.plan_done(db, pid):
            return None
        p = self.get(db, "plan", pid)
        if parent is None:
            m = self.get(db, "milestone", p["milestone_id"])
            subject = REVISE_MASTERPLAN_SUBJECT.format(ref("milestone", m["id"], m["subject"]))
            body = self.prompt("revise_masterplan_prompt", plan_id=pid, plan=ref("plan", pid, p["subject"], True),
                               milestone=ref("milestone", m["id"], m["subject"], True))
            if db.execute("SELECT 1 FROM task WHERE plan_id = ? AND kind IN ('masterplan', 'revise') AND status = 'open' "
                          + NOT_RUNNING, (pid,)).fetchone():
                return None
        else:
            q = self.get(db, "plan", parent)
            subject = REVISE_PLAN_SUBJECT.format(ref("plan", pid, p["subject"]))
            body = (self.prompt("revise_plan_prompt", plan_id=pid, parent_plan_id=parent,
                                plan=ref("plan", pid, p["subject"], True),
                                parent_plan=ref("plan", parent, q["subject"], True))
                    + f"\n\n# Plan {parent}: {q['subject']}\n\n{q['body']}")
            if old := db.execute("SELECT id FROM task WHERE plan_id = ? AND kind = 'revise' AND status = 'open' "
                                 + NOT_RUNNING, (pid,)).fetchone():
                db.execute("UPDATE task SET subject = ?, body = ? WHERE id = ?", (subject, body, old[0]))
                return None
        tid = db.execute("INSERT INTO task (plan_id, subject, kind, priority, body, created_by_task) "
                         "VALUES (?, ?, ?, ?, ?, ?)", (pid, subject, "revise",
                                                       self.config["planning_priority"], body, creator())).lastrowid
        return tid, subject

    def queue_plan_tasks(self, db, pid):
        """Queue the task that splits plan pid into tasks, unless one is waiting. Returns (id, subject) or None."""
        p = self.get(db, "plan", pid)
        subject = PLAN_TASKS_SUBJECT.format(ref("plan", pid, p["subject"]))
        body = self.prompt("plan_tasks_prompt", plan_id=pid, plan_subject=p["subject"],
                           plan=ref("plan", pid, p["subject"], True))
        if old := db.execute("SELECT id FROM task WHERE plan_id = ? AND kind = 'split' AND status = 'open' "
                             + NOT_RUNNING, (pid,)).fetchone():
            db.execute("UPDATE task SET subject = ?, body = ? WHERE id = ?", (subject, body, old[0]))
            return None
        return db.execute("INSERT INTO task (plan_id, subject, kind, priority, body, created_by_task) "
                          "VALUES (?, ?, 'split', ?, ?, ?)",
                          (pid, subject, self.config["planning_priority"], body, creator())).lastrowid, subject

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
            if subject is not None:
                self.rename_refs(db, "plan", pid, p["subject"], subject.strip())
            if not changed:
                return []
            queued = [self.queue_plan_tasks(db, pid)] + [self.queue_revision(db, c, pid) for c in self.child_plans(db, pid)]
            return [q for q in queued if q]

    def task_add(self, pid, subject, body, prio=None, kind="work", web=False):
        """A new task; a plan-writing one gets planning_priority unless prio is given. kind exec: the human's, pid None."""
        if prio is None and kind in PLANNING_KINDS:
            prio = self.config["planning_priority"]
        if kind == "exec":
            human_only("add an exec task")
        with self.connect(write=True) as db:
            if kind != "exec":
                self.check_plan_open(db, pid)
            return db.execute("INSERT INTO task (plan_id, subject, kind, priority, body, created_by_task, web) "
                              "VALUES (?, ?, ?, ?, ?, ?, ?)",
                              (pid, subject, kind, prio, body.strip(), creator(), int(web))).lastrowid

    def task_set(self, tid, **fields):
        """Set fields of an open task (status, note, priority, subject, body, web); a value None clears it. A running task:
        only its own worker changes it, and not its subject or body."""
        with self.connect(write=True) as db:
            if (t := self.get(db, "task", tid))["status"] != "open":
                raise FormatError(f"task {tid} is {t['status']}; a closed task can't be changed")
            if (r := running_run(db)) and r["task_id"] == tid:
                if creator() != tid:
                    raise FormatError(f"task {tid} is running; it can't be changed until its run ends")
                if {"subject", "body"} & fields.keys():
                    raise FormatError(f"task {tid} is running; its subject and body can't be changed. "
                                      f"Add a follow-up task instead: pm task add {t['plan_id']}")
            for k, v in fields.items():
                if k == "status":
                    db.execute("UPDATE task SET status = ?, retired_at = CASE WHEN ? <> 'open' THEN ? END WHERE id = ?",
                               (v, v, now(), tid))
                else:
                    db.execute(f"UPDATE task SET {k} = ? WHERE id = ?", (v, tid))
            if fields.get("subject") is not None:
                self.rename_refs(db, "task", tid, t["subject"], fields["subject"])

    def task_pending_decision(self, tid, did):
        with self.connect(write=True) as db:
            self.block(db, tid, did)

    def decision_add(self, subject, body=None, options=(), kind="rule"):
        with self.connect(write=True) as db:
            return db.execute("INSERT INTO decision (subject, options, body, created_by_task, kind) VALUES (?, ?, ?, ?, ?)",
                              (subject, json.dumps(list(options), ensure_ascii=False), (body or "").strip(),
                               creator(), kind)).lastrowid

    # --- show --------------------------------------------------------------------------------

    def show_ref(self, ref):
        """`pm show`: "7" or "t7" (a task), "d32" or "decision 32", "p3" or "plan 3", "m1" or "milestone 1", "s2" or
        "summary 2"."""
        m = re.fullmatch(r"(?:(t|task|d|decision|p|plan|m|milestone|s|summary)\s*)?(\d+)", " ".join(ref).strip().lower())
        if not m:
            raise FormatError(f"not a task, decision, plan, milestone or summary: {' '.join(ref)!r} (e.g. 7, d32, p3, m1, s2)")
        rid = int(m[2])
        return {"d": self.decision_show, "p": self.plan_show, "m": self.milestone_show, "s": self.summary_show}.get(
            (m[1] or "t")[0], self.show)(rid)

    def plan_show(self, pid):
        with self.connect() as db:
            p = self.get(db, "plan", pid)
            m = self.get(db, "milestone", p["milestone_id"])
            tasks = db.execute("SELECT id, status, kind, subject FROM task WHERE plan_id = ? ORDER BY id",
                               (pid,)).fetchall()
        by = f", by task {p['created_by_task']}" if p["created_by_task"] else ""
        out = [f"# Plan {pid}: {p['subject']} (milestone {m['id']}: {m['subject']}; priority {p['priority'] or 0}, "
               f"created {stamp(p['created_at'])}{by})", "", p["body"], ""]
        out += [f"- task {x['id']} [{x['status']}{'' if x['kind'] == 'work' else ', ' + x['kind']}] {x['subject']}"
                for x in tasks]
        return "\n".join(out).rstrip() + "\n"

    def milestone_show(self, mid):
        with self.connect() as db:
            m = self.get(db, "milestone", mid)
            plans = db.execute("SELECT p.id, p.subject, sum(t.status = 'open') AS open, count(t.id) AS n FROM plan p "
                               "LEFT JOIN task t ON t.plan_id = p.id WHERE p.milestone_id = ? GROUP BY p.id ORDER BY p.id",
                               (mid,)).fetchall()
        implemented = f", implemented {stamp(m['implemented_at'])}" if m["implemented_at"] else ""
        out = [f"# Milestone {mid}: {m['subject']} ({m['status']}, queue index {m['queue_index']}, created {stamp(m['created_at'])}{implemented})", "",
               m["body"], ""]
        out += [f"- plan {x['id']}: {x['subject']} ({x['open'] or 0} of {x['n']} task(s) open)" for x in plans]
        return "\n".join(out).rstrip() + "\n"

    def milestones(self):
        """`pm milestone list`: every milestone, one per line: id, queue index, status, closed/all tasks, subject;
        by queue index."""
        rows = self.rows("SELECT m.id, m.queue_index, m.status, m.subject, count(t.id) AS n, coalesce(sum(t.status <> 'open'), 0) AS closed "
                         "FROM milestone m LEFT JOIN plan p ON p.milestone_id = m.id LEFT JOIN task t ON t.plan_id = p.id "
                         "GROUP BY m.id ORDER BY m.queue_index, m.id")
        if not rows:
            return "No milestones (`pm milestone add` starts one).\n"
        return "\n".join(["  id  queue  status       tasks    subject"] + [
            f"{m['id']:4d}  {m['queue_index']:5d}  {m['status']:<11}  {str(m['closed']) + '/' + str(m['n']):<7}  {m['subject']}"
            for m in rows]) + "\n"

    def show(self, tid):
        """The task with its plan and milestone: the context a worker gets."""
        with self.connect() as db:
            t = self.get(db, "task", tid)
            p = m = None
            if t["plan_id"] is not None:
                p = self.get(db, "plan", t["plan_id"])
                m = self.get(db, "milestone", p["milestone_id"])
                plans = db.execute("SELECT id, subject FROM plan WHERE milestone_id = ? ORDER BY id", (m["id"],)).fetchall()
                siblings = db.execute("SELECT id, status, kind, subject FROM task WHERE plan_id = ? ORDER BY id",
                                      (p["id"],)).fetchall()
            ds = db.execute("SELECT d.* FROM task_decision td JOIN decision d ON d.id = td.decision_id "
                            "WHERE td.task_id = ? ORDER BY d.id", (tid,)).fetchall()
            runs = db.execute("SELECT * FROM run WHERE task_id = ? ORDER BY id", (tid,)).fetchall()
            last_waiting = {r[0] for r in db.execute(
                "SELECT d.id FROM task_decision td JOIN decision d ON d.id = td.decision_id WHERE td.task_id = ? "
                "AND d.status = 'answered' AND d.signoff IS NULL AND d.kind = 'rule' AND NOT EXISTS (SELECT 1 FROM "
                "task_decision o JOIN task t ON t.id = o.task_id WHERE o.decision_id = d.id AND o.task_id <> ? "
                "AND t.status = 'open')", (tid, tid))}
        prio = t["priority"] if t["priority"] is not None else (p and p["priority"]) or 0
        by = lambda r: f", by task {r['created_by_task']}" if r["created_by_task"] else ""
        out = []
        if p:
            implemented = f", implemented {stamp(m['implemented_at'])}" if m["implemented_at"] else ""
            out = [f"# Milestone {m['id']}: {m['subject']} ({m['status']}, created {stamp(m['created_at'])}{implemented})",
                   "", m["body"], "", "Plans: " + ", ".join(f"{x['id']} {x['subject']}" for x in plans), "",
                   f"# Plan {p['id']}: {p['subject']} (priority {p['priority'] or 0}, created {stamp(p['created_at'])}{by(p)})",
                   "", p["body"], ""]
            out += [f"- task {x['id']} [{x['status']}{'' if x['kind'] == 'work' else ', ' + x['kind']}] {x['subject']}"
                    + (" ← this task" if x["id"] == tid else "") for x in siblings] + [""]
        retired = f", retired {stamp(t['retired_at'])}" if t["retired_at"] else ""
        web = (", web access" if self.web_granted(tid) else ", web: needs sign-off") if t["web"] else ""
        out += [f"# Task {tid}: {t['subject']} ({t['status']}, kind {t['kind']}, priority {prio}{web}, "
                f"created {stamp(t['created_at'])}{retired}{by(t)})", ""]
        if t["note"]:
            out.append(f"note: {t['note']}")
        out += [t["body"], ""]
        for d in ds:
            said = d["answer"] or d["reason"] or ""
            out.append(f"Decision {d['id']} ({d['status']}{', sign-off' if d['signoff'] else ''}): {d['subject']}"
                       + (f" → {said}" if said else ""))
            if t["status"] == "open" and d["id"] in last_waiting:
                out.append(f"  Task {tid} is the last open task that waited for decision {d['id']}: when done, record "
                           f"what changed because of its answer (fields, files, rules, what no longer holds): "
                           f"pm decision consequences {d['id']} \"<text>\"")
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
        """[(priority, plan id, subject)] of plans with an open task in an open milestone, by the milestone's queue
        index, then highest priority first."""
        return [tuple(r.values()) for r in self.rows(
            "SELECT coalesce(p.priority, 0) AS priority, p.id, p.subject FROM plan p "
            "JOIN milestone m ON m.id = p.milestone_id WHERE m.status = 'open' "
            "AND EXISTS (SELECT 1 FROM task t WHERE t.plan_id = p.id AND t.status = 'open') "
            "ORDER BY m.queue_index, priority DESC, m.id, p.id")]

    def priority_prompts(self):
        return [text for k in PRIORITY_KEYS if (text := self.prompt(k))]

    def worker_command(self, tid):
        """(command, prompt): the prompt goes to the worker's stdin; None if the command holds it ("{prompt}")."""
        show = "\n".join([self.show(tid), self.earlier_failure(tid), *self.priority_prompts()]).rstrip() + "\n"
        prompt = WORKER_PROMPT.format(id=tid, db=self.db, show=show)
        hook = {"type": "command", "command": shlex.join([sys.executable, str(GUARD)])}
        permissions = {"deny": [f"Read(/{self.db_path.resolve()})"]}
        if self.web_granted(tid):
            permissions["allow"] = WEB_TOOLS
        settings = json.dumps({"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [hook]}], "Stop": [{"hooks": [hook]}]},
                               "permissions": permissions, "env": WORKER_ENV})
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
        return self.git_dir / self.config["log_dir"]

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
            with open(log, "w", encoding="utf-8", errors="replace") as f, subprocess.Popen(
                    cmd, cwd=self.root, env=os.environ | {"PM_WORKER": "1", "PM_TASK": str(tid), "PM_DB": self.db},
                    stdin=subprocess.DEVNULL if prompt is None else subprocess.PIPE, stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace", bufsize=1,
                    start_new_session=True) as proc:
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
                        cache_creation_tokens=u.get("cache_creation_input_tokens"), result=e.get("result"),
                        is_error=bool(e.get("is_error")) or e.get("subtype", "success") != "success")
            cost = f", ${e['total_cost_usd']:.2f}" if e.get("total_cost_usd") is not None else ""
            return f"[{e.get('subtype', 'result')}: {e.get('num_turns')} turns{cost}]\n"
        return None

    @staticmethod
    def limit_line(log, code, info):
        """The usage-limit message of a failed run (exit code or an error result): the worker's result, or one of the
        log's last 5 lines; None if there is none. A run that succeeded may talk about rate limits; it hit none."""
        if code == 0 and not info.get("is_error"):
            return None
        tail = tail_lines(log, 5)
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

    @contextlib.contextmanager
    def run_lock(self):
        """Held for the whole `pm run`: one per repository. The kernel drops it when the process ends."""
        fd = os.open(self.git_dir / "pm-run.lock", os.O_RDWR | os.O_CREAT, 0o644)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise FormatError(f"`pm run` (pid {os.pread(fd, 32, 0).decode().strip()}) is working this repository; "
                                  "`pm stop` stops it after its task") from None
            self.crash = parse_crash(os.pread(fd, 4096, 0))
            os.ftruncate(fd, 0)
            os.pwrite(fd, f"{os.getpid()}\n".encode(), 0)
            try:
                yield
            except Exception as e:
                # Into the lock file's own block, which needs no new space (a full disk is a likely cause).
                msg = "".join(traceback.format_exception_only(e)).strip()[:2000]
                with contextlib.suppress(OSError):
                    os.pwrite(fd, f"{os.getpid()}\n{now()}\n{msg}\n".encode(errors="replace"), 0)
                raise
        finally:
            os.close(fd)

    def run_pid(self):
        """The pid of the `pm run` working this repository, or None."""
        try:
            fd = os.open(self.git_dir / "pm-run.lock", os.O_RDONLY)
        except FileNotFoundError:
            return None
        try:
            fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
            return None
        except BlockingIOError:
            pid = os.pread(fd, 32, 0).decode().split("\n")[0].strip()
            return int(pid) if pid.isdigit() else None
        finally:
            os.close(fd)

    def run_crash(self):
        """While no `pm run` lives: how the last one crashed, {"pid", "at", "error"}, or None."""
        try:
            fd = os.open(self.git_dir / "pm-run.lock", os.O_RDONLY)
        except FileNotFoundError:
            return None
        try:
            fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
            return parse_crash(os.pread(fd, 4096, 0))
        except BlockingIOError:
            return None
        finally:
            os.close(fd)

    def close_stale_run(self):
        """Under the run lock, a 'running' run is one whose `pm run` died: it becomes failed and is committed."""
        r = (self.rows("SELECT * FROM run WHERE result = 'running'") or [None])[0]
        if r is None:
            return
        with self.connect(write=True) as db:
            db.execute("UPDATE run SET ended_at = ?, result = 'failed', reason = ? WHERE id = ?",
                       (now(), f"pm run crashed: {self.crash['error']}" if self.crash else "interrupted", r["id"]))
        self.commit_db(f"run {r['id']} (task {r['task_id']}) " + ("crashed" if self.crash else "interrupted"))

    def work(self, tid):
        """Run the worker for a task until no usage limit stops it; record each run and commit the task.
        Returns ("done" | "blocked" | "failed" | "error" | "limit", message); "failed": the task's own failure,
        "error": one that stops the run (the database, git, Claude needs the human). Claude's passing errors (network,
        overload, a limit without a reset time): the same task again, now, then after RETRY_MINUTES."""
        cmd, prompt = self.worker_command(tid)
        before = self.planning_snapshot()
        attempt = 0
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
            except OSError as e:
                return "error", self.failed(tid, rid, f"the worker command can't start: {e}", None, info, started)[1]
            limit = self.limit_line(log, code, info)
            if limit and (reset := limit_reset(limit)) is not None:
                self.record_run(rid, code, info, "limit", limit.strip())
                margin = datetime.timedelta(minutes=self.config["limit_margin_minutes"])
                until = max(reset + margin, datetime.datetime.now(reset.tzinfo) + margin)
                self.say(f"Usage limit ({limit.strip()}). Waiting until {until:%Y-%m-%d %H:%M %Z}, then the same task "
                         "again.")
                self.wait_until(until)
                if self.stopping:
                    return "limit", "stopped while waiting for the usage limit's reset"
                continue
            if limit:
                trouble = "transient", f"usage limit without a readable reset time: {limit.strip()}"
            elif (code != 0 or info.get("is_error")) and (self.unstaged() or self.task_status(tid) == "open"):
                trouble = claude_trouble(info, tail_lines(log, 5))
            else:
                trouble = None
            if not trouble:
                break
            what, line = trouble
            summary = self.failed(tid, rid, f"Claude: {line}", code, info, started, log)[1]
            if what == "fatal":
                return "error", summary + "\nthis needs you; the task stays as it is"
            delay = RETRY_MINUTES[min(attempt, len(RETRY_MINUTES) - 1)]
            attempt += 1
            until = datetime.datetime.now().astimezone() + datetime.timedelta(minutes=delay)
            self.say(f"Claude: {line}\nThe same task again " + (f"at {until:%H:%M}." if delay else "now."))
            self.wait_until(until)
            if self.stopping:
                return "limit", "stopped while waiting to retry after a Claude error"
        if errors := self.validate():
            msg = "the database is no longer valid:\n  " + "\n  ".join(errors)
            try:
                self.record_run(rid, code, info, "failed", msg)
            except FormatError:
                msg += "\nthe run is not recorded"
            if stash := self.stash_leftovers(f"pm: task {tid} run {rid} failed"):
                msg += "\n" + stash
            return "error", msg
        left = self.unstaged()
        if code != 0 and (left or self.task_status(tid) == "open"):
            return self.failed(tid, rid, exit_text(code), code, info, started, log)
        with self.connect() as db:
            t = self.get(db, "task", tid)
            blocked = [str(r[0]) for r in db.execute(
                "SELECT d.id FROM task_decision td JOIN decision d ON d.id = td.decision_id "
                "WHERE td.task_id = ? AND d.status = 'pending' ORDER BY d.id", (tid,))]
        if t["status"] != "open":
            result = "done", t["status"] + (f": {t['note']}" if t["note"] else "") + (
                f" ({exit_text(code)} after closing it, everything staged)" if code else "")
        elif blocked:
            result = "blocked", "waits for decision(s) " + ", ".join(blocked)
        else:
            return self.failed(tid, rid, "exit 0, but the task is still open and not blocked", code, info, started, log,
                               cause=f"the worker ended without `pm task done|drop {tid}` or a pending decision"
                               + (f"; its last message: {info['last_text'].splitlines()[-1][:200]}"
                                  if info.get("last_text") else ""))
        if t["status"] == "done" and (missing := self.planning_missing(t, before)):
            return self.failed(tid, rid, f"task {tid} is done, but it {missing}", code, info, started, log)
        self.record_run(rid, code, info, result[0])
        try:
            self.commit_task(tid, t["subject"], result[1])
        except subprocess.CalledProcessError as e:
            return "error", f"git commit failed:\n{(e.stdout or '') + (e.stderr or '')}".rstrip()
        if t["status"] != "open" and (dropped := self.drop_task_stashes(tid)):
            self.say(f"dropped the stash(es) of task {tid}'s earlier runs: {', '.join(dropped)}")
        if left:
            note = self.put_aside_leftovers(tid, rid, t["status"], left)
            if note.startswith("stashing failed") or note.startswith("branching failed"):
                return "error", f"committed task {tid}; the worker left changes that are not staged, {note}"
            result = result[0], f"{result[1]}; {note}"
        return result

    def put_aside_leftovers(self, tid, rid, status, left):
        """Changes a worker left unstaged after a task it closed or that waits for a decision. Waiting: stashed for
        the task's next run (task_stashes). Closed: committed on a branch, with a decision to look into it. Notes it
        on the run, commits the database; returns the note."""
        n = f"{len(left)} file{'s' if len(left) > 1 else ''}"
        if status == "open":
            stash = self.stash_leftovers(f"pm: task {tid} run {rid} left")
            if not stash:
                return "nothing left to stash"
            if "stashed as" not in stash:
                return stash.split(", ", 1)[1]
            note = f"left {n} unstaged, stashed as {re.search(r'stashed as (\w+)', stash)[1]} for the task's next run"
        else:
            branch, closed = f"pm/task-{tid}-run-{rid}-left", "drop" if status == "dropped" else status
            try:
                self.branch_leftovers(branch, f"task {tid} run {rid}: changes left unstaged after `pm task {closed}`")
            except subprocess.CalledProcessError as e:
                return f"branching failed: {e.stderr.strip()}"
            did = self.decision_add(
                f"Task {tid} left unstaged changes after `pm task {closed}`: investigate",
                f"Run {rid} of task {tid} closed the task but left {n} unstaged. pm committed them on the branch "
                f"`{branch}` and cleaned the tree.\n\n" + "\n".join(f"    {l}" for l in left[:30])
                + (f"\n    … and {len(left) - 30} more" if len(left) > 30 else "")
                + f"\n\nLook: `git show {branch}`. Keep: `git merge {branch}`. Then `git branch -D {branch}`.",
                kind="once")
            note = f"left {n} unstaged, committed on branch {branch}; decision {did}: investigate"
        with self.connect(write=True) as db:
            db.execute("UPDATE run SET reason = ? WHERE id = ?", (note, rid))
        self.commit_db(f"task {tid}: {note}", running=None)
        return note

    def branch_leftovers(self, branch, message):
        """Commit the working tree's changes, the database aside, on a new branch off HEAD, then clean the tree;
        HEAD and the checkout stay."""
        index = self.git_dir / "pm-leftovers.index"
        env = os.environ | {"GIT_INDEX_FILE": str(index)}
        git = lambda *a: subprocess.run(["git", *a], cwd=self.root, env=env, capture_output=True, text=True,
                                        check=True).stdout.strip()
        rest = ["--", ".", f":(exclude){self.db}"]
        try:
            git("read-tree", "HEAD")
            git("add", "-A", *rest)
            sha = git("commit-tree", git("write-tree"), "-p", "HEAD", "-m", message)
        finally:
            index.unlink(missing_ok=True)
        self.git("branch", branch, sha)
        self.git("restore", "-SW", *rest)
        self.git("clean", "-fdq", *rest)

    def unstaged(self):
        """`git status` lines of changes not staged (untracked included), the database aside."""
        return [l for l in self.git("status", "--porcelain").splitlines() if l[1] != " " and l[3:] != self.db]

    def task_status(self, tid):
        with self.connect() as db:
            return self.get(db, "task", tid)["status"]

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
        with self.connect(write=True) as db:
            if self.get(db, "task", tid)["status"] == "done":
                db.execute("UPDATE task SET status = 'open', note = NULL, retired_at = NULL WHERE id = ?", (tid,))
                lines.append(f"task {tid} was marked done in this run; reopened, so its work isn't lost")
        if log:
            lines.append(f"log: {log}")
        summary = "\n".join(lines)
        self.record_run(rid, code, info, "failed", summary)
        try:
            self.git("commit", "-q", "-m", f"pm: task {tid} failed", "--", self.db)
            self.gc()
        except subprocess.CalledProcessError as e:
            summary += f"\ncommitting {self.db} failed: {e.stderr.strip()}"
        if shown := info.get("shown"):
            summary += "\nlast output:\n" + "\n".join("  " + l for l in shown)
        elif log and log.exists():
            summary += "\nlast lines of the log:\n" + "\n".join(
                "  " + l[:300] for l in log.read_text(encoding="utf-8", errors="replace").splitlines()[-15:])
        return "failed", summary

    def task_stashes(self, tid):
        """[(ref, sha)] of the stashes runs of task tid left (stash_leftovers), newest first."""
        try:
            lines = self.git("stash", "list", "--format=%gd%x00%H%x00%s").splitlines()
        except subprocess.CalledProcessError:
            return []
        return [(ref, sha) for ref, sha, subject in (l.split("\0", 2) for l in lines)
                if re.search(rf"pm: task {tid} run \d+ (failed|left)$", subject)]

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
        earlier runs left. Empty if neither."""
        last = (self.rows("SELECT id, result, reason FROM run WHERE task_id = ? AND result NOT IN ('running', 'limit') "
                          "ORDER BY id DESC LIMIT 1", tid) or [None])[0]
        stashes = self.task_stashes(tid)
        out = []
        if last and last["result"] == "failed":
            out += [f"# Run {last['id']} of this task failed", "",
                    *[f"    {l}" for l in (last["reason"] or "no reason recorded").splitlines()], ""]
        if stashes:
            out.append("Earlier runs of this task left work in git stashes, newest first:")
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
        if t["kind"] in ("plan", "masterplan") and self.planning_snapshot() == before:
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
            self.gc()

    def close_and_commit(self):
        closed, cleanup = self.close_milestones()
        done = [f"milestone {mid}: post-milestone cleanup queued" for mid in cleanup] + \
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
            if dry_run:
                return self.work_queue(once, dry_run)
            with self.run_lock():
                self.ignore_db()
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
            q = self.queue()
            if not q and not self.rows("SELECT 1 FROM milestone WHERE status = 'open'"):
                held = [r["id"] for r in self.rows("SELECT id FROM milestone WHERE status = 'on-hold' ORDER BY id")]
                self.say("No open milestone (`pm milestone add` starts one)." + (
                    f" On hold: milestone(s) {', '.join(map(str, held))} (`pm milestone resume <id>`)." if held else ""))
                return 0
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
            if not self.commit_db("uncommitted changes"):
                return 1
            tid = q[0][1]
            result, message = self.work(tid)
            if result == "error":
                self.say(f"\ntask {tid} failed: {message}\nStopping: this may hit every task.")
                return 1
            if result == "failed":
                self.say(f"\ntask {tid} failed: {message}")
                if self.task_kind(tid) == "exec":
                    self.task_set(tid, status="dropped", note="failed: " + message.splitlines()[0][:200])
                    self.drop_task_stashes(tid)
                    self.commit_db(f"task {tid} failed: dropped")
                    self.say(f"task {tid} has no plan to investigate in: dropped")
                elif self.task_kind(tid) == "investigate":
                    self.say(f"Stopping: task {tid} investigated a failure; pm doesn't investigate an investigation.")
                    return 1
                elif not (iid := self.investigate(tid, message)):
                    self.say(f"Stopping: task {tid} failed again after {MAX_INVESTIGATIONS} investigations.")
                    return 1
                else:
                    self.say(f"task {iid} investigates it first")
            else:
                self.say(f"\ntask {tid}: {result} ({message})")
            self.close_and_commit()
            if once:
                return int(result == "failed")
            if self.stopping:
                self.say("Stopped as asked; `pm run` continues with the queue.")
                return 0

    def task_kind(self, tid):
        with self.connect() as db:
            return self.get(db, "task", tid)["kind"]

    def investigate(self, tid, summary):
        """Queue the investigation of failed task tid: in its plan, above it, so it and the fixes it adds (one below
        it) run before task tid again. Returns its id; None after MAX_INVESTIGATIONS of the task, so it can't loop."""
        with self.connect(write=True) as db:
            t = self.get(db, "task", tid)
            p = self.get(db, "plan", t["plan_id"])
            prio = t["priority"] if t["priority"] is not None else p["priority"] or 0
            earlier = db.execute("SELECT id, status, note FROM task WHERE kind = 'investigate' AND subject LIKE ? "
                                 "ORDER BY id", (INVESTIGATE_SUBJECT.format(f"task {tid}: ") + "%",)).fetchall()
            if len(earlier) >= MAX_INVESTIGATIONS:
                return None
            body = self.prompt("investigate_prompt", task=ref("task", tid, t["subject"], True), task_id=tid,
                               plan_id=t["plan_id"], fix_priority=prio + 1)
            body += "\n\n# The failure\n\n" + "\n".join(f"    {l}" for l in summary.splitlines())
            if earlier:
                body += "\n\n# Earlier investigations of this task\n\n" + "\n".join(
                    f"- task {r['id']} ({r['status']}): {r['note'] or 'no note'}" for r in earlier)
            iid = db.execute("INSERT INTO task (plan_id, subject, kind, priority, body, created_by_task) "
                             "VALUES (?, ?, 'investigate', ?, ?, ?)",
                             (t["plan_id"], INVESTIGATE_SUBJECT.format(ref("task", tid, t["subject"])), prio + 2, body,
                              creator())).lastrowid
        self.commit_db(f"task {tid} failed: task {iid} investigates", running=None)
        return iid

    def stop(self):
        """Ask the running `pm run` to stop after its current task."""
        pid = self.run_pid()
        try:
            os.kill(pid, signal.SIGUSR1)
        except (TypeError, ProcessLookupError):
            raise FormatError("no `pm run` is working this repository") from None
        r = (self.rows("SELECT * FROM run WHERE result = 'running'") or [None])[0]
        self.say(f"`pm run` (pid {pid}) stops " + (f"after task {r['task_id']}" if r else "before its next task"))

    def dirty_message(self):
        files = [l for l in self.git("status", "--porcelain").splitlines() if l[3:] != self.db]
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
            gone = "" if self.run_pid() else "; its `pm run` is gone, the next one records it as failed"
            self.say(f"Running: task {r['task_id']} (run {r['id']}, pid {r['pid']}) since {stamp(r['started_at'])}, "
                     f"{duration(seconds(r['started_at'], None))}{gone}")
        if (c := self.run_crash()) and not self.run_pid():
            self.say(f"`pm run` (pid {c['pid']}) crashed at {stamp(c['at'])}: {c['error']}")
        for m in self.rows(
                "SELECT m.id, m.subject, m.status, m.created_at, m.queue_index, "
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
                "FROM milestone m WHERE m.status <> 'implemented' ORDER BY m.queue_index, m.id"):
            held = ", on hold" if m["status"] == "on-hold" else ""
            self.say(f"Milestone {m['id']}: {m['subject']} (queue index {m['queue_index']}, created {stamp(m['created_at'])}{held}): {m['plans']} plan(s), "
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
    ap.add_argument("--version", action="version", version=f"pm {VERSION}, database schema {SCHEMA_VERSION}")
    ap.add_argument("--root", help="the project's git repository (default: the one around the current directory)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name, text in [("init", f"write {CONFIG_FILE} (if missing) and commit it; the first command that uses the database writes it"),
                       ("status", "open and on-hold milestones, the queue, pending decisions"), ("queue", "the whole queue"),
                       ("validate", "check the database"),
                       ("decide", "answer pending decisions in the terminal, one by one"),
                       ("instructions", "the full reference for agents: every command with examples, recipes")]:
        sub.add_parser(name, help=text)
    s = sub.add_parser("show", help="a task with its plan, milestone, decisions and runs; or a decision (d32, "
                       "decision 32), plan (p3) or milestone (m1)")
    s.add_argument("ref", nargs="+", help="7 or t7: task 7; d32, decision 32; p3, plan 3; m1, milestone 1")
    rs = sub.add_parser("runs", help="worker runs (time, result, tokens, cost) and totals per task kind")
    rs.add_argument("task", type=int, nargs="?", help="only this task's runs")
    r = sub.add_parser("run", help="work the queue with the worker command (claude -p)")
    r.add_argument("--once", action="store_true", help="stop after one task")
    r.add_argument("--dry-run", action="store_true", help="print the next worker command; change nothing")
    sub.add_parser("stop", help="have the running `pm run` stop after its current task")

    ms = sub.add_parser("milestone", aliases=["milestones"], help="add, change, list or show milestones").add_subparsers(dest="action", required=True)
    ma = ms.add_parser("add", help="new open milestone; prints its id. Without a subject: write it in your editor "
                       "(first line: subject, the rest: body); with a subject: the body is read from stdin")
    ma.add_argument("subject", nargs="?")
    ma.add_argument("--queue", type=int, default=0, help="queue index: tasks of lower ones are worked first (default 0)")
    mset = ms.add_parser("set", help="set milestone fields; a new subject or body queues the revision of the masterplan")
    mset.add_argument("milestone", type=int)
    mset.add_argument("--subject")
    mset.add_argument("--status", choices=("open", "on-hold"))
    mset.add_argument("--body", type=text_arg)
    mset.add_argument("--queue", type=int, help="queue index: tasks of lower ones are worked first")
    for name, text in [("hold", "put a milestone on hold: its plans and tasks leave the queue; a running task finishes"),
                       ("resume", "resume a milestone on hold: its tasks are queued again")]:
        ms.add_parser(name, help=text).add_argument("milestone", type=int)
    ms.add_parser("list", help="every milestone, one per line: id, queue index, status, closed/all tasks, subject")
    ms.add_parser("show", help="one milestone: body, plans with their open tasks (also `pm show m1`)").add_argument(
        "milestone", type=int)

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
    p.add_parser("list", help="plans with open tasks in open milestones, one per line: priority, id, subject; by the "
                  "milestone's queue index, then highest priority first")

    t = sub.add_parser("task", help="add or change tasks").add_subparsers(dest="action", required=True)
    a = t.add_parser("add", help="new task; prints its id. `pm task add --exec [<subject>]`: an exec task, no plan")
    a.add_argument("plan", nargs="?", help="the plan's id; none with --exec")
    a.add_argument("subject", nargs="?", help="then the body (Markdown) is read from stdin; without: editor")
    a.add_argument("--exec", action="store_true", help="an executive intervention: no plan, no milestone, queued as "
                   "if at queue index 0; only the human adds one")
    a.add_argument("--priority", type=int, help="default: the plan's; for --kind plan: planning_priority")
    a.add_argument("--kind", choices=("work", "plan"), default="work", help="plan: the task writes a plan")
    a.add_argument("--web", action="store_true", help="its worker gets WebSearch and WebFetch (sign-off: web)")
    for verb, text in [("done", "note: date, files or commit"), ("drop", "reason")]:
        x = t.add_parser(verb)
        x.add_argument("task", type=int)
        x.add_argument("note", help=text)
    ts = t.add_parser("set", help="change an open task that isn't running: subject, body, priority, web")
    ts.add_argument("task", type=int)
    ts.add_argument("--subject")
    ts.add_argument("--body", type=text_arg)
    ts.add_argument("--priority", type=prio_arg, default=argparse.SUPPRESS, help="integer, or none for the plan's")
    ts.add_argument("--web", action=argparse.BooleanOptionalAction, default=argparse.SUPPRESS,
                    help="its worker gets WebSearch and WebFetch (sign-off: web)")
    tp = t.add_parser("priority", help="integer, or none for the plan's")
    tp.add_argument("task", type=int)
    tp.add_argument("priority", type=prio_arg)

    d = sub.add_parser("decision", aliases=["decisions"], help="ask the human; the human's answers: the project's "
                       "decision log").add_subparsers(dest="action", required=True)
    da = d.add_parser("add", help="new pending decision; prints its id")
    da.add_argument("subject", help="the question")
    da.add_argument("--body", type=text_arg, help="context for the human")
    da.add_argument("--option", action="append", default=[], help="repeat; the recommended one first")
    da.add_argument("--once", action="store_true", help="a one-off go-ahead or check, not a lasting rule")
    dl = d.add_parser("list", help="pending decisions with their context, options and the tasks they block; "
                      "--answered: the decision log")
    dl.add_argument("--answered", action="store_true", help="the log: answered lasting decisions nothing fully "
                    "supersedes, newest first")
    dl.add_argument("--all", action="store_true", help="every decision: pending, one-off, superseded, irrelevant, "
                    "sign-offs")
    dl.add_argument("--grep", action="append", default=[], help="only decisions that mention this text (any case); "
                    "repeat: any of them")
    dsh = d.add_parser("show", help="one decision: context, options, answer, consequences, tasks, supersede links")
    dsh.add_argument("decision", type=int)
    dr = d.add_parser("record", help="a decision nobody asked, added answered; prints its id. In a worker: your own "
                      "choice, which the human can supersede")
    dr.add_argument("subject")
    dr.add_argument("--answer", type=text_arg, required=True, help="the decision")
    dr.add_argument("--body", type=text_arg, help="context: why, what was considered")
    dr.add_argument("--once", action="store_true", help="a one-off, not a lasting rule")
    dr.add_argument("--supersedes", type=int, action="append", default=[], help="repeat; decisions it replaces")
    dr.add_argument("--consequences", type=text_arg, help="what changed because of it")
    dn = d.add_parser("answer", help="the human's answer; its tasks become workable")
    dn.add_argument("decision", type=int)
    dn.add_argument("answer", help="an option's number, or own text")
    dn.add_argument("--once", action="store_true", help="a one-off, not a lasting rule")
    dn.add_argument("--supersedes", type=int, action="append", default=[], help="repeat; decisions it replaces")
    dsu = d.add_parser("supersede", help="decision NEW's answer replaces OLD's")
    dsu.add_argument("new", type=int)
    dsu.add_argument("old", type=int)
    dsu.add_argument("--note", default="", help="replaces only part of it, e.g. \"scope only\"")
    dc = d.add_parser("consequences", help="append what changed because of an answered decision")
    dc.add_argument("decision", type=int)
    dc.add_argument("text", type=text_arg, help="fields, files, rules; what no longer holds; - reads stdin")
    dk = d.add_parser("kind", help="rule: a lasting decision, in the log; once: a one-off go-ahead or check")
    dk.add_argument("decision", type=int)
    dk.add_argument("kind", choices=("rule", "once"))
    dap = d.add_parser("approve", help="approve a sign-off")
    dap.add_argument("decision", type=int)
    di = d.add_parser("irrelevant", help="close without answer; drops the open tasks it blocks")
    di.add_argument("decision", type=int)
    di.add_argument("reason")
    su = sub.add_parser("summary", aliases=["summaries"], help="executive summaries of the project").add_subparsers(
        dest="action", required=True)
    su.add_parser("request", help=f"queue an exec task that writes one (priority {SUMMARY_PRIORITY}), unless one is open; prints its id")
    su.add_parser("add", help="in an exec task's worker: store the summary (Markdown, stdin); prints its id")
    su.add_parser("show", help="the newest summary, or one (also `pm show s2`)").add_argument("summary", type=int, nargs="?")
    su.add_parser("list", help="every summary, newest first: id, date, commit, first line")
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
            print(pm.show_ref(args.ref), end="")
        elif args.cmd == "runs":
            pm.print_runs(args.task)
        elif args.cmd == "decide":
            msg = pm.decide() and "decisions answered"
        elif args.cmd == "run":
            return pm.run(once=args.once, dry_run=args.dry_run)
        elif args.cmd == "stop":
            pm.stop()
        elif args.cmd in ("milestone", "milestones"):
            if args.action == "list":
                print(pm.milestones(), end="")
            elif args.action == "show":
                print(pm.milestone_show(args.milestone), end="")
            elif args.action == "add":
                if not (entry := pm.subject_and_body("milestone", args.subject)):
                    print("pm: aborted, no milestone added")
                    return 1
                mid = pm.milestone_add(*entry, args.queue)
                print(mid)
                msg = f"milestone {mid} added: {entry[0]}"
            elif args.action in ("hold", "resume"):
                pm.milestone_hold(args.milestone, args.action == "hold")
                msg = f"milestone {args.milestone} " + ("on hold" if args.action == "hold" else "resumed")
            else:
                print_queued(pm.milestone_set(args.milestone, subject=args.subject, status=args.status,
                                              body=args.body, queue_index=args.queue))
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
                if args.exec:
                    if args.subject is not None or args.kind != "work":
                        raise FormatError("an exec task has no plan or other kind: pm task add --exec \"<subject>\"")
                    plan, subject, kind = None, args.plan, "exec"
                else:
                    if args.plan is None or not args.plan.isdigit():
                        raise FormatError("the plan's id is missing (an exec task: --exec)")
                    plan, subject, kind = int(args.plan), args.subject, args.kind
                    pm.require("plan", plan)
                if not (entry := pm.subject_and_body("task", subject)):
                    print("pm: aborted, no task added")
                    return 1
                tid = pm.task_add(plan, *entry, args.priority, kind, args.web)
                print(tid)
                msg = f"task {tid} added: {entry[0]}"
            elif args.action in ("done", "drop"):
                pm.task_set(args.task, status="done" if args.action == "done" else "dropped", note=args.note)
                msg = f"task {args.task} {args.action}"
            elif args.action == "set":
                fields = {k: v.strip() for k in ("subject", "body") if (v := getattr(args, k)) is not None}
                if "priority" in args:
                    fields["priority"] = args.priority
                if "web" in args:
                    fields["web"] = int(args.web)
                if not fields:
                    raise FormatError("nothing to change: --subject, --body, --priority or --web")
                if fields.get("subject") == "":
                    raise FormatError("the subject can't be empty")
                pm.task_set(args.task, **fields)
                msg = f"task {args.task} changed"
            else:
                pm.task_set(args.task, priority=args.priority)
                msg = f"task {args.task} priority"
        elif args.cmd in ("decision", "decisions"):
            if args.action == "list":
                print(pm.decisions(args.answered, args.all, args.grep), end="")
            elif args.action == "show":
                print(pm.decision_show(args.decision), end="")
            elif args.action == "answer":
                said = pm.answer(args.decision, args.answer, "once" if args.once else None, args.supersedes)
                msg = f"decision {args.decision} answered: {said}"
            elif args.action == "record":
                did = pm.record(args.subject, args.answer, args.body, "once" if args.once else "rule", args.supersedes,
                                args.consequences)
                print(did)
                msg = f"decision {did} recorded: {args.subject}"
            elif args.action == "supersede":
                pm.supersede(args.new, args.old, args.note)
                msg = f"decision {args.new} supersedes decision {args.old}" + (f" ({args.note})" if args.note else "")
            elif args.action == "consequences":
                pm.consequences(args.decision, args.text)
                msg = f"decision {args.decision}: consequences"
            elif args.action == "kind":
                pm.set_kind(args.decision, args.kind)
                msg = f"decision {args.decision}: kind {args.kind}"
            elif args.action == "approve":
                pm.approve(args.decision)
                msg = f"decision {args.decision} approved"
            elif args.action == "irrelevant":
                pm.resolve(args.decision, irrelevant=args.reason)
                msg = f"decision {args.decision} irrelevant: {args.reason}"
            else:
                did = pm.decision_add(args.subject, args.body, args.option, "once" if args.once else "rule")
                print(did)
                msg = f"decision {did} added: {args.subject}"
        elif args.cmd in ("summary", "summaries"):
            if args.action == "request":
                tid, new = pm.summary_request()
                print(tid)
                if new:
                    msg = f"task {tid} queued: {SUMMARY_SUBJECT}"
                else:
                    print(f"task {tid} already writes one", file=sys.stderr)
            elif args.action == "add":
                sid = pm.summary_add(sys.stdin.read())
                print(sid)
                msg = f"summary {sid} added"
            elif args.action == "show":
                print(pm.summary_show(args.summary), end="")
            else:
                print(pm.summaries(), end="")
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

