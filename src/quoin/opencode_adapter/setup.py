"""One-command OpenCode setup (`quoin opencode setup`) and the session
launcher checks behind `quoin code`.

Setup composes the existing individual steps: locate the pinned OpenCode
binary (offering a user-level install for personal profiles), write the
project scaffold, create or reuse a personal profile from `quoin models`,
classify an unclassified project as personal (personal profiles only),
qualify each profile model with the gateway probe, compile the profile, and
finish with the doctor. Every step first checks whether it is already
satisfied, so a re-run with nothing to do writes nothing.

Setup never writes or resolves a credential except through the probe, which
resolves the profile's credential reference at use time; it only names the
environment variable or keychain entry the profile expects.

Everything the steps touch outside the project (environment, home, binary
lookup, version reader, installer, probe, prompt) is passed in through
`Host`, so tests run without a network, a real binary or a terminal.
"""
from __future__ import annotations

import io
import json
import os
import subprocess
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from types import MappingProxyType
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence

from . import compiler, install, jsonio, manifest, paths, profile_dirs, versions
from . import secrets as credentials
from .errors import ConfigErrors

STATE_FILE = "opencode-setup.json"
STATE_SCHEMA = "quoin.opencode.setup-state"
STATE_SCHEMA_VERSION = 1
STATE_MAX_BYTES = 64 * 1024

# The official OpenCode install script. It installs into the user's home
# (~/.opencode/bin) and honours VERSION to select a release.
INSTALL_SCRIPT_URL = "https://opencode.ai/install"
USER_BIN_DIR = (".opencode", "bin")

NONINTERACTIVE_TEXT = "this step needs a confirmation; run interactively or pass --yes"


class SetupError(Exception):
    """A step could not finish; the message says what remains."""


@dataclass
class Host:
    """The outside world, injectable for tests."""

    environ: Mapping[str, str]
    config_env: Mapping[str, str]
    home: Path
    now: datetime
    platform: str
    source_dir: Path
    which: Callable[[str], Optional[str]]
    version_runner: Callable[[str], Optional[str]]
    installer: Callable[[str, Mapping[str, str]], int]
    probe: Callable[..., int]
    ask: Optional[Callable[[str], bool]]
    out: Any
    err: Any


@dataclass
class Report:
    changed: List[str] = field(default_factory=list)
    remaining: List[str] = field(default_factory=list)

    def did(self, text: str) -> None:
        self.changed.append(text)

    def todo(self, text: str) -> None:
        if text not in self.remaining:
            self.remaining.append(text)


# ------------------------------------------------------------------ state


def state_path(host: "Host", project_root) -> Path:
    """The user's setup record for one project. It lives in the user's state
    directory, keyed by project, so it is never committed with the project
    and one user's confirmations never count for another."""
    root = profile_dirs.adapter_root(host.environ, host.home)
    return root / "setup" / ("%s.json" % paths.project_key(Path(project_root)))


def load_state(host: "Host", project_root) -> Dict[str, Any]:
    """The setup record, or an empty one when absent or invalid. It holds no
    secrets: the last profile used and the model ids the user confirmed per
    profile."""
    try:
        got = jsonio.read_regular_bytes(state_path(host, project_root), max_bytes=STATE_MAX_BYTES)
    except (OSError, ValueError):
        return {}
    if got is None:
        return {}
    try:
        data = json.loads(got[0].decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return {}
    if not isinstance(data, dict) or data.get("schema") != STATE_SCHEMA:
        return {}
    return data


def save_state(host: "Host", project_root, data: Dict[str, Any]) -> bool:
    """Write the record only when it changed. Returns True when written."""
    data = dict(data)
    data["schema"] = STATE_SCHEMA
    data["schema_version"] = STATE_SCHEMA_VERSION
    if load_state(host, project_root) == data:
        return False
    target = state_path(host, project_root)
    jsonio.write_private_atomic(target, (json.dumps(data, indent=2, sort_keys=True) + "\n").encode("utf-8"))
    return True


def confirmed_ids(state: Mapping[str, Any], profile: str) -> List[str]:
    value = (state.get("confirmed_model_ids") or {}).get(profile)
    return [v for v in value if isinstance(v, str)] if isinstance(value, list) else []


# ------------------------------------------------------------------ helpers


def default_installer(version: str, environ: Mapping[str, str]) -> int:
    """Run the official install script for `version` into ~/.opencode/bin.

    The script gets only HOME and PATH, so no provider credential in the
    caller's environment reaches it, and it is told not to edit shell files.
    It is downloaded first so a failed download is not hidden by the shell."""
    env = {key: environ[key] for key in ("HOME", "PATH") if key in environ}
    script = (
        'tmp=$(mktemp) || exit 1; trap \'rm -f "$tmp"\' EXIT; '
        'curl -fsSL "$1" -o "$tmp" || exit 1; '
        'bash "$tmp" --version "$2" --no-modify-path'
    )
    try:
        proc = subprocess.run(
            ["/bin/sh", "-c", script, "sh", INSTALL_SCRIPT_URL, version],
            env=env,
            stdin=subprocess.DEVNULL,
        )
    except OSError:
        return 127
    return proc.returncode


def default_ask(prompt: str) -> bool:
    try:
        answer = input(prompt + " [y/N] ")
    except EOFError:
        return False
    return answer.strip().lower() in ("y", "yes")


def _confirm(host: Host, prompt: str, *, yes: bool) -> Optional[bool]:
    """True/False from the user, True for --yes, None when nobody can answer."""
    if yes:
        return True
    if host.ask is None:
        return None
    return host.ask(prompt)


def _version_of(host: Host, binary: str) -> Optional[str]:
    from .doctor import _VERSION_RE

    output = host.version_runner(binary)
    match = _VERSION_RE.search(output) if output else None
    return match.group(0) if match else None


def _user_binary(host: Host) -> Path:
    return host.home.joinpath(*USER_BIN_DIR, "opencode")


def _accepts(qualified: Sequence[str], version: Optional[str], work: bool) -> bool:
    """Whether a launch would accept this runtime: a qualified release for a
    work profile; a qualified or newer-than-floor release for a personal one."""
    return versions.judge(version, qualified).launches("work" if work else "personal")


def _locate(host: Host, qualified: Sequence[str], work: bool = False):
    """(binary, path_hint, found_text). `binary` is an acceptable executable
    or None; `path_hint` says how to fix PATH when the acceptable one is not
    what PATH resolves; `found_text` describes what PATH resolves otherwise."""
    on_path = host.which("opencode")
    path_version = _version_of(host, on_path) if on_path else None
    if on_path and _accepts(qualified, path_version, work):
        return on_path, None, ""
    user = _user_binary(host)
    if user.is_file() and os.access(str(user), os.X_OK) and _accepts(qualified, _version_of(host, str(user)), work):
        if on_path:
            hint = "put %s ahead of %s on PATH" % (user.parent, Path(on_path).parent)
        else:
            hint = "add %s to PATH" % user.parent
        return str(user), hint, ""
    found = "not found" if on_path is None else "reports %s" % (path_version or "no version")
    return None, None, found


UNREADABLE = "unreadable"


def profile_classification(host: Host, profile: str) -> Optional[str]:
    """The profile's declared classification; None when the profile does not
    exist, UNREADABLE when it exists but its classification cannot be read."""
    try:
        target = paths.profile_path(profile, host.config_env, host.home)
    except ValueError:
        return None
    if not os.path.lexists(str(target)):
        return None
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return UNREADABLE
    value = data.get("classification") if isinstance(data, dict) else None
    return value if isinstance(value, str) else UNREADABLE


def project_classification(project_root) -> Optional[str]:
    try:
        data = json.loads(paths.project_runtime_path(Path(project_root)).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    value = data.get("classification") if isinstance(data, dict) else None
    return value if isinstance(value, str) else None


def is_work(host: Host, project_root, profile: str) -> bool:
    """Work profile or work project. A profile that exists but cannot be read
    counts as work, so the stricter path wins when in doubt."""
    return profile_classification(host, profile) in ("work", UNREADABLE) or (
        project_classification(project_root) == "work"
    )


def credential_hint(host: Host, profile: str) -> List[str]:
    """What the user must provide for each provider credential the profile
    references. Hints starting with "set the " name a missing environment
    variable and block; keychain hints are informational because a keychain
    entry cannot be checked without resolving it. Never resolves a value."""
    try:
        data = json.loads(paths.profile_path(profile, host.config_env, host.home).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    out = []
    providers = data.get("providers") if isinstance(data, dict) else None
    for provider in (providers or {}).values():
        ref = provider.get("credential_ref") if isinstance(provider, dict) else None
        try:
            parsed = credentials.parse(ref)
        except ValueError:
            continue
        if parsed.scheme == "env":
            if not host.environ.get(parsed.name):
                out.append("set the %s environment variable (setup never stores credentials)" % parsed.name)
        else:
            out.append(
                "make sure the keychain entry %s/%s exists (setup never stores credentials)"
                % (parsed.service, parsed.account)
            )
    return out


# ------------------------------------------------------------------ steps


def step_binary(host: Host, report: Report, *, work: bool, yes: bool) -> Optional[str]:
    """Ensure an acceptable OpenCode release is available (a qualified one for
    work, a qualified or newer one for personal); offer to install the highest
    qualified release otherwise. Returns its path."""
    qualified = manifest.read_qualified_versions(host.source_dir)
    pinned = qualified[-1]
    binary, hint, found = _locate(host, qualified, work)
    if binary:
        if hint:
            report.todo(hint)
        return binary
    if work:
        report.todo(
            "install OpenCode %s yourself and put it on PATH (opencode %s); work profiles "
            "accept only qualified releases (%s) and setup never installs a binary for them"
            % (pinned, found, ", ".join(qualified))
        )
        return None
    answer = _confirm(
        host,
        "OpenCode %s is required (opencode %s). Install it into ~/%s with the official "
        "installer (shell files are not changed)?" % (pinned, found, "/".join(USER_BIN_DIR)),
        yes=yes,
    )
    if answer is None:
        report.todo("install OpenCode %s: %s" % (pinned, NONINTERACTIVE_TEXT))
        return None
    if not answer:
        report.todo("install OpenCode %s and put it on PATH (install declined)" % pinned)
        return None
    print("installing OpenCode %s with %s" % (pinned, INSTALL_SCRIPT_URL), file=host.out)
    code = host.installer(pinned, host.environ)
    binary, hint, _ = _locate(host, qualified, work)
    if code != 0 or binary is None:
        report.todo("install OpenCode %s and put it on PATH (the installer did not produce it)" % pinned)
        return None
    report.did("installed OpenCode %s" % pinned)
    if hint:
        report.todo(hint)
    return binary


def step_scaffold(host: Host, report: Report, project_root: Path) -> bool:
    sink = io.StringIO()
    code = install.run_install(project_root, host.source_dir, None, True, sink, sink)
    if code == 0:
        return True
    if code != 1:
        host.err.write(sink.getvalue())
        report.todo("resolve the project scaffold problem above, then rerun setup")
        return False
    sink = io.StringIO()
    code = install.run_install(project_root, host.source_dir, None, False, sink, sink)
    if code != 0:
        host.err.write(sink.getvalue())
        report.todo("resolve the project scaffold problem above, then rerun setup")
        return False
    report.did("wrote the project scaffold")
    return True


def step_profile(host: Host, report: Report, project_root: Path, profile: str, state: Dict[str, Any],
                 *, yes: bool) -> bool:
    """Reuse an existing profile, or create a personal one from `quoin models`
    after the user confirms every provider model id."""
    from . import import_preview

    target = paths.profile_path(profile, host.config_env, host.home)
    if os.path.lexists(str(target)):
        return True
    tiers, _ = import_preview.read_source(host.home)
    proposal = import_preview.propose(tiers, profile_name=profile)
    wanted = list(proposal.model_ids)
    if yes:
        if not set(wanted) <= set(confirmed_ids(state, profile)):
            report.todo(
                "confirm the model ids for profile %s: --yes only reuses confirmations already "
                "given; rerun setup interactively" % profile
            )
            return False
        confirmed = wanted
    elif host.ask is None:
        report.todo("confirm the model ids for profile %s: %s" % (profile, NONINTERACTIVE_TEXT))
        return False
    else:
        print("profile %s will use these provider model ids (from quoin models):" % profile, file=host.out)
        confirmed = []
        for model_id in wanted:
            if not host.ask("  use model id %s?" % model_id):
                report.todo(
                    "profile %s was not created (model id %s declined); edit quoin models and rerun setup"
                    % (profile, model_id)
                )
                return False
            confirmed.append(model_id)
    import_preview.apply(proposal, confirmed=confirmed, force=False, env=host.config_env, home=host.home)
    state.setdefault("confirmed_model_ids", {})[profile] = sorted(confirmed)
    report.did("created profile %s" % profile)
    return True


def step_project(host: Host, report: Report, project_root: Path, profile: str, *, yes: bool) -> bool:
    """Classify an unclassified project as personal, for a personal profile.
    An existing project file is never changed, and a work classification is
    never written: that is an explicit decision for the project owner."""
    target = paths.project_runtime_path(project_root)
    if os.path.lexists(str(target)):
        return True
    if profile_classification(host, profile) != "personal":
        report.todo(
            "classify this project in .quoin/runtime.json; setup only writes a personal "
            "classification for a personal profile"
        )
        return False
    answer = _confirm(host, "Classify this project as personal in .quoin/runtime.json?", yes=yes)
    if answer is None:
        report.todo("classify this project as personal: %s" % NONINTERACTIVE_TEXT)
        return False
    if not answer:
        report.todo("classify this project in .quoin/runtime.json (declined)")
        return False
    document = {"schema_version": 1, "runtime": "opencode", "profile": profile, "classification": "personal"}
    target.parent.mkdir(parents=True, exist_ok=True)
    install.atomic_write(target, (json.dumps(document, indent=2) + "\n").encode("utf-8"))
    report.did("classified the project as personal")
    return True


def _evaluate(host: Host, project_root: Path, profile: str):
    return compiler.evaluate(
        project_root=project_root, profile=profile, env=host.config_env, home=host.home, now=host.now,
    )


def step_probe(host: Host, report: Report, project_root: Path, profile: str) -> bool:
    """Probe every profile model that is not currently qualified."""
    ev = _evaluate(host, project_root, profile)
    pending = [name for name, q in ev.qualifications.items() if q.state != "qualified"]
    if not pending:
        return True
    hints = credential_hint(host, profile)
    if any(h.startswith("set the ") for h in hints):
        for hint in hints:
            report.todo(hint)
        report.todo("rerun setup to qualify: %s" % ", ".join(pending))
        return False
    ok = True
    for name in pending:
        code = host.probe(
            profile=profile, model=name, project_root=None, synthetic_only=True,
            env=host.config_env, environ=MappingProxyType(dict(host.environ)),
            home=host.home, now=host.now, platform=host.platform,
        )
        if code == 0:
            report.did("qualified %s" % name)
        else:
            ok = False
            report.todo(
                "model %s did not qualify; check the gateway and rerun "
                "quoin opencode probe --profile %s --synthetic-only --model %s" % (name, profile, name)
            )
    return ok


def compiled_directory(host: Host, ev) -> Path:
    directory = profile_dirs.compiled_dir(
        profile_dirs.adapter_root(host.environ, host.home), ev.profile, ev.project_root
    )
    return compiler.resolve_output_dir(ev, output=directory, env=host.config_env, home=host.home)


def step_compile(host: Host, report: Report, project_root: Path, profile: str) -> bool:
    """Compile the profile unless the compiled pair is already up to date."""
    ev = _evaluate(host, project_root, profile)
    try:
        directory = compiled_directory(host, ev)
        if compiler.check(ev, directory).ok:
            return True
        result = compiler.build(ev)
        compiler.write(result, directory)
    except compiler.CompileBlocked as exc:
        from . import errors

        for finding in exc.findings:
            report.todo("compile: %s" % errors.FINDING_MESSAGES[finding.code])
        return False
    except compiler.CompileGateError as exc:
        report.todo("compile: %s" % exc)
        return False
    except compiler.OutputRefused as exc:
        report.todo("compile: %s (fix: %s)" % (exc, exc.fix))
        return False
    except (jsonio.UnsafeDirectoryError, OSError):
        report.todo("compile: the compiled files could not be written; check the profile state directory")
        return False
    report.did("compiled profile %s" % profile)
    return True


def step_doctor(host: Host, report: Report, project_root: Path, profile: str) -> int:
    from . import doctor

    sink = io.StringIO()
    code = doctor.run_doctor(
        project_root, host.source_dir, False, False, sink, sink,
        env=host.environ, home=host.home, which=host.which, version_runner=host.version_runner,
        profile=profile, config_env=dict(host.config_env), now=host.now,
    )
    if code == doctor.exit_code("warnings"):
        # Warnings do not block a session; show them with their remediation.
        lines = sink.getvalue().splitlines()
        for index, line in enumerate(lines):
            if line.startswith("WARN "):
                host.out.write(line + "\n")
                if index + 1 < len(lines) and lines[index + 1].startswith(" "):
                    host.out.write(lines[index + 1] + "\n")
        return 0
    if code != 0:
        host.out.write(sink.getvalue())
        report.todo("fix the doctor findings above")
    return code


# ------------------------------------------------------------------ commands


def _import_refused():
    from .import_preview import ImportRefused

    return ImportRefused


def _guard(report: Report, func, *args, **kwargs):
    """Run a step, turning configuration and data errors into a remaining item."""
    try:
        return func(*args, **kwargs)
    except ConfigErrors as exc:
        for item in exc.errors:
            report.todo(str(item))
    except paths.AdapterDataMissing:
        report.todo("packaged adapter data not found; reinstall quoin")
    except manifest.ManifestLoadError:
        report.todo("the qualified OpenCode releases could not be read; reinstall quoin")
    except jsonio.UnsafeDirectoryError as exc:
        report.todo("a configuration directory is not private enough: %s" % exc)
    except _import_refused() as exc:
        report.todo(str(exc))
    except ValueError as exc:
        report.todo(str(exc))
    except OSError:
        report.todo("a configuration file could not be read or written; check the profile directories")
    return None


def _print_report(host: Host, report: Report) -> None:
    if not report.changed and not report.remaining:
        print("setup: nothing to do; everything is already set up", file=host.out)
    for line in report.changed:
        print("done: %s" % line, file=host.out)
    if report.remaining:
        print("remaining:", file=host.err)
        for line in report.remaining:
            print("  - %s" % line, file=host.err)


def run_setup(host: Host, project_root, profile: str, *, yes: bool = False) -> int:
    """`quoin opencode setup`. Returns 0 when nothing remains, 1 otherwise."""
    root = Path(project_root).resolve()
    report = Report()
    if not root.is_dir():
        print("quoin: project root %s is not a directory" % project_root, file=host.err)
        return 2
    if not install.PROFILE_RE.fullmatch(profile):
        print("quoin: profile %r is not a valid profile label" % (profile,), file=host.err)
        return 2
    state = load_state(host, root)
    work = is_work(host, root, profile)

    binary = _guard(report, step_binary, host, report, work=work, yes=yes)
    scaffold = _guard(report, step_scaffold, host, report, root)
    have_profile = bool(scaffold) and bool(_guard(report, step_profile, host, report, root, profile, state, yes=yes))
    if have_profile:
        classified = _guard(report, step_project, host, report, root, profile, yes=yes)
        qualified = classified and _guard(report, step_probe, host, report, root, profile)
        if qualified:
            _guard(report, step_compile, host, report, root, profile)
        notes = []
        for hint in credential_hint(host, profile):
            (report.todo if hint.startswith("set the ") else notes.append)(hint)
        for note in notes:
            print("note: %s" % note, file=host.out)
        if binary is not None and not report.remaining:
            _guard(report, step_doctor, host, report, root, profile)
    state["last_profile"] = profile if have_profile else state.get("last_profile")
    if state.get("last_profile") is None:
        state.pop("last_profile", None)
    try:
        save_state(host, root, state)
    except (OSError, jsonio.UnsafeDirectoryError):
        report.todo("the setup record could not be written to the user state directory")
    _print_report(host, report)
    if not report.remaining:
        print("next: quoin code", file=host.out)
    return 0 if not report.remaining else 1


def resolve_code_profile(host: Host, project_root, profile: Optional[str]) -> Optional[str]:
    """The explicit profile, else the one last used in this project."""
    if profile:
        return profile
    value = load_state(host, Path(project_root).resolve()).get("last_profile")
    return value if isinstance(value, str) and install.PROFILE_RE.fullmatch(value) else None


def valid_profile(profile: str) -> bool:
    return bool(install.PROFILE_RE.fullmatch(profile))


def prepare_code(host: Host, project_root, profile: str) -> int:
    """The fast checks behind `quoin code`: binary, scaffold, profile,
    credentials, and a recompile when the profile or project changed. The
    full doctor is left to setup; the launch itself repeats the binary and
    configuration checks. Records the profile as this project's last one.
    Returns 0 when the session can start."""
    root = Path(project_root).resolve()
    report = Report()
    qualified = _guard(report, manifest.read_qualified_versions, host.source_dir)
    binary = host.which("opencode")
    if binary is None:
        report.todo("OpenCode is not on PATH; run quoin opencode setup")
    elif qualified is not None:
        work = is_work(host, root, profile)
        verdict = versions.judge(_version_of(host, binary), qualified)
        if not verdict.launches("work" if work else "personal"):
            report.todo(
                "OpenCode on PATH reports %s, which the %s profile does not accept (qualified: %s); "
                "run quoin opencode setup"
                % (verdict.version or "no version", "work" if work else "personal", ", ".join(qualified))
            )
    sink = io.StringIO()
    if install.run_install(root, host.source_dir, None, True, sink, sink) != 0:
        report.todo("the project scaffold is missing or out of date; run quoin opencode setup")
    if not os.path.lexists(str(paths.profile_path(profile, host.config_env, host.home))):
        report.todo("profile %s does not exist; run quoin opencode setup --profile %s" % (profile, profile))
    else:
        for hint in credential_hint(host, profile):
            if hint.startswith("set the "):
                report.todo(hint)
        if not report.remaining and _guard(report, step_compile, host, report, root, profile) is not True:
            report.todo("rerun quoin opencode setup to requalify or fix the profile")
    if report.remaining:
        print("quoin: cannot start the session:", file=host.err)
        for line in report.remaining:
            print("  - %s" % line, file=host.err)
        return 1
    for line in report.changed:
        print("done: %s" % line, file=host.out)
    state = load_state(host, root)
    state["last_profile"] = profile
    try:
        save_state(host, root, state)
    except (OSError, jsonio.UnsafeDirectoryError):
        pass
    return 0


def default_host(source_dir: Path, *, config_env: Mapping[str, str], now: datetime, platform: str,
                 interactive: bool, out, err) -> Host:
    import shutil

    from . import doctor, probe_cli

    return Host(
        environ=MappingProxyType(dict(os.environ)),
        config_env=dict(config_env),
        home=Path.home(),
        now=now,
        platform=platform,
        source_dir=Path(source_dir),
        which=shutil.which,
        version_runner=doctor._default_version_runner,
        installer=default_installer,
        probe=probe_cli.run,
        ask=default_ask if interactive else None,
        out=out,
        err=err,
    )


__all__: Sequence[str] = (
    "Host", "Report", "run_setup", "prepare_code", "resolve_code_profile", "default_host",
    "load_state", "save_state", "state_path",
)
