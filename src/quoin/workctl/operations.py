"""The normalized operation table and parameter validation.

Every transport (the CLI today, any future MCP server) names an operation
from `OPERATIONS` and passes raw parameters through `normalize`, so the
same validation and the same authorization policy apply whichever way a
request arrives. Backends receive only normalized parameters.
"""
from __future__ import annotations

import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Mapping, Optional

from .envelope import INVALID_INPUT, BrokerError

MAX_QUERY_FILE_BYTES = 16 * 1024
MAX_QUERY_CHARS = 4000
HARD_MAX_LIMIT = 100
DEFAULT_LIMIT = 20

JIRA_KEY_RE = re.compile(r"[A-Z][A-Z0-9_]{0,31}-[1-9][0-9]{0,9}")
SLACK_CHANNEL_RE = re.compile(r"[CGD][A-Z0-9]{2,31}")
SLACK_TS_RE = re.compile(r"[0-9]{9,11}\.[0-9]{1,6}")
MAIL_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_\-=+.]{0,511}")
CURSOR_RE = re.compile(r"[A-Za-z0-9_\-=+/.:]{1,2048}")


@dataclass(frozen=True)
class Operation:
    name: str
    service: str
    read_only: bool
    kind: str  # "record" (one resource) or "search" (a page of results)


OPERATIONS: Dict[str, Operation] = {
    op.name: op
    for op in (
        Operation("jira.issue", "jira", True, "record"),
        Operation("jira.search", "jira", True, "search"),
        Operation("slack.search", "slack", True, "search"),
        Operation("slack.thread", "slack", True, "record"),
        Operation("mail.search", "mail", True, "search"),
        Operation("mail.read", "mail", True, "record"),
    )
}

SERVICES = ("jira", "slack", "mail")

_SEARCH_PARAMS = frozenset(("query", "query_file", "limit", "cursor"))
ALLOWED_PARAMS: Dict[str, frozenset] = {
    "jira.issue": frozenset(("key",)),
    "jira.search": _SEARCH_PARAMS,
    "slack.search": _SEARCH_PARAMS,
    "slack.thread": frozenset(("channel", "thread", "limit", "cursor")),
    "mail.search": _SEARCH_PARAMS,
    "mail.read": frozenset(("id",)),
}


def get(name: str) -> Operation:
    op = OPERATIONS.get(name)
    if op is None:
        raise BrokerError(INVALID_INPUT, "unknown operation")
    return op


def read_query_file(path_text: Any) -> str:
    """Read a query from a regular file without following symlinks. The
    query is passed to backends as data; it is never interpolated into a
    shell command."""
    if not isinstance(path_text, str) or not path_text or "\x00" in path_text:
        raise BrokerError(INVALID_INPUT, "--query-file must name a file")
    path = Path(path_text)
    try:
        fd = os.open(str(path), os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
    except OSError:
        raise BrokerError(INVALID_INPUT, "query file is missing, unreadable or a symlink")
    try:
        with os.fdopen(fd, "rb") as handle:
            info = os.fstat(handle.fileno())
            if not stat.S_ISREG(info.st_mode):
                raise BrokerError(INVALID_INPUT, "query file must be a regular file")
            raw = handle.read(MAX_QUERY_FILE_BYTES + 1)
    except OSError:
        raise BrokerError(INVALID_INPUT, "query file is unreadable")
    if len(raw) > MAX_QUERY_FILE_BYTES:
        raise BrokerError(INVALID_INPUT, "query file exceeds %d bytes" % MAX_QUERY_FILE_BYTES)
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise BrokerError(INVALID_INPUT, "query file must be UTF-8 text")
    if "\x00" in text:
        raise BrokerError(INVALID_INPUT, "query file must not contain NUL bytes")
    text = text.strip()
    if not text:
        raise BrokerError(INVALID_INPUT, "query file is empty")
    if len(text) > MAX_QUERY_CHARS:
        raise BrokerError(INVALID_INPUT, "query exceeds %d characters" % MAX_QUERY_CHARS)
    return text


def _limit(value: Any, max_items: int) -> int:
    if value is None:
        return min(DEFAULT_LIMIT, max_items)
    if isinstance(value, bool):
        raise BrokerError(INVALID_INPUT, "--limit must be a positive integer")
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise BrokerError(INVALID_INPUT, "--limit must be a positive integer")
    if number < 1:
        raise BrokerError(INVALID_INPUT, "--limit must be a positive integer")
    return min(number, max_items, HARD_MAX_LIMIT)


def _match(regex: "re.Pattern[str]", value: Any, what: str) -> str:
    if not isinstance(value, str) or not regex.fullmatch(value):
        raise BrokerError(INVALID_INPUT, "%s is not valid" % what)
    return value


def _cursor(value: Any) -> Optional[str]:
    if value is None:
        return None
    return _match(CURSOR_RE, value, "cursor")


def normalize(op: Operation, raw: Mapping[str, Any], *, max_items: int) -> Dict[str, Any]:
    """Validate raw parameters for `op` and return the normalized form.
    Unknown parameters are refused so a transport cannot smuggle options."""
    allowed = ALLOWED_PARAMS[op.name]
    extra = set(k for k, v in raw.items() if v is not None) - allowed
    if extra:
        raise BrokerError(INVALID_INPUT, "unexpected parameter: %s" % sorted(extra)[0])
    if op.name == "jira.issue":
        return {"key": _match(JIRA_KEY_RE, raw.get("key"), "Jira issue key")}
    if op.name == "mail.read":
        return {"id": _match(MAIL_ID_RE, raw.get("id"), "message id")}
    if op.name == "slack.thread":
        return {
            "channel": _match(SLACK_CHANNEL_RE, raw.get("channel"), "Slack channel id"),
            "thread": _match(SLACK_TS_RE, raw.get("thread"), "Slack thread timestamp"),
            "limit": _limit(raw.get("limit"), max_items),
            "cursor": _cursor(raw.get("cursor")),
        }
    # search operations: the query comes from a file (CLI) or inline (other
    # transports), never both.
    query_file, query = raw.get("query_file"), raw.get("query")
    if (query_file is None) == (query is None):
        raise BrokerError(INVALID_INPUT, "give exactly one of --query-file or an inline query")
    if query_file is not None:
        text = read_query_file(query_file)
    else:
        if not isinstance(query, str) or not query.strip() or "\x00" in query:
            raise BrokerError(INVALID_INPUT, "query must be non-empty text")
        text = query.strip()
        if len(text) > MAX_QUERY_CHARS:
            raise BrokerError(INVALID_INPUT, "query exceeds %d characters" % MAX_QUERY_CHARS)
    return {"query": text, "limit": _limit(raw.get("limit"), max_items), "cursor": _cursor(raw.get("cursor"))}


def resource_id(op: Operation, params: Mapping[str, Any]) -> str:
    if op.name == "jira.issue":
        return params["key"]
    if op.name == "mail.read":
        return params["id"]
    if op.name == "slack.thread":
        return "%s/%s" % (params["channel"], params["thread"])
    return "search"

