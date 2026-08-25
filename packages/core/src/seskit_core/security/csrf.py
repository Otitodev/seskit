"""CSRF protection (§22).

The dashboard is server-rendered and drives state through form posts, so it is
exactly the shape of application cross-site request forgery targets: a hostile
page can make a browser POST to SESKit carrying its session cookie.

The defence is a token tied to the session, rendered into every form and
required on every state-changing request. A hostile page can cause the request
but cannot read the token, because the same-origin policy stops it reading
SESKit's HTML.

``SameSite=Lax`` on the session cookie already blocks most cross-site POSTs.
This is the second layer: SameSite is browser-dependent and has been weakened
before, and §22 asks for CSRF protection on its own terms.
"""

from __future__ import annotations

import secrets

#: Header used by HTMX requests, which post without a form body.
CSRF_HEADER = "X-CSRF-Token"

#: Hidden field name in rendered forms.
CSRF_FIELD = "csrf_token"

#: Verbs that change state and therefore need a token. GET and HEAD must stay
#: safe by definition, so they are never checked.
PROTECTED_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


def generate_csrf_token() -> str:
    return secrets.token_urlsafe(32)


def tokens_match(submitted: str | None, expected: str | None) -> bool:
    """Compare in constant time.

    ``==`` on strings short-circuits at the first differing byte, which leaks
    how much of a guess was right. Not the most practical attack, but
    ``compare_digest`` costs nothing.
    """
    if not submitted or not expected:
        return False
    return secrets.compare_digest(submitted, expected)
