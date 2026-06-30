"""
Status / monitoring router: model list, notifications, account bank/pool
health, and the real-time SSE dashboard stream.
"""
import asyncio
import json

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

from worker import auth_db, bank, config, health
from ..deps import get_dashboard_payload, get_user_from_req

router = APIRouter(tags=["status"])


@router.get("/v1/models")
async def get_v1_models():
    status_blocks = auth_db.get_model_status_blocks(60)
    return {
        "object": "list",
        "data": [
            {
                "id": m["slug"],
                "object": "model",
                "created": 1686935002,
                "owned_by": "easy-ai",
                "label": m.get("label", m["slug"]),
                "blocks": status_blocks.get(m["slug"], [1] * 60),
            }
            for m in config.MODELS
        ],
    }


@router.get("/v1/notifications")
async def get_notifications(req: Request):
    try:
        username = get_user_from_req(req)
        notifications = auth_db.get_user_notifications(username)
        return {"notifications": notifications}
    except HTTPException:
        return {"notifications": []}


@router.get("/bank")
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


@router.get("/health")
async def health_status():
    """Full watchdog readout: status, why, rates, counters, recent errors."""
    if getattr(config, "DIRECT_WS_ENABLED", False):
        from worker.account_pool import POOL
        snap = health.H.snapshot(POOL.ready())
        snap["warm_accounts"] = POOL.ready()
        snap["pool_target"] = POOL.size
        return snap
    return health.H.snapshot(bank.count_fresh())


@router.get("/health/stream")
async def health_stream(req: Request):
    """Real-time SSE stream for dashboard stats."""
    async def gen():
        while True:
            if await req.is_disconnected():
                break
            snap = get_dashboard_payload(req)
            yield f"data: {json.dumps(snap)}\n\n"
            await asyncio.sleep(2)

    return StreamingResponse(gen(), media_type="text/event-stream")
