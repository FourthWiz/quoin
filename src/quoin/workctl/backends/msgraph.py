"""Microsoft Graph mail backend (Microsoft 365).

`mail search` calls `GET /me/messages?$search="..."`; Graph's search syntax
needs the whole expression in double quotes, so embedded double quotes are
replaced by spaces. Paging follows `@odata.nextLink`, but the cursor keeps
only its `$skiptoken` (or `$skip`) value: every page is rebuilt from the
current query, `$top` and `$select`, so a cursor cannot add parameters such
as `$expand` or change the path. `mail read` asks for a
text body (`Prefer: outlook.body-content-type="text"`) and lists attachment
metadata without downloading content. The broker expects an OAuth access
token with `Mail.Read` in its keychain.
"""
from __future__ import annotations

from typing import Any, Dict, Mapping, Optional
import re
from urllib.parse import parse_qsl, quote, urlencode, urlsplit

from ..envelope import INVALID_INPUT, BrokerError
from . import Result
from ._common import as_dict, as_list, bearer, decode_cursor, encode_cursor, get_json, html_to_text, text

NAME = "msgraph"
DEFAULT_BASE = "https://graph.microsoft.com/v1.0"
SEARCH_SELECT = "id,conversationId,subject,from,toRecipients,receivedDateTime,bodyPreview,hasAttachments,webLink"
PAGING_KEYS = ("$skiptoken", "$skip")
_PAGING_VALUE_RE = re.compile(r"[A-Za-z0-9_\-=+/.!~*'()%]{1,700}")
READ_SELECT = "id,conversationId,subject,from,toRecipients,ccRecipients,receivedDateTime,body,hasAttachments,webLink"


def _address(value: Any) -> Optional[str]:
    email = as_dict(as_dict(value).get("emailAddress"))
    name, address = text(email.get("name")), text(email.get("address"))
    if name and address:
        return "%s <%s>" % (name, address)
    return address or name


def _addresses(value: Any) -> Optional[str]:
    joined = ", ".join(a for a in (_address(v) for v in as_list(value)) if a)
    return joined or None


class MsGraph:
    name = NAME
    default_base = DEFAULT_BASE

    def run(self, op_name: str, params: Mapping[str, Any], ctx: Any) -> Result:
        base = ctx.service.base_url or DEFAULT_BASE
        if op_name == "mail.search":
            return self._search(base, params, ctx)
        return self._read(base, params["id"], ctx)

    def _search(self, base: str, params: Mapping[str, Any], ctx: Any) -> Result:
        messages_path = urlsplit(base).path.rstrip("/") + "/me/messages"
        query = {
            "$search": '"%s"' % params["query"].replace('"', " "),
            "$top": params["limit"],
            "$select": SEARCH_SELECT,
        }
        token = decode_cursor(NAME, params.get("cursor"))
        if token is not None:
            if (
                not isinstance(token, list)
                or len(token) != 2
                or token[0] not in PAGING_KEYS
                or not isinstance(token[1], str)
                or not _PAGING_VALUE_RE.fullmatch(token[1])
            ):
                raise BrokerError(INVALID_INPUT, "cursor is not valid for this service")
            query[token[0]] = token[1]
        url = "%s/me/messages?%s" % (base, urlencode(query))
        body = as_dict(get_json(ctx, url, bearer(ctx)))
        items = [
            {
                "id": text(m.get("id")),
                "thread_id": text(m.get("conversationId")),
                "from": _address(m.get("from")),
                "to": _addresses(m.get("toRecipients")),
                "subject": text(m.get("subject")),
                "date": text(m.get("receivedDateTime")),
                "snippet": text(m.get("bodyPreview")) or "",
                "has_attachments": m.get("hasAttachments") is True,
                "url": text(m.get("webLink")),
            }
            for m in map(as_dict, as_list(body.get("value"))[: params["limit"]])
        ]
        next_cursor = None
        next_link = text(body.get("@odata.nextLink"))
        if next_link and urlsplit(next_link).path == messages_path:
            paging = dict(parse_qsl(urlsplit(next_link).query))
            for key in PAGING_KEYS:
                value = paging.get(key)
                if value and _PAGING_VALUE_RE.fullmatch(value):
                    next_cursor = [key, value]
                    break
        return Result(data={"items": items}, cursor=encode_cursor(NAME, next_cursor), trim_key="items")

    def _read(self, base: str, message_id: str, ctx: Any) -> Result:
        headers: Dict[str, str] = dict(bearer(ctx))
        headers["Prefer"] = 'outlook.body-content-type="text"'
        item = "%s/me/messages/%s" % (base, quote(message_id, safe=""))
        msg = as_dict(get_json(ctx, "%s?%s" % (item, urlencode({"$select": READ_SELECT})), headers))
        attachments = []
        if msg.get("hasAttachments") is True:
            listing = as_dict(
                get_json(ctx, "%s/attachments?%s" % (item, urlencode({"$select": "name,contentType,size"})), bearer(ctx))
            )
            attachments = [
                {
                    "name": text(a.get("name")),
                    "mime_type": text(a.get("contentType")),
                    "size": a.get("size") if isinstance(a.get("size"), int) else None,
                    "content_fetched": False,
                }
                for a in map(as_dict, as_list(listing.get("value")))
            ]
        body = as_dict(msg.get("body"))
        content = text(body.get("content")) or ""
        is_text = (text(body.get("contentType")) or "").lower() == "text"
        message = {
            "id": text(msg.get("id")) or message_id,
            "thread_id": text(msg.get("conversationId")),
            "from": _address(msg.get("from")),
            "to": _addresses(msg.get("toRecipients")),
            "cc": _addresses(msg.get("ccRecipients")),
            "subject": text(msg.get("subject")),
            "date": text(msg.get("receivedDateTime")),
            "body": content if is_text else html_to_text(content),
            "body_format": "text" if is_text else "html-as-text",
            "attachments": attachments,
        }
        return Result(data={"message": message}, url=text(msg.get("webLink")))


BACKEND = MsGraph()
