"""Slack Web API backend.

`slack search` calls `search.messages` (a user token with `search:read`),
paged by page number; `slack thread` calls `conversations.replies`, paged by
`response_metadata.next_cursor`. Slack reports most failures as HTTP 200
with `ok: false` and an error string, which is mapped onto the envelope's
closed codes here. Mentions and links in message text are left as Slack
writes them.

The approved writer posts with `chat.postMessage` (a token with
`chat:write`, plus the `*:history` scopes for the channels it posts to,
which the lookup needs) and tags the message with metadata carrying the proposal's
idempotency key; `lookup` reads the channel or thread history with
metadata included to find that tag after an ambiguous outcome, scanning a
bounded time window around the send and refusing to call a partial scan
"not found". An
`ok: false` answer to a post means Slack did not post, except for its
transient server errors, which leave the outcome unknown.
"""
from __future__ import annotations

from typing import Any, Dict, Mapping, Optional
from urllib.parse import urlencode

from ..envelope import (
    AUTH_REQUIRED,
    FORBIDDEN,
    INVALID_INPUT,
    NOT_FOUND,
    RATE_LIMITED,
    UNAVAILABLE,
    BrokerError,
)
from . import Receipt, Result
from ._common import as_dict, as_list, bearer, decode_cursor, encode_cursor, get_json, post_json, text

NAME = "slack-web"
DEFAULT_BASE = "https://slack.com/api"
MAX_SEARCH_PAGE = 100

_ERRORS = {
    "not_authed": AUTH_REQUIRED,
    "invalid_auth": AUTH_REQUIRED,
    "token_revoked": AUTH_REQUIRED,
    "token_expired": AUTH_REQUIRED,
    "account_inactive": AUTH_REQUIRED,
    "missing_scope": FORBIDDEN,
    "not_allowed_token_type": FORBIDDEN,
    "no_permission": FORBIDDEN,
    "access_denied": FORBIDDEN,
    "team_access_not_granted": FORBIDDEN,
    "channel_not_found": NOT_FOUND,
    "thread_not_found": NOT_FOUND,
    "message_not_found": NOT_FOUND,
    "ratelimited": RATE_LIMITED,
    "invalid_arguments": INVALID_INPUT,
    "invalid_cursor": INVALID_INPUT,
    "invalid_ts_latest": INVALID_INPUT,
    "invalid_ts_oldest": INVALID_INPUT,
}

_TRANSIENT = ("internal_error", "fatal_error", "service_unavailable", "request_timeout")

_MESSAGES = {
    AUTH_REQUIRED: "Slack rejected the broker credential",
    FORBIDDEN: "the broker token lacks the scope or access for this read",
    NOT_FOUND: "the Slack channel or thread was not found",
    RATE_LIMITED: "Slack is rate limiting requests",
    INVALID_INPUT: "Slack rejected the request parameters",
    UNAVAILABLE: "Slack returned an unexpected error",
}


def _check(body: Any) -> Dict[str, Any]:
    body = as_dict(body)
    if body.get("ok") is True:
        return body
    error = text(body.get("error")) or ""
    code = _ERRORS.get(error, UNAVAILABLE)
    transient = error in _TRANSIENT
    raise BrokerError(code, _MESSAGES[code], retryable=True if transient else (False if code == UNAVAILABLE else None))


METADATA_EVENT = "quoin_workctl_write"
LOOKUP_PAGE = 200
LOOKUP_PAGES = 5
LOOKUP_WINDOW_SECONDS = 1_200

_WRITE_ERRORS = {
    "not_in_channel": FORBIDDEN,
    "is_archived": FORBIDDEN,
    "restricted_action": FORBIDDEN,
    "restricted_action_read_only_channel": FORBIDDEN,
    "restricted_action_thread_only_channel": FORBIDDEN,
    "restricted_action_non_threadable_channel": FORBIDDEN,
    "ekm_access_denied": FORBIDDEN,
    "msg_too_long": INVALID_INPUT,
    "no_text": INVALID_INPUT,
    "invalid_blocks": INVALID_INPUT,
    "invalid_metadata_format": INVALID_INPUT,
    "invalid_metadata_schema": INVALID_INPUT,
    "metadata_too_large": INVALID_INPUT,
    "too_many_attachments": INVALID_INPUT,
}


def _check_write(body: Any) -> Dict[str, Any]:
    """`ok: false` to a post is a refusal, so it maps to a definite code;
    only Slack's transient server errors stay UNAVAILABLE."""
    if isinstance(body, dict) and body.get("ok") is True:
        return body
    error = text(body.get("error")) if isinstance(body, dict) and body.get("ok") is False else None
    if not error or error in _TRANSIENT:
        raise BrokerError(UNAVAILABLE, "Slack gave no clear answer to the post; it may or may not exist")
    code = _WRITE_ERRORS.get(error) or _ERRORS.get(error) or INVALID_INPUT
    message = "Slack refused the post" if code in (FORBIDDEN, INVALID_INPUT) else _MESSAGES[code]
    raise BrokerError(code, message)


def _permalink(ctx: Any, channel: str, ts: str) -> Optional[str]:
    if not ctx.service.web_url:
        return None
    return "%s/archives/%s/p%s" % (ctx.service.web_url, channel, ts.replace(".", ""))


class SlackWeb:
    name = NAME
    default_base = DEFAULT_BASE

    def run(self, op_name: str, params: Mapping[str, Any], ctx: Any) -> Result:
        base = ctx.service.base_url or DEFAULT_BASE
        if op_name == "slack.search":
            return self._search(base, params, ctx)
        return self._thread(base, params, ctx)

    def _search(self, base: str, params: Mapping[str, Any], ctx: Any) -> Result:
        page = decode_cursor(NAME, params.get("cursor"))
        if page is None:
            page = 1
        if isinstance(page, bool) or not isinstance(page, int) or not 1 <= page <= MAX_SEARCH_PAGE:
            raise BrokerError(INVALID_INPUT, "cursor is not valid for this service")
        query = {"query": params["query"], "count": params["limit"], "page": page, "sort": "timestamp"}
        body = _check(get_json(ctx, "%s/search.messages?%s" % (base, urlencode(query)), bearer(ctx)))
        messages = as_dict(body.get("messages"))
        items = []
        for match in map(as_dict, as_list(messages.get("matches"))[: params["limit"]]):
            channel = as_dict(match.get("channel"))
            items.append(
                {
                    "channel_id": text(channel.get("id")),
                    "channel_name": text(channel.get("name")),
                    "ts": text(match.get("ts")),
                    "user": text(match.get("user")) or text(match.get("username")),
                    "text": text(match.get("text")) or "",
                    "permalink": text(match.get("permalink")),
                }
            )
        paging = as_dict(messages.get("paging")) or as_dict(messages.get("pagination"))
        pages = paging.get("pages") if isinstance(paging.get("pages"), int) else None
        if pages is None and isinstance(paging.get("page_count"), int):
            pages = paging["page_count"]
        next_page = page + 1 if pages is not None and page < pages and page < MAX_SEARCH_PAGE else None
        extra = {"total": messages["total"]} if isinstance(messages.get("total"), int) else {}
        return Result(
            data={"items": items},
            cursor=encode_cursor(NAME, next_page),
            trim_key="items",
            extra_pagination=extra,
        )

    def _thread(self, base: str, params: Mapping[str, Any], ctx: Any) -> Result:
        query: Dict[str, Any] = {"channel": params["channel"], "ts": params["thread"], "limit": params["limit"]}
        token = decode_cursor(NAME, params.get("cursor"))
        if token is not None:
            if not isinstance(token, str):
                raise BrokerError(INVALID_INPUT, "cursor is not valid for this service")
            query["cursor"] = token
        body = _check(get_json(ctx, "%s/conversations.replies?%s" % (base, urlencode(query)), bearer(ctx)))
        messages = [
            {
                "ts": text(m.get("ts")),
                "user": text(m.get("user")) or text(m.get("username")) or text(m.get("bot_id")),
                "text": text(m.get("text")) or "",
                "reply_count": m.get("reply_count") if isinstance(m.get("reply_count"), int) else None,
            }
            for m in map(as_dict, as_list(body.get("messages"))[: params["limit"]])
        ]
        next_cursor = text(as_dict(body.get("response_metadata")).get("next_cursor")) if body.get("has_more") else None
        return Result(
            data={"channel": params["channel"], "thread": params["thread"], "messages": messages},
            url=_permalink(ctx, params["channel"], params["thread"]),
            cursor=encode_cursor(NAME, next_cursor),
            trim_key="messages",
        )

    def write(self, op_name: str, params: Mapping[str, Any], ctx: Any, *, marker: str) -> Receipt:
        base = ctx.service.base_url or DEFAULT_BASE
        payload: Dict[str, Any] = {
            "channel": params["channel"],
            "text": params["body"],
            "unfurl_links": False,
            "unfurl_media": False,
            "metadata": {"event_type": METADATA_EVENT, "event_payload": {"idempotency_key": marker}},
        }
        if params.get("thread"):
            payload["thread_ts"] = params["thread"]
        body = _check_write(post_json(ctx, base + "/chat.postMessage", payload, bearer(ctx)))
        ts = text(body.get("ts"))
        channel = text(body.get("channel")) or params["channel"]
        if not ts:
            raise BrokerError(UNAVAILABLE, "Slack accepted the post without returning its timestamp")
        return Receipt(remote_id=ts, url=_permalink(ctx, channel, ts), extra={"channel": channel})

    def lookup(
        self, op_name: str, params: Mapping[str, Any], ctx: Any, *, marker: str, since: float
    ) -> Optional[Receipt]:
        base = ctx.service.base_url or DEFAULT_BASE
        query: Dict[str, Any] = {
            "channel": params["channel"],
            "oldest": "%d.000000" % max(0, int(since)),
            "latest": "%d.000000" % (int(since) + LOOKUP_WINDOW_SECONDS),
            "inclusive": "true",
            "include_all_metadata": "true",
            "limit": LOOKUP_PAGE,
        }
        method = "conversations.history"
        if params.get("thread"):
            method = "conversations.replies"
            query["ts"] = params["thread"]
        for _ in range(LOOKUP_PAGES):
            body = _check(get_json(ctx, "%s/%s?%s" % (base, method, urlencode(query)), bearer(ctx)))
            for message in map(as_dict, as_list(body.get("messages"))):
                meta = as_dict(message.get("metadata"))
                payload = as_dict(meta.get("event_payload"))
                ts = text(message.get("ts"))
                if meta.get("event_type") == METADATA_EVENT and payload.get("idempotency_key") == marker and ts:
                    channel = params["channel"]
                    return Receipt(remote_id=ts, url=_permalink(ctx, channel, ts), extra={"channel": channel})
            cursor = text(as_dict(body.get("response_metadata")).get("next_cursor"))
            if not cursor:
                return None
            query["cursor"] = cursor
        raise BrokerError(UNAVAILABLE, "too many Slack messages to check in the lookup window", retryable=False)


BACKEND = SlackWeb()
