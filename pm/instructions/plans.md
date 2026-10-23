## Milestones (the human's)

```sh
pm milestone add "Users can export their data as CSV" [--queue N] <<'EOF'
"Export" on the account page downloads one CSV per table the user owns.

- No new dependencies.
- No import, no other formats.
EOF
pm milestone set 1 [--subject …] [--body -] [--queue N]
pm milestone hold 1 ; pm milestone resume 1
```

A milestone defines a goal and a list of constraints: the goal in the subject and the first lines of the body, the constraints as bullet points. A worker that finds a milestone contradictory, incomplete or out of date raises a decision; the human answers it and edits the milestone.

## Plans

```sh
pm plan add 1 "Backend endpoint" --priority 700 \
    --intro "People can download their data as a spreadsheet file, one table at a time. Today they can only look at it on screen." <<'EOF'
`GET /api/export/<table>.csv` streams the rows of one table as CSV.

- Files: `app/api/export.py` (new), `app/api/routes.py`.
- Columns named as in the API, not the database.
EOF
pm plan edit 2 --body - <<'EOF'
…
EOF
pm plan edit 2 --subject "Export endpoint"
pm plan edit 2 --intro "…"       # queues nothing
pm plan edit 2 --priority 910    # its tasks without their own priority move with it; none: back to 0
```

`pm plan add` prints the id on stdout and `pm plan list` on stderr with the new plan marked: compare and fix its priority. An edit prints what it queued.
