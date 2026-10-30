"""The frontmatter scanner compared with the loader OpenCode uses (gray-matter).

The oracle copies `parse` and `sanitize` from OpenCode 1.18.32,
packages/core/src/config/markdown.ts: parse with gray-matter, and when that
throws, parse again after `sanitize` rewrites each unindented, unquoted
`key: value` whose value contains a colon into a `key: |-` block scalar.
A case is skipped only when both parses throw.

Runs only when `node` is on PATH and the `gray-matter` package resolves,
either from Node's default lookup or from the `node_modules` directory named by
QUOIN_GRAY_MATTER_NODE_PATH. The cases are fixed; there is no random input.

The scanner may always answer `unknown` (the work profile refuses it). When it
answers known, it must agree with gray-matter on everything that matters: the
string model, and whether `permission` or `tools` is present.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess

import pytest

from quoin.opencode_adapter import inventory

import test_opencode_inventory_hardening as hardening

ORACLE = r"""
const matter = require('gray-matter');
// Copied from OpenCode 1.18.32 packages/core/src/config/markdown.ts.
function sanitize(content) {
  const match = content.match(/^---\r?\n([\s\S]*?)\r?\n---/);
  if (!match) return content;
  const frontmatter = match[1];
  const result = frontmatter.split(/\r?\n/).flatMap((line) => {
    if (line.trim().startsWith("#") || line.trim() === "" || /^\s+/.test(line)) return [line];
    const entry = line.match(/^([a-zA-Z_][a-zA-Z0-9_]*)\s*:\s*(.*)$/);
    if (!entry) return [line];
    const value = entry[2].trim();
    if (value === "" || value === ">" || value === "|" || value.startsWith('"') || value.startsWith("'")) return [line];
    if (!value.includes(":")) return [line];
    return [`${entry[1]}: |-`, `  ${value}`];
  });
  return content.replace(frontmatter, () => result.join("\n"));
}
function parse(text) {
  try { matter.clearCache(); return matter(text); }
  catch (e) { matter.clearCache(); return matter(sanitize(text)); }
}
let input = '';
process.stdin.on('data', d => input += d);
process.stdin.on('end', () => {
  const out = JSON.parse(input).map(text => {
    try {
      const data = parse(text).data;
      if (data === null || typeof data !== 'object' || Array.isArray(data)) return {ok: true, keys: false};
      const has = k => Object.prototype.hasOwnProperty.call(data, k);
      const m = data.model;
      return {ok: true, keys: true, permission: has('permission'), tools: has('tools'),
              model: typeof m === 'string' ? m : (m === null || m === undefined ? null : {nonstring: true})};
    } catch (e) { return {ok: false}; }
  });
  process.stdout.write(JSON.stringify(out));
});
"""


def _node_env():
    env = dict(os.environ)
    extra = env.get("QUOIN_GRAY_MATTER_NODE_PATH")
    if extra:
        env["NODE_PATH"] = extra + (os.pathsep + env["NODE_PATH"] if env.get("NODE_PATH") else "")
    return env


def _oracle_available():
    if shutil.which("node") is None:
        return "node is not on PATH"
    try:
        probe = subprocess.run(["node", "-e", "require.resolve('gray-matter')"], env=_node_env(),
                               capture_output=True, timeout=30)
    except (OSError, subprocess.SubprocessError) as exc:
        return "node could not be run: %s" % exc
    if probe.returncode != 0:
        return "the gray-matter package does not resolve (set QUOIN_GRAY_MATTER_NODE_PATH to a node_modules directory)"
    return None


_SKIP = _oracle_available()
pytestmark = pytest.mark.skipif(_SKIP is not None, reason=_SKIP or "")

PIECES = [
    "description: a: b", "model: a/b # n: x", "model: # p/m: x", "model: a/b\t# n: x",
    "model: a/b", "model:", "model: ''", "model: # c", "model: a/b\t# c", "model: a/b # c", "model: a/b#c",
    "model: 'x''y'", "model: \"a/b\" # c", "model: ~", "model: true", "model: 12", "  evil/x", "  - evil/x",
    "  a: b", "  # c", "# c", "", "permission: {bash: allow}", "permission:", "  bash: allow", "tools: {bash: true}",
    "...", "... # c", "... ", "...\u0085", "... ", "...　", "name: [a,", "name: {a: 1,", "]", "}",
    "description: |", "description: >", "  text", "tools: {}model: evil/x", "model:evil/x", "\tmodel: a/b",
    "'model': a/b", "description: café",
    "model: amazon-bedrock/anthropic.claude-3-5-sonnet-20241022-v2:0", "model: ollama/llama3:8b",
    "model: 'ollama/llama3:8b'", "model: \"amazon-bedrock/anthropic.claude-3-5-sonnet-20241022-v2:0\"",
]
SEPARATORS = ["\n", "\r\n", "\r", "\n\n"]


def _cases():
    """Every ordered pair and a fixed set of triples of the pieces above,
    joined by each separator, plus the hand cases the hardening tests use."""
    cases = []
    for sep in SEPARATORS:
        for first in PIECES:
            for second in PIECES:
                cases.append("---\n%s%s%s\n---\nbody\n" % (first, sep, second))
        for index, first in enumerate(PIECES):
            second = PIECES[(index * 7 + 3) % len(PIECES)]
            third = PIECES[(index * 11 + 5) % len(PIECES)]
            cases.append("---\n%s%s%s%s%s\n---\n" % (first, sep, second, sep, third))
    cases.extend(hardening.RETRY_PROBES)
    cases.extend(hardening.COLON_MODEL_CASES)
    cases.extend(text for text, _ in hardening.ORDINARY)
    for model_line in ("model:", "model: ''", "model: # c"):
        for gap in hardening.GAPS:
            for value in hardening.NESTED_VALUES:
                cases.append("---\n%s%s%s\n---\n" % (model_line, gap, value))
    for space in (" ", "\u0085", " ", "　"):
        for opening, closing in (("[a,", "]"), ("{a: 1,", "}")):
            cases.append("---\nname: %s\n...%s\n%s\npermission: {bash: allow}\ntools: {bash: true}\nmodel: evil/x\n---\n"
                         % (opening, space, closing))
    return cases


def _disagreement(text, oracle):
    got = inventory.scan_frontmatter(text)
    if got["unknown"] or not oracle["ok"] or not oracle["keys"]:
        return None
    model = oracle["model"]
    if isinstance(model, dict):
        return "a non-string model was read as known"
    readings = got.get("model_readings")
    if readings:
        # An unquoted model with a colon: the scanner lists every reading OpenCode might load.
        if model and model not in readings:
            return "model %r is not among the readings %r" % (model, readings)
    elif (model or None) != got["model"]:
        return "model %r read as %r" % (model, got["model"])
    if oracle["permission"] and not got["has_permission"]:
        return "permission hidden"
    if oracle["tools"] and not got["has_tools"]:
        return "tools hidden"
    return None


def test_the_scanner_never_hides_what_gray_matter_reads():
    cases = _cases()
    run = subprocess.run(["node", "-e", ORACLE], input=json.dumps(cases), env=_node_env(),
                         capture_output=True, text=True, timeout=300)
    assert run.returncode == 0, run.stderr
    results = json.loads(run.stdout)
    assert len(results) == len(cases)
    wrong = [(why, text) for text, oracle in zip(cases, results) for why in [_disagreement(text, oracle)] if why]
    assert not wrong, wrong[:10]


def test_ordinary_files_are_known_and_match_gray_matter():
    texts = [text for text, _ in hardening.ORDINARY]
    run = subprocess.run(["node", "-e", ORACLE], input=json.dumps(texts), env=_node_env(),
                         capture_output=True, text=True, timeout=60)
    assert run.returncode == 0, run.stderr
    for text, oracle in zip(texts, json.loads(run.stdout)):
        got = inventory.scan_frontmatter(text)
        assert got["unknown"] is False and oracle["ok"], text
        assert (got["model"], got["has_permission"], got["has_tools"]) == (
            oracle["model"], oracle["permission"], oracle["tools"]), text
