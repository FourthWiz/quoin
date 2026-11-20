"""pytest CI wrapper for jq merge-logic verification.

Hook stanza registration itself lives in `installer.py::deploy_hooks`'s
`_append_stanza` closure, not in jq — `install.sh` is a 212-line wrapper that
`exec`s `python -m quoin install` and registers nothing with jq. These tests
exist as an independent cross-check of `_append_stanza`'s dedup rule (same
matcher + same script basename → replace, not duplicate), expressed as
equivalent jq filters run against fixture JSON inputs. Three scenario
families per tuple:
  - add: settings.json has no existing stanza → stanza is added
  - update: settings.json has stale quoin stanza at old path → stanza is replaced
  - idempotent: settings.json already has the canonical stanza → no duplicate added

The four `(event, matcher)` fixtures below are illustrative of the merge rule
only — they do not track the shipped stanza roster, which is pinned instead
by `test_hook_stanza_count_parity.py` against `installer.py`'s actual
`_append_stanza` call list (duplicating that roster in a second place is the
drift class R-09 is about).

Skip condition: jq not on PATH (all tests xfail gracefully).

Run:
  python3 -m pytest quoin/dev/tests/test_jq_queries.py -v
"""

import json
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
HOOKS_DIR = "/home/user/.claude/hooks"  # canonical hooks dir used in fixture JSON


def _jq_available() -> bool:
    return shutil.which("jq") is not None


def _run_jq(jq_filter: str, input_json: dict, **args: str) -> dict:
    """Run a jq filter on input_json with --arg bindings, return parsed output."""
    cmd = ["jq"]
    for k, v in args.items():
        cmd += ["--arg", k, v]
    cmd.append(jq_filter)
    result = subprocess.run(
        cmd,
        input=json.dumps(input_json),
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"jq failed: {result.stderr}"
    return json.loads(result.stdout)


# ── jq filters expressing installer.py's dedup rule ───────────────────────────
# These are independent jq-equivalents of installer.py::deploy_hooks's
# _append_stanza dedup rule (same matcher + same script basename → replace),
# not a mirror of any install.sh query — install.sh registers nothing with jq.

UPS_FILTER = (
    '.hooks.UserPromptSubmit = ([(.hooks.UserPromptSubmit // [])[] '
    '| select(.matcher != $matcher or ([ .hooks[]?.command | select(endswith($scriptname)) ] | length == 0))] '
    '+ [{"matcher": $matcher, "hooks": [{"type": "command", "command": $cmd, "timeout": 5}]}])'
)

PC_FILTER = (
    '.hooks.PreCompact = ([(.hooks.PreCompact // [])[] '
    '| select(.matcher != $matcher or ([ .hooks[]?.command | select(endswith($scriptname)) ] | length == 0))] '
    '+ [{"matcher": $matcher, "hooks": [{"type": "command", "command": $cmd, "timeout": 10}]}])'
)

SS_FILTER = (
    '.hooks.SessionStart = ([(.hooks.SessionStart // [])[] '
    '| select(.matcher != $matcher or ([ .hooks[]?.command | select(endswith($scriptname)) ] | length == 0))] '
    '+ [{"matcher": $matcher, "hooks": [{"type": "command", "command": $cmd, "timeout": 5}]}])'
)


pytestmark = pytest.mark.skipif(
    not _jq_available(),
    reason="jq not on PATH — T-23 jq query tests require jq",
)


class TestJqMergeAdd:
    """jq queries correctly add stanzas when no prior entry exists."""

    def test_ups_add_to_empty(self):
        """UserPromptSubmit stanza added to empty hooks."""
        cmd = f"{HOOKS_DIR}/userpromptsubmit.sh"
        result = _run_jq(UPS_FILTER, {}, cmd=cmd, matcher="*", scriptname="userpromptsubmit.sh")
        stanzas = result["hooks"]["UserPromptSubmit"]
        assert len(stanzas) == 1
        assert stanzas[0]["matcher"] == "*"
        assert stanzas[0]["hooks"][0]["command"] == cmd

    def test_ups_add_to_missing_event_key(self):
        """UserPromptSubmit stanza added when other hooks exist but UPS key absent."""
        input_json = {"hooks": {"PreCompact": [{"matcher": "auto", "hooks": []}]}}
        cmd = f"{HOOKS_DIR}/userpromptsubmit.sh"
        result = _run_jq(UPS_FILTER, input_json, cmd=cmd, matcher="*", scriptname="userpromptsubmit.sh")
        assert len(result["hooks"]["UserPromptSubmit"]) == 1
        assert "PreCompact" in result["hooks"]

    def test_precompact_add_to_empty(self):
        """PreCompact stanza added to empty hooks."""
        cmd = f"{HOOKS_DIR}/precompact.sh"
        result = _run_jq(PC_FILTER, {}, cmd=cmd, matcher="auto", scriptname="precompact.sh")
        stanzas = result["hooks"]["PreCompact"]
        assert len(stanzas) == 1
        assert stanzas[0]["matcher"] == "auto"
        assert stanzas[0]["hooks"][0]["timeout"] == 10

    def test_sessionstart_startup_add(self):
        """SessionStart/startup stanza added to empty hooks."""
        cmd = f"{HOOKS_DIR}/sessionstart.sh"
        result = _run_jq(SS_FILTER, {}, cmd=cmd, matcher="startup", scriptname="sessionstart.sh")
        stanzas = result["hooks"]["SessionStart"]
        assert len(stanzas) == 1
        assert stanzas[0]["matcher"] == "startup"

    def test_sessionstart_resume_add(self):
        """SessionStart/resume stanza added when startup already exists."""
        cmd = f"{HOOKS_DIR}/sessionstart.sh"
        # First add startup
        input_with_startup = {
            "hooks": {
                "SessionStart": [
                    {"matcher": "startup", "hooks": [{"type": "command", "command": cmd, "timeout": 5}]}
                ]
            }
        }
        result = _run_jq(SS_FILTER, input_with_startup, cmd=cmd, matcher="resume", scriptname="sessionstart.sh")
        stanzas = result["hooks"]["SessionStart"]
        matchers = [s["matcher"] for s in stanzas]
        assert "startup" in matchers
        assert "resume" in matchers
        assert len(stanzas) == 2


class TestJqMergeUpdate:
    """jq queries replace stale quoin stanzas at old paths."""

    def test_ups_replaces_stale_path(self):
        """UserPromptSubmit/* stale quoin stanza at old path is replaced (not duplicated)."""
        old_cmd = "/old/path/.claude/hooks/userpromptsubmit.sh"
        new_cmd = f"{HOOKS_DIR}/userpromptsubmit.sh"
        input_json = {
            "hooks": {
                "UserPromptSubmit": [
                    {"matcher": "*", "hooks": [{"type": "command", "command": old_cmd, "timeout": 5}]}
                ]
            }
        }
        result = _run_jq(UPS_FILTER, input_json, cmd=new_cmd, matcher="*", scriptname="userpromptsubmit.sh")
        stanzas = result["hooks"]["UserPromptSubmit"]
        # Exactly one stanza for matcher="*"
        star_stanzas = [s for s in stanzas if s["matcher"] == "*"]
        assert len(star_stanzas) == 1, f"Expected 1 stanza for matcher=*, got {len(star_stanzas)}"
        assert star_stanzas[0]["hooks"][0]["command"] == new_cmd

    def test_precompact_replaces_stale_path(self):
        """PreCompact/auto stale quoin stanza at old path is replaced."""
        old_cmd = "/old/.claude/hooks/precompact.sh"
        new_cmd = f"{HOOKS_DIR}/precompact.sh"
        input_json = {
            "hooks": {
                "PreCompact": [
                    {"matcher": "auto", "hooks": [{"type": "command", "command": old_cmd, "timeout": 10}]}
                ]
            }
        }
        result = _run_jq(PC_FILTER, input_json, cmd=new_cmd, matcher="auto", scriptname="precompact.sh")
        stanzas = result["hooks"]["PreCompact"]
        auto_stanzas = [s for s in stanzas if s["matcher"] == "auto"]
        assert len(auto_stanzas) == 1
        assert auto_stanzas[0]["hooks"][0]["command"] == new_cmd

    def test_user_hook_preserved_during_stale_replacement(self):
        """User-defined SessionStart hook at different command path is preserved."""
        user_cmd = "/usr/local/bin/my-startup-hook.sh"
        new_quoin_cmd = f"{HOOKS_DIR}/sessionstart.sh"
        old_quoin_cmd = "/old/.claude/hooks/sessionstart.sh"
        input_json = {
            "hooks": {
                "SessionStart": [
                    # User-defined hook (different scriptname — NOT sessionstart.sh)
                    {"matcher": "startup", "hooks": [{"type": "command", "command": user_cmd, "timeout": 30}]},
                    # Stale quoin hook at old path
                    {"matcher": "startup", "hooks": [{"type": "command", "command": old_quoin_cmd, "timeout": 5}]},
                ]
            }
        }
        result = _run_jq(SS_FILTER, input_json, cmd=new_quoin_cmd, matcher="startup", scriptname="sessionstart.sh")
        stanzas = result["hooks"]["SessionStart"]
        commands = [h["command"] for s in stanzas for h in s.get("hooks", [])]
        assert user_cmd in commands, f"User hook {user_cmd} was removed. Commands: {commands}"
        assert new_quoin_cmd in commands, f"New quoin hook missing. Commands: {commands}"
        assert old_quoin_cmd not in commands, f"Stale quoin hook not removed. Commands: {commands}"


class TestJqMergeIdempotent:
    """jq queries are idempotent — re-running produces no duplicates."""

    def test_ups_idempotent(self):
        """Running UserPromptSubmit merge twice produces exactly one stanza."""
        cmd = f"{HOOKS_DIR}/userpromptsubmit.sh"
        args = dict(cmd=cmd, matcher="*", scriptname="userpromptsubmit.sh")
        # First run: start from empty
        round1 = _run_jq(UPS_FILTER, {}, **args)
        # Second run: idempotency check
        round2 = _run_jq(UPS_FILTER, round1, **args)
        star_stanzas = [s for s in round2["hooks"]["UserPromptSubmit"] if s["matcher"] == "*"]
        assert len(star_stanzas) == 1, f"Idempotency failure: {len(star_stanzas)} stanzas for matcher=*"

    def test_precompact_idempotent(self):
        """Running PreCompact merge twice produces exactly one stanza."""
        cmd = f"{HOOKS_DIR}/precompact.sh"
        args = dict(cmd=cmd, matcher="auto", scriptname="precompact.sh")
        round1 = _run_jq(PC_FILTER, {}, **args)
        round2 = _run_jq(PC_FILTER, round1, **args)
        auto_stanzas = [s for s in round2["hooks"]["PreCompact"] if s["matcher"] == "auto"]
        assert len(auto_stanzas) == 1, f"Idempotency failure: {len(auto_stanzas)} stanzas for matcher=auto"

    def test_sessionstart_startup_idempotent(self):
        """Running SessionStart/startup merge twice produces exactly one stanza."""
        cmd = f"{HOOKS_DIR}/sessionstart.sh"
        args = dict(cmd=cmd, matcher="startup", scriptname="sessionstart.sh")
        round1 = _run_jq(SS_FILTER, {}, **args)
        round2 = _run_jq(SS_FILTER, round1, **args)
        startup_stanzas = [s for s in round2["hooks"]["SessionStart"] if s["matcher"] == "startup"]
        assert len(startup_stanzas) == 1, f"Idempotency failure: {len(startup_stanzas)} startup stanzas"

    def test_sessionstart_resume_idempotent(self):
        """Running SessionStart/resume merge twice produces exactly one stanza."""
        cmd = f"{HOOKS_DIR}/sessionstart.sh"
        args = dict(cmd=cmd, matcher="resume", scriptname="sessionstart.sh")
        round1 = _run_jq(SS_FILTER, {}, **args)
        round2 = _run_jq(SS_FILTER, round1, **args)
        resume_stanzas = [s for s in round2["hooks"]["SessionStart"] if s["matcher"] == "resume"]
        assert len(resume_stanzas) == 1, f"Idempotency failure: {len(resume_stanzas)} resume stanzas"

    def test_full_four_tuple_idempotent(self):
        """Running all four stanza merges twice in sequence produces no duplicates."""
        ups_cmd = f"{HOOKS_DIR}/userpromptsubmit.sh"
        pc_cmd = f"{HOOKS_DIR}/precompact.sh"
        ss_cmd = f"{HOOKS_DIR}/sessionstart.sh"

        def run_full_install(settings: dict) -> dict:
            s = _run_jq(UPS_FILTER, settings, cmd=ups_cmd, matcher="*", scriptname="userpromptsubmit.sh")
            s = _run_jq(PC_FILTER, s, cmd=pc_cmd, matcher="auto", scriptname="precompact.sh")
            s = _run_jq(SS_FILTER, s, cmd=ss_cmd, matcher="startup", scriptname="sessionstart.sh")
            s = _run_jq(SS_FILTER, s, cmd=ss_cmd, matcher="resume", scriptname="sessionstart.sh")
            return s

        round1 = run_full_install({})
        round2 = run_full_install(round1)

        assert len([s for s in round2["hooks"]["UserPromptSubmit"] if s["matcher"] == "*"]) == 1
        assert len([s for s in round2["hooks"]["PreCompact"] if s["matcher"] == "auto"]) == 1
        assert len([s for s in round2["hooks"]["SessionStart"] if s["matcher"] == "startup"]) == 1
        assert len([s for s in round2["hooks"]["SessionStart"] if s["matcher"] == "resume"]) == 1
