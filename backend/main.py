"""
FastAPI orchestrator  (API-only, no frontend).
  GET  /models              -> model list for the dropdown
  GET  /bank                -> bank status (how many warm accounts ready)
  GET  /health              -> full watchdog readout
  POST /chat                -> stateful chat (we hold context), streams reply
  POST /v1/chat             -> stateless, simple OpenAI-ish
  POST /v1/chat/completions -> OpenAI-compatible (drop-in for OpenAI SDK clients)

On startup a background loop keeps the account bank topped up so signup stays
out of the hot path.
"""
import asyncio
import json
import logging
import time
import uuid

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse

from worker import auth_db, bank, config, health
from worker.harvester import top_up
from worker.leech import run_messages, stream_messages
from . import context
from .pool import run_guarded, run_guarded_gen

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("backend")
app = FastAPI(title="WMan")

# Allow cross-origin calls so any external client can hit the API.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


def _require_admin_key(req: Request) -> None:
    if not config.ADMIN_KEY:
        raise HTTPException(status_code=403, detail="Admin API is disabled (ADMIN_KEY not set)")
    auth = req.headers.get("authorization", "")
    token = auth.removeprefix("Bearer ").strip() if auth.lower().startswith("bearer ") else ""
    key = req.headers.get("x-api-key", "").strip()
    if token != config.ADMIN_KEY and key != config.ADMIN_KEY:
        raise HTTPException(status_code=401, detail="Invalid admin key")

def _require_api_key(req: Request) -> None:
    auth = req.headers.get("authorization", "")
    token = auth.removeprefix("Bearer ").strip() if auth.lower().startswith("bearer ") else ""
    key = req.headers.get("x-api-key", "").strip()
    
    client_key = token or key
    if not client_key:
        raise HTTPException(status_code=401, detail="Missing API key")
        
    is_valid, reason = auth_db.validate_and_track_usage(client_key)
    if not is_valid:
        raise HTTPException(status_code=401, detail=reason)


@app.on_event("startup")
async def _start_prewarmer():
    # The headless WS path signs up its own account per request, so the browser
    # harvester/bank isn't needed. Instead start the warm ACCOUNT POOL so signup
    # stays out of the hot path. Only run the browser prewarmer for the fallback.
    if getattr(config, "DIRECT_WS_ENABLED", False):
        from worker.account_pool import POOL
        POOL.start()
        log.info("DIRECT_WS_ENABLED -> headless path, warm account pool started")
        return

    async def loop():
        while True:
            try:
                n = await top_up()
                if n:
                    log.info("bank +%d (fresh=%d)", n, bank.count_fresh())
            except Exception as e:
                log.warning("prewarm error: %s", e)
            await asyncio.sleep(config.PREWARM_INTERVAL_SEC)
    asyncio.create_task(loop())

# --- admin auth management ---------------------------------------------------
@app.get("/admin/keys")
async def admin_list_keys(req: Request):
    _require_admin_key(req)
    return {"keys": auth_db.list_keys()}

@app.post("/admin/keys")
async def admin_create_key(req: Request):
    _require_admin_key(req)
    body = await req.json()
    
    # Generate a random key if none provided
    key_str = body.get("key") or f"leech-{uuid.uuid4().hex[:16]}"
    name = body.get("name")
    expires_in_days = body.get("expires_in_days")
    rpm_limit = body.get("rpm_limit")
    
    expires_at = None
    if expires_in_days is not None:
        expires_at = time.time() + (float(expires_in_days) * 86400)
        
    if rpm_limit is not None:
        rpm_limit = int(rpm_limit)
        
    try:
        new_key = auth_db.create_key(key=key_str, name=name, expires_at=expires_at, rpm_limit=rpm_limit)
        return {"message": "Key created", "key": new_key}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

@app.delete("/admin/keys/{key}")
async def admin_delete_key(key: str, req: Request):
    _require_admin_key(req)
    if auth_db.delete_key(key):
        return {"message": "Key deleted"}
    raise HTTPException(status_code=404, detail="Key not found")



# --- status ------------------------------------------------------------------
@app.get("/models")
async def models():
    return {"models": config.MODELS, "default": config.DEFAULT_MODEL}


@app.get("/bank")
async def bank_status():
    if getattr(config, "DIRECT_WS_ENABLED", False):
        from worker.account_pool import POOL
        snap = health.H.snapshot(POOL.ready())
        return {
            "mode": "headless-ws",
            "warm_accounts": POOL.ready(),
            "pool_target": POOL.size,
            "status": snap["status"],
            "reasons": snap["reasons"],
        }
    snap = health.H.snapshot(bank.count_fresh())
    return {
        "fresh": snap["fresh_accounts"],
        "status": snap["status"],
        "reasons": snap["reasons"],
        "stats": bank.stats(),
    }


@app.get("/health")
async def health_status():
    """Full watchdog readout: status, why, rates, counters, recent errors."""
    if getattr(config, "DIRECT_WS_ENABLED", False):
        from worker.account_pool import POOL
        snap = health.H.snapshot(POOL.ready())
        snap["warm_accounts"] = POOL.ready()
        snap["pool_target"] = POOL.size
        return snap
    return health.H.snapshot(bank.count_fresh())


# --- stateful chat -----------------------------------------------------------
def _sse_payload(payload: dict) -> str:
    return f"data: {json.dumps(payload)}\n\n"


def _sse(token: str) -> str:
    return _sse_payload({"type": "token", "token": token})


@app.post("/chat")
async def chat(req: Request):
    _require_api_key(req)
    body = await req.json()
    message = body.get("message", "")
    model = body.get("model", "default")
    session_id = body.get("sessionId") or str(uuid.uuid4())

    messages = context.build_messages(session_id, message)   # role-tagged history + new turn
    context.append(session_id, "user", message)

    async def gen():
        parts: list[str] = []
        try:
            async for delta in run_guarded_gen(lambda: stream_messages(model, messages)):
                parts.append(delta)
                yield _sse(delta)
        except Exception as exc:
            log.warning("chat stream failed: %s: %s", type(exc).__name__, exc)
            if not parts:
                yield _sse(f"Backend error contacting the model runner ({type(exc).__name__}).")
        reply = "".join(parts).strip()
        context.append(session_id, "assistant", reply)
        yield "data: [DONE]\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream")


# --- stateless: simple -------------------------------------------------------
@app.post("/v1/chat")
async def v1_chat(req: Request):
    _require_api_key(req)
    body = await req.json()
    model = body.get("model", "default")
    reply = await run_guarded(lambda: run_messages(model, body.get("messages", [])))
    return JSONResponse({
        "model": model,
        "choices": [{"message": {"role": "assistant", "content": reply}}],
    })


# --- stateless: OpenAI-compatible -------------------------------------------
def _openai_block(reply: str, model: str) -> dict:
    return {
        "id": "chatcmpl-" + uuid.uuid4().hex[:24],
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": reply},
            "finish_reason": "stop",
        }],
    }


@app.post("/v1/chat/completions")
async def openai_completions(req: Request):
    _require_api_key(req)
    body = await req.json()
    model = body.get("model", "default")
    stream = bool(body.get("stream", False))
    msgs = body.get("messages", [])

    if stream:
        cid = "chatcmpl-" + uuid.uuid4().hex[:24]
        created = int(time.time())

        async def gen():
            base = {"id": cid, "object": "chat.completion.chunk", "created": created, "model": model}
            async for delta in run_guarded_gen(lambda: stream_messages(model, msgs)):
                chunk = {**base, "choices": [{"index": 0, "delta": {"content": delta},
                                              "finish_reason": None}]}
                yield f"data: {json.dumps(chunk)}\n\n"
            done = {**base, "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}
            yield f"data: {json.dumps(done)}\n\n"
            yield "data: [DONE]\n\n"

        return StreamingResponse(gen(), media_type="text/event-stream")

    reply = await run_guarded(lambda: run_messages(model, msgs))
    return JSONResponse(_openai_block(reply, model))
