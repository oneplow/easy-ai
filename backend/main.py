"""
easy-ai backend entrypoint (FastAPI).

The app is intentionally thin: it owns only the app object, CORS, router
registration, and the startup/shutdown lifecycle. Everything else lives in
focused modules:

  backend/deps.py            shared auth guards + rate limiter + dashboard
  backend/schemas.py         Pydantic request models
  backend/google.py          Google OAuth identity resolution
  backend/lifecycle.py       startup/shutdown (prewarmer, KI, housekeeping)
  backend/context.py         per-session conversation memory (sqlite-backed)
  backend/knowledge_store.py Knowledge Items store (sqlite + FTS5)
  backend/tool_support.py    tool-calling prompt injection + stream intercept
  backend/pool.py            concurrency caps for the model runner
  backend/routers/           the route modules (auth/user/admin/chat/health/knowledge)

The model-runner / account farm lives under worker/.
"""
import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from worker import config
from .lifecycle import on_shutdown, on_startup
from .routers import (
    admin_router,
    auth_router,
    chat_router,
    health_router,
    knowledge_router,
    user_router,
)

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("backend")

app = FastAPI(title="WMan")

# CORS must be registered before routes so it wraps the router stack.
_cors_allow_origins = ["*"] if config.CORS_ALLOW_ALL else config.CORS_ALLOW_ORIGINS
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_allow_origins,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
async def _startup():
    await on_startup()
    log.info("backend started")


@app.on_event("shutdown")
async def _shutdown():
    await on_shutdown()
    log.info("backend stopped")


# Register every router. Tags/prefixes are set inside each module.
app.include_router(auth_router)
app.include_router(user_router)
app.include_router(admin_router)
app.include_router(chat_router)
app.include_router(health_router)
app.include_router(knowledge_router)


@app.get("/")
async def root():
    """Tiny health probe so a bare GET / isn't a 404."""
    return {"status": "ok", "service": "easy-ai"}
