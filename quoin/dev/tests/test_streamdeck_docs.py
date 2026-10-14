"""The Stream Deck documentation matches the shipped command line, codes, events, paths and messages.

Pure reads of tracked files and of the library's own tables. Every check is a function that returns a list of
problems for a given text, and a second test runs each one against a copy of the text with the matched content
removed, so a check that can no longer fail is itself reported.
"""
import pathlib
import re

import pytest

import _streamdeck_lifecycle as lc  # noqa: F401
from _streamdeck_ops import (deck_home, forbid_dangerous_subprocess, forbid_real_home_io, forbid_real_seams)  # noqa: F401
from quoin import cli
from quoin.streamdeck import engine, hooks_merge, paths, prereqs, status

pytestmark = pytest.mark.usefixtures("forbid_dangerous_subprocess", "forbid_real_home_io", "forbid_real_seams")

REPO = pathlib.Path(__file__).resolve().parents[3]
GUIDE_PATH = REPO / "quoin" / "docs" / "streamdeck" / "README.md"
VALIDATION_PATH = REPO / "quoin" / "docs" / "streamdeck" / "VALIDATION.md"
VERBS = ("install", "upgrade", "status", "uninstall")
HEADINGS = (
    "What it does", "Prerequisites", "Commands", "Upgrading", "Where things live", "Hooks and trust", "Profile",
    "Status and problem codes", "Interrupted runs and recovery", "Migrating the prototype install",
    "Uninstall and rollback", "Privacy", "Troubleshooting", "What is still checked by hand",
)
PLANNING_WORDS = re.compile(
    r"\b[TDRQSF]-\d{2}\b|\bAC-\d|\bFR-\d|\b(MAJ|MIN|CRIT)-\d|\bround \d|\.workflow_artifacts|current-plan\.md|"
    r"architecture\.md|critic-response")
QUOTED_MESSAGES = (
    "re-run the command", "ignored an unexpected path in the operation record", "nothing to remove",
    "another quoin streamdeck operation is running", "not installed; run `quoin streamdeck install`",
    "rolled back an interrupted operation", "completing an interrupted operation",
    "ignored an unusable binary record in the operation record", "no usable python is recorded",
)


def read(path):
    return path.read_text(encoding="utf-8")


def headings(text):
    return re.findall(r"^## (.+)$", text, re.M)


def section(text, heading):
    match = re.search(r"^## %s\s*$" % re.escape(heading), text, re.M)
    if not match:
        return ""
    rest = text[match.end():]
    nxt = re.search(r"^## ", rest, re.M)
    return rest[:nxt.start()] if nxt else rest


def help_options(verb, capsys):
    with pytest.raises(SystemExit):
        cli.main(["streamdeck", verb, "--help"])
    out = capsys.readouterr().out
    usage = out.split("\n\n", 1)[0]
    block = out.split("options:", 1)[1] if "options:" in out else out
    found = set(re.findall(r"--[a-z][a-z-]*", usage + block.split("exit codes:")[0]))
    found.discard("--help")
    return found


# ------------------------------------------------------------------ checks (text -> problems)

def check_headings(guide):
    found = [h for h in headings(guide) if h in HEADINGS]
    return [] if found == list(HEADINGS) else ["headings %r" % (found,)]


def check_upgrade_contract(guide):
    body = section(guide, "Upgrading")
    return ["Upgrading lacks %r" % n for n in ("pip install --upgrade quoin", "payload only", "quoin streamdeck upgrade")
            if n not in body]


def check_migration(guide):
    body = section(guide, "Migrating the prototype install")
    needles = ["pip install --upgrade quoin", "quoin install", "quoin streamdeck status",
               "quoin streamdeck install --dry-run", "quoin streamdeck install", "/hooks"]
    problems, at = [], 0
    for needle in needles:
        index = body.find(needle, at)
        if index < 0:
            problems.append("Migrating lacks %r after position %d" % (needle, at))
            break
        at = index + len(needle)
    return problems


def check_uninstall(guide):
    body = section(guide, "Uninstall and rollback")
    return ["Uninstall lacks %r" % n for n in ("--purge", "backups") if n not in body]


def check_troubleshooting(guide):
    body = section(guide, "Troubleshooting")
    return ["Troubleshooting lacks %r" % n for n in ("npx", "uvx", "gh", "jq", "launchd") if n not in body]


def check_codes(guide):
    body = section(guide, "Status and problem codes")
    return ["code %s is not documented" % c for c in status.PROBLEM_CODES + status.INFO_CODES
            if "`%s`" % c not in body]


def check_exit_codes(guide):
    body = section(guide, "Commands")
    needles = ("0 success", "1 refused or failed", "2 unsupported platform", "0 healthy", "10 not installed",
               "11 problems found", "12 an operation was interrupted")
    return ["Commands lacks the exit code %r" % n for n in needles if n not in body]


def check_events(guide):
    body = section(guide, "Hooks and trust")
    return ["event %s:%s is not documented" % (kind, event) for kind, events in hooks_merge.EVENTS.items()
            for event in events if event not in body]


def check_env_names(guide):
    body = section(guide, "Where things live")
    names = [getattr(paths, n) for n in dir(paths) if n.startswith("ENV_")] + [prereqs.ENV_APPS_DIR]
    return ["environment variable %s is not documented" % n for n in names if n not in body]


def check_quoted_messages(guide):
    source = read(pathlib.Path(engine.__file__))
    return ["%r is not in the guide" % m for m in QUOTED_MESSAGES if m not in guide] + \
           ["%r is not in the engine" % m for m in QUOTED_MESSAGES if m not in source]


def check_vocabulary(text):
    problems = ["planning vocabulary %r" % m.group(0) for m in PLANNING_WORDS.finditer(text)]
    problems += ["machine path literal %r" % n for n in ("/opt/homebrew", "/Users/") if n in text]
    return problems


def check_validation(text):
    problems = []
    found = headings(text)
    for heading in ("Verified by automated tests", "Verified manually", "Not yet verified"):
        if heading not in found:
            problems.append("VALIDATION lacks the section %r" % heading)
    for name in sorted(set(re.findall(r"test_[a-z0-9_]+\.py", text))):
        if not (REPO / "quoin" / "dev" / "tests" / name).is_file():
            problems.append("VALIDATION names a missing test file %s" % name)
    items = re.split(r"^- ", section(text, "Not yet verified"), flags=re.M)[1:]
    if not items:
        problems.append("no items under Not yet verified")
    for item in items:
        if "Steps" not in item or "Expected" not in item:
            problems.append("an unverified item lacks Steps or Expected: %s" % item.splitlines()[0])
    return problems


GUIDE_CHECKS = (check_headings, check_upgrade_contract, check_migration, check_uninstall, check_troubleshooting,
                check_codes, check_exit_codes, check_events, check_env_names, check_quoted_messages)


# ------------------------------------------------------------------ the guide

@pytest.mark.parametrize("check", GUIDE_CHECKS, ids=lambda c: c.__name__)
def test_the_guide_passes(check):
    assert check(read(GUIDE_PATH)) == []


def test_every_command_option_is_in_the_commands_section(capsys):
    body = section(read(GUIDE_PATH), "Commands")
    for verb in VERBS:
        options = help_options(verb, capsys)
        assert options, verb
        missing = sorted(o for o in options if o not in body)
        assert not missing, (verb, missing)


def test_every_flag_shown_after_a_streamdeck_command_exists(capsys):
    helps = {verb: help_options(verb, capsys) for verb in VERBS}
    guide = read(GUIDE_PATH)
    units = re.findall(r"`([^`\n]+)`", guide)
    in_fence = False
    for line in guide.splitlines():
        if line.startswith("```"):
            in_fence = not in_fence
        elif in_fence:
            units.append(line)
    seen = 0
    for unit in units:
        for match in re.finditer(r"quoin streamdeck (install|upgrade|status|uninstall)([^;&|]*)", unit):
            verb, rest = match.groups()
            for flag in re.findall(r"--[a-z][a-z-]*", rest):
                seen += 1
                assert flag in helps[verb], (unit, flag)
    assert seen >= 5


# ------------------------------------------------------------------ the validation record and shipped sections

def test_the_validation_record_is_complete():
    assert check_validation(read(VALIDATION_PATH)) == []


def shipped_texts():
    readme = read(REPO / "README.md")
    quickstart = read(REPO / "quoin" / "QUICKSTART.md")
    return {
        "guide": read(GUIDE_PATH),
        "validation": read(VALIDATION_PATH),
        "README section": section(readme, "Stream Deck (optional)"),
        "QUICKSTART section": section(quickstart, "Optional: Stream Deck"),
    }


@pytest.mark.parametrize("name", ["guide", "validation", "README section", "QUICKSTART section"])
def test_shipped_docs_use_plain_words(name):
    text = shipped_texts()[name]
    assert text.strip(), "the %s is missing" % name
    assert check_vocabulary(text) == []


def test_the_readme_and_quickstart_point_at_the_guide():
    texts = shipped_texts()
    for name in ("README section", "QUICKSTART section"):
        assert "quoin/docs/streamdeck/README.md" in texts[name], name
    assert "quoin/docs/streamdeck/VALIDATION.md" in texts["README section"]
    for command in ("install", "upgrade", "status", "uninstall"):
        assert "quoin streamdeck %s" % command in texts["QUICKSTART section"]
    assert "pip install --upgrade quoin" in texts["README section"]


# ------------------------------------------------------------------ each check can fail

BREAKERS = {
    check_headings: lambda g: g.replace("## Profile\n", "## Pictures\n"),
    check_upgrade_contract: lambda g: g.replace("payload only", "everything"),
    check_migration: lambda g: g.replace("`/hooks`", "the hooks screen"),
    check_uninstall: lambda g: g.replace("`--purge`", "the purge option"),
    check_troubleshooting: lambda g: g.replace("launchd", "the system"),
    check_codes: lambda g: g.replace("`HEALTH_STALE`", "the stale health code"),
    check_exit_codes: lambda g: g.replace("12 an operation was interrupted", "12 other"),
    check_events: lambda g: g.replace("Interrupt", "Halt"),
    check_env_names: lambda g: g.replace("QUOIN_STREAMDECK_APPS_DIR", "the apps variable"),
    check_quoted_messages: lambda g: g.replace("nothing to remove", "nothing left"),
}


@pytest.mark.parametrize("check", GUIDE_CHECKS, ids=lambda c: c.__name__)
def test_each_guide_check_fails_when_its_content_is_removed(check):
    guide = read(GUIDE_PATH)
    broken = BREAKERS[check](guide)
    assert broken != guide
    assert check(broken), "%s still passes without its content" % check.__name__


def test_the_flag_check_and_the_vocabulary_and_validation_checks_can_fail():
    assert check_vocabulary("see " + "T-" + "07 and " + "round " + "3 in .workflow" + "_artifacts") != []
    assert check_vocabulary("run /opt/homebrew/bin/brew") != []
    text = read(VALIDATION_PATH)
    assert check_validation(text.replace("## Not yet verified", "## Later")) != []
    assert check_validation(text.replace("test_streamdeck_hardening.py", "test_streamdeck_nothing.py")) != []
    assert check_validation(text.replace("Expected:", "Result:")) != []
