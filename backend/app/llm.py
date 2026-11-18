"""Optional LLM access (Anthropic Messages API over httpx). Everything has a deterministic fallback,
so the app works with no key; with a key, policy reading and chat understanding get better."""
from __future__ import annotations
import json, os, re
import httpx

MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-6")


def enabled() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY"))


def complete(system: str, user: str, max_tokens: int = 4000, timeout: float = 90.0) -> str:
    r = httpx.post("https://api.anthropic.com/v1/messages", timeout=timeout,
                   headers={"x-api-key": os.environ["ANTHROPIC_API_KEY"], "anthropic-version": "2023-06-01", "content-type": "application/json"},
                   json={"model": MODEL, "max_tokens": max_tokens, "system": system, "messages": [{"role": "user", "content": user}]})
    r.raise_for_status()
    return "".join(b.get("text", "") for b in r.json()["content"])


def complete_json(system: str, user: str, **kw):
    txt = complete(system + "\nReply with ONE JSON object only, no prose, no markdown fences.", user, **kw)
    txt = re.sub(r"^```(?:json)?|```$", "", txt.strip(), flags=re.M).strip()
    a, b = txt.find("{"), txt.rfind("}")
    return json.loads(txt[a:b + 1])
