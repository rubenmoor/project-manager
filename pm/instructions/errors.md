## Errors

| Message | Meaning |
|---|---|
| `no plan 9`, `no task 9`, `no milestone 9`, `no decision 9` | No such id; look it up with `pm status`, `pm queue` or `pm show` |
| `the change breaks the schema, nothing written: … a plan needs a body` (or `an open task needs a body`) | Empty body: stdin was empty or not piped |
| `` `pm task add` without a subject needs a terminal `` | Give the subject on the command line |
| `task 6 is running; its subject and body can't be changed`, `task 6 is done; a closed task can't be changed` | Add a new task instead |
| `decision 3 is not pending` | Answered or marked irrelevant already |
| `the database has errors (pm validate)` | `tasks.sql` was edited into an invalid state; `pm validate` lists the problems |
| `no project-manager.toml: run pm init first` | The project has no pm yet; that's the human's call |
| `tasks.sql is missing, git may be rewriting it; try again`, `tasks.sql changed under pm, nothing written; try again` | git (stash, checkout, rebase) is rewriting the file; wait a moment, run the command again |
| `the change would remove rows from …; nothing written` | pm only adds rows (prerequisite links excepted); a bug in the command, report it |
