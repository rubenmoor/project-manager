# Agent instructions

- Don't add tests unless asked.
- Use inline comments very sparingly. If a comment seems useful, keep it very short.
- Commit and push directly to main. Don't create branches or PRs unless there is a specific reason.
- Style of .md files: bullet points, ultra-concise, no full sentences needed. Exception: an introductory paragraph in full sentences at the top of a document and under each top-level heading.
- Versioning: `VERSION` in `pm/__init__.py`, one integer, printed by `pm --version`; pyproject and flake read it.
  - Every commit raises it by one, docs included.
  - On a rebase or merge conflict: take the higher number, plus one.
  - Enforced by `.githooks/pre-commit`; enable once per clone: `git config core.hooksPath .githooks` (`nix develop` and Claude Code sessions do it).
- Schema version: `SCHEMA_VERSION` in `pm/__init__.py`, also printed by `pm --version`.
  - Raise it by one with every change to `SCHEMA` (no migrations: older `tasks.sql` files are refused).
  - The pre-commit hook refuses a `SCHEMA` change without it.
