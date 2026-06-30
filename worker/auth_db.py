"""
Backward-compatibility shim.

The auth/keys/quota/usage subsystem used to live entirely in this file
(~1100 lines). It has been split into the `worker.auth` package:

    worker/auth/db.py            schema + shared connection + lock
    worker/auth/users.py         registration, login, sessions, roles
    worker/auth/api_keys.py      key CRUD, validation, RPM tracking
    worker/auth/quotas.py        token quota consume/reset/adjust
    worker/auth/usage.py         usage + request logs + model status
    worker/auth/notifications.py derived user notifications
    worker/auth/tokens_estimator.py  token-count heuristics

Every name that used to be importable from here is re-exported below, so
existing call sites like `auth_db.register_user(...)` and
`from worker import auth_db` keep working unchanged. New code should import
from `worker.auth` directly.

This file deliberately does NOT keep a copy of the implementation: there is
exactly one source of truth now (the package).
"""
# The whole public surface is re-exported via `from .auth import *`.
from .auth import *  # noqa: F401,F403
from .auth import __all__  # noqa: F401
