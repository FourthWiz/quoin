"""Verdict-writer, context-suffix and non-interactive overlay text in the rendered commands."""
from __future__ import annotations

import io
import os
import re

import pytest

import _opencode_helpers as helpers
from quoin.opencode_adapter import generate, install
from test_opencode_driver_prepare import Setup

SOURCE_DIR = helpers.SOURCE_DIR

VERDICT_SENTENCES = (
    "The verdict appears once, in the Verdict section: for a critic response the heading "
    "`## Verdict: PASS` or `## Verdict: REVISE` followed by a `## Summary` section",
    "No other heading names the verdict, a verdict value or the words blocked, revise or changes requested in any case",
    "Describe earlier rounds in a sentence, describe verdict formats in words",
)
CONTEXT_NOTE = "When the command argument holds a parenthesised context suffix naming files, read those files first."
NEW_NOTE = "the task name is the word after `of`, or the first word when there is no `of`, before any parenthesised text;"
OLD_NOTE = "the task name is the word before that marker"


@pytest.fixture(scope="module")
def rendered():
    files = generate.render_source_dir(SOURCE_DIR)
    return {k: v.content.decode("utf-8") for k, v in files.items()}


def _paths(cid):
    slug = cid.replace("_", "-")
    return (".opencode/commands/quoin-%s.md" % slug, ".opencode/skills/quoin-%s/SKILL.md" % slug)


def test_verdict_sentences_once_in_critic_and_review(rendered):
    for cid in ("critic", "review"):
        path = _paths(cid)[1]
        for sentence in VERDICT_SENTENCES:
            assert rendered[path].count(sentence) == 1, (path, sentence)
            assert rendered[_paths(cid)[0]].count(sentence) == 0
    assert rendered[_paths("critic")[1]].count("`Assessment:`") == 1
    assert rendered[_paths("review")[1]].count("`## Test Coverage`") >= 1


def test_context_note_once_in_plan_and_implement(rendered):
    for cid, tail in (("plan", "critic response"), ("implement", "A review means")):
        text = rendered[_paths(cid)[1]]
        assert text.count(CONTEXT_NOTE) == 1
        assert tail in text
    for cid in ("critic", "review", "architect"):
        assert CONTEXT_NOTE not in rendered[_paths(cid)[1]]


def test_non_interactive_note_is_reworded_everywhere(rendered):
    carrying = [k for k, v in rendered.items() if "(non-interactive run)" in v and k.startswith(".opencode/skills/")]
    assert carrying
    for key in carrying:
        text = rendered[key]
        assert text.count(NEW_NOTE) == 1, key
        assert OLD_NOTE not in text and "word before that marker" not in text
    assert not any("word before that marker" in v for v in rendered.values())


def test_instructions_name_the_coordinator_without_the_old_phrase(rendered):
    text = rendered[next(k for k in rendered if k.endswith("quoin-instructions.md") or k.endswith("instructions.md"))]
    assert "quoin run --runtime opencode --workflow" in text
    assert "model diversity" not in text.lower()


def _task_name(argument):
    body = argument.split("(", 1)[0].strip()
    if " of " in " " + body + " " and body.startswith("stage "):
        return body.split(" of ", 1)[1].split()[0]
    return body.split()[0]


@pytest.mark.parametrize("stage", [None, "1"])
def test_coordinator_argument_with_context_ends_with_marker(tmp_path, monkeypatch, stage):
    env = Setup(tmp_path, monkeypatch)
    ref = ".workflow_artifacts/demo/critic-response-1.md"
    env.write(ref, "x\n")
    prepared = env.driver().prepare(env.request(stage=stage, context_refs=(ref,), non_interactive=True))
    argument = prepared.argv[-1]
    assert argument.endswith(" (non-interactive run)")
    assert "(context: %s)" % ref in argument
    assert _task_name(argument) == "demo"


def test_render_and_install_are_deterministic(tmp_path):
    first = generate.render_source_dir(SOURCE_DIR)
    second = generate.render_source_dir(SOURCE_DIR)
    assert {k: v.content for k, v in first.items()} == {k: v.content for k, v in second.items()}
    trees = []
    for name in ("a", "b"):
        root = tmp_path / name
        (root / ".git").mkdir(parents=True)
        out, err = io.StringIO(), io.StringIO()
        assert install.run_install(str(root), SOURCE_DIR, None, False, out, err) == 0
        trees.append({
            str(p.relative_to(root)): p.read_bytes()
            for p in sorted(root.rglob("*")) if p.is_file() and ".git" not in p.parts
        })
    assert trees[0].keys() == trees[1].keys()
    for key in trees[0]:
        if key.endswith((".json", ".jsonc")) and "state" in key:
            continue
        assert trees[0][key] == trees[1][key] or key.startswith(".quoin"), key
