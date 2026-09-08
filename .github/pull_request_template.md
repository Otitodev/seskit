## What and why

<!--
What changes, and why it is worth changing. Not a list of files - the diff
already says what moved. Say what was wrong, or what became possible.
-->

## Checks

<!--
Tick what you did. Delete any line that does not apply rather than leaving it
unticked: an untouched box means nobody decided, and CI refuses those.
-->

- [ ] Migrations: `uv run alembic upgrade head` runs, and `tests/test_migrations.py` passes
- [ ] Docs: updated, and `uv run --group docs mkdocs build --strict` is clean
- [ ] Tried by hand where a test cannot reach it - anything touching mail on the wire, the dashboard in a browser, or a deployment
