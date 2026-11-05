"""Reading a domain out of an address.

A module of its own, importing nothing from SESKit, because the ``Email`` model
needs it and the model sits at the bottom of the import graph. Putting this in
``email.message`` next to ``bare_address`` made the model import ``email.message``,
which imports ``security``, which imports the models: a cycle that only shows when
something imports ``seskit_core.services`` before ``seskit_core.models``.
"""

from __future__ import annotations

from email.utils import parseaddr


def domain_of(value: str) -> str | None:
    """The domain a sender address belongs to, lower-cased, or ``None``.

    ``Display Name <a@b.com>`` and ``a@b.com`` both answer ``b.com``. The mailbox
    is taken with ``parseaddr`` first, so a display name containing an ``@``
    (``"a@b" <real@example.com>``) is not mistaken for it, and the domain is what
    follows the *last* ``@`` because a quoted local part may contain one. A
    trailing dot is dropped: ``example.com.`` and ``example.com`` are one domain.

    ``None`` for something with no usable domain, so a caller can tell "nothing
    to group this under" from a domain, and never has to match on ``""``.
    """
    _, mailbox = parseaddr(value)
    _, at, domain = mailbox.strip().lower().rpartition("@")
    # No "@" at all leaves rpartition returning the whole string as the "domain".
    if not at:
        return None
    return domain.strip().rstrip(".") or None
