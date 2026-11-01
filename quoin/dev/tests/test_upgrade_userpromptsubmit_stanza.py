"""Upgrading an install whose prompt hook stanza already exists.

The hook script keeps its name, so the upgrade replaces quoin's own stanza in
place and touches nothing else: other tools' stanzas (including the Stream
Deck forwarder and a non-quoin stdin logger), unrelated settings keys and the
context-tracker mod folder are all left exactly as they were.
"""
import json
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
QUOIN_SRC = REPO_ROOT / "quoin"

from quoin.installer import deploy_context_tracker, deploy_hooks  # noqa: E402


def _stanza(command, matcher="*", timeout=5):
    return {"matcher": matcher, "hooks": [{"type": "command", "command": command, "timeout": timeout}]}


def _tree(root):
    return {str(p.relative_to(root)): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()}


def test_upgrade_replaces_only_the_quoin_prompt_stanza(tmp_path):
    dest = tmp_path / ".claude"
    dest.mkdir()
    hooks_dir = dest.resolve() / "hooks"
    deck = _stanza("'/Users/x/Library/QuoinStreamDeck/bin/quoin-deck-hook' claude", matcher="", timeout=3)
    smoke = _stanza("/opt/tools/echo_stdin_smoke.sh")
    settings = {
        "model": "opus",
        "enabledPlugins": {"context-tracker@local": True, "other@market": False},
        "statusLine": {"type": "command", "command": "echo hi"},
        "hooks": {
            "UserPromptSubmit": [smoke, _stanza(str(hooks_dir / "userpromptsubmit.sh")), deck],
            "PreCompact": [smoke, _stanza(str(hooks_dir / "precompact.sh"), matcher="auto", timeout=10)],
            "SessionStart": [deck, smoke],
        },
    }
    (dest / "settings.json").write_text(json.dumps(settings), encoding="utf-8")

    deploy_context_tracker(QUOIN_SRC, dest, strict=False)
    mod_dir = dest / "skills" / "context-tracker"
    assert (mod_dir / "hooks" / "hooks.json").is_file()
    mod_before = _tree(mod_dir)

    deploy_hooks(QUOIN_SRC, dest)

    after = json.loads((dest / "settings.json").read_text(encoding="utf-8"))
    ups = after["hooks"]["UserPromptSubmit"]
    quoin_ups = [s for s in ups if any(h["command"] == str(hooks_dir / "userpromptsubmit.sh") for h in s["hooks"])]
    assert len(quoin_ups) == 1
    assert [s for s in ups if s is not quoin_ups[0]] == [smoke, deck]
    assert ups.index(smoke) < ups.index(deck)

    for event, foreign in (("PreCompact", [smoke]), ("SessionStart", [deck, smoke])):
        rest = [s for s in after["hooks"][event]
                if not any(h["command"].startswith(str(hooks_dir)) for h in s["hooks"])]
        assert rest == foreign

    for key in ("model", "enabledPlugins", "statusLine"):
        assert after[key] == settings[key]

    assert _tree(mod_dir) == mod_before
    deployed = (hooks_dir / "userpromptsubmit.sh").read_text(encoding="utf-8")
    assert "jq" not in deployed
