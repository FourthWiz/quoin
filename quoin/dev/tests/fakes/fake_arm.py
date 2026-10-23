"""Offline stand-in for the quoin installer, for tests that build arm sandboxes.

`fake_install_quoin` writes into a sandbox the files the identity checks look
at (one skill directory per canonical name, the rules markers, the hook
scripts, the hook stanzas) in the layout the real installer uses, without
running the installer.
"""
from __future__ import annotations

import json
from pathlib import Path

from quoin.benchmarks.harness.arm_identity import RULES_MARKERS
from quoin.benchmarks.scripts import run_three_arm_gate as gate


def fake_install_quoin(sandbox, arm_root, expected_commit, **_kw) -> dict:
    arm_root = Path(arm_root)
    installer = gate._load_arm_installer(arm_root)
    claude_dir = Path(sandbox.claude_dir)
    for name in installer.CANONICAL_SKILLS:
        skill = claude_dir / "skills" / name
        skill.mkdir(parents=True, exist_ok=True)
        (skill / "SKILL.md").write_text(f"# {name}\n", encoding="utf-8")
    (claude_dir / "CLAUDE.md").write_text("\n".join(RULES_MARKERS) + "\n", encoding="utf-8")

    hooks_dir = claude_dir / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)
    for name in gate.HOOK_SCRIPTS:
        src = arm_root / "quoin" / "hooks" / name
        (hooks_dir / name).write_bytes(installer.expected_deployed_content(src, claude_dir))

    settings_path = claude_dir / "settings.json"
    settings = json.loads(settings_path.read_text(encoding="utf-8"))
    hooks = settings.setdefault("hooks", {})
    for event, matcher, basename in sorted(gate.arm_registered_stanzas(arm_root)):
        hooks.setdefault(event, []).append({
            "matcher": matcher,
            "hooks": [{"type": "command", "command": f"{hooks_dir}/{basename}", "timeout": 5}],
        })
    settings_path.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")
    return {"ok": True, "reason": None, "returncode": 0, "stdout_tail": "", "stderr_tail": "",
            "installed_commit": expected_commit}
