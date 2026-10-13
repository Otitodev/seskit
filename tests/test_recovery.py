"""Recovering a forgotten dashboard password without email or a shell (§7).

The login page cannot ask for an emailed link, because a fresh instance may not
be able to send one yet, and a deployment made by pushing to Git offers no shell
to run a command in. So the way back in is something the owner already holds: a
recovery code shown once at sign-up, or a key the operator puts in the
environment.

What is worth testing is not that a form posts. It is that the code is spent
when it is used and replaced in the same step, that a wrong answer looks the same
whichever half was wrong, that the limit holds when the caller lies about their
address, and that a stolen session cannot mint a code without the password.

The first group needs no database.
"""

from __future__ import annotations

import re

import pytest
from httpx import AsyncClient
from pydantic import ValidationError
from redis.asyncio import Redis
from seskit_core.config import MIN_RECOVERY_KEY_LENGTH, Settings
from seskit_core.security.recovery import (
    ALPHABET,
    CODE_LENGTH,
    code_matches,
    generate_code,
    hash_code,
    key_matches,
    normalise_code,
)
from seskit_core.security.sessions import create_session, read_session
from seskit_core.services import (
    MIN_PASSWORD_LENGTH,
    PasswordTooShort,
    authenticate,
    get_user_by_email,
    issue_recovery_code,
    recover_password,
    regenerate_recovery_code,
    register_user,
)
from sqlalchemy.ext.asyncio import AsyncSession

OLD = "the-old-password-1"
NEW = "a-brand-new-password-2"
EMAIL = "owner@example.com"
KEY = "k" * MIN_RECOVERY_KEY_LENGTH
CODE_RE = re.compile(r"[0-9A-Z]{4}(?:-[0-9A-Z]{4}){3}")


# ------------------------------------------------------------- the code ---


def test_a_code_is_sixteen_unambiguous_characters_in_groups() -> None:
    code = generate_code()

    assert CODE_RE.fullmatch(code)
    assert len(code.replace("-", "")) == CODE_LENGTH
    assert set(code.replace("-", "")) <= set(ALPHABET)


def test_a_code_never_contains_a_character_people_misread() -> None:
    assert not set("ILOU") & set(ALPHABET)


def test_codes_are_not_repeated() -> None:
    assert len({generate_code() for _ in range(500)}) == 500


@pytest.mark.parametrize(
    "retyped",
    [
        lambda c: c.lower(),
        lambda c: c.replace("-", " "),
        lambda c: c.replace("-", ""),
        lambda c: f"  {c}\n",
    ],
    ids=["lowercase", "spaces", "no separators", "padded"],
)
def test_a_code_typed_by_hand_is_read_the_way_it_was_meant(retyped) -> None:  # type: ignore[no-untyped-def]
    """Case, spacing and hyphens are what chat clients and paper change."""
    code = generate_code()

    assert hash_code(retyped(code)) == hash_code(code)


def test_look_alike_letters_resolve_to_the_character_intended() -> None:
    """The alphabet has no O, I, L or U, so one typed is a misreading of the
    digit or letter that is: O is 0, I and L are 1, U is V."""
    assert normalise_code("O0IL1U") == "00111V"


def test_two_different_codes_never_normalise_alike() -> None:
    assert hash_code("0000-0000-0000-0000") != hash_code("0000-0000-0000-0001")


def test_a_code_matches_only_its_own_hash() -> None:
    code = generate_code()

    assert code_matches(code, hash_code(code))
    assert not code_matches(generate_code(), hash_code(code))


def test_no_hash_on_file_is_never_a_match() -> None:
    """An account with no code and one with the wrong code look the same."""
    assert not code_matches(generate_code(), None)
    assert not code_matches("", None)
    assert not code_matches("0" * CODE_LENGTH, None)


# -------------------------------------------------------------- the key ---


def test_the_key_matches_itself_and_only_itself() -> None:
    assert key_matches(KEY, KEY)
    assert key_matches(f"  {KEY}\n", KEY)
    assert not key_matches(KEY + "x", KEY)


def test_nothing_matches_an_unset_key() -> None:
    """An empty form field must not equal an empty setting."""
    assert not key_matches("", "")
    assert not key_matches("anything", "")


def test_a_key_with_non_ascii_text_is_a_miss_not_a_crash() -> None:
    """`hmac.compare_digest` raises on non-ASCII `str`, which would turn a
    hostile form field into a 500."""
    assert not key_matches("ключ-восстановления-" * 3, KEY)


def _settings(**overrides: str) -> Settings:
    """Settings from explicit values only, as `test_config.py` builds them.

    `RECOVERY_KEY` is cleared in the base so an operator's real one in the
    environment running the tests cannot change what these assert.
    """
    base = {
        "SECRET_KEY": "a-real-secret",
        "DATABASE_URL": "postgresql+asyncpg://u:p@localhost:5432/db",
        "REDIS_URL": "redis://localhost:6379/0",
        "RECOVERY_KEY": "",
    }
    return Settings(_env_file=None, **(base | overrides))  # type: ignore[arg-type]


def test_recovery_by_key_is_off_unless_one_is_set() -> None:
    assert _settings().RECOVERY_KEY == ""


def test_a_short_recovery_key_stops_the_instance_booting() -> None:
    """Refused rather than ignored: a key that silently does nothing leaves the
    operator believing they have a way back in."""
    with pytest.raises(ValidationError, match="RECOVERY_KEY"):
        _settings(RECOVERY_KEY="short")


def test_a_long_enough_recovery_key_is_accepted() -> None:
    assert _settings(RECOVERY_KEY=KEY).RECOVERY_KEY == KEY


# ------------------------------------------------------------ the service ---


async def _account(session: AsyncSession, *, with_code: bool = True) -> tuple[str, str | None]:
    user = await register_user(session, email=EMAIL, password=OLD, allow_signup=True)
    code = issue_recovery_code(user) if with_code else None
    await session.flush()
    return user.id, code


async def test_the_code_sets_a_new_password_and_the_old_one_stops_working(
    db_session: AsyncSession,
) -> None:
    _, code = await _account(db_session)
    assert code is not None

    recovered = await recover_password(db_session, email=EMAIL, proof=code, new_password=NEW)

    assert recovered is not None
    assert await authenticate(db_session, email=EMAIL, password=NEW) is not None
    assert await authenticate(db_session, email=EMAIL, password=OLD) is None


async def test_using_a_code_replaces_it_in_the_same_step(db_session: AsyncSession) -> None:
    """Otherwise recovering would leave the account with no way to do it again."""
    _, code = await _account(db_session)
    assert code is not None

    recovered = await recover_password(db_session, email=EMAIL, proof=code, new_password=NEW)

    assert recovered is not None
    assert recovered.recovery_code != code
    assert CODE_RE.fullmatch(recovered.recovery_code)

    again = await recover_password(
        db_session, email=EMAIL, proof=code, new_password="another-new-password-3"
    )
    assert again is None, "a spent code must not work twice"

    fresh = await recover_password(
        db_session,
        email=EMAIL,
        proof=recovered.recovery_code,
        new_password="another-new-password-3",
    )
    assert fresh is not None


async def test_the_code_is_stored_only_as_a_hash(db_session: AsyncSession) -> None:
    _, code = await _account(db_session)
    assert code is not None

    user = await get_user_by_email(db_session, EMAIL)

    assert user is not None
    assert user.recovery_code_hash == hash_code(code)
    assert code not in (user.recovery_code_hash or "")
    assert code not in repr(user)


async def test_a_code_is_accepted_however_it_was_typed(db_session: AsyncSession) -> None:
    _, code = await _account(db_session)
    assert code is not None

    recovered = await recover_password(
        db_session,
        email=f"  {EMAIL.upper()} ",
        proof=code.lower().replace("-", " "),
        new_password=NEW,
    )

    assert recovered is not None


async def test_the_operator_key_recovers_an_account_that_has_no_code(
    db_session: AsyncSession,
) -> None:
    """Every account made before recovery codes existed is in this position."""
    await _account(db_session, with_code=False)

    recovered = await recover_password(
        db_session, email=EMAIL, proof=KEY, new_password=NEW, recovery_key=KEY
    )

    assert recovered is not None
    assert await authenticate(db_session, email=EMAIL, password=NEW) is not None
    assert CODE_RE.fullmatch(recovered.recovery_code), "and leaves the account with a code"


async def test_the_operator_key_is_not_spent_by_use(db_session: AsyncSession) -> None:
    await _account(db_session, with_code=False)

    first = await recover_password(
        db_session, email=EMAIL, proof=KEY, new_password=NEW, recovery_key=KEY
    )
    second = await recover_password(
        db_session, email=EMAIL, proof=KEY, new_password="yet-another-password-4", recovery_key=KEY
    )

    assert first is not None and second is not None


async def test_the_key_means_nothing_when_none_is_configured(db_session: AsyncSession) -> None:
    await _account(db_session, with_code=False)

    assert (
        await recover_password(
            db_session, email=EMAIL, proof=KEY, new_password=NEW, recovery_key=""
        )
        is None
    )
    assert (
        await recover_password(db_session, email=EMAIL, proof="", new_password=NEW, recovery_key="")
        is None
    )


@pytest.mark.parametrize("kind", ["wrong code", "unknown account", "deactivated"])
async def test_every_way_of_being_wrong_gets_the_same_answer(
    db_session: AsyncSession, kind: str
) -> None:
    """None for all of them, so the form cannot be used to learn which
    addresses have accounts."""
    user_id, code = await _account(db_session)
    assert code is not None
    email, proof = EMAIL, code

    if kind == "wrong code":
        proof = generate_code()
    elif kind == "unknown account":
        email = "nobody@example.com"
    else:
        user = await get_user_by_email(db_session, EMAIL)
        assert user is not None and user.id == user_id
        user.is_active = False
        await db_session.flush()

    assert await recover_password(db_session, email=email, proof=proof, new_password=NEW) is None
    assert await authenticate(db_session, email=EMAIL, password=NEW) is None


async def test_a_short_password_changes_nothing_and_does_not_spend_the_code(
    db_session: AsyncSession,
) -> None:
    _, code = await _account(db_session)
    assert code is not None

    with pytest.raises(PasswordTooShort):
        await recover_password(
            db_session, email=EMAIL, proof=code, new_password="x" * (MIN_PASSWORD_LENGTH - 1)
        )

    assert await authenticate(db_session, email=EMAIL, password=OLD) is not None
    assert await recover_password(db_session, email=EMAIL, proof=code, new_password=NEW)


async def test_regenerating_needs_the_current_password(db_session: AsyncSession) -> None:
    """A session is also what a stolen cookie looks like."""
    _, code = await _account(db_session)
    user = await get_user_by_email(db_session, EMAIL)
    assert user is not None and code is not None

    assert await regenerate_recovery_code(db_session, user, password="not-the-password") is None
    assert user.recovery_code_hash == hash_code(code)

    new = await regenerate_recovery_code(db_session, user, password=OLD)

    assert new is not None and new != code
    assert user.recovery_code_hash == hash_code(new)
    assert not code_matches(code, user.recovery_code_hash), "the old code is dead"


# --------------------------------------------------------------- the pages ---

COOKIE = "seskit_session"


def _csrf(html: str) -> str:
    marker = 'name="csrf_token" value="'
    start = html.index(marker) + len(marker)
    return html[start : html.index('"', start)]


def _code_on_page(html: str) -> str:
    match = re.search(r"keyreveal__key\">([^<]+)<", html)
    assert match, "no recovery code on the page"
    return match.group(1)


def _error_on_page(html: str) -> str:
    match = re.search(r'role="alert">([^<]+)<', html)
    assert match, "no error on the page"
    return match.group(1)


async def _sign_up(client: AsyncClient) -> str:
    """Create the owner through the app and return the code the page showed."""
    response = await client.post("/signup", data={"email": EMAIL, "password": OLD})
    assert response.status_code == 200, response.text
    return _code_on_page(response.text)


async def test_signing_up_shows_a_recovery_code_once(app_client: AsyncClient) -> None:
    response = await app_client.post("/signup", data={"email": EMAIL, "password": OLD})

    assert response.status_code == 200
    assert "Save your recovery code" in response.text
    assert CODE_RE.fullmatch(_code_on_page(response.text))
    assert COOKIE in response.cookies, "and the same response starts the session"
    assert "no-store" in response.headers["cache-control"]


async def test_the_code_is_not_on_the_dashboard_afterwards(app_client: AsyncClient) -> None:
    code = await _sign_up(app_client)

    dashboard = await app_client.get("/")
    settings_page = await app_client.get("/settings")

    assert code not in dashboard.text
    assert code not in settings_page.text


async def test_the_login_page_offers_the_way_back(app_client: AsyncClient) -> None:
    assert 'href="/forgot-password"' in (await app_client.get("/login")).text
    assert (await app_client.get("/forgot-password")).status_code == 200


async def test_the_code_recovers_the_account_through_the_page(
    app_client: AsyncClient, redis_client: Redis
) -> None:
    code = await _sign_up(app_client)
    old_cookie = app_client.cookies[COOKIE]
    app_client.cookies.clear()

    response = await app_client.post(
        "/forgot-password", data={"email": EMAIL, "recovery_code": code, "password": NEW}
    )

    assert response.status_code == 200, response.text
    assert "Password changed" in response.text
    assert _code_on_page(response.text) != code, "the spent code is replaced"
    assert await read_session(redis_client, old_cookie, 3600) is None, "old sessions are ended"
    assert (
        await app_client.post(
            "/login", data={"email": EMAIL, "password": NEW}, follow_redirects=False
        )
    ).status_code == 303


async def test_a_wrong_code_and_an_unknown_email_look_identical(app_client: AsyncClient) -> None:
    await _sign_up(app_client)
    app_client.cookies.clear()

    wrong_code = await app_client.post(
        "/forgot-password",
        data={"email": EMAIL, "recovery_code": generate_code(), "password": NEW},
    )
    unknown = await app_client.post(
        "/forgot-password",
        data={"email": "nobody@example.com", "recovery_code": generate_code(), "password": NEW},
    )

    assert wrong_code.status_code == unknown.status_code == 400
    assert _error_on_page(wrong_code.text) == _error_on_page(unknown.text)


async def test_a_short_new_password_is_explained_and_costs_no_attempt(
    app_client: AsyncClient, settings: Settings
) -> None:
    code = await _sign_up(app_client)
    app_client.cookies.clear()

    for _ in range(settings.LOGIN_MAX_ATTEMPTS + 2):
        response = await app_client.post(
            "/forgot-password", data={"email": EMAIL, "recovery_code": code, "password": "short"}
        )
        assert response.status_code == 400
        assert "at least" in response.text

    ok = await app_client.post(
        "/forgot-password", data={"email": EMAIL, "recovery_code": code, "password": NEW}
    )
    assert ok.status_code == 200


async def test_guessing_stops_however_the_caller_claims_to_be_addressed(
    app_client: AsyncClient, settings: Settings
) -> None:
    """The limit is per email, not per address, because the address is whatever
    `X-Forwarded-For` says. An attacker who varies it must not get fresh guesses."""
    await _sign_up(app_client)
    app_client.cookies.clear()

    statuses = []
    for attempt in range(settings.LOGIN_MAX_ATTEMPTS + 3):
        response = await app_client.post(
            "/forgot-password",
            data={"email": EMAIL, "recovery_code": generate_code(), "password": NEW},
            headers={"X-Forwarded-For": f"203.0.113.{attempt}"},
        )
        statuses.append(response.status_code)

    assert statuses[: settings.LOGIN_MAX_ATTEMPTS] == [400] * settings.LOGIN_MAX_ATTEMPTS
    assert set(statuses[settings.LOGIN_MAX_ATTEMPTS :]) == {429}


@pytest.fixture
async def keyed_client(
    monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest
) -> AsyncClient:
    """An app built after `RECOVERY_KEY` is in the environment.

    The `settings` fixture reads the environment when it first runs, so the
    variable has to be set before `app_client` is requested, not after.
    """
    monkeypatch.setenv("RECOVERY_KEY", KEY)
    client: AsyncClient = request.getfixturevalue("app_client")
    return client


async def test_the_key_signs_an_owner_with_no_code_back_in(
    keyed_client: AsyncClient, db_session: AsyncSession
) -> None:
    await _account(db_session, with_code=False)

    response = await keyed_client.post(
        "/forgot-password", data={"email": EMAIL, "recovery_code": KEY, "password": NEW}
    )

    assert response.status_code == 200, response.text
    assert "Password changed" in response.text
    assert CODE_RE.fullmatch(_code_on_page(response.text))


async def test_a_wrong_key_is_refused(keyed_client: AsyncClient, db_session: AsyncSession) -> None:
    await _account(db_session, with_code=False)

    response = await keyed_client.post(
        "/forgot-password", data={"email": EMAIL, "recovery_code": KEY + "x", "password": NEW}
    )

    assert response.status_code == 400


# -------------------------------------------------------------- settings ---


async def test_settings_needs_a_session(app_client: AsyncClient) -> None:
    response = await app_client.get("/settings", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/login"


async def test_settings_says_whether_the_account_has_a_code(app_client: AsyncClient) -> None:
    await _sign_up(app_client)

    page = await app_client.get("/settings")

    assert page.status_code == 200
    assert "You have a recovery code" in page.text


async def test_a_new_code_needs_the_password_and_replaces_the_old_one(
    app_client: AsyncClient,
) -> None:
    code = await _sign_up(app_client)
    csrf = _csrf((await app_client.get("/settings")).text)

    refused = await app_client.post(
        "/settings/recovery-code", data={"csrf_token": csrf, "password": "not-my-password"}
    )
    assert refused.status_code == 400
    assert "not your current password" in refused.text

    issued = await app_client.post(
        "/settings/recovery-code", data={"csrf_token": csrf, "password": OLD}
    )
    assert issued.status_code == 200
    assert "no-store" in issued.headers["cache-control"]
    new_code = _code_on_page(issued.text)
    assert new_code != code

    app_client.cookies.clear()
    old_use = await app_client.post(
        "/forgot-password", data={"email": EMAIL, "recovery_code": code, "password": NEW}
    )
    assert old_use.status_code == 400, "the replaced code is dead"


async def test_a_new_code_cannot_be_made_without_a_csrf_token(app_client: AsyncClient) -> None:
    await _sign_up(app_client)

    response = await app_client.post("/settings/recovery-code", data={"password": OLD})

    assert response.status_code == 403


async def test_another_accounts_session_is_untouched_by_a_recovery(
    app_client: AsyncClient, db_session: AsyncSession, redis_client: Redis
) -> None:
    _, code = await _account(db_session)
    other = await register_user(
        db_session, email="other@example.com", password=OLD, allow_signup=True
    )
    token, _ = await create_session(redis_client, other.id, 3600)

    await app_client.post(
        "/forgot-password", data={"email": EMAIL, "recovery_code": code, "password": NEW}
    )

    assert await read_session(redis_client, token, 3600) is not None
