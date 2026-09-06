"""Security primitives: password hashing, sessions, CSRF (§22)."""

from seskit_core.security.api_keys import (
    display_prefix,
    generate_key,
    hash_key,
    looks_like_key,
    parse_authorization,
)
from seskit_core.security.csrf import (
    CSRF_FIELD,
    CSRF_HEADER,
    PROTECTED_METHODS,
    generate_csrf_token,
    tokens_match,
)
from seskit_core.security.passwords import (
    burn_dummy_hash,
    hash_password,
    verify_and_update,
    verify_password,
)
from seskit_core.security.ratelimit import (
    RateLimitStatus,
    check_rate_limit,
    reset_rate_limit,
)
from seskit_core.security.sessions import (
    SessionData,
    create_session,
    delete_session,
    delete_user_sessions,
    generate_token,
    read_session,
    set_current_project,
)
from seskit_core.security.throttle import clear as clear_login_attempts
from seskit_core.security.throttle import is_throttled, record_failure
from seskit_core.security.unsubscribe import (
    LIST_UNSUBSCRIBE_HEADER,
    LIST_UNSUBSCRIBE_POST_HEADER,
    ONE_CLICK,
    read_token,
    token_matches,
    unsubscribe_token,
)

__all__ = [
    "CSRF_FIELD",
    "CSRF_HEADER",
    "LIST_UNSUBSCRIBE_HEADER",
    "LIST_UNSUBSCRIBE_POST_HEADER",
    "ONE_CLICK",
    "PROTECTED_METHODS",
    "RateLimitStatus",
    "SessionData",
    "burn_dummy_hash",
    "check_rate_limit",
    "clear_login_attempts",
    "create_session",
    "delete_session",
    "delete_user_sessions",
    "display_prefix",
    "generate_csrf_token",
    "generate_key",
    "generate_token",
    "hash_key",
    "hash_password",
    "is_throttled",
    "looks_like_key",
    "parse_authorization",
    "read_session",
    "read_token",
    "record_failure",
    "reset_rate_limit",
    "set_current_project",
    "token_matches",
    "tokens_match",
    "unsubscribe_token",
    "verify_and_update",
    "verify_password",
]
