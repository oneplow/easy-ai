"""
Pydantic request schemas shared across routers.

Centralizing these here keeps the routers thin and gives FastAPI's automatic
request validation (422 responses) + /docs schema generation for free.

Note on field naming: the chat endpoints accept camelCase field names
(`sessionId`) for OpenAI-SDK / frontend compatibility, so those schemas use
the exact field names the wire expects rather than PEP-8 snake_case.
"""
from __future__ import annotations

from pydantic import BaseModel, Field


# --- Auth -------------------------------------------------------------------

class RegisterRequest(BaseModel):
    username: str
    password: str
    email: str | None = None


class LoginRequest(BaseModel):
    username: str
    password: str


class GoogleAuthRequest(BaseModel):
    credential: str | None = None
    access_token: str | None = None


class AdminLoginRequest(BaseModel):
    admin_key: str


# --- User key management ----------------------------------------------------

class UserKeyRequest(BaseModel):
    rpm_limit: int = 60
    expires_in_days: int | None = None
    allowed_models: str | None = None


# --- Admin ------------------------------------------------------------------

class AdminCreateKeyRequest(BaseModel):
    key: str | None = None
    name: str | None = None
    expires_in_days: float | None = None
    rpm_limit: int | None = None


class AdminUpdateKeyRequest(BaseModel):
    name: str | None = None
    rpm_limit: int | None = None
    expires_in_days: int | None = None
    allowed_models: str | None = None


class AdminUpdateUserRequest(BaseModel):
    rpm_limit: int | None = None
    expires_in_days: int | None = None
    allowed_models: str | None = None
    token_limit: int | None = -1  # -1 means "don't change", None means unlimited
    token_reset_period: str | None = None


class TokenLimitRequest(BaseModel):
    token_limit: int | None = None
    reset_period: str | None = None


class AddTokensRequest(BaseModel):
    amount: int


class RoleRequest(BaseModel):
    role: str  # "admin" or "user"


# --- Chat -------------------------------------------------------------------

class ChatRequest(BaseModel):
    """Body for the stateful POST /chat endpoint.

    `sessionId` is camelCase for frontend compatibility; server echoes it back
    (or mints a new one) in the stream.
    """
    model: str = "default"
    message: str = ""
    sessionId: str | None = None


class V1ChatRequest(BaseModel):
    """Body for the simple stateless POST /v1/chat endpoint."""
    model: str = "default"
    messages: list[dict] = Field(default_factory=list)


class OpenAIChatCompletionsRequest(BaseModel):
    """Body for the OpenAI-compatible POST /v1/chat/completions endpoint.

    All fields are permissive: `messages` and `tools` are raw dicts because
    they can contain arbitrarily nested content (multimodal parts, tool
    schemas) that we forward verbatim to the model runner.
    """
    model: str = "default"
    messages: list[dict] = Field(default_factory=list)
    stream: bool = False
    tools: list[dict] = Field(default_factory=list)


class ForceDistillRequest(BaseModel):
    """Body for POST /knowledge/distill/{session_id} (admin debug tool)."""
    model: str = "default"


# --- Knowledge Items --------------------------------------------------------

class KICreateRequest(BaseModel):
    title: str
    summary: str
    content: str = ""
    tags: list[str] = Field(default_factory=list)


class KIUpdateRequest(BaseModel):
    title: str | None = None
    summary: str | None = None
    content: str | None = None
    tags: list[str] | None = None
