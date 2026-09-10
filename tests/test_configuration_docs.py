"""The configuration docs describe the configuration that exists.

`Settings` had grown to 42 fields and `docs/reference/configuration.md`
described 15 of them, so a value found in the code had nowhere to be looked up.
Worse, `.env.example` still explained why there was no `AWS_ACCESS_KEY_ID` by
describing the boto3 credential chain that Phase 14 removed - the conclusion
survived, the reason did not, and the first thing a reader met when looking for
how to configure AWS was a description of a design that had been deleted.

Nothing noticed either. A setting can be added, renamed or removed without any
of this failing, which is how prose that was true when written becomes prose
that is wrong.

These tests do not judge the writing. They can prove a variable is described
somewhere and that nothing is described which does not exist; they cannot prove
the description is any good, and pretending otherwise would make them a ritual.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "packages" / "core" / "src" / "seskit_core" / "config.py"
REFERENCE = ROOT / "docs" / "reference" / "configuration.md"
ENV_EXAMPLE = ROOT / ".env.example"

#: A field on `Settings`: four spaces of indent, a shouting name, a colon.
_FIELD = re.compile(r"^    ([A-Z][A-Z0-9_]+)\s*:", re.MULTILINE)

#: `NAME=value` in the env file, commented out or not.
_ENV_VAR = re.compile(r"^#?\s*([A-Z][A-Z0-9_]+)=", re.MULTILINE)

#: Variables in `.env.example` that Docker Compose reads and SESKit does not.
#: They belong there - they configure the containers the stack runs in - but
#: they are not settings, and the reference says so in a section of its own.
COMPOSE_ONLY = frozenset(
    {
        "POSTGRES_USER",
        "POSTGRES_PASSWORD",
        "POSTGRES_DB",
        "POSTGRES_HOST_PORT",
        "REDIS_HOST_PORT",
    }
)


#: Values a setting may take, which appear in backticks in the same tables and
#: read exactly like names. Listed rather than pattern-matched: `DEBUG` is a log
#: level, not a variable, and the day one of these does become a setting this is
#: the line that has to be revisited. The lowercase values - `local`, `sqs` -
#: need no entry, because a name has to shout to be mistaken for one.
VALUE_LITERALS = frozenset({"DEBUG", "INFO", "WARNING", "ERROR"})


#: Read by the container entrypoint before Python starts, so it is not a
#: setting and never reaches `Settings`. A third category, and a small one -
#: `docker/entrypoint.sh` is the only thing that looks at it.
ENTRYPOINT_ONLY = frozenset({"MIGRATE_ON_START"})


def _settings_fields() -> list[str]:
    fields = _FIELD.findall(CONFIG.read_text(encoding="utf-8"))
    assert fields, "no settings found - has the class moved?"
    return fields


def _reference() -> str:
    return REFERENCE.read_text(encoding="utf-8")


def _env_example() -> str:
    return ENV_EXAMPLE.read_text(encoding="utf-8")


# ------------------------------------------------------------- completeness ---


def test_every_setting_is_in_the_reference() -> None:
    """All of them, including the tuning knobs nobody sets.

    Those are marked rather than left out, because the reason to list a poll
    interval is not that somebody should change it - it is that somebody who
    found it in the code needs somewhere to look it up. A table that silently
    omits a third of the fields cannot be trusted as the list, which makes it
    no more useful than the source.
    """
    reference = _reference()

    missing = [name for name in _settings_fields() if name not in reference]

    assert not missing, (
        f"{len(missing)} setting(s) not in docs/reference/configuration.md: {missing}"
    )


def test_the_reference_describes_nothing_that_does_not_exist() -> None:
    """The other direction, and the one that catches a removed setting.

    A variable deleted from `Settings` leaves its row behind, and a row for
    something that does nothing is worse than no row: it is a setting somebody
    will try, and then wonder why nothing changed.
    """
    known = (
        set(_settings_fields())
        | COMPOSE_ONLY
        | ENTRYPOINT_ONLY
        | _installer_variables()
        | VALUE_LITERALS
    )

    # Only names in a table cell or in `backticks`, so prose like "AWS" or a
    # heading does not read as a setting.
    named = set(re.findall(r"`([A-Z][A-Z0-9_]{3,})`", _reference()))

    unknown = sorted(named - known)

    assert not unknown, f"documented but not a setting: {unknown}"


def _installer_variables() -> set[str]:
    """`install.sh`'s own environment overrides, which the reference may link
    to but does not own - `docs/getting-started/installation.md` documents
    them, and `tests/test_installer.py` holds that pair together.
    """
    return {"SESKIT_VERSION", "SESKIT_DIR", "SESKIT_PUBLIC_URL"}


# ------------------------------------------------------------- .env.example ---


def test_env_example_sets_nothing_that_does_nothing() -> None:
    """Every variable in the file is either a setting or a named Compose one.

    A leftover in `.env.example` is the most convincing kind of wrong
    documentation: it is in the file you are told to copy, so it looks like
    configuration that works.
    """
    fields = set(_settings_fields())

    strays = sorted(
        name
        for name in set(_ENV_VAR.findall(_env_example()))
        if name not in fields | COMPOSE_ONLY | ENTRYPOINT_ONLY
    )

    assert not strays, f".env.example sets variables that are not settings: {strays}"


def test_env_example_carries_the_three_with_no_default() -> None:
    """`cp .env.example .env && docker compose up` has to be enough.

    These three have no default and refuse to boot when missing, so if the file
    you are told to copy does not set them, the documented quickstart does not
    start.
    """
    env = _env_example()

    for required in ("SECRET_KEY", "DATABASE_URL", "REDIS_URL"):
        assert re.search(rf"^{required}=", env, re.MULTILINE), (
            f"{required} has no default and .env.example does not set it"
        )


def test_the_compose_only_variables_are_called_out() -> None:
    """They configure the containers, not SESKit, and setting them does nothing
    to an instance running outside Compose.

    Nothing in the file distinguished them from settings the application reads,
    which is the confusion that prompted all of this.
    """
    reference = _reference()

    assert "not SESKit settings" in reference
    for name in COMPOSE_ONLY:
        assert f"`{name}`" in reference, f"{name} is not explained as a Compose variable"


# ------------------------------------------------------------------- claims ---


def test_nothing_still_describes_the_boto3_credential_chain() -> None:
    """Phase 14 replaced it with a key pasted into the dashboard and stored
    encrypted. The prose explaining the old design outlived it in two files,
    and read as current because its conclusion - that there is no
    `AWS_ACCESS_KEY_ID` setting - happened to stay true.
    """
    claim = re.compile(r"never stores? (them|it)|standard boto3 way", re.IGNORECASE)

    for path in (ENV_EXAMPLE, CONFIG, REFERENCE):
        text = path.read_text(encoding="utf-8")
        assert not claim.search(text), (
            f"{path.name} still describes the credential model Phase 14 replaced"
        )


def test_the_guards_would_notice() -> None:
    """A guard nobody has seen fail is a guard nobody should trust."""
    assert _FIELD.findall("    SECRET_KEY: str = Field(min_length=1)") == ["SECRET_KEY"]
    # Not a local, not a comment, not a method.
    assert _FIELD.findall("    def is_local(self) -> bool:") == []
    assert _FIELD.findall("        NESTED: int = 1") == []

    assert _ENV_VAR.findall("SECRET_KEY=changeme") == ["SECRET_KEY"]
    # Commented-out variables count: PUBLIC_BASE_URL ships that way, and a
    # stray one is just as misleading with a `#` in front of it.
    assert _ENV_VAR.findall("# PUBLIC_BASE_URL=https://mail.example.com") == ["PUBLIC_BASE_URL"]

    # The case this guard got wrong first time: a setting's *values* are
    # backticked in the same table cell as its name.
    assert set(re.findall(r"`([A-Z][A-Z0-9_]{3,})`", "| `LOG_LEVEL` | `INFO` |")) == {
        "LOG_LEVEL",
        "INFO",
    }
