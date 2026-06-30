"""
Admin router: API key CRUD, user management (incl. token quotas + roles),
and system-wide usage stats / logs.

All endpoints require admin auth (deps.require_admin_key: role-based admin
session OR the legacy ADMIN_KEY).
"""
import logging
import time
import uuid

from fastapi import APIRouter, HTTPException, Request

from worker import auth_db, config
from ..deps import require_admin_key
from ..schemas import (
    AddTokensRequest,
    AdminCreateKeyRequest,
    AdminUpdateKeyRequest,
    AdminUpdateUserRequest,
    RoleRequest,
    TokenLimitRequest,
)

log = logging.getLogger("admin")
router = APIRouter(tags=["admin"])


# --- API key management -----------------------------------------------------

@router.get("/admin/keys")
async def admin_list_keys(req: Request):
    require_admin_key(req)
    return {"keys": auth_db.list_keys()}


@router.post("/admin/keys")
async def admin_create_key(req: Request, body: AdminCreateKeyRequest):
    require_admin_key(req)

    # Generate a random key if none provided
    key_str = body.key or f"sk-{uuid.uuid4().hex[:32]}"

    expires_at = None
    if body.expires_in_days is not None:
        expires_at = time.time() + (float(body.expires_in_days) * 86400)

    rpm_limit = int(body.rpm_limit) if body.rpm_limit is not None else None

    try:
        new_key = auth_db.create_key(
            key=key_str, name=body.name, expires_at=expires_at, rpm_limit=rpm_limit
        )
        return {"message": "Key created", "key": new_key}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.delete("/admin/keys/{key}")
async def admin_delete_key(key: str, req: Request):
    require_admin_key(req)
    if auth_db.delete_key(key):
        return {"message": "Key deleted"}
    raise HTTPException(status_code=404, detail="Key not found")


@router.put("/admin/keys/{key}")
async def admin_update_key_route(key: str, req: AdminUpdateKeyRequest, request: Request):
    require_admin_key(request)
    success = auth_db.admin_update_key(
        key=key,
        name=req.name,
        rpm_limit=req.rpm_limit,
        expires_in_days=req.expires_in_days,
        allowed_models=req.allowed_models,
    )
    if not success:
        raise HTTPException(status_code=404, detail="Key not found")
    return {"message": "Key updated successfully"}


@router.post("/admin/keys/{key}/reset")
async def admin_reset_key_limit(key: str, req: Request):
    require_admin_key(req)
    auth_db.reset_limit(key)
    # Even if rowcount was 0 (no usage yet), report success.
    return {"message": "Rate limit reset successfully"}


# --- User management --------------------------------------------------------

@router.get("/admin/users")
async def admin_get_users(req: Request):
    require_admin_key(req)
    users = auth_db.get_all_users()
    for u in users:
        if u.get("key") is None and u.get("token_limit") is None:
            u["token_limit"] = config.DEFAULT_TOKEN_LIMIT
    return {"users": users}


@router.put("/admin/users/{username}")
async def admin_update_user(username: str, req: AdminUpdateUserRequest, request: Request):
    require_admin_key(request)
    success = auth_db.admin_update_user_key(
        username=username,
        rpm_limit=req.rpm_limit,
        expires_in_days=req.expires_in_days,
        allowed_models=req.allowed_models,
    )
    # token_limit is handled separately (-1 = don't change)
    if req.token_limit != -1:
        auth_db.admin_set_token_limit_by_username(username, req.token_limit, req.token_reset_period)
    elif req.token_reset_period:
        auth_db.admin_set_token_limit_by_username(username, None, req.token_reset_period)
    if not success:
        raise HTTPException(status_code=404, detail="User or key not found")
    return {"message": "User updated successfully"}


@router.delete("/admin/users/{username}")
async def admin_delete_user(username: str, req: Request):
    require_admin_key(req)
    if auth_db.delete_user(username):
        return {"message": "User and associated data deleted"}
    raise HTTPException(status_code=404, detail="User not found")


@router.put("/admin/users/{username}/role")
async def admin_set_user_role(username: str, req: RoleRequest, request: Request):
    require_admin_key(request)
    if req.role not in ("admin", "user"):
        raise HTTPException(status_code=400, detail="Role must be 'admin' or 'user'")
    success = auth_db.set_user_role(username, req.role)
    if not success:
        raise HTTPException(status_code=404, detail="User not found")
    return {"message": f"User role updated to {req.role}"}


# --- Token quotas -----------------------------------------------------------

@router.get("/admin/users/{username}/tokens")
async def admin_get_user_tokens(username: str, req: Request):
    require_admin_key(req)
    usage = auth_db.get_token_usage_by_username(username)
    if not usage:
        return {
            "token_limit": None,
            "tokens_used": 0,
            "token_reset_period": "weekly",
            "token_last_reset": None,
        }
    return usage


@router.put("/admin/users/{username}/tokens")
async def admin_set_user_tokens(username: str, req: TokenLimitRequest, request: Request):
    require_admin_key(request)
    success = auth_db.admin_set_token_limit_by_username(username, req.token_limit, req.reset_period)
    if not success:
        raise HTTPException(status_code=404, detail="User or key not found")
    return {"message": "Token limit updated"}


@router.post("/admin/users/{username}/tokens/reset")
async def admin_reset_user_tokens(username: str, req: Request):
    require_admin_key(req)
    success = auth_db.admin_reset_tokens_by_username(username)
    if not success:
        raise HTTPException(status_code=404, detail="User or key not found")
    return {"message": "Token usage reset"}


@router.post("/admin/users/{username}/tokens/add")
async def admin_add_user_tokens(username: str, req: AddTokensRequest, request: Request):
    require_admin_key(request)
    success = auth_db.admin_add_tokens_by_username(username, req.amount)
    if not success:
        raise HTTPException(status_code=404, detail="User or key not found")
    return {"message": f"Added {req.amount} tokens"}


# --- System-wide stats / logs ----------------------------------------------

@router.get("/admin/usage_stats")
async def admin_get_usage_stats_route(req: Request):
    require_admin_key(req)
    days = int(req.query_params.get("days", 90))
    stats = auth_db.admin_get_usage_stats(days)
    return {"stats": stats}


@router.get("/admin/logs")
async def admin_get_logs(req: Request):
    require_admin_key(req)
    limit = int(req.query_params.get("limit", 50))
    offset = int(req.query_params.get("offset", 0))
    return auth_db.admin_get_request_logs(limit, offset)
