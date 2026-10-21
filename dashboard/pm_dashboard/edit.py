"""Writes through pm's own methods, and reads of tasks.sql history from git.

pm's rules apply: while a worker runs, its task can't be changed, and other changes are committed with that task.
A text edit carries the text it started from ("base"); it is refused if the row changed since."""
import json, sqlite3, subprocess, time

import pm

MAX_DIFF = 2_000_000


class EditError(Exception):
    pass


TABLES = {"task_edit": "task", "plan_edit": "plan", "milestone_edit": "milestone"}


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
    if kind in TABLES and isinstance(base := op.get("base"), dict):
        row = project.rows(f"SELECT * FROM {TABLES[kind]} WHERE id = ?", int(op["id"]))
        if row and (changed := [f for f, v in base.items() if f in ("subject", "body", "intro") and row[0][f] != v]):
            raise EditError(f"the {' and '.join(changed)} changed since you started editing (a worker or another "
                            f"window); copy your text, then edit the new version")
    if kind == "milestone_add":
        subject = text(op.get("subject"), "the subject")
        mid = project.milestone_add(subject.strip(), text(op.get("body", ""), "the body", False) or "",
                                    prio(op.get("queue_index")) or 0)
        return f"milestone {mid} added: {subject.strip()}", mid
    if kind == "milestone_edit":
        project.milestone_set(int(op["id"]), subject=text(op.get("subject"), "the subject", False),
                              body=text(op.get("body"), "the body", False) if "body" in op else None,
                              queue_index=(prio(op["queue_index"]) or 0) if "queue_index" in op else None)
        return f"milestone {op['id']} changed", None
    if kind == "milestone_hold":
        mid, hold = int(op["id"]), op.get("hold")
        if not isinstance(hold, bool):
            raise EditError("hold must be true or false")
        project.milestone_hold(mid, hold)
        return f"milestone {mid} " + ("on hold" if hold else "resumed"), None
    if kind == "plan_add":
        subject = text(op.get("subject"), "the subject")
        pid = project.plan_add(int(op["milestone"]), subject.strip(), text(op.get("body"), "the body"), prio(op.get("priority")),
                               text(op.get("intro"), "the intro"))
        return f"plan {pid} added: {subject.strip()}", pid
    if kind == "plan_edit":
        p = op.get("priority", ...)
        project.plan_edit(int(op["id"]), subject=text(op.get("subject"), "the subject", False),
                          body=text(op.get("body"), "the body", False),
                          intro=text(op.get("intro"), "the intro", False),
                          prio=None if p is ... else ("none" if prio(p) is None else p))
        return f"plan {op['id']} changed", None
    if kind == "task_add":
        subject = text(op.get("subject"), "the subject")
        k = op.get("kind", "work")
        if k not in ("work", "plan", "exec"):
            raise EditError("kind must be work, plan or exec")
        tid = project.task_add(None if k == "exec" else int(op["plan"]), subject.strip(), text(op.get("body"), "the body"), prio(op.get("priority")), k,
                               worktree=None if k == "exec" else op.get("worktree"))
        return f"task {tid} added: {subject.strip()}", tid
    if kind == "task_edit":
        fields = {}
        if "subject" in op:
            fields["subject"] = text(op["subject"], "the subject").strip()
        if "body" in op:
            fields["body"] = text(op["body"], "the body", False).strip()
        if "priority" in op:
            fields["priority"] = prio(op["priority"])
        if "model" in op:
            m = op["model"]
            if m is not None and (not isinstance(m, str) or not m.strip()):
                raise EditError("the model must be a name, or null for the kind's")
            fields["model"] = m.strip() if m else None
        if "worktree" in op:
            w = op["worktree"]
            if not isinstance(w, str):
                raise EditError("the work tree is one of [worktrees] in the config")
            fields["worktree"] = w
        if not fields:
            raise EditError("nothing to change")
        project.task_set(int(op["id"]), **fields)
        return f"task {op['id']} " + (list(fields)[0] if list(fields) in (["priority"], ["model"], ["worktree"]) else "changed"), None
    if kind == "task_close":
        status = op.get("status")
        if status not in ("done", "dropped"):
            raise EditError("status must be done or dropped")
        project.task_set(int(op["id"]), status=status, note=text(op.get("note"), "the note"))
        return f"task {op['id']} {'done' if status == 'done' else 'drop'}", None
    if kind == "task_requires":
        add, remove = [int(x) for x in op.get("add") or []], [int(x) for x in op.get("remove") or []]
        project.task_requires(int(op["id"]), add, remove)
        return "; ".join([*([f"task {op['id']} requires {', '.join(map(str, add))}"] if add else []),
                          *([f"task {op['id']} no longer requires {', '.join(map(str, remove))}"] if remove else [])]), None
    if kind == "task_kill":
        project.kill(int(op["id"]))
        return f"task {op['id']} killed", None
    if kind == "task_redo":
        tid = project.task_redo(int(op["id"]))
        return f"task {tid}: task {op['id']} again", tid
    if kind == "decision_answer":
        once, sup = op.get("once", False), op.get("supersedes", [])
        if not isinstance(once, bool) or not isinstance(sup, list) or not all(type(s) is int for s in sup):
            raise EditError("once must be true or false, supersedes a list of decision ids")
        said = project.answer(int(op["id"]), text(op.get("answer"), "the answer").strip(), "once" if once else None, sup)
        return f"decision {op['id']} answered: {short(said)}", None
    if kind == "decision_approve":
        project.approve(int(op["id"]))
        return f"decision {op['id']} approved", None
    if kind == "decision_irrelevant":
        reason = text(op.get("reason"), "the reason").strip()
        project.resolve(int(op["id"]), irrelevant=reason)
        return f"decision {op['id']} irrelevant: {short(reason)}", None
    if kind == "summary_request":
        tid, new = project.summary_request()
        return (f"task {tid} queued: {pm.SUMMARY_SUBJECT}" if new else f"task {tid} already queued"), tid
    if kind == "report_ask":
        cid, tid = project.report_ask(text(op.get("question"), "the question"))
        return f"task {tid} queued: custom report {cid}", tid
    if kind == "report_request":
        tid, new = project.report_request()
        return (f"task {tid} queued: {pm.REPORT_SUBJECT}" if new else f"task {tid} already queued"), tid
    raise EditError(f"unknown edit {kind!r}")


def short(s):
    """A commit subject's worth of a Markdown text: its first line."""
    line = s.strip().splitlines()[0]
    return line if len(line) <= 72 and "\n" not in s.strip() else line[:71].rstrip() + "…"


def edit(project, op):
    """Apply an edit and commit it, unless a worker runs: then it lands in that task's commit."""
    try:
        msg, rid = apply(project, op)
    except (KeyError, ValueError, TypeError) as e:
        raise EditError(f"bad request: {e}") from None
    except pm.FormatError as e:
        raise EditError(str(e)) from None
    for _ in range(10):                    # git may be busy with a commit of pm run (index.lock)
        if project.commit_db(msg + " (dashboard)"):
            break
        time.sleep(0.3)
    return {"ok": True, "id": rid, "message": msg}


# --- history --------------------------------------------------------------------------------

def load(sql):
    """tasks.sql text as {table: {key: row}}."""
    con = pm.load_sql(sql)
    try:
        out = {}
        for (t,) in con.execute("SELECT name FROM sqlite_schema WHERE type = 'table'").fetchall():
            key = pm.primary_key(con, t)
            out[t] = {"/".join(str(r[k]) for k in key): dict(r) for r in con.execute(f"SELECT * FROM {t}")}
        return out
    finally:
        con.close()


def diff(old_sql, new_sql):
    """Two versions of tasks.sql, row by row: {table: {added, removed, changed: [{row, before}]}}."""
    old, new = load(old_sql), load(new_sql)
    tables = {}
    for t in new:
        o, n = old.get(t, {}), new[t]
        added = [n[k] for k in n if k not in o]
        removed = [o[k] for k in o if k not in n]
        # Columns a newer schema added aren't changes.
        before = lambda k: {f: o[k][f] for f in n[k] if f in o[k] and o[k][f] != n[k][f]}
        changed = [{"row": n[k], "before": before(k)} for k in n if k in o and before(k)]
        for rows in (added, removed, [c["row"] for c in changed], [c["before"] for c in changed]):
            for r in rows:
                if isinstance(r.get("options"), str):
                    r["options"] = json.loads(r["options"])
        if added or removed or changed:
            tables[t] = {"added": added, "removed": removed, "changed": changed}
    return tables


def task_commit(project, tid):
    """The commit `pm run` made for task tid ("task N: subject"), with its patch."""
    # --grep matches any line of a message: keep the first commit whose subject is the task's
    out = project.git("log", "-z", "--format=%H%x00%cI%x00%s%x00%b%x00", "-E", f"--grep=^task {int(tid)}: ").split("\0")
    hits = [out[i:i + 4] for i in range(0, len(out) - 3, 5) if out[i + 2].startswith(f"task {int(tid)}: ")]
    if not hits:
        return {"commit": None}
    commit, date, subject, body = [x.lstrip("\n") for x in hits[0]]
    patch = project.git("show", "--format=", "--patch", "-M", "--no-color", commit)
    stat = project.git("show", "--format=", "--numstat", "-M", commit)
    files = []
    for line in stat.splitlines():
        a, d, name = line.split("\t", 2)
        files.append({"file": name, "added": None if a == "-" else int(a), "deleted": None if d == "-" else int(d)})
    big = len(patch) > MAX_DIFF
    return {"commit": commit, "date": date, "subject": subject, "body": body.strip(), "files": files,
            "patch": patch[:MAX_DIFF], "truncated": big, "rows": rows(project, commit)}


def rows(project, commit):
    """The commit's changes to tasks.sql, row by row; None if it didn't touch it or can't be read."""
    try:
        if not project.git("diff", "--name-only", f"{commit}^", commit, "--", project.db).strip():
            return None
        return diff(project.git("show", f"{commit}^:{project.db}"), project.git("show", f"{commit}:{project.db}"))
    except (subprocess.CalledProcessError, sqlite3.Error):
        return None
