"""`quoin streamdeck ...` dispatch. The parser lives in `quoin.cli`; this module does the work.

The three `_ctx_factory` / `_sysops` / `_platform` seams are the only places the real machine is
reached from here; tests replace them.
"""
import datetime
import os
import sys

from quoin import __about__

from . import COMPONENT_VERSION, engine, macos, paths as paths_mod, payload_fingerprint, prereqs, status

PATH_FLAGS = ("runtime_dir", "plugins_dir", "state_root", "claude_settings", "codex_home", "agentdesk")


def _ctx_factory(paths, flags, recorded=None, legacy=None):
    return prereqs.default_ctx(paths, flags, recorded=recorded, legacy=legacy)


def _sysops():
    return engine.default_sysops()


def _platform():
    return macos.detect()


def _now():
    return datetime.datetime.now(datetime.timezone.utc)


def _flags(args):
    flags = {}
    for name in PATH_FLAGS:
        value = getattr(args, name, None)
        if value:
            flags[name] = value
    for name in ("python", "node"):
        value = getattr(args, name, None)
        if value:
            flags[name] = value
    return flags


def _env(args, flags, paths):
    runtimes = getattr(args, "runtimes", None)
    return engine.Env(
        paths=paths, flags=flags, runtimes=tuple(runtimes) if runtimes is not None else None,
        prereq_mode=getattr(args, "prereqs", "ask"), dry_run=bool(getattr(args, "dry_run", False)),
        open_profile=bool(getattr(args, "open_profile", False)), purge=bool(getattr(args, "purge", False)),
        allow_downgrade=bool(getattr(args, "allow_downgrade", False)), ctx_factory=_ctx_factory,
        sysops=_sysops(), environ=dict(os.environ), now=_now(),
        platform_info=_platform(), package_version=__about__.__version__, out=print)


def _status(args, flags, paths):
    report = status.collect(paths, package_version=COMPONENT_VERSION, package_fingerprint=payload_fingerprint(),
                            platform_info=_platform(), now=_now())
    status.finalize(report)
    text = status.render_json(report) + "\n" if getattr(args, "json", False) else status.render_text(report)
    sys.stdout.write(text)
    return report["exit_code"]


def run(args):
    verb = args.streamdeck_command
    flags = _flags(args)
    paths = paths_mod.resolve(flags)
    if verb == "status":
        return _status(args, flags, paths)
    env = _env(args, flags, paths)
    try:
        if verb == "install":
            result = engine.install(env)
        elif verb == "upgrade":
            result = engine.upgrade(env)
        elif verb == "uninstall":
            result = engine.uninstall(env)
        else:
            return 1
    except engine.OperationError as exc:
        print(exc.message, file=sys.stderr)
        return exc.code
    return result.exit_code
