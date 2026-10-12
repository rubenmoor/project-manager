"""Writes through pm's own methods, and reads of tasks.sql history from git.

Rules for writes while a worker runs: the running task can't be changed; everything else can, and is committed with
the running task (pm commits tasks.sql with it), not on its own."""
import json, sqlite3, subprocess, time

import pm

MAX_DIFF = 2_000_000


class EditError(Exception):
    pass


def running_task(project):
    rows = project.rows("SELECT task_id FROM run WHERE result = 'running'")
    return rows[0]["task_id"] if rows else None


def text(v, what, required=True):
    if v is None and not required:
        return None
    if not isinstance(v, str) or (required and not v.strip()):
        raise EditError(f"{what} must be a non-empty text")
    return v


def prio(v):
    if v is None or v == "":
        return None
    if isinstance(v, bool) or not isinstance(v, int):
        raise EditError("priority must be an integer or empty")
    return v


def apply(project, op):
    """Run one edit; returns (message for the commit, the new row's id or None)."""
    kind = op.get("op")
    running = running_task(project)
    if kind in ("task_edit", "task_close") and op.get("id") == running:
        raise EditError(f"task {running} is running; it can't be changed until its run ends")
    if kind == "milestone_add":
        subject = text(op.get("subject"), "the subject")
        mid = project.milestone_add(subject.strip(), text(op.get("body", ""), "the body", False) or "")
        return f"milestone {mid} added: {subject.strip()}", mid
    if kind == "milestone_edit":
        project.milestone_set(int(op["id"]), subject=text(op.get("subject"), "the subject", False),
                              body=text(op.get("body"), "the body", False) if "body" in op else None)
        return f"milestone {op['id']} changed", None
    if kind == "milestone_hold":
        mid, hold = int(op["id"]), op.get("hold")
        if not isinstance(hold, bool):
            raise EditError("hold must be true or false")
        if not hold and project.rows("SELECT 1 FROM milestone WHERE id = ? AND status <> 'on-hold'", mid):
            raise EditError(f"milestone {mid} is not on hold")
        project.milestone_set(mid, status="on-hold" if hold else "open")
        return f"milestone {mid} " + ("on hold" if hold else "resumed"), None
    if kind == "plan_add":
        subject = text(op.get("subject"), "the subject")
        pid = project.plan_add(int(op["milestone"]), subject.strip(), text(op.get("body"), "the body"), prio(op.get("priority")))
        return f"plan {pid} added: {subject.strip()}", pid
    if kind == "plan_edit":
        p = op.get("priority", ...)
        project.plan_edit(int(op["id"]), subject=text(op.get("subject"), "the subject", False),
                          body=text(op.get("body"), "the body", False),
                          prio=None if p is ... else ("none" if prio(p) is None else p))
        return f"plan {op['id']} changed", None
    if kind == "task_add":
        subject = text(op.get("subject"), "the subject")
        k = op.get("kind", "work")
        if k not in ("work", "plan"):
            raise EditError("kind must be work or plan")
        tid = project.task_add(int(op["plan"]), subject.strip(), text(op.get("body"), "the body"), prio(op.get("priority")), k)
        return f"task {tid} added: {subject.strip()}", tid
    if kind == "task_edit":
        fields = {}
        if "subject" in op:
            fields["subject"] = text(op["subject"], "the subject").strip()
        if "body" in op:
            fields["body"] = text(op["body"], "the body", False).strip()
        if "priority" in op:
            fields["priority"] = prio(op["priority"])
        if not fields:
            raise EditError("nothing to change")
        project.task_set(int(op["id"]), **fields)
        return f"task {op['id']} " + ("priority" if list(fields) == ["priority"] else "changed"), None
    if kind == "task_close":
        status = op.get("status")
        if status not in ("done", "dropped"):
            raise EditError("status must be done or dropped")
        project.task_set(int(op["id"]), status=status, note=text(op.get("note"), "the note"))
        return f"task {op['id']} {'done' if status == 'done' else 'drop'}", None
    raise EditError(f"unknown edit {kind!r}")


def edit(project, op):
    """Apply an edit and commit it, unless a worker runs: then it lands in that task's commit."""
    try:
        msg, rid = apply(project, op)
    except (KeyError, ValueError, TypeError) as e:
        raise EditError(f"bad request: {e}") from None
    except pm.FormatError as e:
        raise EditError(str(e)) from None
    for _ in range(10):                    # git may be busy with a commit of pm run (index.lock)
        if running_task(project) is not None or not project.git("status", "--porcelain", "--", project.db).strip():
            break
        project.commit_db(msg + " (dashboard)")
        time.sleep(0.3)
    return {"ok": True, "id": rid, "message": msg}


# --- history --------------------------------------------------------------------------------

def load(sql):
    """tasks.sql text as {table: {key: row}}."""
    con = sqlite3.connect(":memory:")
    try:
        con.row_factory = sqlite3.Row
        con.executescript(f"BEGIN;\n{sql}\nCOMMIT;")
        out = {}
        for (t,) in con.execute("SELECT name FROM sqlite_schema WHERE type = 'table'"):
            info = con.execute(f"PRAGMA table_info({t})").fetchall()
            key = [c[1] for c in sorted(info, key=lambda c: c[5]) if c[5]]
            out[t] = {"/".join(str(r[k]) for k in key): dict(r) for r in con.execute(f"SELECT * FROM {t}")}
        return out
    finally:
        con.close()


def since(project, commit):
    """What changed in the database between `commit` and the working copy, row by row."""
    try:
        old_sql = project.git("show", f"{commit}:{project.db}")
        when = project.git("log", "-1", "--format=%cI", commit).strip()
    except subprocess.CalledProcessError:
        return {"error": f"{project.db} isn't in commit {commit[:12]}"}
    try:
        old, new = load(old_sql), load(project.db_path.read_text(encoding="utf-8"))
    except sqlite3.Error as e:
        return {"error": f"can't read {project.db}: {e}"}
    tables = {}
    for t in new:
        o, n = old.get(t, {}), new[t]
        added = [n[k] for k in n if k not in o]
        removed = [o[k] for k in o if k not in n]
        # Columns a newer schema added aren't changes.
        diff = lambda k: {f: o[k][f] for f in n[k] if f in o[k] and o[k][f] != n[k][f]}
        changed = [{"row": n[k], "before": diff(k)} for k in n if k in o and diff(k)]
        for rows in (added, removed, [c["row"] for c in changed], [c["before"] for c in changed]):
            for r in rows:
                if isinstance(r.get("options"), str):
                    r["options"] = json.loads(r["options"])
        if added or removed or changed:
            tables[t] = {"added": added, "removed": removed, "changed": changed}
    return {"commit": commit, "date": when, "tables": tables}


def task_commit(project, tid):
    """The commit `pm run` made for task tid ("task N: subject"), with its patch."""
    out = project.git("log", "-1", "--format=%H%x00%cI%x00%s%x00%b", "-E", f"--grep=^task {int(tid)}: ").strip()
    if not out:
        return {"commit": None}
    commit, date, subject, body = (out.split("\0") + [""] * 4)[:4]
    patch = project.git("show", "--format=", "--patch", "-M", "--no-color", commit)
    stat = project.git("show", "--format=", "--numstat", "-M", commit)
    files = []
    for line in stat.splitlines():
        a, d, name = line.split("\t", 2)
        files.append({"file": name, "added": None if a == "-" else int(a), "deleted": None if d == "-" else int(d)})
    big = len(patch) > MAX_DIFF
    return {"commit": commit, "date": date, "subject": subject, "body": body.strip(), "files": files,
            "patch": patch[:MAX_DIFF], "truncated": big}
