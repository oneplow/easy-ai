"""
Auth router: register / login (password), Google OAuth, admin fallback login,
and /auth/me.

All auth-write endpoints are protected by per-IP rate limiting (see
deps.enforce_auth_rate_limit) to blunt credential brute-force.
"""
import logging

from fastapi import APIRouter, HTTPException, Request
from urllib import error as urllib_error

from worker import auth_db, config
from ..deps import enforce_auth_rate_limit
from ..google import resolve_google_identity
from ..schemas import (
    AdminLoginRequest,
    GoogleAuthRequest,
    LoginRequest,
    RegisterRequest,
)

log = logging.getLogger("auth")
router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/register")
async def register(req: RegisterRequest, request: Request):
    enforce_auth_rate_limit(request, "register")
    success, token_or_err, role = auth_db.register_user(req.username, req.password, req.email)
    if not success:
        raise HTTPException(status_code=400, detail=token_or_err)
    return {"token": token_or_err, "username": req.username, "role": role}


@router.post("/login")
async def login(req: LoginRequest, request: Request):
    enforce_auth_rate_limit(request, "login")
    success, token_or_err, role = auth_db.login_user(req.username, req.password)
    if not success:
        raise HTTPException(status_code=400, detail=token_or_err)
    return {"token": token_or_err, "username": req.username, "role": role}


@router.post("/google")
async def google_auth(req: GoogleAuthRequest, request: Request):
    enforce_auth_rate_limit(request, "google")
    if not config.GOOGLE_CLIENT_ID:
        log.error("GOOGLE_CLIENT_ID is not configured on the backend")
        raise HTTPException(status_code=500, detail="Google login is not configured")

    try:
        email, name = resolve_google_identity(req)

        success, token, username, role = auth_db.login_or_register_google_user(email, name)
        if not success:
            raise HTTPException(status_code=500, detail=token)

        return {"token": token, "username": username, "role": role}
    except ValueError as e:
        raise HTTPException(status_code=401, detail=f"Invalid Google token: {str(e)}")
    except urllib_error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="ignore")
        log.error("Google token lookup failed: %s", detail or e.reason)
        raise HTTPException(status_code=401, detail="Invalid Google access token")
    except HTTPException:
        # resolve_google_identity raises typed 401/400s we want to surface as-is
        raise
    except Exception as e:
        log.error("Google auth error: %s", e)
        raise HTTPException(status_code=500, detail=f"Google auth error: {str(e)}")


@router.post("/admin/login")
async def admin_login(req: AdminLoginRequest, request: Request):
    enforce_auth_rate_limit(request, "admin_login")
    if not config.ADMIN_KEY or req.admin_key != config.ADMIN_KEY:
        raise HTTPException(status_code=401, detail="Invalid admin key")

    success, token, username, role = auth_db.login_admin_fallback()
    if not success:
        raise HTTPException(status_code=500, detail="Admin fallback login failed")

    return {"token": token, "username": username, "role": role}


@router.get("/me")
async def get_auth_me(request: Request):
    """Return current user info from session token."""
    from ..deps import _bearer_token
    token = _bearer_token(request)
    if not token:
        raise HTTPException(status_code=401, detail="Missing session token")
    user_info = auth_db.get_full_user_by_token(token)
    if not user_info:
        raise HTTPException(status_code=401, detail="Invalid session token")
    return user_info
