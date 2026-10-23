# pm: instructions for agents

`pm` keeps a project's work as **milestones → plans → tasks**, plus **decisions** only the human makes, in `tasks.sql`, read and changed only through `pm`. `pm run` works the queue with a `claude -p` worker per task (`PM_TASK=<task>`), one task at a time per git work tree, and merges each work tree into the trunk. Details per topic: `pm instructions <topic>`; flags: `pm <command> --help`; everything: `pm instructions --all`.

## Rules

1. **Subject on the command line, body on stdin** via `<<'EOF'` (quoted, so Markdown stays as written); `--body -` reads stdin too. No subject: an editor, which fails without a terminal.
2. **Creating commands print the new id** on stdout, nothing else: `tid=$(pm task add 4 "…" --worktree app <<'EOF' … EOF)`.
3. **IDs:** integers per kind; `pm show 12` (task), `d32`, `p3`, `m1`, `s2` (summary), `r2`/`c2` (reports).
4. **`tasks.sql` is pm's:** never read, grep or edit it; query with pm. By hand only when the human asks, then `pm validate`.
5. **References:** by id and full subject, `plan 2: Backend endpoint`: plain in a subject, in backticks in a body, note or decision.
6. **Bodies:** Markdown for an engineer who knows the code but not the intentions: what, why, which files, done when.
7. **Intros** (`--intro`, required for plans and decisions): one paragraph, 2 to 4 sentences, plain English for an outsider: what and why. No ids, files, code or project-management words.
8. **Closed is final:** implemented milestones, done plans, closed tasks, a running task's subject and body. Add new ones instead.
9. **The human's:** milestone add/set/hold/resume, answering decisions, exec tasks, `pm run`. A worker raises a decision instead.
10. **Commits:** outside `pm run` each command commits `tasks.sql`. A worker stages (`git add`), never commits or pushes.
11. **Decision log first:** before a task or choice, `pm decision list --answered --grep <word>`; answers are binding until superseded; cite `decision 32: <subject>`.

## Commands

```sh
# inspect
pm status | pm queue | pm show <7|d32|p3|m1> | pm runs [task] | pm validate
pm milestone list | pm plan list | pm decision list [--answered|--all] [--grep w …]
# plans
pm plan add <milestone> "<subject>" --intro "…" [--priority N] <<'EOF' … EOF
pm plan edit <plan> [--subject …] [--body -] [--intro …] [--priority N]
# tasks
pm task add <plan> "<subject>" --worktree <name> [--requires <task> …] [--kind plan] [--priority N] [--web] [--model opus] <<'EOF' … EOF
pm task set <task> [--subject …] [--body -] [--priority N|none] [--web|--no-web] [--model opus|none] [--worktree <name>]
pm task requires <task> <prerequisite> … [--remove <id>] | pm task priority <task> <N|none>
pm task done <task> "<YYYY-MM-DD, files>" | pm task drop <task> "<reason>" [--replaced-by <task>]
# decisions
pm decision add "<question>" --intro "…" [--body …] --option "<recommended>" --option … [--once]
pm task-pending-decision <task> <decision>
pm decision record "<subject>" --intro "…" --answer "…" --body "<why>" [--supersedes <d>]
pm decision consequences <decision> "<what changed>" | pm decision supersede <new> <old> [--note …]
# the human's (run only when the human asks, in their session)
pm init | pm run [--once|--dry-run] | pm stop | pm kill <task> | pm task redo <task> | pm decide
pm milestone add "<goal>" [--queue N] <<'EOF' … EOF | pm milestone set|hold|resume <milestone>
pm decision answer <d> <option|"text"> [--once] [--supersedes <d>] | approve <d> | irrelevant <d> "<why>" | kind <d> rule|once
pm task add --exec "<subject>" | pm summary request | pm report request | pm report ask "<question>"
```

## Topics

`pm instructions <topic>`:

- `model`: milestones, plans, tasks, task kinds, decisions, sign-offs, the queue
- `queue`: the tasks pm queues itself, priorities and their rules
- `plans`: milestones and plans, with examples
- `tasks`: tasks, dependencies, model, work trees, web access, follow-up tasks
- `decisions`: asking the human, iterative feedback, the decision log, your own choices, consequences
- `recipes`: the mechanics of each kind of task: masterplan, split, revise, cleanup, summary, reports, finish
- `human`: the human's commands and settings
- `errors`: error messages and what they mean
