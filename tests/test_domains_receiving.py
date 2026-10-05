"""Turning receiving on and off from the Domains page (inbound email, Phase D).

What is checked is what a signed-in person can do, what they are told, and - the
reason most of these exist - what is *not* allowed to happen however the request
arrives. The disabled button is a courtesy; the server refuses for the same
reasons, because a form can be posted by hand.

The most important test here is a regression guard from earlier in the series: an
AWS refusal must not leave the page saying "Receiving is on". The routes do not
roll back on an error, so that depends on the service giving the domain back, and
the only way to see it is in the HTML the user is shown.
"""

from __future__ import annotations

import pytest
from fakes.inbound import FakeInboundProvisioner
from httpx import AsyncClient
from seskit_core.errors import APIError, ErrorType
from seskit_core.models import AWSConnection, Identity, Project
from seskit_core.services import create_project, register_user
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from test_domains_page import _connect

DOMAIN = "example.com"
MX_VALUE = "inbound-smtp.us-east-1.amazonaws.com"


@pytest.fixture(autouse=True)
def _reset() -> None:
    FakeInboundProvisioner.reset()


async def _project(session: AsyncSession) -> Project:
    project = await session.scalar(select(Project))
    assert project is not None
    return project


async def _domain(
    session: AsyncSession,
    project: Project,
    value: str = DOMAIN,
    *,
    verified: bool = True,
    kind: str = "domain",
    rule: str | None = None,
    region: str = "us-east-1",
) -> Identity:
    identity = Identity(
        project_id=project.id,
        identity_type=kind,
        value=value,
        region=region,
        verification_status="success" if verified else "pending",
        dkim_status="success" if verified else "pending",
        dkim_tokens=[],
        inbound_rule_name=rule,
        inbound_rule_set="seskit-inbound" if rule else None,
    )
    session.add(identity)
    await session.flush()
    return identity


async def _fresh(session: AsyncSession, identity: Identity) -> Identity:
    await session.refresh(identity)
    return identity


def _start(identity: Identity) -> str:
    return f"/domains/{identity.id}/receiving"


def _stop(identity: Identity) -> str:
    return f"/domains/{identity.id}/receiving/stop"


# ------------------------------------------------------------------- page ---


async def test_a_verified_domain_offers_to_start_receiving(
    app_client: AsyncClient, db_session: AsyncSession
) -> None:
    await _connect(app_client)
    await _domain(db_session, await _project(db_session))

    page = await app_client.get("/domains")

    assert "Start receiving" in page.text
    assert "Receiving</dt>" in page.text
    assert "Receiving is on" not in page.text
    # It says what it will create in the user's own account before it is pressed.
    assert "an S3 bucket" in page.text
    assert "never replaces a rule set you already have" in page.text


async def test_the_controls_are_forms_with_csrf_not_links(
    app_client: AsyncClient, db_session: AsyncSession
) -> None:
    """A link would let any page on the internet start creating resources in
    somebody's AWS account with an <img> tag.
    """
    await _connect(app_client)
    identity = await _domain(db_session, await _project(db_session))

    page = await app_client.get("/domains")

    assert f'<form method="post" action="/domains/{identity.id}/receiving">' in page.text
    assert f'href="/domains/{identity.id}/receiving' not in page.text


async def test_an_unverified_domain_has_a_disabled_button_and_a_reason(
    app_client: AsyncClient, db_session: AsyncSession
) -> None:
    await _connect(app_client)
    await _domain(db_session, await _project(db_session), verified=False)

    page = await app_client.get("/domains")

    assert "Verify this domain first" in page.text
    button = page.text[
        page.text.index("Start receiving") - 120 : page.text.index("Start receiving")
    ]
    assert "disabled" in button


async def test_an_email_address_has_no_receiving_row(
    app_client: AsyncClient, db_session: AsyncSession
) -> None:
    await _connect(app_client)
    await _domain(db_session, await _project(db_session), "me@example.com", kind="email_address")

    page = await app_client.get("/domains")

    assert "Receiving</dt>" not in page.text
    assert "Start receiving" not in page.text


# ----------------------------------------------------------------- starting ---


async def test_starting_shows_the_mx_record_to_add(
    app_client: AsyncClient, db_session: AsyncSession
) -> None:
    token = await _connect(app_client)
    identity = await _domain(db_session, await _project(db_session))

    page = await app_client.post(_start(identity), data={"csrf_token": token})

    assert page.status_code == 200
    assert "Receiving is on" in page.text
    assert MX_VALUE in page.text
    assert f'data-copy="{MX_VALUE}"' in page.text
    assert "10" in page.text
    # And the warning that matters most about an MX record.
    assert "replaces wherever the domain receives mail now" in page.text
    assert f"mail.{DOMAIN}" in page.text
    assert FakeInboundProvisioner.calls == ["provision", "add_rule"]
    assert (await _fresh(db_session, identity)).receives_mail is True


async def test_starting_names_what_was_created(
    app_client: AsyncClient, db_session: AsyncSession
) -> None:
    """Because it is in the user's own account and now on their bill."""
    token = await _connect(app_client)
    identity = await _domain(db_session, await _project(db_session))

    page = await app_client.post(_start(identity), data={"csrf_token": token})

    assert "a bucket, a topic, a queue" in page.text
    assert "one receipt rule" in page.text


async def test_an_aws_refusal_does_not_leave_the_page_saying_receiving_is_on(
    app_client: AsyncClient, db_session: AsyncSession
) -> None:
    """The regression guard. The claim on the domain is written before AWS is
    called, and these routes do not roll back on an error - so without the service
    giving the domain back, this page would render "Receiving is on" beside the
    reason it is not. Checked in the HTML the user is shown, which is the only place
    it can be seen.
    """
    token = await _connect(app_client)
    identity = await _domain(db_session, await _project(db_session))
    FakeInboundProvisioner.error = APIError(
        ErrorType.AUTHORIZATION_FAILED,
        "The AWS identity is not permitted to call s3:CreateBucket. Add it to its IAM policy.",
    )

    page = await app_client.post(_start(identity), data={"csrf_token": token})

    assert page.status_code == 403
    assert "not permitted to call s3:CreateBucket" in page.text
    assert "Receiving is on" not in page.text
    assert "Start receiving" in page.text
    assert (await _fresh(db_session, identity)).receives_mail is False


async def test_a_post_without_a_csrf_token_creates_nothing(
    app_client: AsyncClient, db_session: AsyncSession
) -> None:
    await _connect(app_client)
    identity = await _domain(db_session, await _project(db_session))

    response = await app_client.post(_start(identity), data={})

    assert response.status_code == 403
    assert FakeInboundProvisioner.calls == []
    assert (await _fresh(db_session, identity)).receives_mail is False


async def test_the_server_refuses_what_the_disabled_button_refuses(
    app_client: AsyncClient, db_session: AsyncSession
) -> None:
    """A form can be posted by hand. The button being disabled protects nobody."""
    token = await _connect(app_client)
    identity = await _domain(db_session, await _project(db_session), verified=False)

    page = await app_client.post(_start(identity), data={"csrf_token": token})

    assert page.status_code == 400
    assert "Verify this domain first" in page.text
    assert FakeInboundProvisioner.calls == []
    assert (await _fresh(db_session, identity)).receives_mail is False


async def test_a_region_that_cannot_receive_is_refused_on_the_server(
    app_client: AsyncClient, db_session: AsyncSession
) -> None:
    token = await _connect(app_client)
    project = await _project(db_session)
    connection = await db_session.scalar(select(AWSConnection))
    assert connection is not None
    connection.region = "ap-south-2"
    identity = await _domain(db_session, project, region="ap-south-2")
    await db_session.flush()

    page = await app_client.post(_start(identity), data={"csrf_token": token})

    assert page.status_code == 400
    assert "cannot receive mail in ap-south-2" in page.text
    assert FakeInboundProvisioner.calls == []


async def test_an_email_address_cannot_be_made_to_receive(
    app_client: AsyncClient, db_session: AsyncSession
) -> None:
    token = await _connect(app_client)
    identity = await _domain(
        db_session, await _project(db_session), "me@example.com", kind="email_address"
    )

    page = await app_client.post(_start(identity), data={"csrf_token": token})

    assert page.status_code == 400
    assert "Only a domain can receive mail" in page.text
    assert FakeInboundProvisioner.calls == []


async def test_a_domain_already_receiving_for_another_project_is_refused(
    app_client: AsyncClient, db_session: AsyncSession
) -> None:
    token = await _connect(app_client)
    mine = await _domain(db_session, await _project(db_session))
    other_user = await register_user(
        db_session, email="other@example.com", password="correct-horse-battery", allow_signup=True
    )
    other = await create_project(db_session, user_id=other_user.id, name="Other")
    await _domain(db_session, other, rule="seskit-theirs")

    page = await app_client.post(_start(mine), data={"csrf_token": token})

    assert page.status_code == 400
    assert "already receives mail for another project or region" in page.text
    assert "other@example.com" not in page.text
    assert "Other" not in page.text.replace("Otherwise", "")
    assert FakeInboundProvisioner.calls == []
    assert (await _fresh(db_session, mine)).receives_mail is False


async def test_somebody_elses_domain_cannot_be_started(
    app_client: AsyncClient, db_session: AsyncSession
) -> None:
    """An id from another project resolves to nothing, not to somebody's domain."""
    token = await _connect(app_client)
    await _domain(db_session, await _project(db_session), "mine.example")
    other_user = await register_user(
        db_session, email="other@example.com", password="correct-horse-battery", allow_signup=True
    )
    other = await create_project(db_session, user_id=other_user.id, name="Other")
    theirs = await _domain(db_session, other, "theirs.example")

    page = await app_client.post(_start(theirs), data={"csrf_token": token})

    assert page.status_code == 200
    assert FakeInboundProvisioner.calls == []
    assert (await _fresh(db_session, theirs)).receives_mail is False
    assert "theirs.example" not in page.text


# ----------------------------------------------------------------- stopping ---


async def test_stopping_removes_the_rule_and_says_what_is_kept(
    app_client: AsyncClient, db_session: AsyncSession
) -> None:
    token = await _connect(app_client)
    identity = await _domain(db_session, await _project(db_session))
    await app_client.post(_start(identity), data={"csrf_token": token})
    FakeInboundProvisioner.calls = []

    page = await app_client.post(_stop(identity), data={"csrf_token": token})

    assert page.status_code == 200
    assert FakeInboundProvisioner.calls == ["remove_rule", "remove"]
    assert "Mail already received is kept" in page.text
    assert "remove the MX record" in page.text
    assert "Receiving is on" not in page.text
    assert "Start receiving" in page.text
    assert (await _fresh(db_session, identity)).receives_mail is False


async def test_a_refused_stop_leaves_it_on_and_says_why(
    app_client: AsyncClient, db_session: AsyncSession
) -> None:
    """AWS first, the row second. If the rule cannot be removed it is still there,
    and a page that said it had stopped would be wrong about somebody's account.
    """
    token = await _connect(app_client)
    identity = await _domain(db_session, await _project(db_session))
    await app_client.post(_start(identity), data={"csrf_token": token})
    FakeInboundProvisioner.remove_error = APIError(
        ErrorType.AUTHORIZATION_FAILED, "not permitted to call ses:DeleteReceiptRule"
    )

    page = await app_client.post(_stop(identity), data={"csrf_token": token})

    assert page.status_code == 403
    assert "not permitted to call ses:DeleteReceiptRule" in page.text
    assert "Receiving is on" in page.text
    assert (await _fresh(db_session, identity)).receives_mail is True


async def test_stopping_somebody_elses_domain_does_nothing(
    app_client: AsyncClient, db_session: AsyncSession
) -> None:
    token = await _connect(app_client)
    await _domain(db_session, await _project(db_session), "mine.example")
    other_user = await register_user(
        db_session, email="other@example.com", password="correct-horse-battery", allow_signup=True
    )
    other = await create_project(db_session, user_id=other_user.id, name="Other")
    theirs = await _domain(db_session, other, "theirs.example", rule="seskit-theirs")

    await app_client.post(_stop(theirs), data={"csrf_token": token})

    assert FakeInboundProvisioner.calls == []
    assert (await _fresh(db_session, theirs)).receives_mail is True


# --------------------------------------------- what else removes a receiver ---


async def test_removing_a_receiving_domain_removes_its_rule_first(
    app_client: AsyncClient, db_session: AsyncSession
) -> None:
    """Through the real route, not just the service: a user pressing Remove on a
    domain that receives mail must not strand a rule in their AWS account.
    """
    token = await _connect(app_client)
    identity = await _domain(db_session, await _project(db_session))
    await app_client.post(_start(identity), data={"csrf_token": token})
    FakeInboundProvisioner.calls = []

    page = await app_client.post(f"/domains/{identity.id}/delete", data={"csrf_token": token})

    assert page.status_code == 200
    assert "remove_rule" in FakeInboundProvisioner.calls
    assert await db_session.scalar(select(Identity).where(Identity.id == identity.id)) is None


async def test_disconnecting_aws_removes_the_receipt_rules_first(
    app_client: AsyncClient, db_session: AsyncSession
) -> None:
    """Once the connection is gone there is no key left to remove them with."""
    token = await _connect(app_client)
    identity = await _domain(db_session, await _project(db_session))
    await app_client.post(_start(identity), data={"csrf_token": token})
    FakeInboundProvisioner.calls = []

    page = await app_client.post("/aws/disconnect", data={"csrf_token": token})

    assert page.status_code == 200
    assert "remove_rule" in FakeInboundProvisioner.calls
    assert (await _fresh(db_session, identity)).receives_mail is False
