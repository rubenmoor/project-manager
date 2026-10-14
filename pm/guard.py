"""Claude Code hooks for pm workers; `pm run` passes them with `--settings` ("{settings}" in the worker command).
    PreToolUse (Bash): blocks commands that stop processes by pattern (`pkill -f`, `killall`, `kill -1`), nested ones
        (`bash -c '…'`, `nix develop -c …`) included: the worker's own process can match the pattern. And commands
        that read or search the database file (PM_DB): reading it (`cat`, `grep`, `sed` … naming it), `grep -r`,
        `git grep` and `rg --no-ignore` without excluding it; rg and Claude Code's Grep skip it via .ignore, and the
        settings deny Read of it. It is megabytes of SQL; workers query it with pm.
    Stop: once, keeps the worker going while its task (PM_TASK) is still open and waits for no decision, or while a
        background task runs: `claude -p` ends with the turn, and nothing wakes it later."""
import json, os, re, sys
from pathlib import Path

KILL_BY_PATTERN = re.compile(r"\bpkill\b[^;&|\n]*\s(-\w*f\b|--full\b)|\bkillall\b|\bkill\s+(--?\w*\s+)*-1\b")
KILL_REASON = "pm: stop processes by PID (kill $!) or a pidfile; pkill -f, killall and kill -1 can match this worker"
READERS = {"cat", "head", "tail", "less", "more", "grep", "egrep", "fgrep", "rg", "ag", "awk", "gawk", "sed", "bat",
           "nl", "cut", "sort", "strings", "sqlite3"}
SEGMENTS = re.compile(r"[;&|\n()`]+|\$\(")
EXCLUDED = re.compile(r"--exclude|:!|:\^|:\(exclude\)|-g\s*['\"]?!|--glob[= ]['\"]?!")


def db_reason(command, db):
    """Why the Bash command reads or searches the database file db (a file name), or None."""
    for seg in SEGMENTS.split(command):
        words = [w.strip("'\"") for w in seg.split()]
        while words and re.fullmatch(r"\w+=\S*|sudo|command|env|xargs|nice|time", words[0]):
            words.pop(0)
        if not words:
            continue
        cmd, args, named = os.path.basename(words[0]), words[1:], db in seg
        excluded = named and EXCLUDED.search(seg)
        if cmd in READERS and named and not excluded:
            return f"reads {db}"
        if cmd in ("grep", "egrep", "fgrep") and not excluded and any(
                re.fullmatch(r"-[A-Za-z]*[rR][A-Za-z]*|--recursive|--dereference-recursive", a) for a in args):
            return f"grep -r searches {db}; use rg or the Grep tool, which skip it"
        if cmd == "rg" and not excluded and any(re.fullmatch(r"-u+|--no-ignore(-dot)?|--no-ignore-files", a) for a in args):
            return f"rg without ignore files searches {db}; add -g '!{db}'"
        if cmd == "git" and "grep" in args[:3] and not excluded:
            return f"git grep searches {db}; use rg or the Grep tool, which skip it, or add ':!{db}'"
    return None


def task_unfinished(tid):
    """Why task tid isn't finished (open, waiting for no decision), or None."""
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    import pm
    root = pm.git_root()
    if not root:
        return None
    project = pm.Project(root, pm.load_config(root))
    t = project.rows("SELECT status FROM task WHERE id = ?", tid)
    waits = project.rows("SELECT 1 FROM task_decision td JOIN decision d ON d.id = td.decision_id "
                         "WHERE td.task_id = ? AND d.status = 'pending'", tid)
    if t and t[0]["status"] == "open" and not waits:
        return f"task {tid} is still open"
    return None


def stop(e):
    if e.get("stop_hook_active"):
        return 0
    why = []
    if e.get("background_tasks"):
        why.append("a background task is still running, and this session ends with your turn: nothing wakes you "
                   "later. Wait for it in the foreground (a Bash call with a timeout) or stop it")
    tid = os.environ.get("PM_TASK", "")
    if tid.isdigit():
        try:
            if unfinished := task_unfinished(int(tid)):
                why.append(f"{unfinished}. Finish it now: `pm task done {tid} \"…\"`, `pm task drop {tid} \"…\"`, or "
                           f"make it pending on a decision; a long check runs in the foreground")
        except Exception:  # noqa: BLE001 — never stop a worker over the hook's own failure
            pass
    if why:
        print("pm: " + "; ".join(why) + ".", file=sys.stderr)
        return 2
    return 0


def main():
    try:
        e = json.load(sys.stdin)
    except ValueError:
        return 0
    if not isinstance(e, dict):
        return 0
    if e.get("hook_event_name") == "Stop":
        return stop(e)
    command = str((e.get("tool_input") or {}).get("command") or "")
    if KILL_BY_PATTERN.search(command):
        print(KILL_REASON, file=sys.stderr)
        return 2
    if why := db_reason(command, os.path.basename(os.environ.get("PM_DB", "tasks.sql"))):
        print(f"pm: {why}. It is pm's database: query it with pm (`pm show <id>`, `pm queue`, "
              f"`pm decision list --answered`), not by reading the file", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
