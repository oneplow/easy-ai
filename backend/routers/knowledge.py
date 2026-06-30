"""
Knowledge Items router: CRUD + search + force-distill (admin debug).

All endpoints require admin auth. The KI store itself lives in
backend/knowledge_store.py (sqlite + FTS5).
"""
from fastapi import APIRouter, HTTPException, Request

from worker import config
from backend import knowledge_store
from ..deps import require_admin_key
from ..schemas import ForceDistillRequest, KICreateRequest, KIUpdateRequest

router = APIRouter(prefix="/knowledge", tags=["knowledge"])


@router.get("")
async def list_knowledge(req: Request):
    """List all Knowledge Items (admin only)."""
    require_admin_key(req)
    kis = knowledge_store.list_kis(limit=100)
    return {"knowledge_items": kis}


@router.get("/search")
async def search_knowledge(req: Request):
    """Search Knowledge Items by query."""
    require_admin_key(req)
    query = req.query_params.get("q", "").strip()
    if not query:
        raise HTTPException(status_code=400, detail="Missing query parameter 'q'")
    results = knowledge_store.search_kis(query, limit=10)
    return {"results": results}


@router.post("")
async def create_knowledge(req: KICreateRequest, request: Request):
    """Create a new Knowledge Item."""
    require_admin_key(request)
    ki = knowledge_store.create_ki(req.title, req.summary, req.content, req.tags)
    return {"message": "Knowledge Item created", "ki": ki}


@router.put("/{ki_id}")
async def update_knowledge(ki_id: str, req: KIUpdateRequest, request: Request):
    """Update an existing Knowledge Item."""
    require_admin_key(request)
    success = knowledge_store.update_ki(ki_id, req.title, req.summary, req.content, req.tags)
    if not success:
        raise HTTPException(status_code=404, detail="Knowledge Item not found")
    return {"message": "Knowledge Item updated"}


@router.delete("/{ki_id}")
async def delete_knowledge(ki_id: str, request: Request):
    """Delete a Knowledge Item."""
    require_admin_key(request)
    if not knowledge_store.delete_ki(ki_id):
        raise HTTPException(status_code=404, detail="Knowledge Item not found")
    return {"message": "Knowledge Item deleted"}


@router.post("/distill/{session_id}")
async def force_distill(session_id: str, request: Request, body: ForceDistillRequest):
    """Force KI distillation for a given session (admin debug tool)."""
    require_admin_key(request)
    # Delegate to the chat router's distillation helper (it owns the runner
    # invocation). Imported lazily to avoid a circular import at module load.
    from ..routers.chat import _maybe_distill
    model = config.resolve_model(body.model)
    await _maybe_distill(session_id, model)
    return {"message": "Distillation triggered"}
