"""Claude Code PreToolUse hook for pm workers: blocks Bash commands that stop processes by pattern (`pkill -f`,
`killall`, `kill -1`), nested ones (`bash -c '…'`, `nix develop -c …`) included. The worker's own process can match
the pattern. `pm run` passes it to the worker with `--settings` ("{settings}" in the worker command)."""
import json, re, sys

KILL_BY_PATTERN = re.compile(r"\bpkill\b[^;&|\n]*\s(-\w*f\b|--full\b)|\bkillall\b|\bkill\s+(-\w+\s+)*-1\b")
REASON = "pm: stop processes by PID (kill $!) or a pidfile; pkill -f, killall and kill -1 can match this worker"


def main():
    try:
        command = (json.load(sys.stdin).get("tool_input") or {}).get("command") or ""
    except (ValueError, AttributeError):
        return 0
    if KILL_BY_PATTERN.search(str(command)):
        print(REASON, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
