"""IVG-136 T-06: unit tests for compute_drift + installer helpers + the CLI.

compute_drift cases (a)-(e),(j); CLI cases (f)-(i) + scope/drift/disabled; and the
import-safety cases (k)/(l)/(m) that prove the deferred-import contract (plan D-11).

Loader pattern mirrors test_install_branch_hygiene_deployed.py: installer.py and the
adapter CLI are loaded via importlib.util.spec_from_file_location so the test does not
depend on the pip-installed `quoin` package for module collection.
"""
from __future__ import annotations

import contextlib
import importlib.util
import json
import re
import sys
from pathlib import Path
from unittest import mock

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]  # quoin/ repo root
INSTALLER_PY = REPO_ROOT / "src" / "quoin" / "installer.py"
DDC_PY = REPO_ROOT / "quoin" / "scripts" / "deploy_drift_check.py"


def _load(mod_name: str, path: Path):
    spec = importlib.util.spec_from_file_location(mod_name, path)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def installer():
    return _load("_ddc_installer", INSTALLER_PY)


@pytest.fixture
def ddc():
    return _load("_ddc_cli", DDC_PY)


# ---------------------------------------------------------------------------
# Helpers: build a minimal source tree + a deployed dest_root using the real
# installer helpers, with the manifests monkeypatched down to a small set.
# ---------------------------------------------------------------------------

def _build_trees(installer, tmp_path, monkeypatch):
    """Create source_dir + dest_root with one file per checked category, deployed
    via the real expected_deployed_content so compute_drift returns [] initially."""
    src = tmp_path / "src"
    dest = tmp_path / "dest"

    # memory (tier-1)
    (src / "memory").mkdir(parents=True)
    (src / "memory" / "m1.md").write_text("mem body\n", encoding="utf-8")
    # skills: one with an adapter SKILL.md, one stub-only + a preamble
    (src / "skills" / "alpha").mkdir(parents=True)
    (src / "skills" / "alpha" / "SKILL.md").write_text("alpha stub\n", encoding="utf-8")
    (src / "skills" / "alpha" / "preamble.md").write_text("alpha preamble\n", encoding="utf-8")
    (src / "adapters" / "claude" / "skills" / "alpha").mkdir(parents=True)
    (src / "adapters" / "claude" / "skills" / "alpha" / "SKILL.md").write_text(
        "alpha ADAPTER body\n", encoding="utf-8")
    # scripts
    (src / "scripts").mkdir(parents=True)
    (src / "scripts" / "s1.py").write_text("print('s1')\n", encoding="utf-8")
    # core-scripts
    (src / "core" / "scripts").mkdir(parents=True)
    (src / "core" / "scripts" / "c1.py").write_text("print('c1')\n", encoding="utf-8")
    # core-workflow
    (src / "core" / "workflow").mkdir(parents=True)
    (src / "core" / "workflow" / "w1.md").write_text("workflow body\n", encoding="utf-8")

    monkeypatch.setattr(installer, "TIER1_MEMORY_FILES", ("m1.md",))
    monkeypatch.setattr(installer, "CANONICAL_SKILLS", ("alpha",))
    monkeypatch.setattr(installer, "DEPLOYED_SCRIPTS", ("s1.py",))
    monkeypatch.setattr(installer, "CORE_SCRIPTS", ("c1.py",))
    monkeypatch.setattr(installer, "CORE_WORKFLOW_FILES", ("w1.md",))

    # Deploy each file: write expected_deployed_content to the dest path.
    def _deploy(src_path: Path, dest_path: Path):
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        dest_path.write_bytes(installer.expected_deployed_content(src_path, dest))

    _deploy(src / "memory" / "m1.md", dest / "memory" / "m1.md")
    # skills: adapter-preferred SKILL.md is the source of record
    _deploy(src / "adapters" / "claude" / "skills" / "alpha" / "SKILL.md",
            dest / "skills" / "alpha" / "SKILL.md")
    _deploy(src / "skills" / "alpha" / "preamble.md",
            dest / "skills" / "alpha" / "preamble.md")
    _deploy(src / "scripts" / "s1.py", dest / "scripts" / "s1.py")
    _deploy(src / "core" / "scripts" / "c1.py", dest / "core" / "scripts" / "c1.py")
    _deploy(src / "core" / "workflow" / "w1.md", dest / "core" / "workflow" / "w1.md")

    return src, dest


# ---------------------------------------------------------------------------
# Installer helper tests
# ---------------------------------------------------------------------------

def test_expected_deployed_content_substitutes(installer, tmp_path):
    src = tmp_path / "f.md"
    src.write_text("path=__QUOIN_HOME__/x\n", encoding="utf-8")
    dest_root = tmp_path / "dest"
    out = installer.expected_deployed_content(src, dest_root)
    assert b"__QUOIN_HOME__" not in out
    assert str(dest_root.resolve()).encode("utf-8") in out


def test_expected_deployed_content_bytecopy_nontext(installer, tmp_path):
    src = tmp_path / "img.png"
    src.write_bytes(b"\x89PNG__QUOIN_HOME__")  # placeholder must NOT be substituted
    out = installer.expected_deployed_content(src, tmp_path / "dest")
    assert out == b"\x89PNG__QUOIN_HOME__"


def test_resolve_skill_source_md_adapter_preferred(installer, tmp_path):
    src_skills = tmp_path / "skills"
    src_adapter = tmp_path / "adapters" / "claude" / "skills"
    (src_skills / "alpha").mkdir(parents=True)
    (src_skills / "alpha" / "SKILL.md").write_text("stub", encoding="utf-8")
    (src_adapter / "alpha").mkdir(parents=True)
    (src_adapter / "alpha" / "SKILL.md").write_text("adapter", encoding="utf-8")
    resolved = installer.resolve_skill_source_md(src_skills, src_adapter, "alpha")
    assert resolved == src_adapter / "alpha" / "SKILL.md"


def test_resolve_skill_source_md_stub_fallback(installer, tmp_path):
    src_skills = tmp_path / "skills"
    src_adapter = tmp_path / "adapters" / "claude" / "skills"
    (src_skills / "beta").mkdir(parents=True)
    (src_skills / "beta" / "SKILL.md").write_text("stub", encoding="utf-8")
    resolved = installer.resolve_skill_source_md(src_skills, src_adapter, "beta")
    assert resolved == src_skills / "beta" / "SKILL.md"


# ---------------------------------------------------------------------------
# compute_drift cases (a)-(e),(j)
# ---------------------------------------------------------------------------

def test_compute_drift_clean_fresh_install(installer, tmp_path, monkeypatch):
    src, dest = _build_trees(installer, tmp_path, monkeypatch)
    assert installer.compute_drift(src, dest) == []


def test_compute_drift_a_stale_skill(installer, tmp_path, monkeypatch):
    src, dest = _build_trees(installer, tmp_path, monkeypatch)
    (dest / "skills" / "alpha" / "SKILL.md").write_text("MUTATED\n", encoding="utf-8")
    drift = installer.compute_drift(src, dest)
    assert any(d.reason == "stale" and d.category == "skills" for d in drift)


def test_compute_drift_o_stale_core_workflow(installer, tmp_path, monkeypatch):
    # IVG-248 T-03: core-workflow is a checked category (D-10) — mirrors the
    # core-scripts drift case (a mutated deployed core/workflow/*.md file drifts).
    src, dest = _build_trees(installer, tmp_path, monkeypatch)
    (dest / "core" / "workflow" / "w1.md").write_text("MUTATED\n", encoding="utf-8")
    drift = installer.compute_drift(src, dest)
    assert any(d.reason == "stale" and d.category == "core-workflow" for d in drift)


def test_compute_drift_b_missing_script(installer, tmp_path, monkeypatch):
    src, dest = _build_trees(installer, tmp_path, monkeypatch)
    (dest / "scripts" / "s1.py").unlink()
    drift = installer.compute_drift(src, dest)
    assert any(d.reason == "missing" and d.category == "scripts" for d in drift)


def test_compute_drift_c_substitution_parity(installer, tmp_path, monkeypatch):
    src, dest = _build_trees(installer, tmp_path, monkeypatch)
    # Source memory file uses the placeholder; deployed copy holds substituted path.
    (src / "memory" / "m1.md").write_text("go __QUOIN_HOME__/here\n", encoding="utf-8")
    (dest / "memory" / "m1.md").write_bytes(
        installer.expected_deployed_content(src / "memory" / "m1.md", dest))
    drift = installer.compute_drift(src, dest)
    assert not any(d.category == "memory" for d in drift), (
        "substituted deployed copy must NOT be flagged as drift")


def test_compute_drift_d_adapter_not_stub(installer, tmp_path, monkeypatch):
    src, dest = _build_trees(installer, tmp_path, monkeypatch)
    # Deployed copy matches the ADAPTER body (source of record). Mutating the stub
    # must NOT create drift because the adapter file is what is compared.
    (src / "skills" / "alpha" / "SKILL.md").write_text("stub CHANGED\n", encoding="utf-8")
    drift = installer.compute_drift(src, dest)
    assert not any(d.category == "skills" and "SKILL.md" in d.deployed_path for d in drift)


def test_compute_drift_e_untracked_memory_file_ignored(installer, tmp_path, monkeypatch):
    src, dest = _build_trees(installer, tmp_path, monkeypatch)
    # A memory file NOT in TIER1_MEMORY_FILES must not be compared.
    (src / "memory" / "not_tier1.md").write_text("x\n", encoding="utf-8")
    assert installer.compute_drift(src, dest) == []


def test_compute_drift_j_absent_source_skill_does_not_raise(installer, tmp_path, monkeypatch):
    src, dest = _build_trees(installer, tmp_path, monkeypatch)
    # Canonical skill with NO source SKILL.md on disk anywhere → must not raise (MIN-3).
    monkeypatch.setattr(installer, "CANONICAL_SKILLS", ("alpha", "ghost"))
    drift = installer.compute_drift(src, dest)  # must not raise
    assert isinstance(drift, list)


def test_compute_drift_n_undecodable_source_does_not_raise(installer, tmp_path, monkeypatch):
    # Round-2 MINOR-1: a source .md file that is NOT valid UTF-8 makes
    # expected_deployed_content's read_text(encoding="utf-8") raise UnicodeDecodeError,
    # which is not an OSError. compute_drift's documented "never raises" contract must
    # hold — the file degrades to "no drift for this file" rather than propagating.
    src, dest = _build_trees(installer, tmp_path, monkeypatch)
    (src / "memory" / "m1.md").write_bytes(b"\xff\xfe not valid utf-8 \x80\x81")
    drift = installer.compute_drift(src, dest)  # must not raise UnicodeDecodeError
    assert isinstance(drift, list)
    assert not any(d.category == "memory" for d in drift), (
        "undecodable source degrades to no-drift, not a false 'stale' report")


# ---------------------------------------------------------------------------
# Manifest registration (T-04)
# ---------------------------------------------------------------------------

def test_manifest_registration(installer):
    assert "deploy_drift_check.py" in installer.DEPLOYED_SCRIPTS
    assert "deploy_drift_check.py" not in installer.CORE_SCRIPTS


# ---------------------------------------------------------------------------
# CLI cases
# ---------------------------------------------------------------------------

def test_cli_disabled_env(ddc, monkeypatch):
    monkeypatch.setenv("QUOIN_DISABLE_DEPLOY_DRIFT", "1")
    assert ddc.main([]) == 0


def test_cli_h_argparse_usage_error_exit2(ddc):
    # (h) genuine argparse error on OWN flags → exit 2 propagates (not remapped to 3).
    with pytest.raises(SystemExit) as exc:
        ddc.main(["--format", "bogus"])
    assert exc.value.code == 2


def test_cli_f_compute_drift_raises_exit3(ddc, monkeypatch, tmp_path):
    # (f) compute_drift raises an arbitrary exception → exit 3, not Python default 1.
    import quoin.installer as qi
    monkeypatch.setattr(qi, "compute_drift", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    # Ensure dest_root exists so we reach compute_drift.
    dest = tmp_path / ".claude"
    dest.mkdir()
    monkeypatch.setattr(ddc, "_resolve_dest_root", lambda scope: dest)
    rc = ddc.main(["--no-scope-check", "--project-root", str(tmp_path)])
    assert rc == 3


def test_cli_g_unresolvable_source_exit3(ddc, tmp_path):
    # (g) --source-dir with no skills/ → _resolve_source_dir sys.exit(2) → remapped to 3.
    bad = tmp_path / "nosrc"
    bad.mkdir()
    rc = ddc.main(["--no-scope-check", "--source-dir", str(bad),
                   "--project-root", str(tmp_path)])
    assert rc == 3


def test_cli_k_quoin_unimportable_scope_in_exit3(ddc, monkeypatch, tmp_path):
    # (k) quoin import itself fails on a scope=IN run → exit 3 (not uncaught ImportError→1).
    monkeypatch.setitem(sys.modules, "quoin", None)
    monkeypatch.setitem(sys.modules, "quoin.installer", None)
    monkeypatch.setitem(sys.modules, "quoin.cli", None)
    rc = ddc.main(["--no-scope-check", "--project-root", str(tmp_path)])
    assert rc == 3


def test_cli_l_quoin_unimportable_scope_out_exit0(ddc, monkeypatch, tmp_path):
    # (l) scope=OUT with quoin unimportable → exit 0, proving scope gate runs BEFORE import.
    monkeypatch.setitem(sys.modules, "quoin", None)
    monkeypatch.setattr(ddc, "_scope_is_in", lambda pr, nsc: (False, "out"))
    rc = ddc.main(["--project-root", str(tmp_path)])
    assert rc == 0


def test_cli_m_module_import_no_module_top_quoin(ddc):
    # (m) module collection succeeds even with quoin=None (proves no module-top import).
    # `ddc` fixture already imported the module; re-import under quoin=None to be explicit.
    import importlib.util as _u
    saved = sys.modules.get("quoin")
    sys.modules["quoin"] = None
    try:
        spec = _u.spec_from_file_location("_ddc_reimport", DDC_PY)
        mod = _u.module_from_spec(spec)
        spec.loader.exec_module(mod)  # must NOT raise
    finally:
        if saved is not None:
            sys.modules["quoin"] = saved
        else:
            sys.modules.pop("quoin", None)


def test_cli_scope_out_exit0(ddc, monkeypatch, tmp_path):
    monkeypatch.setattr(ddc, "_scope_is_in", lambda pr, nsc: (False, "out"))
    rc = ddc.main(["--project-root", str(tmp_path)])
    assert rc == 0


def test_cli_git_error_exit3(ddc, monkeypatch, tmp_path):
    # Deferred MINOR (1): scope-gate git-error → exit 3 (WARN), never a false exit-0 PASS.
    monkeypatch.setattr(ddc, "_scope_is_in", lambda pr, nsc: (False, "git-error"))
    rc = ddc.main(["--project-root", str(tmp_path)])
    assert rc == 3


# ---------------------------------------------------------------------------
# Round-2 MINOR-5: the three tests above (and test_cli_git_error_exit3) drive
# _scope_is_in only at the mapping level (main() given a pre-mocked reason
# token). These drive the REAL _scope_is_in — only the underlying
# affected_tests.resolve_repo/changed_files calls it makes are simulated —
# so the "diff_reason == 'git-error'" / RuntimeError / None-repo branches
# inside _scope_is_in itself are actually exercised end-to-end.
# ---------------------------------------------------------------------------

def test_scope_gate_real_multiple_repos_exit3(ddc, monkeypatch, tmp_path):
    monkeypatch.setattr(
        ddc._affected_tests, "resolve_repo",
        lambda project_root: (_ for _ in ()).throw(RuntimeError("multiple git repos found")))
    scope_in, reason = ddc._scope_is_in(tmp_path, no_scope_check=False)
    assert (scope_in, reason) == (False, "multiple-repos")
    rc = ddc.main(["--project-root", str(tmp_path)])
    assert rc == 3


def test_scope_gate_real_zero_repos_exit3(ddc, monkeypatch, tmp_path):
    monkeypatch.setattr(ddc._affected_tests, "resolve_repo", lambda project_root: None)
    scope_in, reason = ddc._scope_is_in(tmp_path, no_scope_check=False)
    assert (scope_in, reason) == (False, "no-repo")
    rc = ddc.main(["--project-root", str(tmp_path)])
    assert rc == 3


def test_scope_gate_real_git_error_exit3(ddc, monkeypatch, tmp_path):
    fake_repo = tmp_path / "repo"
    fake_repo.mkdir()
    monkeypatch.setattr(ddc._affected_tests, "resolve_repo", lambda project_root: fake_repo)
    monkeypatch.setattr(ddc._affected_tests, "changed_files", lambda repo: ([], "git-error"))
    scope_in, reason = ddc._scope_is_in(tmp_path, no_scope_check=False)
    assert (scope_in, reason) == (False, "git-error")
    rc = ddc.main(["--project-root", str(tmp_path)])
    assert rc == 3


def test_cli_clean_pass_names_coverage(ddc, monkeypatch, tmp_path, capsys):
    # (i)-adjacent: clean PASS output MUST contain the "not covered" qualifier.
    import quoin.installer as qi
    dest = tmp_path / ".claude"
    dest.mkdir()
    monkeypatch.setattr(qi, "compute_drift", lambda *a, **k: [])
    monkeypatch.setattr(ddc, "_resolve_dest_root", lambda scope: dest)
    rc = ddc.main(["--no-scope-check", "--project-root", str(tmp_path)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "not covered" in out


def test_cli_drift_found_exit1(ddc, monkeypatch, tmp_path):
    import quoin.installer as qi
    dest = tmp_path / ".claude"
    dest.mkdir()
    fake = qi.DriftEntry("scripts", "/src/s1.py", "/dest/s1.py", "stale")
    monkeypatch.setattr(qi, "compute_drift", lambda *a, **k: [fake])
    monkeypatch.setattr(ddc, "_resolve_dest_root", lambda scope: dest)
    rc = ddc.main(["--no-scope-check", "--project-root", str(tmp_path)])
    assert rc == 1


# ---------------------------------------------------------------------------
# Working-tree src injection
#
# SYS.PATH TEARDOWN: main()'s injection inserts str(src) into sys.path with
# no production-side removal (deliberate — the real CLI wants it for the
# process lifetime). A test that fires injection and never tears it down
# leaves the entry in place after tmp_path is deleted; any quoin submodule
# not yet cached resolves against the empty fake tree in a LATER test,
# producing an "affected-area GREEN, full-suite RED" failure shape. Every
# case below that can fire injection runs under _sys_path_guard, which
# captures post-call state, restores unconditionally in a finally block,
# and only then hands the captured state back for assertions — a failing
# assertion must never leave a leaked entry in place.
# ---------------------------------------------------------------------------

@contextlib.contextmanager
def _sys_path_guard():
    saved_path = sys.path[:]
    saved_modules = set(sys.modules.keys())
    state: dict = {}
    try:
        yield state
    finally:
        state["path_after"] = sys.path[:]
        state["modules_after"] = set(sys.modules.keys())
        sys.path[:] = saved_path
        for name in state["modules_after"] - saved_modules:
            sys.modules.pop(name, None)
        state["saved_path"] = saved_path
        state["saved_modules"] = saved_modules


def _make_quoin_src(repo: Path) -> Path:
    pkg = repo / "src" / "quoin"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("")
    return repo / "src"


def test_injection_fires(ddc, tmp_path, capsys):
    repo = tmp_path / "repo"
    _make_quoin_src(repo)
    (repo / ".git").mkdir(parents=True)
    with _sys_path_guard() as state:
        ddc.main(["--no-scope-check", "--project-root", str(repo), "--format", "json"])
    out = capsys.readouterr().out
    data = json.loads(out)
    assert data.get("src_injected") is True
    assert str(repo / "src") in state["path_after"], "injection must actually touch sys.path"
    assert sys.path == state["saved_path"], "sys.path must be restored to its pre-call state"


def test_injection_does_not_fire_without_quoin_package(ddc, tmp_path, capsys):
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    with _sys_path_guard() as state:
        ddc.main(["--no-scope-check", "--project-root", str(repo), "--format", "json"])
    out = capsys.readouterr().out
    data = json.loads(out)
    assert "src_injected" not in data
    assert state["path_after"] == state["saved_path"]


def test_injection_block_no_repo_resolvable_no_crash(ddc, tmp_path, capsys):
    """The cheapest falsifier for the injection-block crash risk: no
    resolvable repo at all under a bare tmp_path."""
    with _sys_path_guard() as state:
        rc = ddc.main(["--no-scope-check", "--project-root", str(tmp_path), "--format", "json"])
    out = capsys.readouterr().out
    data = json.loads(out)
    assert "src_injected" not in data
    assert rc in (0, 1, 3)
    assert state["path_after"] == state["saved_path"]


def test_resolve_repo_runtime_error_tolerated(ddc, monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(
        ddc._affected_tests, "resolve_repo",
        lambda project_root: (_ for _ in ()).throw(RuntimeError("multiple git repos found")))
    with _sys_path_guard() as state:
        rc = ddc.main(["--no-scope-check", "--project-root", str(tmp_path), "--format", "json"])
    out = capsys.readouterr().out
    data = json.loads(out)
    assert "src_injected" not in data
    assert rc in (0, 1, 3)
    assert state["path_after"] == state["saved_path"]


def test_resolve_repo_oserror_maps_to_exit3_no_nameerror(ddc, monkeypatch, tmp_path, capsys):
    """resolve_repo itself cannot naturally raise OSError (discover_repos
    swallows it internally), so this monkeypatches resolve_repo directly to
    exercise the one path that reaches the generic exception handler while
    src_injected is still unbound in the architecture's original placement
    — proving the pre-try binding prevents a NameError from masking the
    real OSError."""
    monkeypatch.setattr(
        ddc._affected_tests, "resolve_repo",
        lambda project_root: (_ for _ in ()).throw(OSError("boom")))
    with _sys_path_guard() as state:
        rc = ddc.main(["--no-scope-check", "--project-root", str(tmp_path), "--format", "json"])
    out = capsys.readouterr().out
    data = json.loads(out)
    assert rc == 3
    assert data["reason"] == "exception"
    assert "boom" in data["error"]
    assert state["path_after"] == state["saved_path"]


def test_is_file_probe_oserror_maps_to_exit3(ddc, monkeypatch, tmp_path, capsys):
    """The genuinely reachable OSError path is the .is_file() probe on the
    injection candidate itself, not resolve_repo."""
    repo = tmp_path / "repo"
    _make_quoin_src(repo)
    (repo / ".git").mkdir(parents=True)

    real_is_file = Path.is_file

    def flaky_is_file(self):
        if self.name == "__init__.py":
            raise OSError("simulated probe failure")
        return real_is_file(self)

    with _sys_path_guard() as state:
        with mock.patch.object(Path, "is_file", flaky_is_file):
            rc = ddc.main(["--no-scope-check", "--project-root", str(repo), "--format", "json"])
    out = capsys.readouterr().out
    data = json.loads(out)
    assert rc == 3
    assert data["reason"] == "exception"
    assert state["path_after"] == state["saved_path"]


def test_text_output_never_carries_src_injected(ddc, tmp_path, capsys):
    repo = tmp_path / "repo"
    _make_quoin_src(repo)
    (repo / ".git").mkdir(parents=True)
    with _sys_path_guard() as state:
        ddc.main(["--no-scope-check", "--project-root", str(repo), "--format", "text"])
    out = capsys.readouterr().out
    assert "src_injected" not in out
    assert str(repo / "src") in state["path_after"], "injection must actually touch sys.path"
    assert sys.path == state["saved_path"], "sys.path must be restored to its pre-call state"


def test_sys_path_not_duplicated_across_two_runs(ddc, tmp_path, capsys):
    repo = tmp_path / "repo"
    _make_quoin_src(repo)
    (repo / ".git").mkdir(parents=True)
    src_str = str(repo / "src")
    with _sys_path_guard() as state:
        ddc.main(["--no-scope-check", "--project-root", str(repo), "--format", "json"])
        ddc.main(["--no-scope-check", "--project-root", str(repo), "--format", "json"])
        assert sys.path.count(src_str) <= 1, "src must not be inserted twice across two runs"
    capsys.readouterr()
    assert src_str in state["path_after"], "injection must actually touch sys.path"
    assert sys.path == state["saved_path"], "sys.path must be restored to its pre-call state"


def test_coverage_qualifier_byte_identity(ddc):
    assert ddc._COVERAGE_QUALIFIER == (
        "Deploy drift: PASS (checked: skills, scripts, core-scripts, core-workflow, memory, plugins; "
        "not covered: hooks, CLAUDE.md, settings.json, dashboard assets, "
        "QUICKSTART.md — see D-07/D-09)"
    )


def test_gate_skill_qualifier_prefixes_coverage_qualifier(ddc):
    """MIN-2 (T-03 site 6): gate/SKILL.md quotes the checked/not-covered qualifier
    verbatim (minus the internal " — see D-07/D-09" cross-reference) three times.
    This closes the qualifier class permanently: any future category added to
    _COVERAGE_QUALIFIER without updating all three gate/SKILL.md instances fails
    here instead of drifting silently."""
    gate_skill_path = REPO_ROOT / "quoin" / "adapters" / "claude" / "skills" / "gate" / "SKILL.md"
    text = gate_skill_path.read_text(encoding="utf-8")

    matches = re.findall(r"checked: [^\n]*?QUICKSTART\.md", text)
    assert len(matches) == 3, f"expected exactly 3 qualifier quotes in gate/SKILL.md, found {len(matches)}"
    assert len(set(matches)) == 1, "all 3 qualifier quotes in gate/SKILL.md must be byte-identical"

    start = ddc._COVERAGE_QUALIFIER.index("(") + 1
    end = ddc._COVERAGE_QUALIFIER.rindex(")")
    parenthetical = ddc._COVERAGE_QUALIFIER[start:end]
    assert parenthetical.startswith(matches[0]), (
        "gate/SKILL.md's coverage-qualifier quote must be a prefix of "
        "deploy_drift_check._COVERAGE_QUALIFIER's parenthetical"
    )
