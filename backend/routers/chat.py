"""
Chat router: the three model-running endpoints.

  POST /chat                  stateful, SSE stream, server holds context
  POST /v1/chat               stateless, simple OpenAI-ish
  POST /v1/chat/completions   OpenAI-compatible (drop-in for OpenAI SDK)

Token accounting (input + output + images) uses the improved estimator in
worker.auth.tokens_estimator (CJK/Thai-aware + image tile model) instead of
the old len//4 flat guess.

Tool-calling support (tool_support.inject_tools_and_results + the stream
interceptor) is unchanged from the old main.py.
"""
import asyncio
import json
import logging
import time
import uuid

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse

from worker import auth_db, config
from worker.auth import estimate_tokens, estimate_image_tokens
from worker.easy_ai import run_messages, stream_messages

from backend.tool_support import inject_tools_and_results, ToolCallStreamInterceptor, _extract_tool_calls
from backend import context
from backend.deps import require_api_key
from backend.pool import run_guarded, run_guarded_gen
from backend.schemas import (
    ChatRequest,
    OpenAIChatCompletionsRequest,
    V1ChatRequest,
)

log = logging.getLogger("chat")
router = APIRouter(tags=["chat"])


# ---------------------------------------------------------------------------
# SSE helpers
# ---------------------------------------------------------------------------

def _sse_payload(payload: dict) -> str:
    return f"data: {json.dumps(payload)}\n\n"


def _sse(token: str) -> str:
    return _sse_payload({"type": "token", "token": token})


async def _maybe_distill(session_id: str, model: str):
    """Background KI distillation: runs after enough turns accumulate.
    Uses the same model to distill session insights into Knowledge Items."""
    try:
        history = context.get_history(session_id)
        distill_interval = getattr(config, "KI_DISTILL_EVERY_N_TURNS", 6)
        if len(history) < distill_interval or len(history) % distill_interval != 0:
            return

        from backend import knowledge_store
        prompt = knowledge_store.build_distillation_prompt(session_id, history)
        distill_msgs = [{"role": "user", "content": prompt}]
        result = await run_guarded(lambda: run_messages(model, distill_msgs))
        actions = knowledge_store.apply_distillation(result)
        if actions:
            log.info("KI distillation for session %s: %s", session_id[:8], actions)
    except Exception as e:
        log.debug("KI distillation failed (non-fatal): %s", e)


# ---------------------------------------------------------------------------
# Shared token accounting
# ---------------------------------------------------------------------------

def _count_message_tokens(msgs: list[dict]) -> tuple[int, int]:
    """Return (text_tokens, image_tokens) for a list of OpenAI-style messages,
    summing across all parts of every message's content."""
    text_parts: list[str] = []
    image_tokens = 0
    for m in msgs:
        content = m.get("content")
        if not content:
            continue
        if isinstance(content, str):
            text_parts.append(content)
        elif isinstance(content, list):
            for part in content:
                if not isinstance(part, dict):
                    continue
                ptype = part.get("type")
                if ptype == "text" and "text" in part:
                    text_parts.append(part["text"])
                elif ptype in ("image_url", "image"):
                    image_tokens += estimate_image_tokens(detail="auto")
    return estimate_tokens(" ".join(text_parts)), image_tokens


def _record_usage(client_key: str, model: str, input_tokens: int,
                  output_tokens: int, latency_ms: int) -> None:
    auth_db.consume_tokens(client_key, input_tokens + output_tokens)
    auth_db.log_usage(client_key, model, input_tokens + output_tokens, True, latency_ms)
    auth_db.insert_request_log(
        client_key, str(uuid.uuid4()), model, "POST", "/chat", True,
        input_tokens, output_tokens, latency_ms,
    )


# ---------------------------------------------------------------------------
# POST /chat — stateful, SSE stream
# ---------------------------------------------------------------------------

@router.post("/chat")
async def chat(req: Request, body: ChatRequest):
    start_time = time.time()

    model = config.resolve_model(body.model)
    client_key = require_api_key(req, model)
    message = body.message
    session_id = body.sessionId or str(uuid.uuid4())

    messages = context.build_messages(session_id, message)
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

        input_tokens = await asyncio.to_thread(estimate_tokens, message)
        output_tokens = await asyncio.to_thread(estimate_tokens, reply)
        latency = int((time.time() - start_time) * 1000)
        await asyncio.to_thread(_record_usage, client_key, model, input_tokens, output_tokens, latency)

        # Background KI distillation (non-blocking)
        if getattr(config, "KI_DISTILLATION_ENABLED", False):
            asyncio.create_task(_maybe_distill(session_id, model))
        yield "data: [DONE]\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream")


# ---------------------------------------------------------------------------
# POST /v1/chat — stateless, simple
# ---------------------------------------------------------------------------

@router.post("/v1/chat")
async def v1_chat(req: Request, body: V1ChatRequest):
    start_time = time.time()

    model = config.resolve_model(body.model)
    client_key = require_api_key(req, model)
    msgs = body.messages

    reply = await run_guarded(lambda: run_messages(model, msgs))

    input_tokens, image_tokens = _count_message_tokens(msgs)
    input_tokens += image_tokens
    output_tokens = await asyncio.to_thread(estimate_tokens, reply)

    latency = int((time.time() - start_time) * 1000)
    await asyncio.to_thread(_record_usage, client_key, model, input_tokens, output_tokens, latency)

    return JSONResponse({
        "model": model,
        "choices": [{"message": {"role": "assistant", "content": reply}}],
    })


# ---------------------------------------------------------------------------
# POST /v1/chat/completions — OpenAI-compatible
# ---------------------------------------------------------------------------

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


@router.post("/v1/chat/completions")
async def openai_completions(req: Request, body: OpenAIChatCompletionsRequest):
    start_time = time.time()

    model = config.resolve_model(body.model)
    client_key = require_api_key(req, model)
    stream = body.stream
    msgs = body.messages
    tools = body.tools

    if tools:
        msgs = inject_tools_and_results(msgs, tools)

    input_tokens, image_tokens = _count_message_tokens(msgs)
    input_tokens += image_tokens

    if stream:
        cid = "chatcmpl-" + uuid.uuid4().hex[:24]
        created = int(time.time())

        async def gen():
            base = {"id": cid, "object": "chat.completion.chunk", "created": created, "model": model}
            output_parts: list[str] = []

            final_finish_reason = "stop"
            if tools:
                # Buffer only if it's a tool call, otherwise stream in real-time
                valid_tool_names = {t["function"]["name"] for t in tools if t.get("type") == "function"}
                interceptor = ToolCallStreamInterceptor(valid_tools=valid_tool_names)
                async for delta in run_guarded_gen(lambda: stream_messages(model, msgs)):
                    output_parts.append(delta)
                    interceptor.feed(delta)
                    for chunk in interceptor.get_passthrough():
                        yield f"data: {json.dumps({**base, **chunk})}\n\n"
                for chunk in interceptor.finish():
                    yield f"data: {json.dumps({**base, **chunk})}\n\n"
                    if chunk.get("choices") and chunk["choices"][0].get("finish_reason"):
                        final_finish_reason = None
            else:
                # Normal streaming — no buffering needed
                async for delta in run_guarded_gen(lambda: stream_messages(model, msgs)):
                    output_parts.append(delta)
                    chunk = {**base, "choices": [{"index": 0, "delta": {"content": delta},
                                                 "finish_reason": None}]}
                    yield f"data: {json.dumps(chunk)}\n\n"

            if final_finish_reason:
                done = {**base, "choices": [{"index": 0, "delta": {}, "finish_reason": final_finish_reason}]}
                yield f"data: {json.dumps(done)}\n\n"
            yield "data: [DONE]\n\n"

            # Count tokens after stream completes
            output_text = "".join(output_parts)
            output_tokens = await asyncio.to_thread(estimate_tokens, output_text)
            latency = int((time.time() - start_time) * 1000)
            await asyncio.to_thread(_record_usage, client_key, model, input_tokens, output_tokens, latency)

        return StreamingResponse(gen(), media_type="text/event-stream")

    reply = await run_guarded(lambda: run_messages(model, msgs))

    output_tokens = await asyncio.to_thread(estimate_tokens, reply)
    latency = int((time.time() - start_time) * 1000)
    await asyncio.to_thread(_record_usage, client_key, model, input_tokens, output_tokens, latency)

    if tools:
        valid_tool_names = {t["function"]["name"] for t in tools if t.get("type") == "function"}
        tool_calls = _extract_tool_calls(reply, valid_tool_names)
        if tool_calls:
            block = _openai_block("", model)
            block["choices"][0]["message"]["tool_calls"] = tool_calls
            block["choices"][0]["message"].pop("content", None)
            block["choices"][0]["finish_reason"] = "tool_calls"
            return JSONResponse(block)

    return JSONResponse(_openai_block(reply, model))
