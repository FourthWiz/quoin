"""`workctl` command line: one JSON envelope on stdout, diagnostics on stderr.

    workctl jira issue KEY --json
    workctl jira search --query-file F --limit N [--cursor C] --json
    workctl slack search --query-file F --limit N [--cursor C] --json
    workctl slack thread CHANNEL THREAD [--limit N] [--cursor C] --json
    workctl mail search --query-file F --limit N [--cursor C] --json
    workctl mail read ID --json

Output is always JSON; `--json` is accepted so the documented syntax stays
explicit. Usage errors are reported as an INVALID_INPUT envelope rather
than argparse text, and `--help` returns the usage inside an ok envelope,
so stdout stays parseable. Exit status is 0 when the envelope is ok and 1
otherwise. Proxy and CA-bundle variables are dropped from the environment
first (the broker repeats this for every transport), so a caller cannot
route the broker's credentialed requests through a proxy or trust store of
its choosing; those settings come only from the broker configuration.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from typing import Any, Dict, List, Optional, Sequence

from . import broker
from .envelope import INVALID_INPUT, BrokerError, failure, success
from .transport import scrub_environment


class _UsageError(Exception):
    pass


class _HelpRequested(Exception):
    def __init__(self, text: str) -> None:
        super().__init__(text)
        self.text = text


class _HelpAction(argparse.Action):
    def __init__(self, option_strings, dest, **kwargs):  # noqa: D401
        super().__init__(option_strings, dest, nargs=0, default=argparse.SUPPRESS, help="show usage")

    def __call__(self, parser, namespace, values, option_string=None):
        raise _HelpRequested(parser.format_help())


class _Parser(argparse.ArgumentParser):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        kwargs["add_help"] = False
        kwargs["allow_abbrev"] = False
        super().__init__(*args, **kwargs)
        self.add_argument("-h", "--help", action=_HelpAction)

    def error(self, message: str) -> None:  # type: ignore[override]
        raise _UsageError(message)

    def exit(self, status: int = 0, message: Optional[str] = None) -> None:  # type: ignore[override]
        if message:
            sys.stderr.write(message)
        raise SystemExit(status)


def _search_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--query-file", dest="query_file", required=True)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--cursor")
    parser.add_argument("--json", action="store_true")


def build_parser() -> argparse.ArgumentParser:
    parser = _Parser(prog="workctl", description="Read-only Jira, Slack and mail broker.")
    parser.add_argument("--no-cache", action="store_true", help="bypass the profile-bound cache")
    parser.add_argument("--verbose", action="store_true", help="write a diagnostic line to stderr")
    services = parser.add_subparsers(dest="service", parser_class=_Parser)

    jira = services.add_parser("jira").add_subparsers(dest="action", parser_class=_Parser)
    issue = jira.add_parser("issue")
    issue.add_argument("key")
    issue.add_argument("--json", action="store_true")
    _search_args(jira.add_parser("search"))

    slack = services.add_parser("slack").add_subparsers(dest="action", parser_class=_Parser)
    _search_args(slack.add_parser("search"))
    thread = slack.add_parser("thread")
    thread.add_argument("channel")
    thread.add_argument("thread")
    thread.add_argument("--limit", type=int)
    thread.add_argument("--cursor")
    thread.add_argument("--json", action="store_true")

    mail = services.add_parser("mail").add_subparsers(dest="action", parser_class=_Parser)
    _search_args(mail.add_parser("search"))
    read = mail.add_parser("read")
    read.add_argument("id")
    read.add_argument("--json", action="store_true")
    return parser


_PARAM_NAMES = ("key", "query_file", "limit", "cursor", "channel", "thread", "id")


def _emit(envelope: Dict[str, Any]) -> int:
    sys.stdout.write(json.dumps(envelope, ensure_ascii=False, sort_keys=False) + "\n")
    sys.stdout.flush()
    return 0 if envelope.get("ok") else 1


def main(argv: Optional[Sequence[str]] = None, **execute_kwargs: Any) -> int:
    scrub_environment()
    args_list: List[str] = list(sys.argv[1:] if argv is None else argv)
    try:
        args = build_parser().parse_args(args_list)
        if not args.service or not getattr(args, "action", None):
            raise _UsageError("give a service and an action, for example: workctl jira issue KEY --json")
    except _HelpRequested as exc:
        return _emit(success({"usage": exc.text}, pagination=None, provenance=None, request_id=uuid.uuid4().hex))
    except _UsageError as exc:
        sys.stderr.write("workctl: %s\n" % exc)
        err = BrokerError(INVALID_INPUT, "invalid command line; run workctl --help for the syntax")
        return _emit(failure(err, request_id=uuid.uuid4().hex))
    params = {name: getattr(args, name) for name in _PARAM_NAMES if getattr(args, name, None) is not None}
    execute_kwargs.setdefault("env", dict(os.environ))
    execute_kwargs.setdefault("use_cache", not args.no_cache)
    execute_kwargs.setdefault("stderr", sys.stderr if args.verbose else None)
    envelope = broker.execute("%s.%s" % (args.service, args.action), params, **execute_kwargs)
    return _emit(envelope)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
