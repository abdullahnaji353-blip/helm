"""Cloud model providers for Chatbot: Claude and OpenAI-compatible APIs.

Messages use the assistant's internal (Ollama-style) shape:
  {"role": "assistant", "content": str, "tool_calls": [{"id", "function": {"name", "arguments": dict}}]}
  {"role": "tool", "content": str, "tool_name": str, "tool_call_id": str}
  {"role": "user", "content": str, "images": [base64 png]}  # images optional
"""

from __future__ import annotations

import json
import os
import re
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


PROVIDERS = {
    "ollama": {"label": "Local (Ollama)", "env": None},
    "anthropic": {"label": "Claude", "model": "claude-opus-5-5", "env": "ANTHROPIC_API_KEY"},
    "openai": {
        "label": "ChatGPT",
        "model": "gpt-5-mini",
        "env": "OPENAI_API_KEY",
        "url": "https://api.openai.com/v1/chat/completions",
    },
    "gemini": {
        "label": "Gemini",
        "model": "gemini-2.5-flash",
        "env": "GEMINI_API_KEY",
        "url": "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions",
    },
    "openrouter": {
        "label": "OpenRouter",
        "model": "openai/gpt-5-mini",
        "env": "OPENROUTER_API_KEY",
        "url": "https://openrouter.ai/api/v1/chat/completions",
    },
}
# Models that accept the server-side refusal fallback ("default" form).
FALLBACK_MODELS = {"claude-fable-5-1", "claude-opus-5-5", "claude-opus-5", "claude-sonnet-5-5"}


def provider_key(provider: str) -> str:
    """The app passes the Keychain key as CHATBOT_API_KEY; terminals can use the usual env vars."""
    env = PROVIDERS[provider]["env"]
    return os.environ.get("CHATBOT_API_KEY") or (os.environ.get(env, "") if env else "")


def parse_json_object(text: str) -> object:
    """Cloud models may wrap JSON in prose or code fences; take the outermost object."""
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end <= start:
            raise
        return json.loads(text[start:end + 1])


def _plain(messages: list[dict]) -> list[dict]:
    """Turn tool exchanges saved without call ids (older history) into plain text."""
    out = []
    for m in messages:
        calls = m.get("tool_calls") or []
        if m["role"] == "assistant" and calls and not all(c.get("id") for c in calls):
            names = ", ".join(c.get("function", {}).get("name", "?") for c in calls)
            out.append({"role": "assistant", "content": m.get("content") or f"(used {names})"})
        elif m["role"] == "tool" and not m.get("tool_call_id"):
            out.append({"role": "user", "content": f"[tool result] {m['content']}"})
        else:
            out.append(m)
    return out


def to_openai_messages(messages: list[dict]) -> list[dict]:
    out = []
    for m in _plain(messages):
        if m["role"] == "assistant" and m.get("tool_calls"):
            out.append({
                "role": "assistant",
                "content": m.get("content") or None,
                "tool_calls": [{
                    "id": c["id"],
                    "type": "function",
                    "function": {
                        "name": c["function"]["name"],
                        "arguments": json.dumps(c["function"].get("arguments") or {}),
                    },
                } for c in m["tool_calls"]],
            })
        elif m["role"] == "tool":
            out.append({"role": "tool", "tool_call_id": m["tool_call_id"], "content": m["content"]})
        elif m.get("images"):
            out.append({"role": m["role"], "content": [{"type": "text", "text": m["content"]}] + [
                {"type": "image_url", "image_url": {"url": "data:image/png;base64," + image}}
                for image in m["images"]
            ]})
        else:
            out.append({"role": m["role"], "content": m["content"]})
    return out


def to_anthropic_messages(messages: list[dict]) -> tuple[str, list[dict]]:
    system = messages[0]["content"] if messages and messages[0]["role"] == "system" else ""
    out: list[dict] = []
    for m in _plain([m for m in messages if m["role"] != "system"]):
        if m.get("_anthropic") is not None:
            # This turn's raw blocks (thinking, fallback, tool_use) go back unchanged.
            out.append({"role": "assistant", "content": m["_anthropic"]})
        elif m["role"] == "assistant":
            blocks = [{"type": "text", "text": m["content"]}] if m.get("content") else []
            blocks += [{
                "type": "tool_use",
                "id": c["id"],
                "name": c["function"]["name"],
                "input": c["function"].get("arguments") or {},
            } for c in m.get("tool_calls") or []]
            if blocks:
                out.append({"role": "assistant", "content": blocks})
        elif m["role"] == "tool":
            result = {"type": "tool_result", "tool_use_id": m["tool_call_id"], "content": m["content"]}
            previous = out[-1] if out else None
            if (
                previous and previous["role"] == "user" and isinstance(previous["content"], list)
                and previous["content"][0].get("type") == "tool_result"
            ):
                previous["content"].append(result)  # all results of one turn in one message
            else:
                out.append({"role": "user", "content": [result]})
        elif m.get("images"):
            out.append({"role": "user", "content": [
                {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": image}}
                for image in m["images"]
            ] + [{"type": "text", "text": m["content"]}]})
        else:
            out.append({"role": "user", "content": m["content"]})
    return system, out


def anthropic_turn(model: str, key: str, messages: list[dict], tools: list[dict] | None, effort: str = "low") -> dict:
    import anthropic

    client = anthropic.Anthropic(api_key=key) if key else anthropic.Anthropic()
    system, converted = to_anthropic_messages(messages)
    params: dict = {"model": model, "max_tokens": 16000, "system": system, "messages": converted}
    if not re.search(r"claude-3|haiku-4|sonnet-4-5", model):
        params["output_config"] = {"effort": effort}
    if tools:
        params["tools"] = [{
            "name": t["function"]["name"],
            "description": t["function"]["description"],
            "input_schema": t["function"]["parameters"],
        } for t in tools]
    if model in FALLBACK_MODELS:
        params["betas"] = ["server-side-fallback-2026-07-01"]
        params["fallbacks"] = "default"
    try:
        response = client.beta.messages.create(**params)
    except anthropic.AuthenticationError as error:
        raise RuntimeError("Claude rejected the API key. Update it in Chatbot's settings.") from error
    except anthropic.RateLimitError as error:
        raise RuntimeError("Claude's rate limit was reached. Try again in a moment.") from error
    except anthropic.APIStatusError as error:
        raise RuntimeError(f"Claude API error {error.status_code}: {error.message}") from error
    except anthropic.APIConnectionError as error:
        raise RuntimeError("Could not reach Claude. Check your internet connection.") from error
    if response.stop_reason == "refusal":
        raise RuntimeError("Claude declined this request.")
    return {
        "content": "".join(b.text for b in response.content if b.type == "text"),
        "tool_calls": [
            {"id": b.id, "function": {"name": b.name, "arguments": b.input}}
            for b in response.content if b.type == "tool_use"
        ],
        "_anthropic": response.content,
    }


def openai_turn(provider: str, model: str, key: str, messages: list[dict], tools: list[dict] | None) -> dict:
    label = PROVIDERS[provider]["label"]
    if not key:
        raise RuntimeError(f"Add your {label} API key in Chatbot's settings.")
    body: dict = {"model": model, "messages": to_openai_messages(messages)}
    if tools:
        body["tools"] = tools
    request = Request(
        PROVIDERS[provider]["url"],
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=180) as response:
            result = json.loads(response.read().decode("utf-8"))
    except HTTPError as error:
        if error.code in {401, 403}:
            raise RuntimeError(f"{label} rejected the API key. Update it in Chatbot's settings.") from error
        detail = error.read().decode("utf-8", errors="replace").strip()[:300]
        raise RuntimeError(f"{label} returned HTTP {error.code}: {detail}") from error
    except (URLError, TimeoutError) as error:
        raise RuntimeError(f"Could not reach {label}. Check your internet connection.") from error
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        raise RuntimeError(f"{label} returned an invalid response.") from error
    try:
        message = result["choices"][0]["message"]
    except (KeyError, IndexError, TypeError) as error:
        raise RuntimeError(f"{label} returned an unexpected response.") from error
    calls = []
    for call in message.get("tool_calls") or []:
        try:
            arguments = json.loads(call["function"].get("arguments") or "{}")
        except json.JSONDecodeError:
            arguments = {}
        calls.append({"id": call["id"], "function": {"name": call["function"]["name"], "arguments": arguments}})
    return {"content": message.get("content") or "", "tool_calls": calls}
