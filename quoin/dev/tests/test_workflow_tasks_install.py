"""Opt-in workflow-tasks mod: install state machine, CLI flags, install.sh
forwarding, drift coverage, source-tree invariants and docs.

The mod is copied to <dest>/skills/workflow-tasks/ only when explicitly
requested; a plain install never creates it, and an installed copy is
refreshed on later installs. It shares the opt-in registry with the
context-tracker mod, so both can be installed side by side.
"""
from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import shutil
import subprocess
import unittest.mock
from pathlib import Path

import pytest

import quoin.cli as cli
import quoin.installer as inst

REPO = Path(__file__).resolve().parents[3]
QUOIN_SRC = REPO / "quoin"
PLUGIN_SRC = QUOIN_SRC / "plugins" / "workflow-tasks"
ALLOWLIST = (
    ".claude-plugin/plugin.json",
    "hooks/hooks.json",
    "hooks/register.tsx",
    "hooks/tasks.ts",
    "types/index.d.ts",
)
SOURCE_FILES = ALLOWLIST + (
    "hooks/register.test.ts",
    "hooks/tasks.test.ts",
    "hooks/stage-fixtures.ts",
    "tsconfig.json",
)


def _no_which(*_a, **_k):
    return None


def _files(root: Path) -> set[str]:
    return {p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()}


def _snapshot(root: Path) -> dict[str, bytes]:
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob("*") if p.is_file()}


def _installed(dest: Path) -> Path:
    inst.deploy_workflow_tasks(QUOIN_SRC, dest, strict=True)
    return dest / "skills" / "workflow-tasks"


def _foreign(dest: Path) -> Path:
    folder = dest / "skills" / "workflow-tasks"
    folder.mkdir(parents=True)
    (folder / "notes.txt").write_text("mine")
    return folder


def _src_copy(tmp_path: Path, *, drop: str | None = None) -> Path:
    src = tmp_path / "srctree"
    shutil.copytree(PLUGIN_SRC, src / "plugins" / "workflow-tasks")
    if drop:
        (src / "plugins" / "workflow-tasks" / drop).unlink()
    return src


# ── state ───────────────────────────────────────────────────────────────────

class TestState:
    def test_absent(self, tmp_path):
        assert inst.workflow_tasks_state(tmp_path) == "absent"

    def test_installed(self, tmp_path):
        _installed(tmp_path)
        assert inst.workflow_tasks_state(tmp_path) == "installed"

    def test_foreign_other_name(self, tmp_path):
        d = tmp_path / "skills" / "workflow-tasks" / ".claude-plugin"
        d.mkdir(parents=True)
        (d / "plugin.json").write_text(json.dumps({"name": "something-else"}))
        assert inst.workflow_tasks_state(tmp_path) == "foreign"

    def test_foreign_unparsable_json(self, tmp_path):
        d = tmp_path / "skills" / "workflow-tasks" / ".claude-plugin"
        d.mkdir(parents=True)
        (d / "plugin.json").write_text("{not json")
        assert inst.workflow_tasks_state(tmp_path) == "foreign"

    def test_foreign_missing_plugin_json(self, tmp_path):
        (tmp_path / "skills" / "workflow-tasks").mkdir(parents=True)
        assert inst.workflow_tasks_state(tmp_path) == "foreign"

    def test_foreign_plain_file(self, tmp_path):
        (tmp_path / "skills").mkdir()
        (tmp_path / "skills" / "workflow-tasks").write_text("x")
        assert inst.workflow_tasks_state(tmp_path) == "foreign"

    def test_symlinked_folder_is_foreign(self, tmp_path):
        real = _installed(tmp_path / "real")
        dest = tmp_path / "dest"
        (dest / "skills").mkdir(parents=True)
        (dest / "skills" / "workflow-tasks").symlink_to(real, target_is_directory=True)
        assert inst.workflow_tasks_state(dest) == "foreign"

    def test_a_context_tracker_folder_is_not_ours(self, tmp_path):
        # Ownership is by plugin name: the other mod's folder never reads as installed here.
        inst.deploy_context_tracker(QUOIN_SRC, tmp_path, strict=True)
        assert inst.workflow_tasks_state(tmp_path) == "absent"
        assert inst.context_tracker_state(tmp_path) == "installed"


# ── apply ───────────────────────────────────────────────────────────────────

class TestApply:
    def test_none_absent_creates_nothing_and_prints_nothing(self, tmp_path, capsys):
        assert inst.apply_workflow_tasks(QUOIN_SRC, tmp_path, mode=None) == "noop"
        assert not (tmp_path / "skills").exists()
        out = capsys.readouterr()
        assert out.out == "" and out.err == ""

    def test_none_installed_refreshes(self, tmp_path):
        folder = _installed(tmp_path)
        old = 1_000_000_000
        for p in folder.rglob("*"):
            if p.is_file():
                os.utime(p, (old, old))
        (folder / "hooks" / "tasks.ts").write_text("// edited")
        assert inst.apply_workflow_tasks(QUOIN_SRC, tmp_path, mode=None) == "refreshed"
        assert (folder / "hooks" / "tasks.ts").read_bytes() == (PLUGIN_SRC / "hooks" / "tasks.ts").read_bytes()
        assert (folder / "hooks" / "hooks.json").stat().st_mtime == old

    def test_none_installed_missing_source_warns_and_returns(self, tmp_path, capsys):
        dest = tmp_path / "dest"
        _installed(dest)
        src = _src_copy(tmp_path, drop="hooks/tasks.ts")
        assert inst.apply_workflow_tasks(src, dest, mode=None) == "refreshed"
        assert "tasks.ts" in capsys.readouterr().err

    def test_none_foreign_untouched(self, tmp_path):
        _foreign(tmp_path)
        before = _snapshot(tmp_path)
        assert inst.apply_workflow_tasks(QUOIN_SRC, tmp_path, mode=None) == "noop"
        assert _snapshot(tmp_path) == before

    def test_with_absent_deploys_exactly_the_allowlist(self, tmp_path, capsys):
        assert inst.apply_workflow_tasks(QUOIN_SRC, tmp_path, mode="with") == "deployed"
        folder = tmp_path / "skills" / "workflow-tasks"
        assert _files(folder) == set(ALLOWLIST)
        assert not (folder / ".claude-plugin" / "types").exists()
        assert "Deployed workflow-tasks mod to" in capsys.readouterr().out

    def test_with_twice_is_byte_identical(self, tmp_path):
        inst.apply_workflow_tasks(QUOIN_SRC, tmp_path, mode="with")
        first = _snapshot(tmp_path)
        assert inst.apply_workflow_tasks(QUOIN_SRC, tmp_path, mode="with") == "refreshed"
        assert _snapshot(tmp_path) == first

    def test_remove_installed_deletes_only_the_mod(self, tmp_path, capsys):
        folder = _installed(tmp_path)
        inst.deploy_context_tracker(QUOIN_SRC, tmp_path, strict=True)
        (folder / ".claude-plugin" / "types").mkdir()
        (folder / ".claude-plugin" / "types" / "tsconfig.json").write_text("{}")
        (tmp_path / "skills" / "plan").mkdir()
        (tmp_path / "skills" / "plan" / "SKILL.md").write_text("plan")
        before = {k: v for k, v in _snapshot(tmp_path).items() if "workflow-tasks" not in k}
        assert inst.apply_workflow_tasks(QUOIN_SRC, tmp_path, mode="remove") == "removed"
        assert not folder.exists()
        assert _snapshot(tmp_path) == before
        assert (tmp_path / "skills" / "context-tracker").is_dir()
        assert "Removed workflow-tasks mod from" in capsys.readouterr().out

    def test_remove_absent_prints_nothing_to_remove(self, tmp_path, capsys):
        assert inst.apply_workflow_tasks(QUOIN_SRC, tmp_path, mode="remove") == "absent"
        assert "nothing to remove" in capsys.readouterr().out

    def test_remove_foreign_leaves_bytes(self, tmp_path):
        _foreign(tmp_path)
        before = _snapshot(tmp_path)
        assert inst.apply_workflow_tasks(QUOIN_SRC, tmp_path, mode="remove") == "foreign"
        assert _snapshot(tmp_path) == before

    def test_remove_symlink_never_follows(self, tmp_path):
        real = _installed(tmp_path / "real")
        dest = tmp_path / "dest"
        (dest / "skills").mkdir(parents=True)
        link = dest / "skills" / "workflow-tasks"
        link.symlink_to(real, target_is_directory=True)
        assert inst.apply_workflow_tasks(QUOIN_SRC, dest, mode="remove") == "foreign"
        assert link.is_symlink()
        assert real.is_dir() and (real / ".claude-plugin" / "plugin.json").exists()


# ── preflight ───────────────────────────────────────────────────────────────

def _pre(tmp_path, *, mode, project=False, source=QUOIN_SRC, dest=None, home=None, cwd=None):
    dest = dest or tmp_path / "dest"
    return inst.workflow_tasks_preflight(
        source, dest, mode=mode, is_project_mode=project,
        home_dest_root=home or tmp_path / "home" / ".claude",
        cwd_dest_root=cwd or tmp_path / "cwd" / ".claude",
    )


class TestPreflight:
    def test_with_foreign_is_error(self, tmp_path):
        _foreign(tmp_path / "dest")
        errors, _ = _pre(tmp_path, mode="with")
        assert errors and "workflow-tasks" in errors[0]

    def test_with_missing_source_is_error(self, tmp_path):
        src = _src_copy(tmp_path, drop="hooks/tasks.ts")
        errors, _ = _pre(tmp_path, mode="with", source=src)
        assert errors == ["quoin: workflow-tasks source files missing: hooks/tasks.ts"]

    def test_with_project_and_home_copy_is_error(self, tmp_path):
        _installed(tmp_path / "home" / ".claude")
        errors, _ = _pre(tmp_path, mode="with", project=True)
        assert any("--remove-workflow-tasks" in e for e in errors)

    def test_with_user_and_cwd_copy_is_warning(self, tmp_path):
        _installed(tmp_path / "cwd" / ".claude")
        errors, warnings = _pre(tmp_path, mode="with")
        assert not errors and warnings

    def test_remove_foreign_is_warning(self, tmp_path):
        _foreign(tmp_path / "dest")
        errors, warnings = _pre(tmp_path, mode="remove")
        assert not errors and warnings

    @pytest.mark.parametrize("state", ["absent", "installed", "foreign"])
    def test_mode_none_is_silent(self, tmp_path, state):
        dest = tmp_path / "dest"
        if state == "installed":
            _installed(dest)
        elif state == "foreign":
            _foreign(dest)
        assert _pre(tmp_path, mode=None) == ([], [])


# ── CLI ─────────────────────────────────────────────────────────────────────

class TestCli:
    def test_mutually_exclusive_flags_exit_2(self):
        with unittest.mock.patch.object(cli, "_cmd_install", side_effect=AssertionError("reached")):
            with pytest.raises(SystemExit) as exc:
                cli.main(["install", "--with-workflow-tasks", "--remove-workflow-tasks"])
        assert exc.value.code == 2

    def test_codex_runtime_refused(self):
        with unittest.mock.patch.object(cli, "_cmd_codex_init", side_effect=AssertionError("reached")):
            with pytest.raises(SystemExit) as exc:
                cli.main(["install", "--runtime", "codex", "--with-workflow-tasks"])
        assert exc.value.code == 2

    def test_opencode_runtime_refused(self):
        with unittest.mock.patch.object(cli, "_cmd_opencode_install", side_effect=AssertionError("reached")):
            with pytest.raises(SystemExit) as exc:
                cli.main(["install", "--runtime", "opencode", "--remove-workflow-tasks"])
        assert exc.value.code == 2

    def test_runtime_guard_message_names_the_new_flags(self, capsys):
        with pytest.raises(SystemExit):
            cli.main(["install", "--runtime", "codex", "--with-workflow-tasks"])
        assert (
            "--with-workflow-tasks/--remove-workflow-tasks are only valid with --runtime claude"
            in capsys.readouterr().err
        )

    def test_validate_returns_mode(self):
        v = cli._validate_workflow_tasks_args
        assert v(argparse.Namespace(with_workflow_tasks=True, remove_workflow_tasks=False)) == "with"
        assert v(argparse.Namespace(with_workflow_tasks=False, remove_workflow_tasks=True)) == "remove"
        assert v(argparse.Namespace(with_workflow_tasks=False, remove_workflow_tasks=False)) is None

    def test_validate_tolerates_bare_namespace(self):
        assert cli._validate_workflow_tasks_args(argparse.Namespace()) is None

    def test_validate_rejects_both(self):
        with pytest.raises(ValueError):
            cli._validate_workflow_tasks_args(
                argparse.Namespace(with_workflow_tasks=True, remove_workflow_tasks=True))

    def test_install_help_lists_both_flags(self, capsys):
        with pytest.raises(SystemExit) as exc:
            cli.main(["install", "--help"])
        assert exc.value.code == 0
        out = capsys.readouterr().out
        assert "--with-workflow-tasks" in out and "--remove-workflow-tasks" in out


# ── install.sh ──────────────────────────────────────────────────────────────

class TestInstallSh:
    def _text(self):
        return (QUOIN_SRC / "install.sh").read_text(encoding="utf-8")

    def test_case_arms_present(self):
        text = self._text()
        assert "--with-workflow-tasks)" in text
        assert "--remove-workflow-tasks)" in text

    def test_forwarded_into_install_args(self):
        text = self._text()
        assert 'INSTALL_ARGS+=("$WITH_WORKFLOW_TASKS_FLAG")' in text
        assert 'INSTALL_ARGS+=("$REMOVE_WORKFLOW_TASKS_FLAG")' in text

    def test_usage_and_help_list_both(self):
        text = self._text()
        assert text.count("--with-workflow-tasks") >= 4
        assert text.count("--remove-workflow-tasks") >= 4

    def test_help_output_lists_both(self):
        res = subprocess.run(["bash", str(QUOIN_SRC / "install.sh"), "--help"],
                             capture_output=True, text=True, timeout=30)
        assert res.returncode == 0
        assert "--with-workflow-tasks" in res.stdout and "--remove-workflow-tasks" in res.stdout

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

        both = run("--with-workflow-tasks", "--remove-workflow-tasks")
        assert "--with-workflow-tasks" in both.stdout, both.stdout + both.stderr
        assert "--remove-workflow-tasks" in both.stdout, both.stdout + both.stderr
        plain = run()
        assert "workflow-tasks" not in plain.stdout, plain.stdout + plain.stderr


# ── end to end (real project-scope install into tmp dirs) ───────────────────

def _install(base: Path, name: str, *, with_ct=False, with_wt=False, remove_wt=False,
             seed: Path | None = None, fake_home: Path | None = None) -> tuple[int, Path]:
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
        with_context_tracker=with_ct, remove_context_tracker=False,
        with_workflow_tasks=with_wt, remove_workflow_tasks=remove_wt,
    )
    cwd = os.getcwd()
    with contextlib.ExitStack() as stack:
        stack.enter_context(unittest.mock.patch.object(inst, "check_prerequisites", lambda: []))
        stack.enter_context(unittest.mock.patch("time.sleep"))
        stack.enter_context(unittest.mock.patch.object(Path, "home", return_value=home))
        stack.enter_context(unittest.mock.patch("shutil.which", _no_which))
        os.chdir(project)
        try:
            rc = cli._cmd_claude_install(args)
        finally:
            os.chdir(cwd)
    return rc, project / ".claude"


@pytest.fixture(scope="module")
def shared(tmp_path_factory):
    base = tmp_path_factory.mktemp("wt-e2e")
    rc_a, plain = _install(base, "plain")
    rc_o, opted = _install(base, "opted", with_wt=True)
    assert (rc_a, rc_o) == (0, 0)
    return base, plain, opted


def _fresh_copy(dest: Path, tmp_path: Path) -> Path:
    project = tmp_path / "work" / dest.parent.name
    shutil.copytree(dest.parent, project)
    return project / ".claude"


class TestEndToEnd:
    def test_default_install_has_no_mod_folder(self, shared):
        _, plain, _ = shared
        assert not (plain / "skills" / "workflow-tasks").exists()
        assert not (plain / "skills" / "context-tracker").exists()

    def test_opt_in_deploys_exactly_the_allowlist(self, shared):
        _, _, opted = shared
        assert _files(opted / "skills" / "workflow-tasks") == set(ALLOWLIST)
        assert not (opted / "skills" / "context-tracker").exists()

    def test_opt_in_does_not_touch_settings(self, shared):
        _, plain, opted = shared
        read = lambda d: (d / "settings.json").read_text(encoding="utf-8").replace(str(d), "DEST")
        assert read(opted) == read(plain)

    def test_second_opt_in_run_is_byte_identical(self, tmp_path):
        rc, dest = _install(tmp_path, "twice", with_wt=True)
        assert rc == 0
        first = _snapshot(dest / "skills" / "workflow-tasks")
        rc, dest = _install(tmp_path, "twice", with_wt=True)
        assert rc == 0
        assert _snapshot(dest / "skills" / "workflow-tasks") == first

    def test_plain_install_after_opt_in_keeps_and_refreshes(self, shared, tmp_path):
        _, _, opted = shared
        seed = _fresh_copy(opted, tmp_path)
        edited = seed / "skills" / "workflow-tasks" / "hooks" / "tasks.ts"
        edited.write_text("// edited")
        rc, dest = _install(tmp_path, "refresh", seed=seed)
        assert rc == 0
        refreshed = dest / "skills" / "workflow-tasks" / "hooks" / "tasks.ts"
        assert refreshed.read_bytes() == (PLUGIN_SRC / "hooks" / "tasks.ts").read_bytes()

    def test_removal_after_opt_in(self, shared, tmp_path):
        _, _, opted = shared
        seed = _fresh_copy(opted, tmp_path)
        rc, dest = _install(tmp_path, "removed", remove_wt=True, seed=seed)
        assert rc == 0
        assert not (dest / "skills" / "workflow-tasks").exists()

    def test_foreign_folder_refused_and_nothing_written(self, shared, tmp_path):
        _, plain, _ = shared
        seed = _fresh_copy(plain, tmp_path)
        _foreign(seed)
        before = _snapshot(seed)
        rc, dest = _install(tmp_path, "refused", with_wt=True, seed=seed)
        assert rc == 1
        assert _snapshot(dest) == before

    def test_project_opt_in_with_user_copy_refused(self, shared, tmp_path):
        _, plain, _ = shared
        seed = _fresh_copy(plain, tmp_path)
        home = tmp_path / "fake-home"
        _installed(home / ".claude")
        before = _snapshot(seed)
        rc, dest = _install(tmp_path, "double", with_wt=True, seed=seed, fake_home=home)
        assert rc == 1
        assert _snapshot(dest) == before

    def test_both_refusals_are_reported_in_one_run(self, shared, tmp_path, capsys):
        _, plain, _ = shared
        seed = _fresh_copy(plain, tmp_path)
        for mod in ("context-tracker", "workflow-tasks"):
            (seed / "skills" / mod).mkdir()
            (seed / "skills" / mod / "mine.txt").write_text("keep")
        before = _snapshot(seed)
        capsys.readouterr()
        rc, dest = _install(tmp_path, "both-refused", with_ct=True, with_wt=True, seed=seed)
        err = capsys.readouterr().err
        assert rc == 1
        assert "context-tracker" in err and "workflow-tasks" in err
        assert _snapshot(dest) == before

    def test_both_mods_deploy_both_folders(self, tmp_path):
        rc, dest = _install(tmp_path, "both", with_ct=True, with_wt=True)
        assert rc == 0
        assert (dest / "skills" / "context-tracker" / ".claude-plugin" / "plugin.json").is_file()
        assert _files(dest / "skills" / "workflow-tasks") == set(ALLOWLIST)


# ── source tree invariants ──────────────────────────────────────────────────

FORBIDDEN = ("prompt.submit", "fs.write", "process.")


class TestSourceInvariant:
    def test_exact_source_file_set(self):
        assert _files(PLUGIN_SRC) == set(SOURCE_FILES)

    def test_no_generated_types(self):
        assert not (PLUGIN_SRC / ".claude-plugin" / "types").exists()

    def test_plugin_json_name_and_types(self):
        data = json.loads((PLUGIN_SRC / ".claude-plugin" / "plugin.json").read_text())
        assert data["name"] == "workflow-tasks"
        assert data["types"] == "./types/index.d.ts"

    def test_hooks_json_lists_register(self):
        data = json.loads((PLUGIN_SRC / "hooks" / "hooks.json").read_text())
        assert data == {"modules": ["./register.tsx"]}

    def test_manifest_entries_exist(self):
        for rel in inst.WORKFLOW_TASKS_FILES:
            assert (PLUGIN_SRC / rel).is_file(), rel
        assert tuple(inst.WORKFLOW_TASKS_FILES) == ALLOWLIST

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

    @pytest.mark.parametrize("rel", ["hooks/register.tsx", "hooks/tasks.ts"])
    def test_module_is_read_only_and_never_sends(self, rel):
        # The mod only reads the project and fills the prompt box: no prompt
        # submission, no file writes and no host commands.
        text = (PLUGIN_SRC / rel).read_text(encoding="utf-8")
        for needle in FORBIDDEN:
            assert needle not in text, (rel, needle)

    def test_pure_logic_never_touches_the_engine(self):
        # tasks.ts takes an Fs and plain data; the engine interface appears only in register.tsx.
        text = (PLUGIN_SRC / "hooks" / "tasks.ts").read_text(encoding="utf-8")
        assert not re.search(r"\$\.", text)
        assert "from 'claude-code'" not in text


# ── drift ───────────────────────────────────────────────────────────────────

class TestDrift:
    def test_never_installed_is_clean(self, tmp_path):
        assert inst.compute_drift(QUOIN_SRC, tmp_path, categories=("plugins",)) == []

    def test_fresh_deploy_is_clean(self, tmp_path):
        _installed(tmp_path)
        assert inst.compute_drift(QUOIN_SRC, tmp_path, categories=("plugins",)) == []

    def test_edited_file_is_stale(self, tmp_path):
        folder = _installed(tmp_path)
        (folder / "hooks" / "tasks.ts").write_text("// edited")
        drift = inst.compute_drift(QUOIN_SRC, tmp_path, categories=("plugins",))
        assert [(d.category, d.reason) for d in drift] == [("plugins", "stale")]

    def test_both_mods_installed_and_one_edited_reports_only_that_mod(self, tmp_path):
        folder = _installed(tmp_path)
        inst.deploy_context_tracker(QUOIN_SRC, tmp_path, strict=True)
        assert inst.compute_drift(QUOIN_SRC, tmp_path, categories=("plugins",)) == []
        (folder / "hooks" / "register.tsx").write_text("// edited")
        drift = inst.compute_drift(QUOIN_SRC, tmp_path, categories=("plugins",))
        assert len(drift) == 1
        assert "workflow-tasks" in drift[0].deployed_path
        assert "context-tracker" not in drift[0].deployed_path

    def test_foreign_folder_is_not_compared(self, tmp_path):
        _foreign(tmp_path)
        assert inst.compute_drift(QUOIN_SRC, tmp_path, categories=("plugins",)) == []


# ── affected-tests mapping ──────────────────────────────────────────────────

class TestAffectedTests:
    @staticmethod
    def _select(changed: str) -> tuple[str, list]:
        import importlib.util
        import sys

        path = QUOIN_SRC / "core" / "scripts" / "affected_tests.py"
        spec = importlib.util.spec_from_file_location("_wt_affected_tests", path)
        mod = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = mod
        spec.loader.exec_module(mod)
        selectors, _unmatched, ignored = mod.map_changed_to_tests([changed], REPO)
        return " ".join(str(s) for s in selectors), ignored

    def test_pure_logic_selects_install_ts_and_parity_tests(self):
        joined, ignored = self._select("quoin/plugins/workflow-tasks/hooks/tasks.ts")
        assert "test_workflow_tasks_install.py" in joined
        assert "test_workflow_tasks_plugin_ts.py" in joined
        assert "test_workflow_tasks_stage_parity.py" in joined
        assert not ignored

    @pytest.mark.parametrize("rel", sorted(SOURCE_FILES))
    def test_every_plugin_file_selects_its_install_and_ts_tests(self, rel):
        joined, ignored = self._select(f"quoin/plugins/workflow-tasks/{rel}")
        assert "test_workflow_tasks_install.py" in joined
        assert "test_workflow_tasks_plugin_ts.py" in joined
        assert not ignored

    def test_install_script_and_hooks_guide_select_the_install_tests(self):
        for changed in ("quoin/install.sh", "quoin/docs/hooks-guide.md"):
            joined, _ = self._select(changed)
            assert "test_workflow_tasks_install.py" in joined, changed

    def test_status_graph_change_selects_the_parity_test(self):
        joined, _ = self._select("quoin/core/scripts/status_graph.py")
        assert "test_workflow_tasks_stage_parity.py" in joined
