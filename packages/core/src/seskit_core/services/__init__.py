"""Domain logic.

Keeps business rules out of route handlers, so they can be tested without HTTP
and reused from a CLI later (§32.12). Phase 6's send logic belongs here too.
"""

from seskit_core.services.api_keys import (
    IssuedKey,
    create_api_key,
    get_owned_api_key,
    list_api_keys,
    revoke_api_key,
    touch_last_used,
    verify_api_key,
)
from seskit_core.services.aws import (
    ProviderFactory,
    check_is_allowed,
    clear_check_marker,
    connect_aws,
    disconnect_aws,
    get_connection,
    refresh_connection,
)
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
    "IssuedKey",
    "ProviderFactory",
    "SignupClosed",
    "authenticate",
    "check_is_allowed",
    "clear_check_marker",
    "connect_aws",
    "count_users",
    "create_api_key",
    "create_project",
    "disconnect_aws",
    "get_connection",
    "get_default_project",
    "get_owned_api_key",
    "get_owned_project",
    "get_user_by_email",
    "get_user_by_id",
    "list_api_keys",
    "list_projects",
    "refresh_connection",
    "register_user",
    "revoke_api_key",
    "signup_allowed",
    "touch_last_used",
    "verify_api_key",
]
