"""
worker.auth — auth/keys/quota/usage subsystem, split out of the old
worker/auth_db.py monolith.

Public API surface is re-exported here so callers can do either:
    from worker import auth
    auth.register_user(...)
or import a sub-module directly:
    from worker.auth import users, api_keys, quotas, usage, notifications

The legacy `worker.auth_db` name is kept as a shim that re-exports this whole
package (see worker/auth_db.py), so existing `auth_db.xxx` call sites keep
working without changes.
"""
from .db import (
    SESSION_TTL,
    _lock,
    get_conn,
    close_conn,
    check_auth_rate_limit,
    sweep_auth_attempts,
)
from .users import (
    register_user,
    login_user,
    login_or_register_google_user,
    get_user_from_token,
    login_admin_fallback,
    get_user_role,
    set_user_role,
    get_full_user_by_token,
    get_all_users,
    delete_user,
)
from .api_keys import (
    create_key,
    admin_update_key,
    get_key,
    get_username_from_key,
    list_keys,
    delete_key,
    reset_limit,
    validate_and_track_usage,
    _auto_create_user_key,
    create_or_update_user_key,
    admin_update_user_key,
    get_user_key,
)
from .quotas import (
    _auto_reset_if_needed,
    consume_tokens,
    get_token_usage,
    get_token_usage_by_username,
    get_total_system_tokens,
    admin_set_token_limit,
    admin_set_token_limit_by_username,
    admin_reset_tokens,
    admin_reset_tokens_by_username,
    admin_add_tokens,
    admin_add_tokens_by_username,
)
from .usage import (
    log_usage,
    get_usage_stats,
    admin_get_usage_stats,
    insert_request_log,
    get_request_logs,
    admin_get_request_logs,
    get_model_status_blocks,
)
from .notifications import get_user_notifications
from .tokens_estimator import estimate_tokens, estimate_image_tokens

__all__ = [
    # db
    "SESSION_TTL", "_lock", "get_conn", "close_conn",
    "check_auth_rate_limit", "sweep_auth_attempts",
    # users
    "register_user", "login_user", "login_or_register_google_user",
    "get_user_from_token", "login_admin_fallback", "get_user_role",
    "set_user_role", "get_full_user_by_token", "get_all_users", "delete_user",
    # api_keys
    "create_key", "admin_update_key", "get_key", "get_username_from_key",
    "list_keys", "delete_key", "reset_limit", "validate_and_track_usage",
    "_auto_create_user_key", "create_or_update_user_key",
    "admin_update_user_key", "get_user_key",
    # quotas
    "_auto_reset_if_needed", "consume_tokens", "get_token_usage",
    "get_token_usage_by_username", "get_total_system_tokens",
    "admin_set_token_limit", "admin_set_token_limit_by_username",
    "admin_reset_tokens", "admin_reset_tokens_by_username",
    "admin_add_tokens", "admin_add_tokens_by_username",
    # usage
    "log_usage", "get_usage_stats", "admin_get_usage_stats",
    "insert_request_log", "get_request_logs", "admin_get_request_logs",
    "get_model_status_blocks",
    # notifications
    "get_user_notifications",
    # tokens
    "estimate_tokens", "estimate_image_tokens",
]
