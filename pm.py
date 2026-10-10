#!/usr/bin/env python3
"""pm: a project manager for Claude Code workers (README.md). Milestones -> masterplan -> plans ->
tasks, one TOML file per milestone (plans/Mnnn.toml), decisions for the human in
plans/pm-decisions.toml. The task queue is computed: open tasks of open milestones that no pending decision blocks, highest priority first, then
by task id. Worked one task at a time by `claude -p`. Standard library only (Python 3.11+).

    pm status | queue | show <task> | validate | sync | decide | run [--once] [--dry-run]
    pm task add|done|drop|block|priority …   pm plan add|priority …
    pm decision add …                         pm masterplan <milestone> …

Works on the git repository around the current directory; --dir names another folder than plans/.
`sync`, `decide` and `run` commit the plans folder ("pm: …"); the edit commands don't (the worker
commits). Nothing is pushed. Worker logs: .git/pm-logs/.
"""
import argparse, datetime, json, re, subprocess, sys, time, tomllib, zoneinfo
from pathlib import Path


MID_RE = re.compile(r"^M\d{3}$")
PID_RE = re.compile(r"^P\d{3}$")
TID_RE = re.compile(r"^T\d{3}$")
DID_RE = re.compile(r"^D\d{4}$")
QID_RE = re.compile(r"^(M\d{3})-(P\d{3})-(T\d{3})$")
PLAN_REF_RE = re.compile(r"^(M\d{3})-(P\d{3})$")
MILESTONE_FILE_RE = re.compile(r"^M\d{3}\.toml$")

MILESTONE_STATES = ("open", "implemented")
MASTER_STATES = ("stub", "draft", "agreed")
TASK_STATES = ("open", "done", "dropped")
DECISION_STATES = ("pending", "answered", "irrelevant")

# Key order in the files; a key not listed here is a format error.
MILESTONE_KEYS = ("id", "title", "status", "goal")
MASTER_KEYS = ("status", "goal", "scope", "exit_criteria")
PLAN_KEYS = ("id", "title", "priority", "goal")
TASK_KEYS = ("id", "plan", "subject", "status", "priority", "blocked_by", "note", "details")
DECISION_KEYS = ("id", "question", "status", "options", "context", "answer", "reason", "closed")
TEXT_KEYS = {"goal", "scope", "exit_criteria", "details", "context"}

DECISIONS_FILE = "pm-decisions.toml"
WORKER_TOOLS = "Bash Read Edit Write Glob Grep"
WORKER_PROMPT = """You are the worker for task {qid}. Follow the project's own instructions for agents \
(AGENTS.md, CLAUDE.md or similar, if there are any), then do task {qid} and nothing else.

Rules:
1. Work on {qid} only. Change {dir}/*.toml only through `pm` (prose fields may be edited by hand).
2. Follow-up work: `pm task add <milestone>-<plan> "<subject>" --details "<text>"` in the same plan, or \
`pm plan add <milestone> "<title>" --goal "<text>"` and tasks in it for work outside this plan. Set \
priorities (`--priority N`, positive = earlier) only where the default order (plan, then task id) is wrong.
3. Needs a human answer: `pm decision add "<question>" --context "<text>" --option "<recommended>" \
--option "<other>" --blocks <task ids>`. If {qid} itself is blocked, include it in --blocks, commit what \
is done and stop.
4. Finished: `pm task done {qid} "<YYYY-MM-DD, files or commit>"` or `pm task drop {qid} "<reason>"`. Run \
`pm validate` and the project's checks for what you changed. Commit everything in one commit, message \
"{qid} <subject>". Don't push. Stop.

The task, its plan and its milestone ({dir}/{mid}.toml; `pm show {qid}` prints this again):

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
    """A file in the plans folder breaks the format (README.md), or a command names something that doesn't exist."""


def today():
    return datetime.date.today().isoformat()


# --- TOML writing (tomllib only reads) ---------------------------------------------------------

def toml_value(v, prose=False):
    """A TOML value; prose (TEXT_KEYS) is written as a multi-line literal string whenever it can be."""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, list):
        return "[" + ", ".join(toml_value(x) for x in v) + "]"
    s = str(v)
    plain = not any(ord(c) < 32 and c not in "\n\t" for c in s)
    if ("\n" in s or prose) and s and plain and "'''" not in s:
        return "'''\n" + s + "\n'''"  # literal: prose stays as typed, no escapes
    return json.dumps(s, ensure_ascii=False)  # a JSON string is a valid TOML basic string


def toml_table(lines, table, keys):
    for k in keys:
        if k in table:
            lines.append(f"{k} = {toml_value(table[k], prose=k in TEXT_KEYS)}")


def dump_milestone(m):
    lines = [f"# Milestone {m['id']} (pm). Edit with `pm` or by hand; check with `pm validate`.", ""]
    toml_table(lines, m, MILESTONE_KEYS)
    if "masterplan" in m:
        lines += ["", "[masterplan]"]
        toml_table(lines, m["masterplan"], MASTER_KEYS)
    for p in m.get("plans", []):
        lines += ["", "[[plans]]"]
        toml_table(lines, p, PLAN_KEYS)
    for t in m.get("tasks", []):
        lines += ["", "[[tasks]]"]
        toml_table(lines, t, TASK_KEYS)
    return "\n".join(lines) + "\n"


def dump_decisions(ds):
    lines = ["# Decisions for the human (pm). Edit with `pm decision add` and `pm decide`.", ""]
    for d in ds:
        lines += ["[[decisions]]"]
        toml_table(lines, d, DECISION_KEYS)
        lines.append("")
    return "\n".join(lines).rstrip("\n") + "\n"


def normalize(table):
    """Prose fields are stored stripped, so a file round-trips through dump unchanged."""
    for k in TEXT_KEYS & table.keys():
        if isinstance(table[k], str):
            table[k] = table[k].strip()
    return table


# --- model helpers ----------------------------------------------------------------------------

def qid_of(m, t):
    return f"{m['id']}-{t['plan']}-{t['id']}"


def plan_of(m, pid):
    return next((p for p in m.get("plans", []) if p["id"] == pid), None)


def priority(m, t):
    """A task's own priority, else its plan's, else 0."""
    if "priority" in t:
        return t["priority"]
    return (plan_of(m, t["plan"]) or {}).get("priority", 0)


def next_id(prefix, ids, width):
    return f"{prefix}{max((int(i[1:]) for i in ids), default=0) + 1:0{width}d}"


def is_int(v):
    return isinstance(v, int) and not isinstance(v, bool)


class Project:
    def __init__(self, root, claude="claude", out=sys.stdout, plans_dir="plans"):
        self.root = Path(root)
        self.dir = plans_dir
        self.plans = self.root / plans_dir
        self.claude = claude
        self.out = out

    def say(self, *args):
        print(*args, file=self.out, flush=True)

    # --- files -------------------------------------------------------------------------------

    def milestone_path(self, mid):
        return self.plans / f"{mid}.toml"

    def milestone_ids(self):
        return sorted(p.stem for p in self.plans.glob("M*.toml") if MILESTONE_FILE_RE.match(p.name))

    def read_toml(self, path):
        try:
            return tomllib.loads(path.read_text(encoding="utf-8"))
        except tomllib.TOMLDecodeError as e:
            raise FormatError(f"{path.name}: {e}") from None

    def load(self, mid):
        path = self.milestone_path(mid)
        if not path.exists():
            raise FormatError(f"no milestone {mid} ({self.dir}/{mid}.toml)")
        m = normalize(self.read_toml(path))
        if isinstance(m.get("masterplan"), dict):
            normalize(m["masterplan"])
        for x in m.get("plans", []) + m.get("tasks", []):
            if isinstance(x, dict):
                normalize(x)
        return m

    def milestones(self):
        return [self.load(mid) for mid in self.milestone_ids()]

    def open_milestones(self):
        return [m for m in self.milestones() if m.get("status") == "open"]

    def save(self, m):
        self.milestone_path(m["id"]).write_text(dump_milestone(m), encoding="utf-8")

    def decisions(self):
        path = self.plans / DECISIONS_FILE
        if not path.exists():
            return []
        return [normalize(d) for d in self.read_toml(path).get("decisions", [])]

    def save_decisions(self, ds):
        (self.plans / DECISIONS_FILE).write_text(dump_decisions(ds), encoding="utf-8")

    def pending(self):
        return [d for d in self.decisions() if d.get("status") == "pending"]

    def find(self, qid):
        """(milestone, task) for a qualified task id."""
        q = QID_RE.match(qid)
        if not q:
            raise FormatError(f"not a task id (Mnnn-Pnnn-Tnnn): {qid}")
        m = self.load(q[1])
        t = next((t for t in m.get("tasks", []) if t["plan"] == q[2] and t["id"] == q[3]), None)
        if t is None:
            raise FormatError(f"no task {qid}")
        return m, t

    # --- git ---------------------------------------------------------------------------------

    def git(self, *args):
        return subprocess.run(["git", *args], cwd=self.root, capture_output=True, text=True, check=True).stdout

    def dirty(self, *paths):
        return bool(self.git("status", "--porcelain", "--", *paths).strip())

    def commit(self, message):
        if not self.dirty(self.dir):
            return False
        self.git("add", "-A", "--", self.dir)
        self.git("commit", "-q", "-m", f"pm: {message}", "--", self.dir)
        return True

    # --- validate ----------------------------------------------------------------------------

    def check_decisions(self, ds):
        errors, seen = [], set()
        for i, d in enumerate(ds):
            where = f"{DECISIONS_FILE}: decision {i + 1}"
            if not isinstance(d, dict):
                errors.append(f"{where}: not a table")
                continue
            errors += [f"{where}: unknown key {k!r}" for k in d.keys() - set(DECISION_KEYS)]
            did = d.get("id")
            if not isinstance(did, str) or not DID_RE.match(did):
                errors.append(f"{where}: id must be Dnnnn")
            elif did in seen:
                errors.append(f"{DECISIONS_FILE}: {did} is used twice")
            else:
                seen.add(did)
                where = f"{DECISIONS_FILE}: {did}"
            if not isinstance(d.get("question"), str) or not d["question"].strip():
                errors.append(f"{where}: question is missing")
            if d.get("status") not in DECISION_STATES:
                errors.append(f"{where}: status must be one of {', '.join(DECISION_STATES)}")
            if not isinstance(d.get("options", []), list) or not all(isinstance(o, str) for o in d.get("options", [])):
                errors.append(f"{where}: options must be a list of strings")
            for k in ("context", "answer", "reason", "closed"):
                if k in d and not isinstance(d[k], str):
                    errors.append(f"{where}: {k} must be a string")
            if d.get("status") == "answered" and not (d.get("answer") and d.get("closed")):
                errors.append(f"{where}: an answered decision needs answer and closed")
            if d.get("status") == "irrelevant" and not d.get("closed"):
                errors.append(f"{where}: an irrelevant decision needs closed")
            if d.get("status") == "pending" and ("answer" in d or "closed" in d):
                errors.append(f"{where}: a pending decision has no answer or closed")
        return errors

    def check_milestone(self, m, fname, dids):
        errors = []
        errors += [f"{fname}: unknown key {k!r}" for k in m.keys() - set(MILESTONE_KEYS) - {"masterplan", "plans", "tasks"}]
        if m.get("id") != fname.removesuffix(".toml"):
            errors.append(f"{fname}: id must be {fname.removesuffix('.toml')!r}")
        if not isinstance(m.get("title"), str) or not m["title"].strip():
            errors.append(f"{fname}: title is missing")
        if m.get("status") not in MILESTONE_STATES:
            errors.append(f"{fname}: status must be one of {', '.join(MILESTONE_STATES)}")
        if not isinstance(m.get("goal", ""), str):
            errors.append(f"{fname}: goal must be a string")
        mp = m.get("masterplan")
        if mp is not None:
            if not isinstance(mp, dict):
                errors.append(f"{fname}: masterplan must be a table")
            else:
                errors += [f"{fname}: [masterplan] unknown key {k!r}" for k in mp.keys() - set(MASTER_KEYS)]
                if mp.get("status") not in MASTER_STATES:
                    errors.append(f"{fname}: [masterplan] status must be one of {', '.join(MASTER_STATES)}")
                errors += [f"{fname}: [masterplan] {k} must be a string" for k in MASTER_KEYS[1:]
                           if k in mp and not isinstance(mp[k], str)]
        plans, pids = m.get("plans", []), set()
        if not isinstance(plans, list):
            return errors + [f"{fname}: plans must be [[plans]] tables"]
        for i, p in enumerate(plans):
            where = f"{fname}: plan {i + 1}"
            if not isinstance(p, dict):
                errors.append(f"{where}: not a table")
                continue
            errors += [f"{where}: unknown key {k!r}" for k in p.keys() - set(PLAN_KEYS)]
            pid = p.get("id")
            if not isinstance(pid, str) or not PID_RE.match(pid):
                errors.append(f"{where}: id must be Pnnn")
            elif pid in pids:
                errors.append(f"{fname}: plan {pid} is used twice")
            else:
                pids.add(pid)
                where = f"{fname}: {pid}"
            if not isinstance(p.get("title"), str) or not p["title"].strip():
                errors.append(f"{where}: title is missing")
            if "priority" in p and not is_int(p["priority"]):
                errors.append(f"{where}: priority must be an integer")
            if "goal" in p and not isinstance(p["goal"], str):
                errors.append(f"{where}: goal must be a string")
        tasks, tids = m.get("tasks", []), set()
        if not isinstance(tasks, list):
            return errors + [f"{fname}: tasks must be [[tasks]] tables"]
        for i, t in enumerate(tasks):
            where = f"{fname}: task {i + 1}"
            if not isinstance(t, dict):
                errors.append(f"{where}: not a table")
                continue
            errors += [f"{where}: unknown key {k!r}" for k in t.keys() - set(TASK_KEYS)]
            if not isinstance(t.get("id"), str) or not TID_RE.match(t["id"]):
                errors.append(f"{where}: id must be Tnnn")
            elif t.get("plan") not in pids:
                errors.append(f"{where} ({t['id']}): plan {t.get('plan')!r} is not a plan of {m.get('id')}")
            elif (t["plan"], t["id"]) in tids:
                errors.append(f"{fname}: task {t['plan']}-{t['id']} is used twice")
            else:
                tids.add((t["plan"], t["id"]))
                where = f"{fname}: {t['plan']}-{t['id']}"
            if not isinstance(t.get("subject"), str) or not t["subject"].strip():
                errors.append(f"{where}: subject is missing")
            if t.get("status") not in TASK_STATES:
                errors.append(f"{where}: status must be one of {', '.join(TASK_STATES)}")
            if "priority" in t and not is_int(t["priority"]):
                errors.append(f"{where}: priority must be an integer")
            blocked = t.get("blocked_by", [])
            if not isinstance(blocked, list) or not all(isinstance(d, str) for d in blocked):
                errors.append(f"{where}: blocked_by must be a list of decision ids")
            else:
                errors += [f"{where}: blocked_by names {d}, which is not in {DECISIONS_FILE}"
                           for d in blocked if d not in dids]
            for k in ("note", "details"):
                if k in t and not isinstance(t[k], str):
                    errors.append(f"{where}: {k} must be a string")
            if t.get("status") == "open" and not (t.get("details") or "").strip():
                errors.append(f"{where}: an open task needs details")
        return errors

    def validate(self):
        """Every format error in the plans folder, as a list of messages (empty: all good)."""
        errors = []
        try:
            ds = self.decisions()
        except FormatError as e:
            errors.append(str(e))
            ds = []
        errors += self.check_decisions(ds)
        dids = {d.get("id") for d in ds}
        for mid in self.milestone_ids():
            try:
                m = self.read_toml(self.milestone_path(mid))
            except FormatError as e:
                errors.append(str(e))
                continue
            errors += self.check_milestone(m, f"{mid}.toml", dids)
        return errors

    def guarded(self, change):
        """Apply `change()` (which writes files in the plans folder); undo every write if it is no longer valid."""
        files = [self.milestone_path(mid) for mid in self.milestone_ids()] + [self.plans / DECISIONS_FILE]
        before = {f: f.read_text(encoding="utf-8") if f.exists() else None for f in files}
        result = change()
        errors = self.validate()
        if errors:
            for f in set(files) | {self.milestone_path(mid) for mid in self.milestone_ids()}:
                if before.get(f) is None:
                    f.unlink(missing_ok=True)
                else:
                    f.write_text(before[f], encoding="utf-8")
            raise FormatError("the change would make the plans folder invalid, nothing written:\n  " + "\n  ".join(errors))
        return result

    # --- queue -------------------------------------------------------------------------------

    def blocked(self, t, pending_ids):
        return any(d in pending_ids for d in t.get("blocked_by", []))

    def queue(self):
        """[(priority, qid, subject)]: open, unblocked tasks of open milestones; highest priority first, then id."""
        pending_ids = {d["id"] for d in self.pending()}
        rows = [(priority(m, t), qid_of(m, t), t["subject"]) for m in self.open_milestones()
                for t in m.get("tasks", []) if t["status"] == "open" and not self.blocked(t, pending_ids)]
        return sorted(rows, key=lambda r: (-r[0], r[1]))

    # --- sync --------------------------------------------------------------------------------

    def stub(self, m):
        """Masterplan stub and plan P001, which writes the masterplan (README.md, sync)."""
        mid = m["id"]
        m["masterplan"] = {"status": "stub", "goal": m.get("goal", "")}
        if m.get("plans"):
            return
        m["plans"] = [{"id": "P001", "title": "write masterplan",
                       "goal": f"Turn milestone {mid} into a masterplan with plans and tasks."}]
        m["tasks"] = [
            {"id": "T001", "plan": "P001", "subject": "write goal, scope and exit criteria of the masterplan",
             "status": "open", "details": (
                 f"Read milestone {mid} (`pm show {mid}-P001-T001` shows it), the plans and decisions it cites and "
                 f"the code it touches. Write the masterplan's goal, scope (in and out) and exit criteria (checkable): "
                 f"`pm masterplan {mid} --goal … --scope … --exit-criteria …`. Every question only a human "
                 "can answer becomes a decision (`pm decision add`).")},
            {"id": "T002", "plan": "P001", "subject": "split the milestone into plans with tasks",
             "status": "open", "details": (
                 f"Split {mid} into plans P002, P003, … (`pm plan add {mid} <title> --goal …`), each with tasks small "
                 f"enough for one worker run (`pm task add {mid}-Pnnn <subject> --details …`). Order them with "
                 "priorities where the default (plan, then task id) is wrong. Then "
                 f"`pm masterplan {mid} --status draft`.")}]

    def sync(self):
        """Stub masterplans of open milestones that have none; close finished milestones. Returns the changes."""
        changes = []
        for m in self.open_milestones():
            if "masterplan" not in m:
                self.stub(m)
                self.save(m)
                changes.append(f"{m['id']} masterplan stub")
            elif (m["masterplan"]["status"] != "stub" and m.get("tasks")
                  and all(t["status"] != "open" for t in m["tasks"])):
                m["status"] = "implemented"
                self.save(m)
                changes.append(f"{m['id']} implemented")
        return changes

    # --- decide ------------------------------------------------------------------------------

    def resolve(self, did, answer=None, irrelevant=None):
        """Close a pending decision. Answered: its tasks unblock by themselves. Irrelevant: the open tasks it
        blocks are dropped."""
        ds = self.decisions()
        d = next((x for x in ds if x["id"] == did and x["status"] == "pending"), None)
        if d is None:
            raise FormatError(f"no pending decision {did}")
        if irrelevant is None:
            d.update(status="answered", answer=answer, closed=today())
        else:
            d.update(status="irrelevant", reason=irrelevant, closed=today())
            for m in self.open_milestones():
                hit = [t for t in m.get("tasks", []) if t["status"] == "open" and did in t.get("blocked_by", [])]
                for t in hit:
                    t.update(status="dropped", note=f"{did} irrelevant")
                if hit:
                    self.save(m)
        self.save_decisions(ds)

    def decide(self, ask=input):
        """Ask about every pending decision; returns how many were closed."""
        closed = 0
        for d in self.pending():
            opts = d.get("options", [])
            blocks = [qid_of(m, t) for m in self.milestones() for t in m.get("tasks", [])
                      if d["id"] in t.get("blocked_by", []) and t["status"] == "open"]
            self.say(f"\n{d['id']} {d['question']}")
            if blocks:
                self.say(f"  blocks: {', '.join(blocks)}")
            if d.get("context"):
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

    # --- edit commands (used by workers; no commit) -------------------------------------------

    def task_add(self, plan_ref, subject, details, prio=None, blocked_by=()):
        r = PLAN_REF_RE.match(plan_ref)
        if not r:
            raise FormatError(f"not a plan (Mnnn-Pnnn): {plan_ref}")

        def change():
            m = self.load(r[1])
            if not plan_of(m, r[2]):
                raise FormatError(f"no plan {plan_ref}")
            tid = next_id("T", [t["id"] for t in m.get("tasks", []) if t["plan"] == r[2]], 3)
            t = {"id": tid, "plan": r[2], "subject": subject, "status": "open"}
            if prio is not None:
                t["priority"] = prio
            if blocked_by:
                t["blocked_by"] = list(blocked_by)
            t["details"] = details.strip()
            m.setdefault("tasks", []).append(t)
            self.save(m)
            return f"{plan_ref}-{tid}"
        return self.guarded(change)

    def task_set(self, qid, **fields):
        """Set fields of a task; a value None removes the field."""
        def change():
            m, t = self.find(qid)
            for k, v in fields.items():
                if v is None:
                    t.pop(k, None)
                else:
                    t[k] = v
            self.save(m)
        self.guarded(change)

    def task_block(self, qid, did):
        m, t = self.find(qid)
        self.task_set(qid, blocked_by=sorted(set(t.get("blocked_by", [])) | {did}))

    def plan_add(self, mid, title, goal=None, prio=None):
        def change():
            m = self.load(mid)
            pid = next_id("P", [p["id"] for p in m.get("plans", [])], 3)
            p = {"id": pid, "title": title}
            if prio is not None:
                p["priority"] = prio
            if goal:
                p["goal"] = goal.strip()
            m.setdefault("plans", []).append(p)
            self.save(m)
            return f"{mid}-{pid}"
        return self.guarded(change)

    def plan_priority(self, plan_ref, prio):
        r = PLAN_REF_RE.match(plan_ref)
        if not r:
            raise FormatError(f"not a plan (Mnnn-Pnnn): {plan_ref}")

        def change():
            m = self.load(r[1])
            p = plan_of(m, r[2])
            if not p:
                raise FormatError(f"no plan {plan_ref}")
            if prio is None:
                p.pop("priority", None)
            else:
                p["priority"] = prio
            self.save(m)
        self.guarded(change)

    def masterplan_set(self, mid, **fields):
        def change():
            m = self.load(mid)
            mp = m.setdefault("masterplan", {"status": "stub"})
            mp.update({k: v.strip() for k, v in fields.items() if v is not None})
            self.save(m)
        self.guarded(change)

    def decision_add(self, question, context=None, options=(), blocks=()):
        def change():
            ds = self.decisions()
            did = next_id("D", [d["id"] for d in ds], 4)
            d = {"id": did, "question": question, "status": "pending"}
            if options:
                d["options"] = list(options)
            if context:
                d["context"] = context.strip()
            self.save_decisions(ds + [d])
            for qid in blocks:
                m, t = self.find(qid)
                t["blocked_by"] = sorted(set(t.get("blocked_by", [])) | {did})
                self.save(m)
            return did
        return self.guarded(change)

    # --- show --------------------------------------------------------------------------------

    def show(self, qid):
        m, t = self.find(qid)
        p = plan_of(m, t["plan"])
        mp = m.get("masterplan", {})
        ds = {d["id"]: d for d in self.decisions()}
        out = [f"Milestone {m['id']} {m['title']} ({m['status']})", m.get("goal", ""), "",
               f"Masterplan ({mp.get('status', 'none')})"]
        for k in ("goal", "scope", "exit_criteria"):
            if mp.get(k):
                out += [f"{k.replace('_', ' ')}:", mp[k]]
        out += ["", f"Plan {m['id']}-{p['id']} {p['title']} (priority {p.get('priority', 0)})"]
        if p.get("goal"):
            out.append(p["goal"])
        for x in m.get("tasks", []):
            if x["plan"] == p["id"]:
                out.append(f"  {x['id']} [{x['status']}] {x['subject']}" + (" ← this task" if x is t else ""))
        out += ["", f"Task {qid}: {t['subject']} ({t['status']}, priority {priority(m, t)})"]
        if t.get("note"):
            out.append(f"note: {t['note']}")
        out += [t.get("details", "")]
        for did in t.get("blocked_by", []):
            d = ds.get(did, {})
            state = d.get("status", "?")
            said = d.get("answer") or d.get("reason") or ""
            out.append(f"Decision {did} ({state}): {d.get('question', '?')}" + (f" → {said}" if said else ""))
        return "\n".join(out).rstrip() + "\n"

    # --- run ---------------------------------------------------------------------------------

    def worker_command(self, qid):
        prompt = WORKER_PROMPT.format(qid=qid, mid=qid[:4], dir=self.dir, show=self.show(qid))
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
            return "failed", "the plans folder is no longer valid:\n  " + "\n  ".join(errors)
        if self.dirty():
            return "failed", "the worker left uncommitted changes (git status)"
        _, t = self.find(qid)
        if t["status"] != "open":
            return "done", t["status"] + (f": {t['note']}" if t.get("note") else "")
        if self.blocked(t, {d["id"] for d in self.pending()}):
            return "blocked", "waits for " + ", ".join(t["blocked_by"])
        return "failed", f"exit 0, but {qid} is still open and not blocked\n{tail}"

    def run(self, once=False, dry_run=False, interactive=None, ask=input):
        interactive = sys.stdin.isatty() if interactive is None else interactive
        if dry_run:  # changes nothing: no sync, no commit
            errors = self.validate()
            if errors:
                self.say("the plans folder has format errors:\n  " + "\n  ".join(errors))
                return 1
            q = self.queue()
            if not q:
                self.say("Queue empty (`pm sync` stubs masterplans for open milestones).")
                return 0
            self.say(" ".join(f"'{a}'" if " " in a else a for a in self.worker_command(q[0][1])))
            return 0
        while True:
            errors = self.validate()
            if errors:
                self.say("the plans folder has format errors:\n  " + "\n  ".join(errors))
                return 1
            changes = self.sync()
            if changes:
                self.commit("sync: " + ", ".join(changes))
            if interactive and self.pending() and self.decide(ask):
                self.commit("decisions answered")
            if not self.open_milestones():
                self.say("No open milestone.")
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
            changes = self.sync()
            self.commit(f"{qid} {result}" + (f"; {', '.join(changes)}" if changes else ""))
            if once:
                return 0

    # --- status ------------------------------------------------------------------------------

    def status(self):
        for m in self.open_milestones():
            if "masterplan" not in m:
                self.say(f"{m['id']} {m['title']}: no masterplan yet")
                continue
            ts = m.get("tasks", [])
            done = sum(t["status"] != "open" for t in ts)
            self.say(f"{m['id']} {m['title']}: masterplan {m['masterplan']['status']}, {len(m.get('plans', []))} "
                     f"plan(s), {done}/{len(ts)} tasks closed")
        self.print_queue(limit=10)
        pending = self.pending()
        self.say(f"\nPending decisions ({len(pending)}):")
        for d in pending:
            self.say(f"  {d['id']} {d['question']}")

    def print_queue(self, limit=None):
        q = self.queue()
        self.say(f"\nQueue ({len(q)}):")
        for prio, qid, subject in q[:limit]:
            self.say(f"  {prio:+d} {qid} {subject}" if prio else f"     {qid} {subject}")
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
    ap.add_argument("--dir", default="plans", help="folder of the milestone files, relative to the root (default: plans)")
    ap.add_argument("--claude", default="claude", help=argparse.SUPPRESS)  # tests: a fake worker
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name, text in [("status", "open milestones, the queue, pending decisions"), ("queue", "the whole queue"),
                       ("validate", "check every file in the plans folder"), ("sync", "stub masterplans, close milestones"),
                       ("decide", "answer pending decisions in the terminal")]:
        sub.add_parser(name, help=text)
    s = sub.add_parser("show", help="a task with its plan, milestone and decisions")
    s.add_argument("task")
    r = sub.add_parser("run", help="work the queue with claude -p")
    r.add_argument("--once", action="store_true", help="stop after one task")
    r.add_argument("--dry-run", action="store_true", help="print the next worker command; change nothing")

    t = sub.add_parser("task", help="add or change tasks").add_subparsers(dest="action", required=True)
    a = t.add_parser("add", help="new task; prints its id")
    a.add_argument("plan", help="Mnnn-Pnnn")
    a.add_argument("subject")
    a.add_argument("--details", required=True, type=text_arg, help="text, or - for stdin")
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

    p = sub.add_parser("plan", help="add or change plans").add_subparsers(dest="action", required=True)
    pa = p.add_parser("add", help="new plan; prints its id")
    pa.add_argument("milestone")
    pa.add_argument("title")
    pa.add_argument("--goal", type=text_arg)
    pa.add_argument("--priority", type=int)
    pp = p.add_parser("priority", help="integer, or none for 0")
    pp.add_argument("plan", help="Mnnn-Pnnn")
    pp.add_argument("priority", type=prio_arg)

    d = sub.add_parser("decision", help="ask the human").add_subparsers(dest="action", required=True)
    da = d.add_parser("add", help="new pending decision; prints its id")
    da.add_argument("question")
    da.add_argument("--context", type=text_arg)
    da.add_argument("--option", action="append", default=[], help="repeat; the recommended one first")
    da.add_argument("--blocks", nargs="+", default=[], metavar="TASK")

    mp = sub.add_parser("masterplan", help="set masterplan fields")
    mp.add_argument("milestone")
    mp.add_argument("--status", choices=MASTER_STATES)
    for k in ("goal", "scope", "exit-criteria"):
        mp.add_argument(f"--{k}", type=text_arg)

    args = ap.parse_args(argv)
    root = args.root or git_root()
    if root is None:
        print("pm: not inside a git repository (or pass --root)")
        return 2
    pm = Project(root=root, claude=args.claude, plans_dir=args.dir)
    try:
        if args.cmd == "validate":
            errors = pm.validate()
            print("\n".join(errors) if errors else "the plans folder is valid")
            return 1 if errors else 0
        if args.cmd != "run" and (errors := pm.validate()):
            print("the plans folder has format errors (`pm validate`):\n  " + "\n  ".join(errors))
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
            if changes:
                pm.commit("sync: " + ", ".join(changes))
        elif args.cmd == "decide":
            if pm.decide():
                pm.commit("decisions answered")
        elif args.cmd == "run":
            return pm.run(once=args.once, dry_run=args.dry_run)
        elif args.cmd == "task":
            if args.action == "add":
                print(pm.task_add(args.plan, args.subject, args.details, args.priority, args.blocked_by))
            elif args.action == "done":
                pm.task_set(args.task, status="done", note=args.note)
            elif args.action == "drop":
                pm.task_set(args.task, status="dropped", note=args.note)
            elif args.action == "block":
                pm.task_block(args.task, args.decision)
            else:
                pm.task_set(args.task, priority=args.priority)
        elif args.cmd == "plan":
            if args.action == "add":
                print(pm.plan_add(args.milestone, args.title, args.goal, args.priority))
            else:
                pm.plan_priority(args.plan, args.priority)
        elif args.cmd == "decision":
            print(pm.decision_add(args.question, args.context, args.option, args.blocks))
        elif args.cmd == "masterplan":
            pm.masterplan_set(args.milestone, status=args.status, goal=args.goal, scope=args.scope,
                              exit_criteria=args.exit_criteria)
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
