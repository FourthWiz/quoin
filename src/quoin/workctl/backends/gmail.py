"""Gmail API backend.

`mail search` lists message ids (`users/me/messages?q=`) and fetches each
one's headers and snippet (`format=metadata`), so a page costs one request
per result, bounded by `--limit`. `mail read` fetches `format=full` and
renders the text/plain part (or the HTML part as text). Attachments are
reported as metadata only; their content is never downloaded. The broker
expects an OAuth access token with a read-only Gmail scope in its keychain.
"""
from __future__ import annotations

import base64
import binascii
import codecs
import re
from typing import Any, Dict, List, Mapping, Optional
from urllib.parse import quote, urlencode

from ..envelope import INVALID_INPUT, NOT_FOUND, BrokerError
from . import Result
from ._common import (
    as_dict,
    as_list,
    bearer,
    decode_cursor,
    encode_cursor,
    get_json,
    html_to_text,
    text,
)

NAME = "gmail"
DEFAULT_BASE = "https://gmail.googleapis.com"
METADATA_HEADERS = ("From", "To", "Cc", "Subject", "Date")
MAX_PARTS = 200


def _headers(payload: Mapping[str, Any]) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for header in map(as_dict, as_list(payload.get("headers"))):
        name, value = text(header.get("name")), text(header.get("value"))
        if name and value is not None and name.lower() not in out:
            out[name.lower()] = value
    return out


_CHARSET_RE = re.compile(r"""charset\s*=\s*["']?([A-Za-z0-9._:-]{1,40})""", re.IGNORECASE)


def _charset(part: Mapping[str, Any]) -> str:
    match = _CHARSET_RE.search(_headers(part).get("content-type", ""))
    if match:
        try:
            return codecs.lookup(match.group(1)).name
        except LookupError:
            pass
    return "utf-8"


def _decode(data: Any, charset: str = "utf-8") -> Optional[str]:
    """Gmail returns the part's bytes in its declared charset with only the
    transfer encoding removed."""
    if not isinstance(data, str):
        return None
    try:
        raw = base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))
    except (binascii.Error, ValueError):
        return None
    return raw.decode(charset, errors="replace")


def _walk(payload: Mapping[str, Any], out: List[Mapping[str, Any]]) -> None:
    if len(out) >= MAX_PARTS:
        return
    out.append(payload)
    for part in map(as_dict, as_list(payload.get("parts"))):
        _walk(part, out)


class Gmail:
    name = NAME
    default_base = DEFAULT_BASE

    def run(self, op_name: str, params: Mapping[str, Any], ctx: Any) -> Result:
        base = (ctx.service.base_url or DEFAULT_BASE) + "/gmail/v1/users/me"
        if op_name == "mail.search":
            return self._search(base, params, ctx)
        return self._read(base, params["id"], ctx)

    def _search(self, base: str, params: Mapping[str, Any], ctx: Any) -> Result:
        query: Dict[str, Any] = {"q": params["query"], "maxResults": params["limit"]}
        token = decode_cursor(NAME, params.get("cursor"))
        if token is not None:
            if not isinstance(token, str):
                raise BrokerError(INVALID_INPUT, "cursor is not valid for this service")
            query["pageToken"] = token
        listing = as_dict(get_json(ctx, "%s/messages?%s" % (base, urlencode(query)), bearer(ctx)))
        items = []
        skipped = 0
        meta_query = urlencode([("format", "metadata")] + [("metadataHeaders", h) for h in METADATA_HEADERS])
        for ref in map(as_dict, as_list(listing.get("messages"))[: params["limit"]]):
            message_id = text(ref.get("id"))
            if not message_id:
                continue
            try:
                msg = as_dict(
                    get_json(ctx, "%s/messages/%s?%s" % (base, quote(message_id, safe=""), meta_query), bearer(ctx))
                )
            except BrokerError as exc:
                if exc.code != NOT_FOUND:
                    raise
                skipped += 1  # deleted between the listing and the fetch
                continue
            headers = _headers(as_dict(msg.get("payload")))
            items.append(
                {
                    "id": message_id,
                    "thread_id": text(msg.get("threadId")),
                    "from": headers.get("from"),
                    "to": headers.get("to"),
                    "subject": headers.get("subject"),
                    "date": headers.get("date"),
                    "snippet": text(msg.get("snippet")) or "",
                }
            )
        extra = {"skipped_items": skipped} if skipped else {}
        if isinstance(listing.get("resultSizeEstimate"), int):
            extra["total_estimate"] = listing["resultSizeEstimate"]
        return Result(
            data={"items": items},
            cursor=encode_cursor(NAME, text(listing.get("nextPageToken"))),
            trim_key="items",
            extra_pagination=extra,
        )

    def _read(self, base: str, message_id: str, ctx: Any) -> Result:
        msg = as_dict(
            get_json(ctx, "%s/messages/%s?format=full" % (base, quote(message_id, safe="")), bearer(ctx))
        )
        payload = as_dict(msg.get("payload"))
        headers = _headers(payload)
        parts: List[Mapping[str, Any]] = []
        _walk(payload, parts)
        plain = html = None
        attachments = []
        for part in parts:
            body = as_dict(part.get("body"))
            mime = (text(part.get("mimeType")) or "").lower()
            if text(part.get("filename")):
                attachments.append(
                    {
                        "name": part["filename"],
                        "mime_type": mime or None,
                        "size": body.get("size") if isinstance(body.get("size"), int) else None,
                        "content_fetched": False,
                    }
                )
                continue
            if mime == "text/plain" and plain is None:
                plain = _decode(body.get("data"), _charset(part))
            elif mime == "text/html" and html is None:
                html = _decode(body.get("data"), _charset(part))
        if plain is not None:
            body_text, fmt = plain, "text"
        elif html is not None:
            body_text, fmt = html_to_text(html), "html-as-text"
        else:
            body_text, fmt = text(msg.get("snippet")) or "", "snippet"
        message = {
            "id": text(msg.get("id")) or message_id,
            "thread_id": text(msg.get("threadId")),
            "from": headers.get("from"),
            "to": headers.get("to"),
            "cc": headers.get("cc"),
            "subject": headers.get("subject"),
            "date": headers.get("date"),
            "body": body_text,
            "body_format": fmt,
            "attachments": attachments,
        }
        return Result(data={"message": message})


BACKEND = Gmail()
