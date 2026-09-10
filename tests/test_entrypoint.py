"""The container entrypoint (`docker/entrypoint.sh`).

It exists because of a deployment with nowhere to run `alembic upgrade head`.
Under Compose the one-shot `migrate` service applies the schema and both
processes wait on it, and the documentation covers running it from a checkout —
but a platform that runs the image directly may offer no release hook, no
one-off job and no shell at all. The instance builds, starts, and fails on its
first query, with no way in to fix it.

Text over the file rather than a real container, for the same reason
`test_dockerfiles.py` reads the Dockerfiles: this runs in milliseconds inside
the ordinary suite, and what is being checked is a property of the script.

Two things here would be quiet if they broke. A `MIGRATE_ON_START` that reads
as true when it is empty would migrate on every start for people who never
asked. And a missing `exec` would leave this shell as PID 1, so the platform's
stop signal reaches the shell instead of uvicorn — costing a graceful shutdown
on every deploy, visibly nowhere.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENTRYPOINT = ROOT / "docker" / "entrypoint.sh"


def _script() -> str:
    return ENTRYPOINT.read_text(encoding="utf-8")


def _code() -> str:
    """The script with its comments removed.

    Needed because this file's comments quote the very things it forbids - the
    script explains why there is no `|| true` on the migration, and a search
    over the raw text finds that sentence and fails. A guard that a file cannot
    document itself past is a guard that discourages the documentation.
    """
    lines = [
        line.split("#", 1)[0]
        for line in _script().splitlines()
        if not line.lstrip().startswith("#")
    ]
    return "\n".join(lines)


def test_the_entrypoint_exists_and_is_posix_sh() -> None:
    """Both base images are `python:3.12-slim`, whose /bin/sh is dash. A
    bashism here fails at container start, which is the worst place to find it.
    """
    script = _script()

    assert script.startswith("#!/bin/sh")
    assert "[[" not in script, "double brackets are a bashism"
    assert "function " not in script, "so is the function keyword"


def test_it_hands_over_with_exec() -> None:
    """So the application becomes PID 1 and receives the platform's signals.

    Without it this shell holds PID 1, a stop goes to the shell rather than to
    uvicorn or arq, and every deploy ends in a kill after the timeout instead
    of a graceful shutdown. Nothing reports that as an error.
    """
    assert re.search(r'^exec "\$@"$', _script(), re.MULTILINE)


def test_migrations_are_off_unless_asked_for() -> None:
    """The default has to be "do nothing".

    Under Compose the `migrate` service already applies the schema and both
    processes wait on it, which is the better arrangement where it exists: it
    is visible in `docker compose ps`, and a failure stops the stack rather
    than being buried in an application log.
    """
    script = _script()

    assert "MIGRATE_ON_START" in script
    # Defaulted to empty, so an unset variable takes the "do nothing" branch
    # rather than tripping `set -u`.
    assert "${MIGRATE_ON_START:-}" in script


def test_only_a_clear_yes_turns_it_on() -> None:
    """An empty value must not read as true.

    Most hosting platforms represent "declared, not set" as an empty string -
    the same thing that used to stop SESKit booting at all. Here it would fail
    in the opposite direction and quietly migrate the database on every start
    for someone who never asked.
    """
    case = re.search(r"case .*?\n(.*?)esac", _code(), re.DOTALL)
    assert case, "the switch on MIGRATE_ON_START has moved"

    accepted = case.group(1)
    assert "true" in accepted
    # No wildcard arm: `*)` would make every value true, including "false".
    assert not re.search(r"^\s*\*\)", accepted, re.MULTILINE), (
        "a catch-all arm would treat 'false' as true"
    )


def test_a_failed_migration_stops_the_container() -> None:
    """Starting anyway would turn one loud failure into a stream of query
    errors against a schema that is not what the code expects.
    """
    code = _code()

    assert "set -eu" in code
    assert "alembic upgrade head" in code
    assert "|| true" not in code, "a failed migration must not be swallowed"


def test_both_images_use_it() -> None:
    """The API and the worker are the same build with a different last line,
    and both need the schema. An entrypoint on only one of them is a worker
    that starts against a database the API has not migrated yet.
    """
    for name in ("Dockerfile", "Dockerfile.worker"):
        text = (ROOT / "docker" / name).read_text(encoding="utf-8")
        assert 'ENTRYPOINT ["seskit-entrypoint"]' in text, name
        assert "docker/entrypoint.sh" in text, name


def test_the_command_still_decides_which_process_runs() -> None:
    """The entrypoint runs before the command and then hands over to it, so
    `docker compose run --rm migrate` and a platform overriding the command
    both keep working - the entrypoint execs whatever it is given.
    """
    api = (ROOT / "docker" / "Dockerfile").read_text(encoding="utf-8")
    worker = (ROOT / "docker" / "Dockerfile.worker").read_text(encoding="utf-8")

    assert "CMD [" in api and "uvicorn" in api
    assert "CMD [" in worker and "arq" in worker


def test_migrations_serialise_across_replicas() -> None:
    """Two replicas starting together is the normal case on the platforms this
    was written for, and Alembic does not serialise concurrent runs itself.

    The lock lives in `migrations/env.py` rather than in the entrypoint, so it
    protects every way of running migrations - the Compose service and a hand-
    run upgrade included - not only the new one.
    """
    env = (ROOT / "migrations" / "env.py").read_text(encoding="utf-8")

    assert "pg_advisory_lock" in env
    assert "pg_advisory_unlock" in env
