"""Domain logic.

Keeps business rules out of route handlers, so they can be tested without HTTP
and reused from a CLI later (§32.12). Phase 3's key issuance and Phase 6's send
logic belong here too.
"""

from seskit_core.services.projects import (
    create_project,
    get_default_project,
    get_owned_project,
    list_projects,
)
from seskit_core.services.users import (
    EmailAlreadyRegistered,
    SignupClosed,
    authenticate,
    count_users,
    get_user_by_email,
    get_user_by_id,
    register_user,
    signup_allowed,
)

__all__ = [
    "EmailAlreadyRegistered",
    "SignupClosed",
    "authenticate",
    "count_users",
    "create_project",
    "get_default_project",
    "get_owned_project",
    "get_user_by_email",
    "get_user_by_id",
    "list_projects",
    "register_user",
    "signup_allowed",
]
