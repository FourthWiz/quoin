"""Tests for quoin/core/scripts/repo_map.py (ranked, token-budgeted symbol map)."""

import importlib.util
import sys
from pathlib import Path

import pytest

_CORE = Path(__file__).resolve().parents[2] / "core" / "scripts"
sys.path.insert(0, str(_CORE))

import repo_map  # noqa: E402


def _write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


@pytest.fixture()
def polyglot(tmp_path: Path) -> Path:
    """A small repo where `core_engine` and `SharedConfig` are used everywhere."""
    _write(tmp_path, "pkg/core.py",
           "class SharedConfig:\n    pass\n\n\ndef core_engine(cfg):\n    return cfg\n\n\n"
           "def lonely_helper():\n    return 1\n")
    for i in range(3):
        _write(tmp_path, f"pkg/user{i}.py",
               "from pkg.core import core_engine, SharedConfig\n\n\n"
               f"def caller_{i}():\n    return core_engine(SharedConfig())\n")
    _write(tmp_path, "web/app.ts",
           "export interface RenderOptions { core_engine: string }\n"
           "export function renderPage(opts: RenderOptions) { return opts }\n")
    _write(tmp_path, "web/main.js",
           "import { renderPage } from './app';\nrenderPage({});\n")
    _write(tmp_path, "svc/server.go",
           "package main\n\ntype Handler struct{}\n\nfunc ServeHTTP() {}\n")
    _write(tmp_path, "lib/src/lib.rs",
           "pub struct Parser {}\npub fn parse_all() {}\n")
    _write(tmp_path, "java/App.java",
           "public class App {\n    public static void main(String[] a) {}\n}\n")
    _write(tmp_path, "scripts/run.sh", "deploy_all() {\n  echo hi\n}\ndeploy_all\n")
    _write(tmp_path, "node_modules/dep/index.js", "function vendored() {}\n")
    return tmp_path


def _names(ranked):
    return [r[4] for r in ranked]


def test_fallback_covers_every_language(polyglot):
    ranked, stats = repo_map.build_map(polyglot, backend="fallback")
    names = set(_names(ranked))
    assert {"SharedConfig", "core_engine", "RenderOptions", "renderPage", "Handler",
            "ServeHTTP", "Parser", "parse_all", "App", "main", "deploy_all"} <= names
    assert "vendored" not in names
    assert stats["backend"] == "fallback"


def test_referenced_symbols_outrank_unused_ones(polyglot):
    ranked, _ = repo_map.build_map(polyglot, backend="fallback")
    order = _names(ranked)
    assert order.index("core_engine") < order.index("lonely_helper")
    assert order.index("SharedConfig") < order.index("lonely_helper")
    assert order.index("renderPage") < order.index("parse_all")


def test_focus_personalises_ranking(polyglot):
    ranked, _ = repo_map.build_map(polyglot, backend="fallback", focus=["web/main.js"])
    assert _names(ranked)[0] == "renderPage"


@pytest.mark.parametrize("budget", [100, 160, 400])
def test_output_stays_within_budget(polyglot, budget):
    text = repo_map.generate(polyglot, budget, backend="fallback")
    assert repo_map.est_tokens(text) <= budget
    assert text.startswith("---\npath: ")
    assert f"budget {budget} tokens" in text


def test_larger_budget_shows_more_symbols(polyglot):
    small = repo_map.generate(polyglot, 100, backend="fallback")
    large = repo_map.generate(polyglot, 2000, backend="fallback")
    assert small.count("\n  ") < large.count("\n  ")
    assert "lonely_helper" in large


def test_resolve_budget_env_and_flag(monkeypatch):
    monkeypatch.delenv(repo_map.ENV_TOKENS, raising=False)
    assert repo_map.resolve_budget() == 1000
    monkeypatch.setenv(repo_map.ENV_TOKENS, "2500")
    assert repo_map.resolve_budget() == 2500
    assert repo_map.resolve_budget(300) == 300
    monkeypatch.setenv(repo_map.ENV_TOKENS, "lots")
    assert repo_map.resolve_budget() == 1000
    assert repo_map.resolve_budget(5) == repo_map.MIN_TOKENS


def test_tree_sitter_backend_when_available(polyglot):
    if importlib.util.find_spec("tree_sitter") is None or \
            importlib.util.find_spec("tree_sitter_python") is None:
        pytest.skip("tree-sitter Python grammar not installed")
    defs, refs, used = repo_map.extract(polyglot / "pkg/core.py", "python", "tree-sitter")
    assert used == "tree-sitter"
    assert {"SharedConfig", "core_engine", "lonely_helper"} <= {d[0] for d in defs}
    assert "cfg" in refs


def test_cli_writes_cache_file(polyglot, tmp_path, capsys):
    out = tmp_path / "cache" / "demo" / "repo-map.md"
    rc = repo_map.main(["--repo", str(polyglot), "--out", str(out), "--tokens", "200",
                        "--name", "demo", "--format", "json"])
    assert rc == 0
    text = out.read_text(encoding="utf-8")
    assert "path: demo\n" in text
    assert "updated_by: repo_map.py" in text
    assert repo_map.est_tokens(text) <= 200
    assert '"budget": 200' in capsys.readouterr().out


def test_cli_missing_repo_fails_open(tmp_path):
    assert repo_map.main(["--repo", str(tmp_path / "nope")]) == 3


def test_empty_repo_still_renders(tmp_path, capsys):
    assert repo_map.main(["--repo", str(tmp_path)]) == 0
    assert "No supported source files" in capsys.readouterr().out


def test_budget_holds_with_long_name_at_minimum(polyglot):
    text = repo_map.render_within_budget(*repo_map.build_map(polyglot, "fallback"),
                                         repo_map.MIN_TOKENS, "n" * 300, "f" * 40,
                                         "2026-01-01T00:00:00Z")
    assert repo_map.est_tokens(text) <= repo_map.MIN_TOKENS


def test_references_do_not_cross_languages(tmp_path):
    _write(tmp_path, "hooks/run.sh", "field() {\n  echo x\n}\n")
    _write(tmp_path, "a.py", "from dataclasses import field\nx = field()\ny = field()\n")
    ranked, _ = repo_map.build_map(tmp_path, backend="fallback")
    (score, rel, _line, _kind, name), = ranked
    assert (rel, name) == ("hooks/run.sh", "field")
    assert score < 1e-2


def test_git_ignored_files_are_excluded(polyglot):
    import shutil
    import subprocess
    if shutil.which("git") is None:
        pytest.skip("git not installed")
    subprocess.run(["git", "init", "-q", str(polyglot)], check=True)
    (polyglot / ".gitignore").write_text("generated/\n", encoding="utf-8")
    _write(polyglot, "generated/out.py", "def ignored_symbol():\n    pass\n")
    names = _names(repo_map.build_map(polyglot, backend="fallback")[0])
    assert "ignored_symbol" not in names
    assert "core_engine" in names


def test_wrapper_runs_as_script(polyglot):
    import subprocess
    wrapper = Path(__file__).resolve().parents[2] / "scripts" / "repo_map.py"
    proc = subprocess.run([sys.executable, str(wrapper), "--repo", str(polyglot),
                           "--tokens", "150", "--backend", "fallback"],
                          capture_output=True, text=True, check=False)
    assert proc.returncode == 0, proc.stderr
    assert "# Repo map:" in proc.stdout
