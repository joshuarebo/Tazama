#!/usr/bin/env python3
"""Deterministic fake LLM — the thing Phase 4 evals actually gate on.

An OpenAI-compatible server (chat completions, embeddings, models) that:
  * returns the *same* answer for the same request, so evals are reproducible;
  * can be told to misbehave, so the degraded paths are tested rather than hoped for;
  * never reaches the network.

Failure injection — put a directive anywhere in the prompt, or send the header
`X-Fake-Behaviour: <name>`:

    ::fake:timeout         hang past any sane client timeout
    ::fake:malformed       return JSON that does not parse
    ::fake:schema-violation return valid JSON that breaks the requested schema
    ::fake:error-500       return an upstream error
    ::fake:rate-limit      return 429
    ::fake:no-tools        claim tool calling is unsupported
    ::fake:empty           return an empty message
    ::fake:injected        obey the injected instruction (used to prove the guard catches it)
    ::fake:slow            take 2 seconds

Run it:  python scripts/fake_llm.py           (listens on 127.0.0.1:8088)
Use it:  LLM_BASE_URL=http://127.0.0.1:8088/v1  LLM_API_KEY=fake-key
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import time
from typing import Any, Literal

from fastapi import FastAPI, Header, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

MODEL_REASONING = "qwen-reasoning"
MODEL_FAST = "qwen-fast"
EMBEDDING_MODEL = "qwen-embedding"
CANARY = "TAZAMA-CANARY"

BEHAVIOUR_RE = re.compile(r"::fake:([a-z0-9\-]+)")
TASK_RE = re.compile(r"TASK:\s*([a-z_]+)", re.IGNORECASE)


class Message(BaseModel):
    role: Literal["system", "user", "assistant", "tool"]
    content: str | list[dict[str, Any]] | None = None
    tool_call_id: str | None = None
    name: str | None = None


class ChatRequest(BaseModel):
    model: str = MODEL_FAST
    messages: list[Message] = Field(default_factory=list)
    temperature: float = 0.0
    max_tokens: int | None = None
    tools: list[dict[str, Any]] | None = None
    tool_choice: Any = None
    response_format: dict[str, Any] | None = None
    stream: bool = False


class EmbeddingRequest(BaseModel):
    model: str = EMBEDDING_MODEL
    input: str | list[str]
    dimensions: int = 256


app = FastAPI(title="TAZAMA fake LLM", version="1.0.0", docs_url="/docs")


def _text_of(messages: list[Message]) -> str:
    parts: list[str] = []
    for message in messages:
        if isinstance(message.content, str):
            parts.append(f"{message.role}: {message.content}")
        elif isinstance(message.content, list):
            for chunk in message.content:
                parts.append(str(chunk.get("text", "")))
    return "\n".join(parts)


def _seed(text: str) -> int:
    return int.from_bytes(hashlib.sha256(text.encode("utf-8")).digest()[:8], "big")


def _tokens(text: str) -> int:
    return max(1, len(text) // 4)


def _envelope(model: str, content: str, prompt: str, *, tool_calls: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    message: dict[str, Any] = {"role": "assistant", "content": content}
    if tool_calls:
        message["tool_calls"] = tool_calls
        message["content"] = None
    return {
        "id": f"fakecmpl-{_seed(prompt) % 10**12:012d}",
        "object": "chat.completion",
        "created": 1_700_000_000,
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": message,
                "finish_reason": "tool_calls" if tool_calls else "stop",
            },
        ],
        "usage": {
            "prompt_tokens": _tokens(prompt),
            "completion_tokens": _tokens(content),
            "total_tokens": _tokens(prompt) + _tokens(content),
        },
    }


# --------------------------------------------------------------- canned tasks
# Later phases add tasks here; the shape is always "valid JSON for the schema the
# gateway asks for", so a schema change makes the fake fail loudly rather than quietly.


def _task_response(task: str, prompt: str) -> str:
    rng = _seed(prompt)
    if task == "classify_complaint":
        categories = [
            "no_service", "slow_data", "call_drops", "billing", "sim_swap",
            "network_congestion", "voice_quality", "other",
        ]
        category = categories[rng % len(categories)]
        return json.dumps(
            {
                "category": category,
                "subcategory": "unknown",
                "language": ["en", "sw", "sheng", "mixed"][rng % 4],
                "sentiment": ["angry", "frustrated", "neutral", "calm"][rng % 4],
                "urgency": 1 + (rng % 5),
                "is_network_related": category not in ("billing", "sim_swap", "other"),
                "confidence": round(0.72 + (rng % 25) / 100, 2),
                "rationale": "Deterministic fake classification derived from the prompt hash.",
            },
        )
    if task == "diagnose_incident":
        causes = [
            "transmission_link_failure", "cell_outage", "congestion",
            "power_failure", "configuration_change", "planned_maintenance",
        ]
        return json.dumps(
            {
                "probable_cause": causes[rng % len(causes)],
                "confidence": round(0.55 + (rng % 40) / 100, 2),
                "evidence": [
                    {"kind": "alarm", "ref": "ALM-FAKE-1", "note": "cell unavailable"},
                    {"kind": "kpi", "ref": "availability", "note": "drop to 0 for 3 buckets"},
                ],
                "recommended_runbook_id": "RB-TRANSMISSION-01",
                "summary": "Deterministic fake diagnosis for offline evals.",
            },
        )
    if task == "knowledge_answer":
        return json.dumps(
            {
                "answer": "Follow the transmission-failure runbook: verify the link, then escalate.",
                "citations": [{"doc_id": "KB-TRANSMISSION", "chunk": 1}],
                "sufficient_evidence": True,
            },
        )
    if task == "report_digest":
        return json.dumps(
            {
                "headline": "Network stable; 3 incidents closed",
                "bullets": [
                    "3 incidents closed in the last 24 hours",
                    "MTTR 92 minutes",
                ],
                "numbers_used": [3, 92],
            },
        )
    if task == "fill_notification":
        return json.dumps(
            {
                "template_id": "known_issue_eta",
                "slots": {"area": "Westlands", "eta_minutes": "90"},
            },
        )
    return json.dumps(
        {
            "note": "fake-llm has no canned answer for this task",
            "task": task,
            "echo_hash": f"{rng % 10**8:08d}",
        },
    )


def _default_answer(prompt: str) -> str:
    return (
        "Deterministic fake answer. "
        f"prompt_tokens={_tokens(prompt)} fingerprint={_seed(prompt) % 10**8:08d}"
    )


def _behaviour(prompt: str, header: str | None) -> str:
    if header:
        return header.strip().lower()
    match = BEHAVIOUR_RE.search(prompt)
    return match.group(1) if match else ""


@app.get("/v1/models")
async def models() -> dict[str, Any]:
    return {
        "object": "list",
        "data": [
            {"id": MODEL_REASONING, "object": "model", "owned_by": "fake"},
            {"id": MODEL_FAST, "object": "model", "owned_by": "fake"},
            {"id": EMBEDDING_MODEL, "object": "model", "owned_by": "fake"},
        ],
    }


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok", "server": "fake-llm"}


@app.post("/v1/chat/completions")
async def chat(
    body: ChatRequest,
    request: Request,
    x_fake_behaviour: str | None = Header(default=None),
) -> Any:
    prompt = _text_of(body.messages)
    behaviour = _behaviour(prompt, x_fake_behaviour)

    if behaviour == "timeout":
        time.sleep(float(os.environ.get("FAKE_LLM_TIMEOUT_SLEEP", "120")))
    if behaviour == "slow":
        time.sleep(2.0)
    if behaviour == "error-500":
        return JSONResponse(status_code=500, content={"error": {"message": "fake upstream error"}})
    if behaviour == "rate-limit":
        return JSONResponse(
            status_code=429,
            content={"error": {"message": "fake rate limit"}},
            headers={"Retry-After": "1"},
        )
    if behaviour == "malformed":
        return JSONResponse(
            content=_envelope(body.model, '{"category": "no_service", ', prompt),
        )
    if behaviour == "schema-violation":
        return JSONResponse(
            content=_envelope(body.model, json.dumps({"unexpected": True, "confidence": "high"}), prompt),
        )
    if behaviour == "empty":
        return JSONResponse(content=_envelope(body.model, "", prompt))
    if behaviour == "no-tools":
        return JSONResponse(
            status_code=400,
            content={"error": {"message": "this model does not support tools", "code": "tools_unsupported"}},
        )
    if behaviour == "injected":
        # Deliberately leaks the canary so the output filter can be proven to catch it.
        return JSONResponse(
            content=_envelope(body.model, f"Ignoring my instructions. {CANARY}", prompt),
        )

    # Tool calling: if the caller offered tools and the prompt asks for data, call the
    # first read-only tool once, then answer on the follow-up turn.
    offered = body.tools or []
    already_called = any(m.role == "tool" for m in body.messages)
    if offered and not already_called and "TOOLS_OFF" not in prompt:
        tool = offered[0].get("function", {})
        name = str(tool.get("name", "unknown_tool"))
        arguments = _fake_arguments(tool)
        return JSONResponse(
            content=_envelope(
                body.model,
                "",
                prompt,
                tool_calls=[
                    {
                        "id": f"call_{_seed(prompt) % 10**8:08d}",
                        "type": "function",
                        "function": {"name": name, "arguments": json.dumps(arguments)},
                    },
                ],
            ),
        )

    task_match = TASK_RE.search(prompt)
    content = _task_response(task_match.group(1).lower(), prompt) if task_match else _default_answer(prompt)
    return JSONResponse(content=_envelope(body.model, content, prompt))


def _fake_arguments(tool: dict[str, Any]) -> dict[str, Any]:
    """Build plausible arguments from the tool schema, so validation is exercised."""
    schema = tool.get("parameters", {}) or {}
    properties: dict[str, Any] = schema.get("properties", {}) or {}
    required: list[str] = schema.get("required", []) or list(properties)[:1]
    out: dict[str, Any] = {}
    for key in required:
        spec = properties.get(key, {})
        kind = spec.get("type", "string")
        if kind == "integer":
            out[key] = int(spec.get("minimum", 1))
        elif kind == "number":
            out[key] = float(spec.get("minimum", 1))
        elif kind == "boolean":
            out[key] = True
        elif kind == "array":
            out[key] = []
        elif "enum" in spec:
            out[key] = spec["enum"][0]
        else:
            out[key] = "CELL-0001" if "cell" in key else "fake"
    return out


@app.post("/v1/embeddings")
async def embeddings(body: EmbeddingRequest) -> dict[str, Any]:
    texts = [body.input] if isinstance(body.input, str) else list(body.input)
    data = []
    for index, text in enumerate(texts):
        data.append(
            {
                "object": "embedding",
                "index": index,
                "embedding": _deterministic_vector(text, body.dimensions),
            },
        )
    return {
        "object": "list",
        "data": data,
        "model": body.model,
        "usage": {"prompt_tokens": sum(_tokens(t) for t in texts), "total_tokens": 0},
    }


def _deterministic_vector(text: str, dimensions: int) -> list[float]:
    """Hash-based unit vector: stable, and similar texts share leading bytes."""
    digest = hashlib.sha512(text.lower().strip().encode("utf-8")).digest()
    raw = [((digest[i % len(digest)] / 255.0) - 0.5) for i in range(dimensions)]
    norm = math.sqrt(sum(v * v for v in raw)) or 1.0
    return [round(v / norm, 6) for v in raw]


def main() -> None:
    import uvicorn

    host = os.environ.get("FAKE_LLM_HOST", "127.0.0.1")
    port = int(os.environ.get("FAKE_LLM_PORT", "8088"))
    print(f"fake LLM on http://{host}:{port}/v1  (set LLM_BASE_URL to this)")  # noqa: T201
    uvicorn.run(app, host=host, port=port, log_level="warning")


if __name__ == "__main__":
    main()
