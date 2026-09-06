"""`doctor.py` — checking a local setup (§31 Phase 13).

The worst first hour with a self-hosted product is "it starts, and nothing
works". This script exists to turn that into one line naming the thing to
change, so what is worth testing is the naming: that a failure says which
setting, and that a check which cannot answer says so rather than reporting a
problem that is its own.

Deliberately does not need a running stack. A doctor whose own tests only run
when everything is already up is a doctor that has never been tried on a
broken machine.
"""

from __future__ import annotations

import pytest
from seskit_api.doctor import (
    Result,
    check_configuration,
    check_postgres,
    report,
    run,
)
from seskit_core.config import INSECURE_PLACEHOLDER, Settings

#: Refused immediately rather than after a DNS timeout, so the failure path is
#: fast enough to be a test.
UNREACHABLE = "postgresql+asyncpg://nobody@127.0.0.1:1/nothing"


def _settings(settings: Settings, **overrides: object) -> Settings:
    return settings.model_copy(update=overrides)


def _named(results: list[Result], name: str) -> Result:
    found = next((result for result in results if result.name == name), None)
    assert found is not None, f"no check called {name!r} in {[r.name for r in results]}"
    return found


# --------------------------------------------------------- configuration ---


def test_a_placeholder_secret_key_is_a_failure(settings: Settings) -> None:
    """The one that matters most, because nothing visibly breaks. Sessions
    still work; they are just signed with a value in the repository.
    """
    results = check_configuration(_settings(settings, SECRET_KEY=INSECURE_PLACEHOLDER))

    assert _named(results, "secret key").ok is False


def test_the_secret_key_failure_says_how_to_make_one(settings: Settings) -> None:
    """ "Set a real secret" is not an instruction anyone can follow at speed."""
    results = check_configuration(_settings(settings, SECRET_KEY=INSECURE_PLACEHOLDER))

    assert "secrets.token_urlsafe" in _named(results, "secret key").fix


def test_a_real_secret_key_passes(settings: Settings) -> None:
    results = check_configuration(_settings(settings, SECRET_KEY="a-real-one"))

    assert _named(results, "secret key").ok is True


def test_a_sync_database_url_is_a_failure(settings: Settings) -> None:
    """The whole application is async. A psycopg URL fails at the first query
    with a message about drivers rather than about configuration.
    """
    results = check_configuration(
        _settings(settings, DATABASE_URL="postgresql://user@localhost/seskit")
    )

    assert _named(results, "database url").ok is False


def test_an_unset_public_url_is_reported_and_not_failed(settings: Settings) -> None:
    """An instance ingesting events over SQS and not caring about one-click
    unsubscribe needs no public URL. Failing here would train people to ignore
    the output.
    """
    results = check_configuration(_settings(settings, PUBLIC_BASE_URL=None))

    public = _named(results, "public url")
    assert public.ok is True
    assert "unsubscribe" in public.detail


def test_an_unset_public_url_says_what_it_costs(settings: Settings) -> None:
    results = check_configuration(_settings(settings, PUBLIC_BASE_URL=None))

    assert _named(results, "public url").fix


# ---------------------------------------------------------------- probing ---


async def test_an_unreachable_database_is_one_line_not_a_traceback(
    settings: Settings,
) -> None:
    """Somebody reading this is already having a bad time. A stack trace is not
    an instruction.
    """
    results = await check_postgres(_settings(settings, DATABASE_URL=UNREACHABLE))

    postgres = _named(results, "postgres")
    assert postgres.ok is False
    assert "\n" not in postgres.detail
    assert "docker compose" in postgres.fix


async def test_an_unreachable_database_does_not_also_claim_the_schema_is_wrong(
    settings: Settings,
) -> None:
    """Every later check reporting the same root cause in its own words is how
    a diagnostic becomes noise.
    """
    results = await check_postgres(_settings(settings, DATABASE_URL=UNREACHABLE))

    assert [result.name for result in results] == ["postgres"]


# ----------------------------------------------------------------- output ---


def test_a_passing_check_does_not_print_a_fix() -> None:
    rendered = Result("redis", True, "connected", fix="Start it").render()

    assert "->" not in rendered


def test_a_failing_check_prints_its_fix() -> None:
    rendered = Result("redis", False, "refused", fix="Start it").render()

    assert "Start it" in rendered
    assert "FAIL" in rendered


def test_the_report_says_everything_is_well_when_it_is() -> None:
    assert "All checks passed." in report([Result("redis", True, "connected")])


def test_the_report_counts_the_failures() -> None:
    lines = report(
        [
            Result("redis", True, "connected"),
            Result("postgres", False, "refused"),
            Result("worker", False, "stuck"),
        ]
    )

    assert "2 of 3 checks failed" in lines


def test_the_report_says_which_failure_to_fix_first() -> None:
    """The checks are ordered the way a first hour fails, so the first failure
    usually causes the rest. Saying so stops somebody chasing five problems.
    """
    assert "first one first" in report([Result("postgres", False, "refused")])


# ------------------------------------------------------------------- whole ---


@pytest.mark.parametrize(
    "name", ["secret key", "database url", "public url", "postgres", "redis", "sending", "worker"]
)
async def test_every_check_runs(settings: Settings, name: str) -> None:
    """Against a stack that is not there, which is the state this is for. What
    matters is that each check reports rather than raising - a doctor that
    crashes on a broken machine is worse than none.
    """
    results = await run(_settings(settings, DATABASE_URL=UNREACHABLE))

    assert _named(results, name)
