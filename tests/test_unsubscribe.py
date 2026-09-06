"""One-click unsubscribe (RFC 8058, §31 Phase 11).

Three things have to hold, and they pull against each other.

*It must work with no account.* The recipient is not a SESKit user. Authority
comes from the link, so the link has to be unforgeable.

*It must not work for anyone else.* An unsigned link would let anybody
unsubscribe anybody by editing a URL - and since a suppression stops mail for
good, that is a way to silently cut somebody off from their own password
resets.

*It must not become an oracle.* Every answer is a 200, so nobody can tell an
address, a message or a project that exists from one that does not.
"""

from __future__ import annotations

import base64
from typing import Any, cast

import pytest
from httpx import AsyncClient
from seskit_core.config import Settings
from seskit_core.email import build_message
from seskit_core.errors import APIError
from seskit_core.models import Email, EmailEvent, EmailStatus, EventType
from seskit_core.providers.types import OutboundEmail
from seskit_core.security.unsubscribe import (
    ONE_CLICK,
    read_token,
    token_matches,
    unsubscribe_token,
)
from seskit_core.services import (
    create_project,
    find_suppression,
    register_user,
    unsubscribe_link,
)
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

SECRET = "an-instance-secret"
PROJECT = "proj_abc"
EMAIL_ID = "email_123"
ADDRESS = "reader@example.com"
PASSWORD = "correct-horse-battery"


def _token(*, secret: str = SECRET, project_id: str = PROJECT, address: str = ADDRESS) -> str:
    return unsubscribe_token(secret, project_id=project_id, email_id=EMAIL_ID, address=address)


# ----------------------------------------------------------------- tokens ---


def test_a_token_says_who_it_is_for() -> None:
    assert read_token(_token()) == (EMAIL_ID, ADDRESS)


def test_a_token_this_instance_issued_verifies() -> None:
    assert token_matches(SECRET, project_id=PROJECT, token=_token())


def test_a_token_for_another_project_does_not_verify() -> None:
    """The key is derived per project, so a link from one project is inert
    against another even though both were signed by the same instance.
    """
    assert not token_matches(SECRET, project_id="proj_other", token=_token())


def test_a_token_from_another_instance_does_not_verify() -> None:
    assert not token_matches(SECRET, project_id=PROJECT, token=_token(secret="a-different-one"))


def test_an_edited_address_does_not_verify() -> None:
    """The attack the signature exists to stop: change whose address is in the
    payload and unsubscribe somebody else.
    """
    _, signature = _token().split(".")
    forged = base64.urlsafe_b64encode(f"{EMAIL_ID}:victim@example.com".encode()).rstrip(b"=")

    assert not token_matches(SECRET, project_id=PROJECT, token=f"{forged.decode()}.{signature}")


@pytest.mark.parametrize(
    "token",
    ["", ".", "nosignature", "!!!!.abc", "YWJj", "AAAAAAAA.deadbeef"],
    ids=["empty", "separator", "no-separator", "not-base64", "no-signature", "no-colon"],
)
def test_nonsense_reads_as_nothing(token: str) -> None:
    assert read_token(token) is None


def test_an_address_with_a_plus_survives_the_round_trip() -> None:
    """base64url, not base64: ``+`` and ``/`` in the alphabet would be mangled
    by the URL they travel in, and a mangled token is one that stops verifying
    for reasons nobody can see.
    """
    tagged = "reader+newsletter@example.com"

    assert read_token(_token(address=tagged)) == (EMAIL_ID, tagged)


# ----------------------------------------------------------------- header ---


def _outbound(**kwargs: object) -> OutboundEmail:
    return OutboundEmail(
        sender="Acme <hello@example.com>",
        to=[ADDRESS],
        subject="Welcome",
        text="Hello",
        **kwargs,  # type: ignore[arg-type]
    )


def test_the_message_carries_both_headers() -> None:
    """Both or neither. Without ``List-Unsubscribe-Post`` a client may open the
    URL and wait for a confirmation, which is not the one click Gmail and
    Outlook now ask bulk senders for.
    """
    message = build_message(_outbound(unsubscribe_url="https://mail.example.com/u/tok"))

    assert message["List-Unsubscribe"] == "<https://mail.example.com/u/tok>"
    assert message["List-Unsubscribe-Post"] == ONE_CLICK


def test_the_url_is_in_angle_brackets() -> None:
    """RFC 2369 syntax, not decoration - some clients ignore a bare URL."""
    message = build_message(_outbound(unsubscribe_url="https://mail.example.com/u/tok"))

    assert message["List-Unsubscribe"].startswith("<")
    assert message["List-Unsubscribe"].endswith(">")


def test_no_url_means_no_header_at_all() -> None:
    """Better than a header pointing at localhost: a button that cannot work
    is answered with Report spam, which is the outcome this feature exists to
    avoid.
    """
    message = build_message(_outbound())

    assert message["List-Unsubscribe"] is None
    assert message["List-Unsubscribe-Post"] is None


def test_a_caller_cannot_supply_their_own_unsubscribe_header() -> None:
    """It is a promise about what happens when the button is pressed, and only
    SESKit can keep it - its list is the one that decides whether the next
    message is sent.
    """
    with pytest.raises(APIError) as raised:
        build_message(_outbound(headers={"List-Unsubscribe": "<https://elsewhere.example/x>"}))

    assert "cannot be overridden" in raised.value.message


# ------------------------------------------------------------------- link ---


def _email(**kwargs: object) -> Email:
    fields: dict[str, object] = {
        "id": EMAIL_ID,
        "project_id": PROJECT,
        "from_address": "hello@example.com",
        "to_addresses": [ADDRESS],
        "cc_addresses": [],
        "bcc_addresses": [],
        "reply_to": [],
        "subject": "Welcome",
        "text_body": "Hello",
        "status": EmailStatus.QUEUED.value,
    }
    fields.update(kwargs)
    return Email(**fields)


def test_a_single_recipient_gets_a_link() -> None:
    link = unsubscribe_link(_email(), secret=SECRET, base_url="https://mail.example.com/u")

    assert link is not None
    assert link.startswith("https://mail.example.com/u/")
    assert token_matches(SECRET, project_id=PROJECT, token=link.rsplit("/", 1)[1])


def test_no_public_url_means_no_link() -> None:
    assert unsubscribe_link(_email(), secret=SECRET, base_url=None) is None


def test_two_recipients_get_no_link() -> None:
    """One header, one token, one address. With two recipients the link would
    unsubscribe whichever of them the token happened to name, on behalf of
    whichever of them pressed it.
    """
    email = _email(to_addresses=[ADDRESS, "other@example.com"])

    assert unsubscribe_link(email, secret=SECRET, base_url="https://mail.example.com/u") is None


def test_a_blind_copy_counts_as_a_second_person() -> None:
    """The case that would be easy to miss: bcc is not in the message, but the
    person is still reading it, and the header would let them unsubscribe the
    recipient in the To line.
    """
    email = _email(bcc_addresses=["archive@example.com"])

    assert unsubscribe_link(email, secret=SECRET, base_url="https://mail.example.com/u") is None


def test_the_address_in_the_link_is_normalised() -> None:
    """It has to reduce the way the suppression list reduces, or the link
    suppresses a spelling the send path never looks up.
    """
    email = _email(to_addresses=["Bob <BOB@Example.COM>"])
    link = unsubscribe_link(email, secret=SECRET, base_url="https://mail.example.com/u")

    assert link is not None
    parsed = read_token(link.rsplit("/", 1)[1])
    assert parsed is not None
    assert parsed[1] == "bob@example.com"


# ----------------------------------------------------------------- config ---


def test_no_public_base_url_means_no_unsubscribe_url(settings: Settings) -> None:
    assert settings.model_copy(update={"PUBLIC_BASE_URL": None}).unsubscribe_base_url is None


def test_the_unsubscribe_url_does_not_double_its_slash(settings: Settings) -> None:
    updated = settings.model_copy(update={"PUBLIC_BASE_URL": "https://mail.example.com/"})

    assert updated.unsubscribe_base_url == "https://mail.example.com/u"


# ----------------------------------------------------------------- routes ---


async def _stored(session: AsyncSession, *, owner: str = "owner@example.com") -> Email:
    user = await register_user(session, email=owner, password=PASSWORD, allow_signup=True)
    project = await create_project(session, user_id=user.id, name="Sending")
    row = Email(
        project_id=project.id,
        from_address="hello@example.com",
        to_addresses=[ADDRESS],
        cc_addresses=[],
        bcc_addresses=[],
        reply_to=[],
        subject="Welcome",
        text_body="Hello",
        status=EmailStatus.SENT.value,
    )
    session.add(row)
    await session.flush()
    return row


def _live_token(email: Email, settings: Settings, *, address: str = ADDRESS) -> str:
    return unsubscribe_token(
        settings.SECRET_KEY, project_id=email.project_id, email_id=email.id, address=address
    )


async def test_the_page_asks_before_doing_anything(
    app_client: AsyncClient, db_session: AsyncSession, settings: Settings
) -> None:
    email = await _stored(db_session)

    page = await app_client.get(f"/u/{_live_token(email, settings)}")

    assert page.status_code == 200
    assert ADDRESS in page.text
    assert "Unsubscribe" in page.text


async def test_opening_the_link_does_not_unsubscribe(
    app_client: AsyncClient, db_session: AsyncSession, settings: Settings
) -> None:
    """The test that matters most here. Mail clients and scanners fetch links
    in messages without being asked, so a GET that acted would unsubscribe
    people who never pressed anything.
    """
    email = await _stored(db_session)

    await app_client.get(f"/u/{_live_token(email, settings)}")

    assert await find_suppression(db_session, project_id=email.project_id, address=ADDRESS) is None


async def test_posting_unsubscribes(
    app_client: AsyncClient, db_session: AsyncSession, settings: Settings
) -> None:
    email = await _stored(db_session)

    response = await app_client.post(f"/u/{_live_token(email, settings)}")

    assert response.status_code == 200
    found = await find_suppression(db_session, project_id=email.project_id, address=ADDRESS)
    assert found is not None
    assert found.reason == "unsubscribe"


async def test_posting_needs_no_csrf_token(
    app_client: AsyncClient, db_session: AsyncSession, settings: Settings
) -> None:
    """Not an oversight. The sender of the one-click POST is a mail provider
    with no session here, so the signed token in the URL has to be the whole
    authorisation - and forging one means forging an HMAC.
    """
    email = await _stored(db_session)

    response = await app_client.post(f"/u/{_live_token(email, settings)}")

    assert response.status_code != 403


async def test_unsubscribing_twice_changes_nothing_the_second_time(
    app_client: AsyncClient, db_session: AsyncSession, settings: Settings
) -> None:
    """A provider retrying its POST, or somebody pressing the button again on
    a page they left open. Neither should produce a second event for an
    application to deduplicate.
    """
    email = await _stored(db_session)
    token = _live_token(email, settings)

    await app_client.post(f"/u/{token}")
    second = await app_client.post(f"/u/{token}")

    assert second.status_code == 200
    events = await db_session.scalars(
        select(EmailEvent).where(
            EmailEvent.email_id == email.id,
            EmailEvent.event_type == EventType.SUPPRESSED.value,
        )
    )
    assert len(list(events)) == 1


async def test_an_unsubscribe_is_reported_like_any_other_suppression(
    app_client: AsyncClient, db_session: AsyncSession, settings: Settings
) -> None:
    """An application reconciling its own mailing list against SESKit's has to
    hear about every way an address leaves, not only the ones a provider told
    us about.
    """
    email = await _stored(db_session)

    await app_client.post(f"/u/{_live_token(email, settings)}")

    event = await db_session.scalar(
        select(EmailEvent).where(
            EmailEvent.email_id == email.id,
            EmailEvent.event_type == EventType.SUPPRESSED.value,
        )
    )
    assert event is not None
    # ``payload`` is JSON to SQLAlchemy, so its type is ``object`` until
    # something says otherwise.
    data = cast(dict[str, Any], cast(dict[str, Any], event.payload)["data"])
    assert data["to"] == [ADDRESS]
    assert data["reason"] == "unsubscribe"
    # Null rather than absent: the recipient told SESKit directly, and a
    # consumer should be able to read the key without checking for it.
    assert data["caused_by"] is None


async def test_a_token_naming_someone_who_never_received_the_message_is_refused(
    app_client: AsyncClient, db_session: AsyncSession, settings: Settings
) -> None:
    """A correct signature over the wrong address still gets nowhere.

    Nothing issues such a token today - only the send path signs one, and only
    for the single address the message went to. The check is here so that if
    something one day signs an address from somewhere else, the link cannot
    quietly suppress a stranger.
    """
    email = await _stored(db_session)
    signed_for_someone_else = _live_token(email, settings, address="victim@example.com")

    response = await app_client.post(f"/u/{signed_for_someone_else}")

    assert response.status_code == 200
    assert "not valid" in response.text
    assert (
        await find_suppression(
            db_session, project_id=email.project_id, address="victim@example.com"
        )
        is None
    )


async def test_a_token_for_no_message_answers_the_same_way(
    app_client: AsyncClient, settings: Settings
) -> None:
    """200 and the same sentence as a bad signature. Telling "no such message"
    apart from "bad signature" is exactly the distinction that would turn this
    into a way to probe what exists.
    """
    token = unsubscribe_token(
        settings.SECRET_KEY, project_id=PROJECT, email_id="email_nope", address=ADDRESS
    )

    response = await app_client.get(f"/u/{token}")

    assert response.status_code == 200
    assert "not valid" in response.text


async def test_the_page_says_so_when_it_is_already_done(
    app_client: AsyncClient, db_session: AsyncSession, settings: Settings
) -> None:
    """Somebody following the link a second time should be told they are off
    the list, not shown a button that implies they are not.
    """
    email = await _stored(db_session)
    token = _live_token(email, settings)
    await app_client.post(f"/u/{token}")

    page = await app_client.get(f"/u/{token}")

    assert "Unsubscribed" in page.text
