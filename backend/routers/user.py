"""
User router: self-service API key management, token usage, usage stats, logs.

Authenticated by session bearer token (deps.get_user_from_req). Error
responses are uniform HTTPExceptions (Phase 6b) rather than the mixed
JSONResponse(401) the old main.py used.
"""
import logging

from fastapi import APIRouter, HTTPException, Request

from worker import auth_db
from ..deps import get_user_from_req
from ..schemas import UserKeyRequest

log = logging.getLogger("user")
router = APIRouter(tags=["user"])


# --- User API key management ------------------------------------------------

@router.post("/user/keys")
async def create_user_key(req: UserKeyRequest, request: Request):
    username = get_user_from_req(request)
    key_info = auth_db.create_or_update_user_key(
        username, req.rpm_limit, req.expires_in_days, req.allowed_models
    )
    return {"message": "Key created or updated successfully", "key": key_info}


@router.get("/user/keys")
async def get_user_keys(request: Request):
    username = get_user_from_req(request)
    key = auth_db.get_user_key(username)
    return {"key": key}


@router.post("/user/keys/reset")
async def reset_user_key(request: Request):
    username = get_user_from_req(request)
    key = auth_db.get_user_key(username)
    if not key:
        raise HTTPException(status_code=404, detail="No key found")
    auth_db.reset_limit(key["key"])
    return {"message": "Rate limit reset successfully"}


@router.delete("/user/keys")
async def delete_user_key(request: Request):
    username = get_user_from_req(request)
    key = auth_db.get_user_key(username)
    if not key:
        raise HTTPException(status_code=404, detail="No key found")
    if auth_db.delete_key(key["key"]):
        return {"message": "Key revoked successfully"}
    raise HTTPException(status_code=500, detail="Failed to revoke key")


# --- Token usage ------------------------------------------------------------

@router.get("/user/tokens")
async def get_user_tokens(request: Request):
    username = get_user_from_req(request)
    usage = auth_db.get_token_usage_by_username(username)
    if not usage:
        return {
            "token_limit": None,
            "tokens_used": 0,
            "token_reset_period": "weekly",
            "token_last_reset": None,
        }
    return usage


# --- Usage stats + logs -----------------------------------------------------

@router.get("/user/usage_stats")
async def get_user_usage_stats(request: Request):
    username = get_user_from_req(request)
    days = int(request.query_params.get("days", 90))
    stats = auth_db.get_usage_stats(username, days)
    return {"stats": stats}


@router.get("/user/logs")
async def get_user_logs(request: Request):
    username = get_user_from_req(request)
    limit = int(request.query_params.get("limit", 50))
    offset = int(request.query_params.get("offset", 0))
    return auth_db.get_request_logs(username, limit, offset)
