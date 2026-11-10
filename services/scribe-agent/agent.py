"""Agent loop that turns an encounter transcript into a cited SOAP note.

Two model providers are supported, selected with LLM_PROVIDER:

- anthropic: Claude through the Anthropic API, authenticated with ANTHROPIC_API_KEY.
- openai-compatible: any OpenAI-compatible endpoint, such as the optional vLLM
  server that runs in the hospital account.

Both providers read the chart through the FHIR MCP server. Only the read tools in
READ_TOOLS are offered to the model; chart writes happen in app.py after a
clinician approves the note.
"""

import json
import os
import time
from contextlib import asynccontextmanager
from typing import Awaitable, Callable

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

LLM_PROVIDER = os.environ.get("LLM_PROVIDER", "anthropic")
MCP_URL = os.environ.get("MCP_URL", "http://localhost:8000/mcp")
DEFAULT_MODELS = {"anthropic": "claude-opus-5-5", "openai-compatible": "Qwen/Qwen2.5-7B-Instruct"}
LLM_MODEL = os.environ.get("LLM_MODEL") or DEFAULT_MODELS.get(LLM_PROVIDER, "")
LLM_MAX_TOKENS = int(os.environ.get("LLM_MAX_TOKENS", "16000"))
MAX_STEPS = int(os.environ.get("AGENT_MAX_STEPS", "8"))

# openai-compatible only. Current Claude models reject sampling parameters.
LLM_TEMPERATURE = float(os.environ.get("LLM_TEMPERATURE", "0.2"))
# anthropic only: low, medium, high, xhigh or max.
CLAUDE_EFFORT = os.environ.get("CLAUDE_EFFORT", "medium")
# anthropic only: route a declined request to a fallback model chosen by the API.
CLAUDE_FALLBACKS = os.environ.get("CLAUDE_FALLBACKS", "true").lower() == "true"

READ_TOOLS = {
    "get_patient",
    "get_conditions",
    "get_medications",
    "get_allergies",
    "get_observations",
    "get_encounters",
}

SYSTEM_PROMPT = """You are a clinical documentation assistant working inside a hospital.
You draft notes for a licensed clinician, who reviews and signs every note.

Steps:
1. Use the tools to read the patient's demographics, active conditions, active medications,
   allergies and recent laboratory results and vital signs.
2. Write a SOAP note in Markdown using only facts from the transcript and the chart.

Rules:
- Cite every chart fact with its FHIR reference in square brackets, for example [Condition/123].
- Cite facts that come from the conversation with [transcript].
- Do not invent findings, doses or results. If something is missing, write "not documented".
- After the Plan section, add a section "## Flags for clinician review" that lists drug interactions,
  allergy conflicts, abnormal results and chart discrepancies with the conversation.
  Write "None identified." if there are none.

Output only the note, with these sections in order:
## Subjective
## Objective
## Assessment
## Plan
## Flags for clinician review
"""

ToolCallback = Callable[[dict], Awaitable[None]] | None


@asynccontextmanager
async def mcp_session():
    async with streamablehttp_client(MCP_URL) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            yield session


def _result_text(result) -> str:
    return "\n".join(part.text for part in result.content if getattr(part, "type", "") == "text")


async def call_tool(name: str, arguments: dict) -> str:
    async with mcp_session() as session:
        result = await session.call_tool(name, arguments)
        text = _result_text(result)
        if result.isError:
            raise RuntimeError(f"{name} failed: {text}")
        return text


def _user_prompt(patient_id: str, transcript: str) -> str:
    return f"Patient id: {patient_id}\n\nEncounter transcript:\n\"\"\"\n{transcript}\n\"\"\""


async def _run_tool(session: ClientSession, name: str, args: dict, log: list, on_tool_call: ToolCallback):
    """Run one read tool through MCP. Returns (output text, is_error)."""
    started = time.monotonic()
    is_error = False
    try:
        if name not in READ_TOOLS:
            raise PermissionError(f"tool {name} is not allowed for the model")
        result = await session.call_tool(name, args)
        output = _result_text(result)
        is_error = bool(result.isError)
    except Exception as exc:  # the model sees the error and can recover
        output = json.dumps({"error": str(exc)})
        is_error = True
    entry = {
        "tool": name,
        "arguments": args,
        "duration_ms": int((time.monotonic() - started) * 1000),
        "result_preview": output[:400],
    }
    log.append(entry)
    if on_tool_call:
        await on_tool_call(entry)
    return output, is_error


async def _list_read_tools(session: ClientSession):
    listed = await session.list_tools()
    return [t for t in listed.tools if t.name in READ_TOOLS]


async def _draft_with_claude(session, patient_id, transcript, on_tool_call):
    import anthropic

    client = anthropic.AsyncAnthropic()
    tools = [
        {"name": t.name, "description": t.description or "", "input_schema": t.inputSchema}
        for t in await _list_read_tools(session)
    ]
    messages: list = [{"role": "user", "content": _user_prompt(patient_id, transcript)}]
    log: list[dict] = []
    extra = {"betas": ["server-side-fallback-2026-07-01"], "fallbacks": "default"} if CLAUDE_FALLBACKS else {}

    for _ in range(MAX_STEPS):
        response = await client.beta.messages.create(
            model=LLM_MODEL,
            max_tokens=LLM_MAX_TOKENS,
            system=SYSTEM_PROMPT,
            tools=tools,
            messages=messages,
            thinking={"type": "adaptive"},
            output_config={"effort": CLAUDE_EFFORT},
            **extra,
        )
        if response.stop_reason == "refusal":
            category = response.stop_details.category if response.stop_details else None
            raise RuntimeError(f"the model declined this request (category: {category})")
        if response.stop_reason == "max_tokens":
            raise RuntimeError("the note exceeded LLM_MAX_TOKENS; raise the limit and retry")

        # Keep the full content, including thinking blocks, so the next turn is valid.
        messages.append({"role": "assistant", "content": response.content})
        if response.stop_reason == "pause_turn":
            continue

        tool_uses = [b for b in response.content if b.type == "tool_use"]
        if not tool_uses:
            text = "\n".join(b.text for b in response.content if b.type == "text").strip()
            return text, log

        results = []
        for block in tool_uses:
            output, is_error = await _run_tool(session, block.name, dict(block.input), log, on_tool_call)
            results.append({"type": "tool_result", "tool_use_id": block.id, "content": output, "is_error": is_error})
        # All results for one turn go back in a single user message.
        messages.append({"role": "user", "content": results})

    raise RuntimeError(f"the agent did not finish within {MAX_STEPS} steps")


async def _draft_with_openai_compatible(session, patient_id, transcript, on_tool_call):
    from openai import AsyncOpenAI

    client = AsyncOpenAI(
        base_url=os.environ.get("LLM_BASE_URL", "http://localhost:8001/v1"),
        api_key=os.environ.get("LLM_API_KEY") or "not-needed",
    )
    tools = [
        {"type": "function", "function": {"name": t.name, "description": t.description or "", "parameters": t.inputSchema}}
        for t in await _list_read_tools(session)
    ]
    messages: list = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": _user_prompt(patient_id, transcript)},
    ]
    log: list[dict] = []

    for _ in range(MAX_STEPS):
        response = await client.chat.completions.create(
            model=LLM_MODEL,
            messages=messages,
            tools=tools,
            tool_choice="auto",
            temperature=LLM_TEMPERATURE,
            max_tokens=LLM_MAX_TOKENS,
        )
        message = response.choices[0].message
        if not message.tool_calls:
            return (message.content or "").strip(), log

        messages.append(message.model_dump(exclude_none=True))
        for call in message.tool_calls:
            try:
                args = json.loads(call.function.arguments or "{}")
            except json.JSONDecodeError as exc:
                output = json.dumps({"error": f"invalid JSON arguments: {exc}"})
            else:
                output, _ = await _run_tool(session, call.function.name, args, log, on_tool_call)
            messages.append({"role": "tool", "tool_call_id": call.id, "content": output})

    messages.append({"role": "user", "content": "Stop calling tools and write the note now."})
    response = await client.chat.completions.create(
        model=LLM_MODEL, messages=messages, temperature=LLM_TEMPERATURE, max_tokens=LLM_MAX_TOKENS
    )
    return (response.choices[0].message.content or "").strip(), log


async def draft_note(patient_id: str, transcript: str, on_tool_call: ToolCallback = None) -> tuple[str, list[dict]]:
    """Run the agent loop. Returns the note and the list of tool calls the model made."""
    async with mcp_session() as session:
        if LLM_PROVIDER == "anthropic":
            return await _draft_with_claude(session, patient_id, transcript, on_tool_call)
        if LLM_PROVIDER == "openai-compatible":
            return await _draft_with_openai_compatible(session, patient_id, transcript, on_tool_call)
        raise ValueError(f"unknown LLM_PROVIDER {LLM_PROVIDER!r}; use anthropic or openai-compatible")
