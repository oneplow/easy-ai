"""
Google OAuth identity resolution.

Verifies Google ID tokens (credential) OR access tokens against
config.GOOGLE_CLIENT_ID and returns (email, name). Used by the /auth/google
endpoint.

Split out of the old main.py monolith; logic is unchanged.
"""
import json
import logging
from urllib import error as urllib_error, parse as urllib_parse, request as urllib_request

from fastapi import HTTPException

from google.oauth2 import id_token
from google.auth.transport import requests as google_requests

from worker import config
from .schemas import GoogleAuthRequest

log = logging.getLogger("google")

_TOKENINFO_URL = "https://www.googleapis.com/oauth2/v3/tokeninfo"
_USERINFO_URL = "https://www.googleapis.com/oauth2/v3/userinfo"


def _fetch_google_json(url: str, headers: dict[str, str] | None = None) -> dict:
    req = urllib_request.Request(url, headers=headers or {})
    with urllib_request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode("utf-8"))


def resolve_google_identity(req: GoogleAuthRequest) -> tuple[str, str]:
    """Resolve a GoogleAuthRequest to (email, name).

    Raises HTTPException(401/400) on any verification failure so the route
    handler can simply `email, name = resolve_google_identity(req)`.

    Raises urllib_error.HTTPError on network errors from Google's tokeninfo
    endpoint — the caller is expected to catch and map that to a 401.
    """
    if req.credential:
        idinfo = id_token.verify_oauth2_token(
            req.credential,
            google_requests.Request(),
            config.GOOGLE_CLIENT_ID,
        )
        issuer = idinfo.get("iss")
        if issuer not in ("accounts.google.com", "https://accounts.google.com"):
            raise HTTPException(status_code=401, detail="Invalid Google token issuer")

        email = idinfo.get("email")
        if not email:
            raise HTTPException(status_code=400, detail="Google account has no email")

        return email, idinfo.get("name", "")

    if req.access_token:
        token_info_url = _TOKENINFO_URL + "?" + urllib_parse.urlencode(
            {"access_token": req.access_token}
        )
        token_info = _fetch_google_json(token_info_url)
        audience = token_info.get("aud") or token_info.get("azp")
        if audience != config.GOOGLE_CLIENT_ID:
            raise HTTPException(status_code=401, detail="Google token audience mismatch")

        profile = _fetch_google_json(
            _USERINFO_URL,
            headers={"Authorization": f"Bearer {req.access_token}"},
        )
        email = profile.get("email")
        if not email:
            raise HTTPException(status_code=400, detail="Google account has no email")

        return email, profile.get("name", "")

    raise HTTPException(status_code=400, detail="Missing Google credential")


__all__ = ["resolve_google_identity", "resolve_google_identity_error"]
# re-export the urllib error type for convenience in the route handler
resolve_google_identity_error = urllib_error.HTTPError
