"""Opt-in context-tracker mod: install state machine, CLI flags, install.sh
forwarding, drift coverage, source-tree invariants and docs.

The mod is copied to <dest>/skills/context-tracker/ only when explicitly
requested; a plain install never creates it, and an installed copy is
refreshed on later installs.
"""
from __future__ import annotations

import argparse
import contextlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import unittest.mock
from pathlib import Path

import pytest

import quoin.cli as cli
import quoin.installer as inst

REPO = Path(__file__).resolve().parents[3]
QUOIN_SRC = REPO / "quoin"
PLUGIN_SRC = QUOIN_SRC / "plugins" / "context-tracker"
ALLOWLIST = (
    ".claude-plugin/plugin.json",
    "hooks/hooks.json",
    "hooks/register.tsx",
    "types/index.d.ts",
)
SOURCE_FILES = ALLOWLIST + ("hooks/register.test.ts", "tsconfig.json")


def _no_which(*_a, **_k):
    return None


def _files(root: Path) -> set[str]:
    return {p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()}


def _snapshot(root: Path) -> dict[str, bytes]:
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob("*") if p.is_file()}


def _installed(dest: Path) -> Path:
    inst.deploy_context_tracker(QUOIN_SRC, dest, strict=True)
    return dest / "skills" / "context-tracker"


def _src_copy(tmp_path: Path, *, drop: str | None = None) -> Path:
    src = tmp_path / "srctree"
    shutil.copytree(PLUGIN_SRC, src / "plugins" / "context-tracker")
    if drop:
        (src / "plugins" / "context-tracker" / drop).unlink()
    return src


# ── state ───────────────────────────────────────────────────────────────────

class TestState:
    def test_absent(self, tmp_path):
        assert inst.context_tracker_state(tmp_path) == "absent"

    def test_installed(self, tmp_path):
        _installed(tmp_path)
        assert inst.context_tracker_state(tmp_path) == "installed"

    def test_foreign_other_name(self, tmp_path):
        d = tmp_path / "skills" / "context-tracker" / ".claude-plugin"
        d.mkdir(parents=True)
        (d / "plugin.json").write_text(json.dumps({"name": "something-else"}))
        assert inst.context_tracker_state(tmp_path) == "foreign"

    def test_foreign_unparsable_json(self, tmp_path):
        d = tmp_path / "skills" / "context-tracker" / ".claude-plugin"
        d.mkdir(parents=True)
        (d / "plugin.json").write_text("{not json")
        assert inst.context_tracker_state(tmp_path) == "foreign"

    def test_foreign_missing_plugin_json(self, tmp_path):
        (tmp_path / "skills" / "context-tracker").mkdir(parents=True)
        assert inst.context_tracker_state(tmp_path) == "foreign"

    def test_foreign_plain_file(self, tmp_path):
        (tmp_path / "skills").mkdir()
        (tmp_path / "skills" / "context-tracker").write_text("x")
        assert inst.context_tracker_state(tmp_path) == "foreign"

    def test_symlinked_folder_is_foreign(self, tmp_path):
        real = _installed(tmp_path / "real")
        dest = tmp_path / "dest"
        (dest / "skills").mkdir(parents=True)
        (dest / "skills" / "context-tracker").symlink_to(real, target_is_directory=True)
        assert inst.context_tracker_state(dest) == "foreign"


# ── apply ───────────────────────────────────────────────────────────────────

class TestApply:
    def test_none_absent_creates_nothing_and_prints_nothing(self, tmp_path, capsys):
        assert inst.apply_context_tracker(QUOIN_SRC, tmp_path, mode=None) == "noop"
        assert not (tmp_path / "skills").exists()
        out = capsys.readouterr()
        assert out.out == "" and out.err == ""

    def test_none_installed_refreshes(self, tmp_path):
        folder = _installed(tmp_path)
        old = 1_000_000_000
        for p in folder.rglob("*"):
            if p.is_file():
                os.utime(p, (old, old))
        (folder / "hooks" / "register.tsx").write_text("// edited")
        assert inst.apply_context_tracker(QUOIN_SRC, tmp_path, mode=None) == "refreshed"
        assert (folder / "hooks" / "register.tsx").read_bytes() == (PLUGIN_SRC / "hooks" / "register.tsx").read_bytes()
        assert (folder / "hooks" / "hooks.json").stat().st_mtime == old

    def test_none_installed_missing_source_warns_and_returns(self, tmp_path, capsys):
        dest = tmp_path / "dest"
        _installed(dest)
        src = _src_copy(tmp_path, drop="hooks/register.tsx")
        result = inst.apply_context_tracker(src, dest, mode=None)
        assert result == "refreshed"
        assert "register.tsx" in capsys.readouterr().err

    def test_none_foreign_untouched(self, tmp_path):
        folder = tmp_path / "skills" / "context-tracker"
        folder.mkdir(parents=True)
        (folder / "notes.txt").write_text("mine")
        before = _snapshot(tmp_path)
        assert inst.apply_context_tracker(QUOIN_SRC, tmp_path, mode=None) == "noop"
        assert _snapshot(tmp_path) == before

    def test_with_absent_deploys_exactly_allowlist(self, tmp_path, capsys):
        assert inst.apply_context_tracker(QUOIN_SRC, tmp_path, mode="with") == "deployed"
        assert _files(tmp_path / "skills" / "context-tracker") == set(ALLOWLIST)
        assert "Deployed context-tracker mod to" in capsys.readouterr().out

    def test_with_twice_is_byte_identical(self, tmp_path):
        inst.apply_context_tracker(QUOIN_SRC, tmp_path, mode="with")
        first = _snapshot(tmp_path)
        assert inst.apply_context_tracker(QUOIN_SRC, tmp_path, mode="with") == "refreshed"
        assert _snapshot(tmp_path) == first

    def test_remove_installed_deletes_only_the_mod(self, tmp_path, capsys):
        folder = _installed(tmp_path)
        (folder / ".claude-plugin" / "types").mkdir()
        (folder / ".claude-plugin" / "types" / "tsconfig.json").write_text("{}")
        (tmp_path / "skills" / "plan").mkdir()
        (tmp_path / "skills" / "plan" / "SKILL.md").write_text("plan")
        (tmp_path / "settings.json").write_text("{}")
        before = {k: v for k, v in _snapshot(tmp_path).items() if "context-tracker" not in k}
        assert inst.apply_context_tracker(QUOIN_SRC, tmp_path, mode="remove") == "removed"
        assert not folder.exists()
        assert _snapshot(tmp_path) == before
        assert "Removed context-tracker mod from" in capsys.readouterr().out

    def test_remove_absent_prints_nothing_to_remove(self, tmp_path, capsys):
        assert inst.apply_context_tracker(QUOIN_SRC, tmp_path, mode="remove") == "absent"
        assert "nothing to remove" in capsys.readouterr().out

    def test_remove_foreign_leaves_bytes(self, tmp_path):
        folder = tmp_path / "skills" / "context-tracker"
        folder.mkdir(parents=True)
        (folder / "notes.txt").write_text("mine")
        before = _snapshot(tmp_path)
        assert inst.apply_context_tracker(QUOIN_SRC, tmp_path, mode="remove") == "foreign"
        assert _snapshot(tmp_path) == before

    def test_remove_symlink_never_follows(self, tmp_path):
        real = _installed(tmp_path / "real")
        dest = tmp_path / "dest"
        (dest / "skills").mkdir(parents=True)
        link = dest / "skills" / "context-tracker"
        link.symlink_to(real, target_is_directory=True)
        assert inst.apply_context_tracker(QUOIN_SRC, dest, mode="remove") == "foreign"
        assert link.is_symlink()
        assert real.is_dir() and (real / ".claude-plugin" / "plugin.json").exists()


# ── preflight ───────────────────────────────────────────────────────────────

def _pre(tmp_path, *, mode, project=False, source=QUOIN_SRC, dest=None, home=None, cwd=None):
    dest = dest or tmp_path / "dest"
    return inst.context_tracker_preflight(
        source, dest, mode=mode, is_project_mode=project,
        home_dest_root=home or tmp_path / "home" / ".claude",
        cwd_dest_root=cwd or tmp_path / "cwd" / ".claude",
    )


class TestPreflight:
    def _foreign(self, dest):
        (dest / "skills" / "context-tracker").mkdir(parents=True)

    def test_with_foreign_is_error(self, tmp_path):
        self._foreign(tmp_path / "dest")
        errors, _ = _pre(tmp_path, mode="with")
        assert errors

    def test_with_missing_source_is_error(self, tmp_path):
        src = _src_copy(tmp_path, drop="hooks/hooks.json")
        errors, _ = _pre(tmp_path, mode="with", source=src)
        assert any("hooks/hooks.json" in e for e in errors)

    def test_with_project_and_home_copy_is_error(self, tmp_path):
        _installed(tmp_path / "home" / ".claude")
        errors, _ = _pre(tmp_path, mode="with", project=True)
        assert any("--remove-context-tracker" in e for e in errors)

    def test_with_user_and_cwd_copy_is_warning(self, tmp_path):
        _installed(tmp_path / "cwd" / ".claude")
        errors, warnings = _pre(tmp_path, mode="with")
        assert not errors and warnings

    def test_same_cwd_and_dest_no_warning(self, tmp_path):
        dest = tmp_path / "same" / ".claude"
        _installed(dest)
        errors, warnings = _pre(tmp_path, mode="with", dest=dest, cwd=dest)
        assert not errors and not warnings

    def test_remove_foreign_is_warning(self, tmp_path):
        self._foreign(tmp_path / "dest")
        errors, warnings = _pre(tmp_path, mode="remove")
        assert not errors and warnings

    @pytest.mark.parametrize("state", ["absent", "installed", "foreign"])
    def test_mode_none_is_silent(self, tmp_path, state):
        dest = tmp_path / "dest"
        if state == "installed":
            _installed(dest)
        elif state == "foreign":
            self._foreign(dest)
        assert _pre(tmp_path, mode=None) == ([], [])


# ── CLI ─────────────────────────────────────────────────────────────────────

class TestCli:
    def test_mutually_exclusive_flags_exit_2(self):
        with unittest.mock.patch.object(cli, "_cmd_install", side_effect=AssertionError("reached")):
            with pytest.raises(SystemExit) as exc:
                cli.main(["install", "--with-context-tracker", "--remove-context-tracker"])
        assert exc.value.code == 2

    def test_codex_runtime_refused(self):
        with unittest.mock.patch.object(cli, "_cmd_codex_init", side_effect=AssertionError("reached")):
            with pytest.raises(SystemExit) as exc:
                cli.main(["install", "--runtime", "codex", "--with-context-tracker"])
        assert exc.value.code == 2

    def test_opencode_runtime_refused(self):
        with unittest.mock.patch.object(cli, "_cmd_opencode_install", side_effect=AssertionError("reached")):
            with pytest.raises(SystemExit) as exc:
                cli.main(["install", "--runtime", "opencode", "--remove-context-tracker"])
        assert exc.value.code == 2

    def test_validate_returns_mode(self):
        v = cli._validate_context_tracker_args
        assert v(argparse.Namespace(with_context_tracker=True, remove_context_tracker=False)) == "with"
        assert v(argparse.Namespace(with_context_tracker=False, remove_context_tracker=True)) == "remove"
        assert v(argparse.Namespace(with_context_tracker=False, remove_context_tracker=False)) is None

    def test_validate_tolerates_bare_namespace(self):
        assert cli._validate_context_tracker_args(argparse.Namespace()) is None

    def test_validate_rejects_both(self):
        with pytest.raises(ValueError):
            cli._validate_context_tracker_args(
                argparse.Namespace(with_context_tracker=True, remove_context_tracker=True))


# ── install.sh ──────────────────────────────────────────────────────────────

class TestInstallSh:
    def _text(self):
        return (QUOIN_SRC / "install.sh").read_text(encoding="utf-8")

    def test_case_arms_present(self):
        text = self._text()
        assert "--with-context-tracker)" in text
        assert "--remove-context-tracker)" in text

    def test_forwarded_into_install_args(self):
        text = self._text()
        assert 'INSTALL_ARGS+=("$WITH_CONTEXT_TRACKER_FLAG")' in text
        assert 'INSTALL_ARGS+=("$REMOVE_CONTEXT_TRACKER_FLAG")' in text

    def test_usage_and_help_list_both(self):
        text = self._text()
        assert text.count("--with-context-tracker") >= 4
        assert text.count("--remove-context-tracker") >= 4

    def test_functional_forwarding(self, tmp_path):
        stub = tmp_path / "_pystub"
        stub.write_text(
            "#!/usr/bin/env bash\n"
            'if [[ "$1" == "-c" ]]; then\n'
            '  case "$2" in\n'
            "    *sys.version_info*) echo 3013 ;;\n"
            "    *__about__*) : ;;\n"
            '    *"import quoin"*) if [[ -n "${PYTHONPATH:-}" ]]; then exit 0; else exit 1; fi ;;\n'
            "    *) exit 0 ;;\n"
            "  esac\n"
            "  exit 0\n"
            "fi\n"
            'if [[ "$1" == "-m" && "$2" == "quoin" && "$3" == "--version" ]]; then exit 1; fi\n'
            'echo "ARGV:$*"\n'
            "exit 0\n",
            encoding="utf-8",
        )
        stub.chmod(0o755)
        for name in ("python3.13", "python3.12", "python3.11", "python3.10", "python3", "python"):
            link = tmp_path / name
            if not link.exists():
                link.symlink_to(stub)
        env = {**os.environ, "PATH": f"{tmp_path}:{os.environ.get('PATH', '')}"}

        def run(*flags):
            return subprocess.run(
                ["bash", str(QUOIN_SRC / "install.sh"), "--scope", "project", *flags],
                cwd=str(tmp_path), capture_output=True, text=True, timeout=30, env=env,
            )

        both = run("--with-context-tracker", "--remove-context-tracker")
        assert "--with-context-tracker" in both.stdout, both.stdout + both.stderr
        assert "--remove-context-tracker" in both.stdout, both.stdout + both.stderr
        plain = run()
        assert "context-tracker" not in plain.stdout, plain.stdout + plain.stderr


# ── end to end (real project-scope install into tmp dirs) ───────────────────

def _install(base: Path, name: str, *, with_ct=False, remove_ct=False, seed: Path | None = None,
             fake_home: Path | None = None, skip_apply=False) -> tuple[int, Path]:
    """Run a real project-scope install; returns (exit code, <project>/.claude)."""
    project = base / name
    if seed is not None:
        shutil.copytree(seed.parent, project)
    else:
        project.mkdir(parents=True, exist_ok=True)
    home = fake_home or (base / (name + "-home"))
    home.mkdir(parents=True, exist_ok=True)
    args = argparse.Namespace(
        scope="project", allow_hook_merge=True, runtime="claude", check=False,
        source_dir=str(QUOIN_SRC), force_merge=False, dev=False, use_pip=False,
        with_context_tracker=with_ct, remove_context_tracker=remove_ct,
    )
    cwd = os.getcwd()
    with contextlib.ExitStack() as stack:
        stack.enter_context(unittest.mock.patch.object(inst, "check_prerequisites", lambda: []))
        stack.enter_context(unittest.mock.patch("time.sleep"))
        stack.enter_context(unittest.mock.patch.object(Path, "home", return_value=home))
        stack.enter_context(unittest.mock.patch("shutil.which", _no_which))
        if skip_apply:
            stack.enter_context(unittest.mock.patch.object(
                inst, "apply_context_tracker", lambda *a, **k: "noop"))
        os.chdir(project)
        try:
            rc = cli._cmd_claude_install(args)
        finally:
            os.chdir(cwd)
    return rc, project / ".claude"


@pytest.fixture(scope="module")
def shared(tmp_path_factory):
    base = tmp_path_factory.mktemp("ct-e2e")
    rc_a, plain_a = _install(base, "plain-a")
    rc_b, plain_b = _install(base, "plain-b", skip_apply=True)
    rc_o, opted = _install(base, "opted", with_ct=True)
    assert (rc_a, rc_b, rc_o) == (0, 0, 0)
    return base, plain_a, plain_b, opted


def _normalised(dest: Path) -> dict[str, bytes]:
    out = {}
    for rel, data in _snapshot(dest).items():
        if rel == "quoin-runtime.json":
            out[rel] = b""
        else:
            out[rel] = data.replace(str(dest).encode(), b"DEST")
    return out


def _settings(dest: Path) -> dict:
    text = (dest / "settings.json").read_text(encoding="utf-8").replace(str(dest), "DEST")
    return json.loads(text)


def _all_keys(node) -> list[str]:
    if isinstance(node, dict):
        return [str(k) for k in node] + [k for v in node.values() for k in _all_keys(v)]
    if isinstance(node, list):
        return [k for v in node for k in _all_keys(v)]
    return []


def _fresh_copy(dest: Path, tmp_path: Path) -> Path:
    project = tmp_path / "work" / dest.parent.name
    shutil.copytree(dest.parent, project)
    return project / ".claude"


class TestEndToEnd:
    def test_default_install_unchanged(self, shared):
        _, plain_a, plain_b, _ = shared
        assert _normalised(plain_a) == _normalised(plain_b)
        assert not (plain_a / "skills" / "context-tracker").exists()
        keys = _all_keys(json.loads((plain_a / "settings.json").read_text(encoding="utf-8")))
        assert not [k for k in keys if "context-tracker" in k or "plugin" in k.lower()]

    def test_opt_in_deploys_four_files(self, shared):
        _, plain_a, _, opted = shared
        assert _files(opted / "skills" / "context-tracker") == set(ALLOWLIST)
        assert _settings(opted) == _settings(plain_a)

    def test_foreign_folder_refused_and_nothing_written(self, shared, tmp_path):
        _, plain_a, _, _ = shared
        seed = _fresh_copy(plain_a, tmp_path)
        (seed / "skills" / "context-tracker").mkdir()
        (seed / "skills" / "context-tracker" / "mine.txt").write_text("keep")
        before = _snapshot(seed)
        assert "quoin-runtime.json" in before
        rc, dest = _install(tmp_path, "refused", with_ct=True, seed=seed)
        assert rc == 1
        assert _snapshot(dest) == before

    def test_project_opt_in_with_user_copy_refused(self, shared, tmp_path):
        _, plain_a, _, _ = shared
        seed = _fresh_copy(plain_a, tmp_path)
        home = tmp_path / "fake-home"
        _installed(home / ".claude")
        before = _snapshot(seed)
        rc, dest = _install(tmp_path, "double", with_ct=True, seed=seed, fake_home=home)
        assert rc == 1
        assert _snapshot(dest) == before

    def test_settings_idempotent_across_opt_in_runs(self, shared, tmp_path):
        rc, dest = _install(tmp_path, "idem", with_ct=True)
        assert rc == 0
        first = (dest / "settings.json").read_bytes()
        rc, dest = _install(tmp_path, "idem", with_ct=True)
        assert rc == 0
        assert (dest / "settings.json").read_bytes() == first

    def test_removal_after_opt_in(self, shared, tmp_path):
        _, _, _, opted = shared
        seed = _fresh_copy(opted, tmp_path)
        rc, dest = _install(tmp_path, "removed", remove_ct=True, seed=seed)
        assert rc == 0
        assert not (dest / "skills" / "context-tracker").exists()
        _, plain_a, _, _ = shared
        after = {k: v for k, v in _normalised(dest).items()}
        expected = {k: v for k, v in _normalised(plain_a).items()}
        assert after == expected


# ── source tree invariants ──────────────────────────────────────────────────

class TestSourceInvariant:
    def test_exactly_six_files(self):
        assert _files(PLUGIN_SRC) == set(SOURCE_FILES)

    def test_no_generated_types(self):
        assert not (PLUGIN_SRC / ".claude-plugin" / "types").exists()

    def test_plugin_json_name(self):
        data = json.loads((PLUGIN_SRC / ".claude-plugin" / "plugin.json").read_text())
        assert data["name"] == "context-tracker"

    def test_hooks_json_lists_register(self):
        assert "./register.tsx" in (PLUGIN_SRC / "hooks" / "hooks.json").read_text()

    def test_manifest_entries_exist(self):
        for rel in inst.CONTEXT_TRACKER_FILES:
            assert (PLUGIN_SRC / rel).is_file(), rel
        assert tuple(inst.CONTEXT_TRACKER_FILES) == ALLOWLIST

    def test_git_does_not_track_generated_types(self):
        if shutil.which("git") is None:
            pytest.skip("git not available")
        res = subprocess.run(["git", "-C", str(REPO), "ls-files", "quoin/plugins"],
                             capture_output=True, text=True)
        if res.returncode != 0:
            pytest.skip("not a git checkout")
        assert ".claude-plugin/types/" not in res.stdout

    def test_no_install_placeholder(self):
        for rel in SOURCE_FILES:
            assert "__QUOIN_HOME__" not in (PLUGIN_SRC / rel).read_text(encoding="utf-8"), rel


# ── drift ───────────────────────────────────────────────────────────────────

class TestDrift:
    def test_never_installed_is_clean(self, tmp_path):
        assert inst.compute_drift(QUOIN_SRC, tmp_path, categories=("plugins",)) == []

    def test_fresh_deploy_is_clean(self, tmp_path):
        _installed(tmp_path)
        assert inst.compute_drift(QUOIN_SRC, tmp_path, categories=("plugins",)) == []

    def test_edited_file_is_stale(self, tmp_path):
        folder = _installed(tmp_path)
        (folder / "hooks" / "register.tsx").write_text("// edited")
        drift = inst.compute_drift(QUOIN_SRC, tmp_path, categories=("plugins",))
        assert [(d.category, d.reason) for d in drift] == [("plugins", "stale")]

    def test_deleted_file_is_missing(self, tmp_path):
        folder = _installed(tmp_path)
        (folder / "types" / "index.d.ts").unlink()
        drift = inst.compute_drift(QUOIN_SRC, tmp_path, categories=("plugins",))
        assert [(d.category, d.reason) for d in drift] == [("plugins", "missing")]

    def test_generated_types_ignored(self, tmp_path):
        folder = _installed(tmp_path)
        (folder / ".claude-plugin" / "types").mkdir()
        (folder / ".claude-plugin" / "types" / "foo.d.ts").write_text("x")
        assert inst.compute_drift(QUOIN_SRC, tmp_path, categories=("plugins",)) == []

    def test_foreign_folder_is_clean(self, tmp_path):
        (tmp_path / "skills" / "context-tracker").mkdir(parents=True)
        assert inst.compute_drift(QUOIN_SRC, tmp_path, categories=("plugins",)) == []

    def test_all_categories_on_never_installed_has_no_plugins_entry(self, tmp_path):
        assert not [d for d in inst.compute_drift(QUOIN_SRC, tmp_path) if d.category == "plugins"]


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


class TestDriftScript:
    @pytest.fixture
    def ddc(self):
        return _load("_ct_ddc", QUOIN_SRC / "scripts" / "deploy_drift_check.py")

    def _run(self, ddc, dest: Path) -> int:
        return ddc.main([
            "--no-scope-check", "--scope", f"project:{dest.parent}",
            "--source-dir", str(QUOIN_SRC), "--project-root", str(REPO),
        ])

    def test_category_parity(self, ddc):
        assert tuple(ddc._CHECKED_CATEGORIES) == tuple(inst.DRIFT_CATEGORIES)
        assert "plugins" in ddc._COVERAGE_QUALIFIER

    def test_plain_install_clean(self, ddc, shared):
        assert self._run(ddc, shared[1]) == 0

    def test_opted_in_install_clean(self, ddc, shared):
        assert self._run(ddc, shared[3]) == 0

    def test_edited_mod_file_is_drift(self, ddc, shared, tmp_path):
        # Deployed files embed their install path, so a fresh install (not a
        # copied tree) is needed for the drift run to start clean.
        rc, dest = _install(tmp_path, "drifted", with_ct=True)
        assert rc == 0 and self._run(ddc, dest) == 0
        (dest / "skills" / "context-tracker" / "hooks" / "register.tsx").write_text("// edited")
        assert self._run(ddc, dest) == 1


# ── docs ────────────────────────────────────────────────────────────────────

class TestDocs:
    def test_hooks_guide_section(self):
        text = (QUOIN_SRC / "docs" / "hooks-guide.md").read_text(encoding="utf-8")
        assert "## Opt-in context-tracker mod" in text
        section = text.split("## Opt-in context-tracker mod", 1)[1]
        for needle in ("--with-context-tracker", "--remove-context-tracker",
                       "context-tracker@skills-dir", "user scope"):
            assert needle in section, needle
