"""The normalized operation table and parameter validation.

Every transport (the CLI today, any future MCP server) names an operation
from `OPERATIONS` and passes raw parameters through `normalize`, so the
same validation and the same authorization policy apply whichever way a
request arrives. Backends receive only normalized parameters.

Write operations (`read_only=False`) share the table so every transport
sees them, but the read broker's policy refuses them. Their parameters go
through `normalize_write`, which `workctl propose` applies when it builds a
proposal and the writer applies again before it executes one.
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
    kind: str  # "record" (one resource), "search" (a page of results) or "write"


OPERATIONS: Dict[str, Operation] = {
    op.name: op
    for op in (
        Operation("jira.issue", "jira", True, "record"),
        Operation("jira.search", "jira", True, "search"),
        Operation("slack.search", "slack", True, "search"),
        Operation("slack.thread", "slack", True, "record"),
        Operation("mail.search", "mail", True, "search"),
        Operation("mail.read", "mail", True, "record"),
        Operation("jira.comment", "jira", False, "write"),
        Operation("slack.post", "slack", False, "write"),
        Operation("mail.send", "mail", False, "write"),
    )
}

READ_OPERATIONS = tuple(name for name, op in OPERATIONS.items() if op.read_only)
WRITE_OPERATIONS = tuple(name for name, op in OPERATIONS.items() if not op.read_only)

MAX_BODY_FILE_BYTES = 128 * 1024
MAX_BODY_CHARS = 32_000
MAX_SUBJECT_CHARS = 998
MAX_RECIPIENTS = 20
MAIL_ADDRESS_RE = re.compile(r"[A-Za-z0-9._%+'-]{1,64}@[A-Za-z0-9-]{1,63}(\.[A-Za-z0-9-]{1,63})*\.[A-Za-z]{2,63}")
MESSAGE_ID_RE = re.compile(r"<[!-;=?-~]{1,500}>")

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

WRITE_PARAMS: Dict[str, frozenset] = {
    "jira.comment": frozenset(("key", "body", "body_file")),
    "slack.post": frozenset(("channel", "thread", "body", "body_file")),
    "mail.send": frozenset(("to", "cc", "subject", "subject_file", "in_reply_to", "body", "body_file")),
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
    return _read_text_file(path_text, "--query-file", "query", MAX_QUERY_FILE_BYTES, MAX_QUERY_CHARS).strip()


def _read_text_file(path_text: Any, flag: str, what: str, max_bytes: int, max_chars: int) -> str:
    if not isinstance(path_text, str) or not path_text or "\x00" in path_text:
        raise BrokerError(INVALID_INPUT, "%s must name a file" % flag)
    path = Path(path_text)
    try:
        fd = os.open(str(path), os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
    except OSError:
        raise BrokerError(INVALID_INPUT, "%s file is missing, unreadable or a symlink" % what)
    try:
        with os.fdopen(fd, "rb") as handle:
            info = os.fstat(handle.fileno())
            if not stat.S_ISREG(info.st_mode):
                raise BrokerError(INVALID_INPUT, "%s file must be a regular file" % what)
            raw = handle.read(max_bytes + 1)
    except OSError:
        raise BrokerError(INVALID_INPUT, "%s file is unreadable" % what)
    if len(raw) > max_bytes:
        raise BrokerError(INVALID_INPUT, "%s file exceeds %d bytes" % (what, max_bytes))
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise BrokerError(INVALID_INPUT, "%s file must be UTF-8 text" % what)
    if "\x00" in text:
        raise BrokerError(INVALID_INPUT, "%s file must not contain NUL bytes" % what)
    if not text.strip():
        raise BrokerError(INVALID_INPUT, "%s file is empty" % what)
    if len(text.strip()) > max_chars:
        raise BrokerError(INVALID_INPUT, "%s exceeds %d characters" % (what, max_chars))
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
    if not op.read_only:
        raise BrokerError(INVALID_INPUT, "%s is a write operation" % op.name)
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


def _text(raw: Mapping[str, Any], name: str, what: str, max_chars: int, *, single_line: bool = False) -> str:
    """One text value given inline or as a file, never both. Bodies keep
    their inner layout; only trailing whitespace is dropped."""
    inline, path = raw.get(name), raw.get(name + "_file")
    if (inline is None) == (path is None):
        raise BrokerError(INVALID_INPUT, "give exactly one of --%s-file or an inline %s" % (name, what))
    if path is not None:
        text = _read_text_file(path, "--%s-file" % name, what, MAX_BODY_FILE_BYTES, max_chars)
    else:
        if not isinstance(inline, str) or not inline.strip() or "\x00" in inline:
            raise BrokerError(INVALID_INPUT, "%s must be non-empty text" % what)
        text = inline
    text = text.strip() if single_line else text.rstrip()
    if len(text) > max_chars:
        raise BrokerError(INVALID_INPUT, "%s exceeds %d characters" % (what, max_chars))
    if single_line and (len(text.splitlines()) > 1 or any(ord(c) < 0x20 or ord(c) == 0x7F for c in text)):
        raise BrokerError(INVALID_INPUT, "%s must be a single line without control characters" % what)
    return text


def _addresses(value: Any, what: str, *, required: bool) -> list:
    if value is None:
        value = []
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple)) or (required and not value) or len(value) > MAX_RECIPIENTS:
        raise BrokerError(INVALID_INPUT, "%s must list 1 to %d addresses" % (what, MAX_RECIPIENTS))
    out = []
    for item in value:
        if not isinstance(item, str) or not MAIL_ADDRESS_RE.fullmatch(item):
            raise BrokerError(INVALID_INPUT, "%s holds an invalid address" % what)
        if item.lower() not in (o.lower() for o in out):
            out.append(item)
    return out


def normalize_write(op: Operation, raw: Mapping[str, Any]) -> Dict[str, Any]:
    """Validate the target and content of a write. The result names the
    exact destination and the full content; it is what a proposal records
    and what the approval's hash covers."""
    if op.read_only:
        raise BrokerError(INVALID_INPUT, "%s is not a write operation" % op.name)
    allowed = WRITE_PARAMS[op.name]
    extra = set(k for k, v in raw.items() if v is not None) - allowed
    if extra:
        raise BrokerError(INVALID_INPUT, "unexpected parameter: %s" % sorted(extra)[0])
    if op.name == "jira.comment":
        return {
            "key": _match(JIRA_KEY_RE, raw.get("key"), "Jira issue key"),
            "body": _text(raw, "body", "body", MAX_BODY_CHARS),
        }
    if op.name == "slack.post":
        thread = raw.get("thread")
        return {
            "channel": _match(SLACK_CHANNEL_RE, raw.get("channel"), "Slack channel id"),
            "thread": None if thread is None else _match(SLACK_TS_RE, thread, "Slack thread timestamp"),
            "body": _text(raw, "body", "body", MAX_BODY_CHARS),
        }
    in_reply_to = raw.get("in_reply_to")
    return {
        "to": _addresses(raw.get("to"), "--to", required=True),
        "cc": _addresses(raw.get("cc"), "--cc", required=False),
        "subject": _text(raw, "subject", "subject", MAX_SUBJECT_CHARS, single_line=True),
        "in_reply_to": None if in_reply_to is None else _match(MESSAGE_ID_RE, in_reply_to, "In-Reply-To message id"),
        "body": _text(raw, "body", "body", MAX_BODY_CHARS),
    }


def resource_id(op: Operation, params: Mapping[str, Any]) -> str:
    if op.name == "jira.issue":
        return params["key"]
    if op.name == "mail.read":
        return params["id"]
    if op.name in ("slack.thread", "slack.post") and params.get("thread"):
        return "%s/%s" % (params["channel"], params["thread"])
    if op.name == "slack.post":
        return params["channel"]
    if op.name == "jira.comment":
        return params["key"]
    if op.name == "mail.send":
        return ",".join(params["to"])
    return "search"

