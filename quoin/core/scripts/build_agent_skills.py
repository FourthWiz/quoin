#!/usr/bin/env python3
"""build_agent_skills.py - emit quoin's kept skills as a plain Agent Skills folder.

Codex reads Agent Skills from a project's `.agents/skills/` (and from
`~/.codex/skills/`); OpenCode reads the same format. This copies the kept skills
into that layout so other agents get them without a runtime-specific adapter:

  <out>/<skill>/SKILL.md      one folder per skill, with its reference files;
                              names use the standard's lowercase-and-hyphens
                              form (init_workflow -> init-workflow), and the
                              frontmatter keeps only name and description
  <out>/quoin-support/        the scripts, core and memory guides the skills
                              call; `${CLAUDE_PLUGIN_ROOT}` in the skill text
                              becomes this folder's absolute path

Claude-only frontmatter (model, context, allowed-tools) is dropped: the other
runtimes choose their own model and permissions.

Usage
  python3 build_agent_skills.py --out DIR [--source-dir DIR]

Prints one JSON object: the skills written and the support folder.
Standard library only.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
from pathlib import Path

KEPT_SKILLS = (
    "discover", "init_workflow", "status", "workspace", "run", "review", "review-cross",
    "pr", "security_review", "repo_cleanup", "cleanup",
)
SUPPORT = "quoin-support"
SUPPORT_PATHS = ("scripts", "core", "memory")
_IGNORE = shutil.ignore_patterns("__pycache__", "*.pyc", ".DS_Store", "tests")


def agent_name(name: str) -> str:
    return re.sub(r"[^a-z0-9-]+", "-", name.lower()).strip("-")


def _frontmatter(text: str) -> tuple[dict, str]:
    if not text.startswith("---\n"):
        return {}, text
    end = text.index("\n---", 4)
    fields = {}
    for line in text[4:end].splitlines():
        key, sep, value = line.partition(":")
        if sep:
            fields[key.strip()] = value.strip()
    return fields, text[end + 4:].lstrip("\n")


def _unquote(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


def convert(text: str, name: str, support: Path) -> str:
    fields, body = _frontmatter(text)
    description = _unquote(fields.get("description", "")).replace('"', "'").replace("${CLAUDE_PLUGIN_ROOT}", str(support))[:1024]
    body = body.replace("${CLAUDE_PLUGIN_ROOT}", str(support))
    return f'---\nname: {name}\ndescription: "{description}"\n---\n\n{body}'


def build(out: Path, source_dir: Path) -> dict:
    out = out.resolve()
    skills_src = source_dir / "adapters" / "claude" / "skills"
    support = out / SUPPORT
    shutil.rmtree(support, ignore_errors=True)
    support.mkdir(parents=True)
    for rel in SUPPORT_PATHS:
        if (source_dir / rel).is_dir():
            shutil.copytree(source_dir / rel, support / rel, ignore=_IGNORE)
    written = []
    for skill in KEPT_SKILLS:
        src = skills_src / skill
        if not (src / "SKILL.md").is_file():
            continue
        name = agent_name(skill)
        dest = out / name
        shutil.rmtree(dest, ignore_errors=True)
        dest.mkdir(parents=True)
        for path in sorted(src.iterdir()):
            if path.suffix != ".md":
                continue
            text = path.read_text(encoding="utf-8")
            if path.name == "SKILL.md":
                text = convert(text, name, support)
            else:
                text = text.replace("${CLAUDE_PLUGIN_ROOT}", str(support))
            (dest / path.name).write_text(text, encoding="utf-8")
        written.append(name)
    return {"out": str(out), "skills": written, "support": str(support)}


def default_source() -> Path:
    return Path(__file__).resolve().parents[2]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--out", required=True)
    parser.add_argument("--source-dir", default=None)
    args = parser.parse_args(argv)
    source = Path(args.source_dir).resolve() if args.source_dir else default_source()
    print(json.dumps(build(Path(args.out), source)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
