"""Output bounds: per-field text length and total envelope bytes."""
from __future__ import annotations

import copy
import json
from typing import Any, Dict, List, Optional, Tuple

MIN_TEXT_CHARS = 200


def _trim_text(value: Any, max_chars: int, path: str, trimmed: List[str]) -> Any:
    if isinstance(value, str):
        if len(value) > max_chars:
            trimmed.append(path)
            return value[:max_chars]
        return value
    if isinstance(value, list):
        return [_trim_text(v, max_chars, "%s[]" % path, trimmed) for v in value]
    if isinstance(value, dict):
        return {k: _trim_text(v, max_chars, "%s.%s" % (path, k) if path else k, trimmed) for k, v in value.items()}
    return value


def size(obj: Any) -> int:
    return len(json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def bound(
    data: Dict[str, Any], *, trim_key: Optional[str], max_text_chars: int, max_bytes: int, overhead: int
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """Return the bounded data and truncation metadata. Long strings are cut
    to `max_text_chars`; then, while the result is larger than `max_bytes`
    (less `overhead` for the rest of the envelope), entries are dropped from
    the end of `data[trim_key]`, and finally the text limit is halved."""
    budget = max(1024, max_bytes - overhead)
    chars = max_text_chars
    original = data
    while True:
        trimmed: List[str] = []
        out = _trim_text(copy.deepcopy(original), chars, "", trimmed)
        omitted = 0
        if trim_key and isinstance(out.get(trim_key), list):
            items = out[trim_key]
            while items and size(out) > budget:
                items.pop()
                omitted += 1
        if size(out) <= budget or chars <= MIN_TEXT_CHARS:
            meta: Dict[str, Any] = {}
            if trimmed:
                meta["text_truncated"] = True
                meta["max_text_chars"] = chars
                meta["truncated_fields"] = sorted(set(trimmed))[:50]
            if omitted:
                meta["omitted_items"] = omitted
            if size(out) > budget:
                meta["over_budget"] = True
            return out, meta
        chars = max(MIN_TEXT_CHARS, chars // 2)
