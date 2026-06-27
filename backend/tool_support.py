"""
Tool Calling support for OpenAI-compatible API.

Strategy:
  1. Strict system prompt forces the AI to output ONLY raw JSON when it wants
     to call a tool — no preamble, no markdown fences.
  2. When tools are present, we ALWAYS buffer the full response first (no
     partial streaming) so we can reliably decide: tool-call JSON or plain text.
  3. If the full response contains a valid tool_calls JSON anywhere (even buried
     in preamble text), we extract it with regex.  This handles the hallucination
     case where the AI writes "Sure! Let me read that file: {…}".
  4. Once decided, we replay the result back to the caller in proper OpenAI
     chunk format (streaming) or as a single block (non-streaming).
"""
import json
import re
import uuid

# ---------------------------------------------------------------------------
# 1. System prompt generation
# ---------------------------------------------------------------------------

_TOOL_SYSTEM_TEMPLATE = """\
You have access to the following tools.  When you decide to use one or more \
tools, you MUST respond with **ONLY** a single raw JSON object — no \
explanation, no markdown fences, no text before or after.  The JSON must \
match this schema exactly:

{{"tool_calls": [{{"id": "call_<random_id>", "type": "function", "function": {{"name": "<tool_name>", "arguments": "<json_string_of_args>"}}}}]}}

CRITICAL RULES:
- The "arguments" value MUST be a JSON-encoded STRING, not a raw object.
- Output NOTHING except the JSON object when calling a tool.
- If you do NOT need a tool, reply normally in natural language.
- NEVER wrap the JSON in ```json``` or any markdown code block.
- You can call MULTIPLE tools at once by adding more items to the "tool_calls" array.

THOROUGHNESS RULES:
- When asked to analyze, debug, or work with a codebase, you MUST read ALL \
relevant files before giving your answer — not just 1 or 2.
- If a project has config files, entry points, utilities, and sub-modules, \
read them ALL.  Do NOT guess or summarize from partial information.
- Prefer calling multiple read operations in a SINGLE tool_calls response \
to minimize round-trips.
- If you are unsure whether a file is relevant, READ IT.  It is always \
better to read too much than too little.

ANTI-HALLUCINATION RULES:
- You do NOT have any subagents or assistants.
- You MUST perform all file reading yourself by explicitly calling the tools.
- Do NOT claim to have explored the codebase if you did not explicitly call the read tools.
- Do NOT hallucinate file contents or summarize without reading.

Available tools:
{tool_list}"""


def build_tool_system_prompt(tools: list) -> str:
    if not tools:
        return ""
    lines = []
    for t in tools:
        if t.get("type") != "function":
            continue
        f = t.get("function", {})
        name = f.get("name", "?")
        desc = f.get("description", "")
        params = json.dumps(f.get("parameters", {}), ensure_ascii=False)
        lines.append(f"- **{name}**: {desc}\n  Parameters: {params}")
    return _TOOL_SYSTEM_TEMPLATE.format(tool_list="\n".join(lines))


# ---------------------------------------------------------------------------
# 2. Message pre-processing (inject prompt, convert tool results)
# ---------------------------------------------------------------------------

def inject_tools_and_results(msgs: list, tools: list) -> list:
    """
    1. Prepend (or merge with existing) system prompt that teaches tool usage.
    2. Convert role="tool" messages into role="user" so the plain-text backend
       can forward them.
    3. Convert role="assistant" messages that contain tool_calls into the
       text representation the AI originally produced.
    """
    if not tools:
        return msgs

    tool_prompt = build_tool_system_prompt(tools)
    new_msgs: list[dict] = []

    # Check if first message is already a system prompt — merge if so
    start = 0
    if msgs and msgs[0].get("role") == "system":
        merged = msgs[0]["content"] + "\n\n" + tool_prompt
        new_msgs.append({"role": "system", "content": merged})
        start = 1
    else:
        new_msgs.append({"role": "system", "content": tool_prompt})

    for m in msgs[start:]:
        role = m.get("role", "user")

        if role == "tool":
            # IDE is returning a tool execution result
            content = m.get("content", "")
            tid = m.get("tool_call_id", "?")
            new_msgs.append({
                "role": "user",
                "content": f"[Tool Result id={tid}]\n{content}"
            })

        elif role == "assistant" and m.get("tool_calls"):
            # Previous assistant turn that contained tool calls —
            # reconstruct the JSON the AI would have emitted
            tc_json = json.dumps({"tool_calls": m["tool_calls"]}, ensure_ascii=False)
            new_msgs.append({"role": "assistant", "content": tc_json})

        else:
            new_msgs.append(m)

    return new_msgs


# ---------------------------------------------------------------------------
# 3. Response parsing — extract tool calls from AI text
# ---------------------------------------------------------------------------

# Regex: find a JSON object containing "tool_calls" anywhere in the text
_TOOL_JSON_RE = re.compile(
    r'\{\s*"tool_calls"\s*:\s*\[.*?\]\s*\}',
    re.DOTALL,
)


def _extract_tool_calls(text: str) -> list[dict] | None:
    """
    Scan *text* for a JSON blob matching {"tool_calls": [...]}.
    Returns the formatted list of tool calls, or None if not found.
    """
    # Step 1: strip markdown fences if present
    cleaned = text.strip()
    if cleaned.startswith("```"):
        # Remove opening fence (```json or ```)
        first_nl = cleaned.find("\n")
        if first_nl != -1:
            cleaned = cleaned[first_nl + 1:]
        if cleaned.endswith("```"):
            cleaned = cleaned[:-3]
        cleaned = cleaned.strip()

    # Step 2: try to parse the whole thing as JSON first (fast path)
    try:
        data = json.loads(cleaned)
        if "tool_calls" in data:
            return _format_tool_calls(data["tool_calls"])
    except (json.JSONDecodeError, ValueError):
        pass

    # Step 3: regex scan for {"tool_calls": [...]} buried in preamble text
    match = _TOOL_JSON_RE.search(text)
    if match:
        try:
            data = json.loads(match.group(0))
            return _format_tool_calls(data.get("tool_calls", []))
        except (json.JSONDecodeError, ValueError):
            pass

    return None


def _format_tool_calls(raw_calls: list) -> list[dict]:
    """Normalise tool calls into OpenAI format."""
    out = []
    for i, tc in enumerate(raw_calls):
        func = tc.get("function", {})
        args = func.get("arguments", "")
        # If AI returned arguments as a dict instead of a string, fix it
        if isinstance(args, dict):
            args = json.dumps(args, ensure_ascii=False)
        out.append({
            "index": i,
            "id": tc.get("id", f"call_{uuid.uuid4().hex[:12]}"),
            "type": "function",
            "function": {
                "name": func.get("name", ""),
                "arguments": args,
            },
        })
    return out


# ---------------------------------------------------------------------------
# 4. Stream interceptor
# ---------------------------------------------------------------------------

class ToolCallStreamInterceptor:
    """
    When tools are present, Cursor/Cline ALWAYS sends `tools` in every request.
    If we blindly buffer everything, normal text responses will freeze the IDE
    until the 500-line script is fully generated.

    This interceptor buffers only the beginning. If the response looks like a
    tool JSON (starts with '{' or '```json'), it buffers the whole thing and
    emits a tool_call chunk at the end.
    If it's normal text, it switches to passthrough mode and yields text
    immediately for a real-time typing effect.
    """

    def __init__(self):
        self.buffer = ""
        self.mode = "inspecting"  # 'inspecting', 'buffering', 'passthrough'
        self.passthrough_queue = []

    def feed(self, delta: str) -> None:
        if self.mode == "passthrough":
            self.passthrough_queue.append(delta)
            return

        self.buffer += delta
        
        # We need a few characters to decide
        if len(self.buffer.strip()) >= 5:
            if self.buffer.strip().startswith("{") or self.buffer.strip().startswith("```"):
                self.mode = "buffering"
            else:
                self.mode = "passthrough"
                self.passthrough_queue.append(self.buffer)
                self.buffer = ""

    def get_passthrough(self) -> list[dict]:
        """Call this in the loop to yield any text that is safe to stream."""
        chunks = []
        for text in self.passthrough_queue:
            if text:
                chunks.append({
                    "choices": [{
                        "index": 0,
                        "delta": {"content": text},
                        "finish_reason": None,
                    }]
                })
        self.passthrough_queue.clear()
        return chunks

    def finish(self) -> list[dict]:
        """
        Called when the upstream stream is done.
        """
        chunks = self.get_passthrough()
        
        if self.mode == "buffering" and self.buffer:
            tool_calls = _extract_tool_calls(self.buffer)
            if tool_calls:
                chunks.append({
                    "choices": [{
                        "index": 0,
                        "delta": {"tool_calls": tool_calls},
                        "finish_reason": "tool_calls",
                    }]
                })
            else:
                # False alarm, was just a JSON-like text block, flush as text
                chunks.append({
                    "choices": [{
                        "index": 0,
                        "delta": {"content": self.buffer},
                        "finish_reason": None,
                    }]
                })
        elif self.mode == "inspecting" and self.buffer:
            # Stream ended before we got 5 chars, just flush as text
            chunks.append({
                "choices": [{
                    "index": 0,
                    "delta": {"content": self.buffer},
                    "finish_reason": None,
                }]
            })
            
        return chunks

