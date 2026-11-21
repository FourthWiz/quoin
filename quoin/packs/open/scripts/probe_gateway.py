#!/usr/bin/env python3
"""probe_gateway.py - check an Anthropic-compatible gateway before a run.

Separates protocol failures from task failures: before blaming a model for a
bad run, confirm the gateway answers, knows the configured models, and returns
a well-formed tool call. Three checks, each reported on its own:

  models   the gateway's model list names every configured model (and, where
           the list says so, that it supports tools); also reports context
  message  one tiny Messages API request to the main model succeeds
  tools    the main model answers a forced tool-use request with a tool_use
           block whose input parses and matches the tool's schema

Usage
  python3 probe_gateway.py [--profile FILE] [--skip-tools]

The profile is the JSON `quoin open setup` writes (default
~/.config/quoin/open.json). The API key is read from the environment variable
the profile names; it is never printed. Costs a fraction of a cent.
Prints one JSON object; exit 0 when every check passes, 1 otherwise.
Standard library only.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

DEFAULT_PROFILE = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "quoin" / "open.json"
TIMEOUT_S = 60
TOOL = {
    "name": "record_number",
    "description": "Record a number.",
    "input_schema": {"type": "object", "properties": {"value": {"type": "integer"}}, "required": ["value"]},
}


def _request(url: str, key: str, body: dict | None = None, *, anthropic: bool = True) -> tuple[int, dict | str]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method="POST" if body is not None else "GET")
    req.add_header("Authorization", f"Bearer {key}")
    if anthropic:
        req.add_header("x-api-key", key)
        req.add_header("anthropic-version", "2023-06-01")
    if body is not None:
        req.add_header("content-type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:
            raw = resp.read().decode("utf-8", "replace")
            status = resp.status
    except urllib.error.HTTPError as exc:
        raw, status = exc.read().decode("utf-8", "replace"), exc.code
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return 0, str(exc)
    try:
        return status, json.loads(raw)
    except ValueError:
        return status, raw[:300]


def check_models(base: str, key: str, models: dict) -> dict:
    # Without the Anthropic headers a gateway such as OpenRouter returns its own
    # full list (context and tool support included) rather than a paged
    # Anthropic-style list with prefixed ids.
    status, data = _request(f"{base.rstrip('/')}/v1/models", key, anthropic=False)
    if status != 200 or not isinstance(data, dict):
        return {"ok": False, "detail": f"model list returned {status}: {str(data)[:200]}"}
    listed = {m.get("id"): m for m in data.get("data", []) if isinstance(m, dict)}
    report, ok = {}, True
    for tier, model in models.items():
        entry = listed.get(model)
        if entry is None:
            report[tier] = f"{model}: not in the gateway's model list"
            ok = False
            continue
        params = entry.get("supported_parameters")
        tools = "tools" in params if isinstance(params, list) else None
        if tools is False:
            ok = False
        report[tier] = {"model": model, "context": entry.get("context_length"), "tools": tools}
    return {"ok": ok, "detail": report}


def check_message(base: str, key: str, model: str) -> dict:
    body = {"model": model, "max_tokens": 16, "messages": [{"role": "user", "content": "Reply with the word ready."}]}
    status, data = _request(f"{base.rstrip('/')}/v1/messages", key, body)
    if status != 200 or not isinstance(data, dict) or data.get("type") == "error":
        return {"ok": False, "detail": f"messages returned {status}: {str(data)[:200]}"}
    text = "".join(b.get("text", "") for b in data.get("content", []) if isinstance(b, dict))
    return {"ok": bool(text.strip()), "detail": text.strip()[:60] or "empty reply"}


def check_tools(base: str, key: str, model: str) -> dict:
    body = {
        "model": model,
        "max_tokens": 200,
        "tools": [TOOL],
        "tool_choice": {"type": "tool", "name": TOOL["name"]},
        "messages": [{"role": "user", "content": "Record the number 42."}],
    }
    status, data = _request(f"{base.rstrip('/')}/v1/messages", key, body)
    if status != 200 or not isinstance(data, dict):
        return {"ok": False, "detail": f"tool request returned {status}: {str(data)[:200]}"}
    calls = [b for b in data.get("content", []) if isinstance(b, dict) and b.get("type") == "tool_use"]
    if not calls:
        return {"ok": False, "detail": "no tool_use block in the reply"}
    arguments = calls[0].get("input")
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except ValueError:
            return {"ok": False, "detail": "tool input is not valid JSON"}
    good = isinstance(arguments, dict) and isinstance(arguments.get("value"), int)
    return {"ok": good, "detail": {"tool": calls[0].get("name"), "input": arguments}}


def probe(profile: dict, *, skip_tools: bool = False) -> dict:
    key_env = profile.get("key_env", "OPENROUTER_API_KEY")
    key = os.environ.get(key_env, "")
    if not key:
        return {"ok": False, "checks": {"key": {"ok": False, "detail": f"{key_env} is not set"}}}
    base = profile["base_url"]
    models = profile["models"]
    main_model = models.get("sonnet") or next(iter(models.values()))
    checks = {"models": check_models(base, key, models), "message": check_message(base, key, main_model)}
    if not skip_tools:
        checks["tools"] = check_tools(base, key, main_model)
    return {"ok": all(c["ok"] for c in checks.values()), "base_url": base, "checks": checks}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--profile", default=str(DEFAULT_PROFILE))
    parser.add_argument("--skip-tools", action="store_true")
    args = parser.parse_args(argv)
    try:
        profile = json.loads(Path(args.profile).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(json.dumps({"ok": False, "error": f"cannot read profile {args.profile}: {exc}"}))
        return 1
    result = probe(profile, skip_tools=args.skip_tools)
    print(json.dumps(result, indent=2))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
