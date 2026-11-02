"""Jira Cloud REST API v3 backend.

Reads one issue (`GET /rest/api/3/issue/{key}`) and searches with JQL
through the enhanced search endpoint (`GET /rest/api/3/search/jql`, paged
by `nextPageToken`). Authentication is HTTP basic with the configured
account and an API token when `account` is set, otherwise a bearer token
(OAuth, where `base_url` is the `api.atlassian.com/ex/jira/<cloud id>`
gateway and `web_url` gives the site for browse links).
Descriptions and comments arrive as Atlassian Document Format and are
flattened to plain text; attachments are reported as metadata only.
"""
from __future__ import annotations

import base64
import re
from typing import Any, Dict, List, Mapping, Optional
from urllib.parse import quote, urlencode

from ..envelope import INVALID_INPUT, POLICY_DENIED, BrokerError
from . import Result
from ._common import as_dict, as_list, bearer, decode_cursor, encode_cursor, get_json, text

NAME = "jira-cloud"
ISSUE_FIELDS = "summary,status,issuetype,priority,assignee,reporter,labels,created,updated,description,comment,attachment"
SEARCH_FIELDS = "summary,status,issuetype,priority,assignee,updated"
_BLOCK_NODES = {
    "paragraph", "heading", "blockquote", "codeBlock", "rule", "tableRow", "panel",
    "taskItem", "decisionItem", "mediaSingle", "expand", "nestedExpand",
}


def adf_to_text(node: Any) -> str:
    """Flatten an Atlassian Document Format tree to plain text."""
    parts: List[str] = []

    def walk(n: Any) -> None:
        if isinstance(n, list):
            for child in n:
                walk(child)
            return
        if not isinstance(n, dict):
            return
        kind = n.get("type")
        if kind == "text" and isinstance(n.get("text"), str):
            parts.append(n["text"])
        elif kind == "hardBreak":
            parts.append("\n")
        elif kind in ("mention", "emoji", "status"):
            attrs = as_dict(n.get("attrs"))
            label = attrs.get("text") if isinstance(attrs.get("text"), str) else attrs.get("shortName")
            if isinstance(label, str):
                parts.append(label)
        elif kind == "inlineCard" and isinstance(as_dict(n.get("attrs")).get("url"), str):
            parts.append(n["attrs"]["url"])
        walk(n.get("content"))
        if kind in _BLOCK_NODES:
            parts.append("\n")

    if isinstance(node, str):
        return node
    walk(node)
    joined = "\n".join(line.rstrip() for line in "".join(parts).split("\n"))
    return re.sub(r"\n{3,}", "\n\n", joined).strip()


def _person(value: Any) -> Optional[str]:
    value = as_dict(value)
    return text(value.get("displayName")) or text(value.get("accountId"))


def _name(value: Any) -> Optional[str]:
    return text(as_dict(value).get("name"))


class JiraCloud:
    name = NAME
    default_base = None

    def _base(self, ctx: Any) -> str:
        if not ctx.service.base_url:
            raise BrokerError(POLICY_DENIED, "jira base_url is required for the jira-cloud backend")
        return ctx.service.base_url

    def _headers(self, ctx: Any) -> Dict[str, str]:
        if ctx.service.account:
            pair = "%s:%s" % (ctx.service.account, ctx.credential.reveal())
            return {"Authorization": "Basic %s" % base64.b64encode(pair.encode("utf-8")).decode("ascii")}
        return bearer(ctx)

    def _web(self, ctx: Any) -> str:
        return ctx.service.web_url or ctx.service.base_url

    def run(self, op_name: str, params: Mapping[str, Any], ctx: Any) -> Result:
        base = self._base(ctx)
        if op_name == "jira.issue":
            return self._issue(base, params["key"], ctx)
        return self._search(base, params, ctx)

    def _issue(self, base: str, key: str, ctx: Any) -> Result:
        url = "%s/rest/api/3/issue/%s?%s" % (base, quote(key, safe=""), urlencode({"fields": ISSUE_FIELDS}))
        body = as_dict(get_json(ctx, url, self._headers(ctx)))
        fields = as_dict(body.get("fields"))
        real_key = text(body.get("key")) or key
        comment_block = as_dict(fields.get("comment"))
        comments = [
            {
                "id": text(c.get("id")),
                "author": _person(c.get("author")),
                "created": text(c.get("created")),
                "body": adf_to_text(c.get("body")),
            }
            for c in map(as_dict, as_list(comment_block.get("comments")))
        ]
        attachments = [
            {
                "name": text(a.get("filename")),
                "mime_type": text(a.get("mimeType")),
                "size": a.get("size") if isinstance(a.get("size"), int) else None,
                "content_fetched": False,
            }
            for a in map(as_dict, as_list(fields.get("attachment")))
        ]
        web = "%s/browse/%s" % (self._web(ctx), quote(real_key, safe=""))
        issue = {
            "key": real_key,
            "summary": text(fields.get("summary")),
            "status": _name(fields.get("status")),
            "type": _name(fields.get("issuetype")),
            "priority": _name(fields.get("priority")),
            "assignee": _person(fields.get("assignee")),
            "reporter": _person(fields.get("reporter")),
            "labels": [l for l in as_list(fields.get("labels")) if isinstance(l, str)],
            "created": text(fields.get("created")),
            "updated": text(fields.get("updated")),
            "description": adf_to_text(fields.get("description")),
            "attachments": attachments,
            "comment_total": comment_block.get("total") if isinstance(comment_block.get("total"), int) else len(comments),
            "url": web,
        }
        return Result(data={"issue": issue, "comments": comments}, url=web, trim_key="comments")

    def _search(self, base: str, params: Mapping[str, Any], ctx: Any) -> Result:
        query: Dict[str, Any] = {"jql": params["query"], "maxResults": params["limit"], "fields": SEARCH_FIELDS}
        token = decode_cursor(NAME, params.get("cursor"))
        if token is not None:
            if not isinstance(token, str):
                raise BrokerError(INVALID_INPUT, "cursor is not valid for this service")
            query["nextPageToken"] = token
        url = "%s/rest/api/3/search/jql?%s" % (base, urlencode(query))
        body = as_dict(get_json(ctx, url, self._headers(ctx)))
        items = []
        for issue in map(as_dict, as_list(body.get("issues"))[: params["limit"]]):
            fields = as_dict(issue.get("fields"))
            key = text(issue.get("key"))
            items.append(
                {
                    "key": key,
                    "summary": text(fields.get("summary")),
                    "status": _name(fields.get("status")),
                    "type": _name(fields.get("issuetype")),
                    "priority": _name(fields.get("priority")),
                    "assignee": _person(fields.get("assignee")),
                    "updated": text(fields.get("updated")),
                    "url": "%s/browse/%s" % (self._web(ctx), quote(key, safe="")) if key else None,
                }
            )
        next_token = None if body.get("isLast") is True else text(body.get("nextPageToken"))
        return Result(
            data={"items": items},
            url="%s/issues/?%s" % (self._web(ctx), urlencode({"jql": params["query"]})),
            cursor=encode_cursor(NAME, next_token),
            trim_key="items",
        )


BACKEND = JiraCloud()
