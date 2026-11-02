"""`workctl-write`: approve, execute and reconcile external writes.

    workctl-write approve PROPOSAL [--out APPROVAL]
    workctl-write execute PROPOSAL APPROVAL
    workctl-write reconcile IDEMPOTENCY_KEY [--confirm-not-sent]
    workctl-write status IDEMPOTENCY_KEY

These commands belong to the person and the writer, not to the agent: they
refuse to run inside a launcher session, `approve` and `--confirm-not-sent`
need an interactive terminal, and they read the approval key and write
credentials that the agent's broker never touches. Every command prints
one JSON envelope on stdout; `approve` shows the proposal on stderr.
"""
from __future__ import annotations

import os
import sys
import uuid
from typing import Any, List, Optional, Sequence

from . import writer
from .cli import _emit, _HelpRequested, _Parser, _UsageError
from .envelope import INVALID_INPUT, BrokerError, failure, success
from .transport import scrub_environment


def build_parser() -> _Parser:
    parser = _Parser(prog="workctl-write", description="Approve and execute workctl write proposals.")
    commands = parser.add_subparsers(dest="command", parser_class=_Parser)
    approve = commands.add_parser("approve")
    approve.add_argument("proposal")
    approve.add_argument("--out")
    execute = commands.add_parser("execute")
    execute.add_argument("proposal")
    execute.add_argument("approval")
    reconcile = commands.add_parser("reconcile")
    reconcile.add_argument("key")
    reconcile.add_argument("--confirm-not-sent", dest="confirm_not_sent", action="store_true")
    status = commands.add_parser("status")
    status.add_argument("key")
    return parser


def main(argv: Optional[Sequence[str]] = None, **kwargs: Any) -> int:
    scrub_environment()
    args_list: List[str] = list(sys.argv[1:] if argv is None else argv)
    try:
        args = build_parser().parse_args(args_list)
        if not args.command:
            raise _UsageError("give a command: approve, execute, reconcile or status")
    except _HelpRequested as exc:
        return _emit(success({"usage": exc.text}, pagination=None, provenance=None, request_id=uuid.uuid4().hex))
    except _UsageError as exc:
        sys.stderr.write("workctl-write: %s\n" % exc)
        err = BrokerError(INVALID_INPUT, "invalid command line; run workctl-write --help for the syntax")
        return _emit(failure(err, request_id=uuid.uuid4().hex))
    kwargs.setdefault("env", dict(os.environ))
    if args.command == "approve":
        return _emit(writer.approve(args.proposal, out=args.out, **kwargs))
    if args.command == "execute":
        return _emit(writer.execute(args.proposal, args.approval, **kwargs))
    if args.command == "reconcile":
        return _emit(writer.reconcile(args.key, confirm_not_sent=args.confirm_not_sent, **kwargs))
    return _emit(writer.status(args.key, **kwargs))


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
