"""The return path row and its records, rendered from the real template.

Needs no database: the partial is rendered with a plain ``Identity`` and the real
component layer, the way the rest of the page tests that read markup do.
"""

from __future__ import annotations

from seskit_api.templating import templates
from seskit_core.models import Identity
from seskit_core.providers import IdentityType, VerificationStatus


def _render(**overrides: object) -> str:
    fields: dict[str, object] = {
        "id": "dom_01TEST",
        "project_id": "proj_01TEST",
        "identity_type": IdentityType.DOMAIN.value,
        "value": "example.com",
        "region": "eu-west-2",
        "verification_status": VerificationStatus.SUCCESS.value,
        "dkim_status": VerificationStatus.SUCCESS.value,
        "dkim_tokens": ["a", "b", "c"],
    }
    fields.update(overrides)
    identity = Identity(**fields)
    return templates.env.get_template("partials/identity_row.html").render(
        identity=identity, csrf_token="token"
    )


def test_a_domain_with_no_mail_from_shows_no_return_path() -> None:
    page = _render()

    assert "Return path" not in page
    assert "feedback-smtp" not in page


def test_a_pending_mail_from_shows_its_records() -> None:
    page = _render(
        mail_from_domain="send.example.com",
        mail_from_status=VerificationStatus.PENDING.value,
    )

    assert "Return path" in page
    assert "feedback-smtp.eu-west-2.amazonses.com" in page
    assert "v=spf1 include:amazonses.com ~all" in page
    assert "send.example.com" in page


def test_a_verified_mail_from_stops_asking_for_records() -> None:
    page = _render(
        mail_from_domain="send.example.com",
        mail_from_status=VerificationStatus.SUCCESS.value,
    )

    assert "Return path" in page
    assert "feedback-smtp" not in page


def test_a_failed_mail_from_says_so() -> None:
    page = _render(
        mail_from_domain="send.example.com",
        mail_from_status=VerificationStatus.FAILED.value,
    )

    assert "Failed" in page
    assert "feedback-smtp" in page
