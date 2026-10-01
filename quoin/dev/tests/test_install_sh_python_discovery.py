"""install.sh interpreter discovery and tier-3 resilience.

Every case runs install.sh under /bin/bash (3.2 on macOS) with a scrubbed
environment: PATH holds only a directory of symlinks to the few external tools
the script needs plus the fake interpreters a test lays out, HOME is a scratch
directory, and the off-PATH search list is replaced via QUOIN_PYTHON_SEARCH_DIRS.
Nothing depends on the Python layout of the machine running the tests.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
INSTALL_SH = REPO_ROOT / "quoin" / "install.sh"
BASH = "/bin/bash"

pytestmark = pytest.mark.skipif(
    not Path(BASH).exists(), reason="requires /bin/bash"
)


def _requires_python() -> str:
    text = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    return re.search(r'requires-python\s*=\s*"([^"]+)"', text).group(1)


def _tools_dir(tmp_path: Path) -> Path:
    tools = tmp_path / "tools"
    tools.mkdir(exist_ok=True)
    for name in ("dirname", "awk", "env", "cat", "mktemp", "rm", "sleep"):
        found = shutil.which(name)
        assert found, name
        link = tools / name
        if not link.exists():
            link.symlink_to(found)
    return tools


def _fake_python(path: Path, version: int) -> Path:
    """Executable stand-in that reports `version` (major*1000+minor) to -c probes."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        '#!/bin/sh\n[ "$1" = "-c" ] && { echo %d; exit 0; }\nexit 0\n' % version
    )
    path.chmod(0o755)
    return path


def _run(tmp_path, path_dirs, search_dirs="", extra_env=None, args=None,
         install_sh=INSTALL_SH, cwd=None):
    tools = _tools_dir(tmp_path)
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    env = {
        "PATH": ":".join([str(d) for d in path_dirs] + [str(tools)]),
        "HOME": str(home),
    }
    if search_dirs is not None:
        env["QUOIN_PYTHON_SEARCH_DIRS"] = search_dirs
    env.update(extra_env or {})
    return subprocess.run(
        [BASH, str(install_sh)] + list(args or ["--print-python", "--scope", "user"]),
        capture_output=True, text=True, timeout=60, env=env,
        cwd=str(cwd or tmp_path), stdin=subprocess.DEVNULL,
    )


class TestDiscovery:
    def test_python314_beats_old_python3(self, tmp_path):
        d = tmp_path / "bin"
        _fake_python(d / "python3", 3008)
        want = _fake_python(d / "python3.14", 3014)
        r = _run(tmp_path, [d])
        assert r.returncode == 0, r.stderr
        assert r.stdout.strip() == str(want)

    def test_off_path_search_dir(self, tmp_path):
        onpath = tmp_path / "onpath"
        _fake_python(onpath / "python3", 3008)
        off = tmp_path / "opt" / "bin"
        want = _fake_python(off / "python3.12", 3012)
        r = _run(tmp_path, [onpath], search_dirs=str(off))
        assert r.returncode == 0, r.stderr
        assert r.stdout.strip() == str(want)

    def test_dangling_link_is_skipped(self, tmp_path):
        d1 = tmp_path / "d1"
        d1.mkdir()
        (d1 / "python3.13").symlink_to(tmp_path / "gone")
        d2 = tmp_path / "d2"
        want = _fake_python(d2 / "python3", 3011)
        r = _run(tmp_path, [d1, d2])
        assert r.returncode == 0, r.stderr
        assert r.stdout.strip() == str(want)

    def test_path_order_beats_higher_version_later(self, tmp_path):
        d1, d2 = tmp_path / "d1", tmp_path / "d2"
        want = _fake_python(d1 / "python3", 3010)
        _fake_python(d2 / "python3.13", 3013)
        r = _run(tmp_path, [d1, d2])
        assert r.stdout.strip() == str(want)

    def test_quoin_python_wins_over_path(self, tmp_path):
        d = tmp_path / "bin"
        _fake_python(d / "python3", 3012)
        override = _fake_python(tmp_path / "elsewhere" / "py", 3011)
        r = _run(tmp_path, [d], extra_env={"QUOIN_PYTHON": str(override)})
        assert r.stdout.strip() == str(override)

    def test_insufficient_quoin_python_warns_and_continues(self, tmp_path):
        d = tmp_path / "bin"
        want = _fake_python(d / "python3", 3012)
        old = _fake_python(tmp_path / "elsewhere" / "py", 3008)
        r = _run(tmp_path, [d], extra_env={"QUOIN_PYTHON": str(old)})
        assert r.returncode == 0, r.stderr
        assert r.stdout.strip() == str(want)
        assert "QUOIN_PYTHON" in r.stderr and str(old) in r.stderr

    def test_uv_fallback(self, tmp_path):
        want = _fake_python(tmp_path / "uvpy" / "python", 3013)
        uvdir = tmp_path / "uvbin"
        uvdir.mkdir()
        uv = uvdir / "uv"
        uv.write_text('#!/bin/sh\n[ "$1" = python ] && echo "%s"\n' % want)
        uv.chmod(0o755)
        r = _run(tmp_path, [uvdir])
        assert r.returncode == 0, r.stderr
        assert r.stdout.strip() == str(want)

    def test_nothing_sufficient_lists_what_was_tried(self, tmp_path):
        d = tmp_path / "bin"
        a = _fake_python(d / "python3", 3009)
        b = _fake_python(d / "python", 3009)
        r = _run(tmp_path, [d])
        assert r.returncode == 1
        assert str(a) in r.stderr and str(b) in r.stderr
        assert "3.9" in r.stderr
        assert _requires_python() in r.stderr
        assert "QUOIN_PYTHON" in r.stderr

    def test_config_helper_names_never_chosen(self, tmp_path):
        d = tmp_path / "bin"
        _fake_python(d / "python3.14-config", 3014)
        _fake_python(d / "python3", 3009)
        r = _run(tmp_path, [d])
        assert r.returncode == 1

    def test_shim_with_noise_is_not_runnable(self, tmp_path):
        d1, d2 = tmp_path / "d1", tmp_path / "d2"
        d1.mkdir()
        shim = d1 / "python3"
        shim.write_text('#!/bin/sh\necho "pyenv: version not installed"\necho 0\nexit 1\n')
        shim.chmod(0o755)
        want = _fake_python(d2 / "python3", 3012)
        r = _run(tmp_path, [d1, d2])
        assert r.returncode == 0, r.stderr
        assert r.stdout.strip() == str(want)
        assert "syntax error" not in r.stderr and "arithmetic" not in r.stderr

    def test_minimum_read_from_pyproject_without_trailing_newline(self, tmp_path):
        root = tmp_path / "proj"
        (root / "quoin").mkdir(parents=True)
        shutil.copy(INSTALL_SH, root / "quoin" / "install.sh")
        (root / "pyproject.toml").write_text('[project]\nrequires-python = ">=3.11"')
        d = tmp_path / "bin"
        _fake_python(d / "python3", 3010)
        r = _run(tmp_path, [d], install_sh=root / "quoin" / "install.sh")
        assert r.returncode == 1
        assert ">=3.11" in r.stderr

    def test_empty_path_entry_does_not_probe_cwd(self, tmp_path):
        d = tmp_path / "bin"
        _fake_python(d / "python3", 3009)
        cwd = tmp_path / "cwd"
        stray = _fake_python(cwd / "python3", 3014)
        tools = _tools_dir(tmp_path)
        env = {"PATH": f"{d}::{tools}", "HOME": str(tmp_path / "home"),
               "QUOIN_PYTHON_SEARCH_DIRS": ""}
        r = subprocess.run([BASH, str(INSTALL_SH), "--print-python", "--scope", "user"],
                           capture_output=True, text=True, timeout=60, env=env,
                           cwd=str(cwd), stdin=subprocess.DEVNULL)
        assert r.returncode == 1
        assert str(stray) not in r.stdout
        assert "unbound" not in r.stderr

    @staticmethod
    def _sandboxed_installer(tmp_path):
        """Copy of install.sh whose absolute well-known roots point into tmp_path,
        so host interpreters under /opt, /usr/local etc. cannot win the search."""
        fake = tmp_path / "fakeroot"
        root = tmp_path / "proj"
        (root / "quoin").mkdir(parents=True)
        shutil.copy(REPO_ROOT / "pyproject.toml", root / "pyproject.toml")
        text = INSTALL_SH.read_text(encoding="utf-8")
        for prefix in ("/opt/", "/usr/local/", "/home/linuxbrew/", "/Library/"):
            text = text.replace('"' + prefix, '"%s%s' % (fake, prefix))
            text = text.replace(" " + prefix, " %s%s" % (fake, prefix))
        dest = root / "quoin" / "install.sh"
        dest.write_text(text, encoding="utf-8")
        return fake, dest

    @pytest.mark.parametrize("rel", [
        "home/miniconda3/envs/work/bin/python3",
        "home/opt/anaconda3/bin/python3",
        "home/opt/miniconda3/bin/python3",
        "home/.linuxbrew/bin/python3.12",
        "fakeroot/opt/miniconda3/bin/python3",
        "fakeroot/opt/anaconda3/envs/ml/bin/python3",
        "fakeroot/opt/homebrew/Caskroom/miniconda/base/bin/python3",
        "fakeroot/opt/homebrew/Caskroom/miniconda/base/envs/x/bin/python3",
        "fakeroot/home/linuxbrew/.linuxbrew/bin/python3.12",
    ])
    def test_default_off_path_locations(self, tmp_path, rel):
        _, script = self._sandboxed_installer(tmp_path)
        onpath = tmp_path / "onpath"
        _fake_python(onpath / "python3", 3008)
        want = _fake_python(tmp_path / rel, 3012)
        r = _run(tmp_path, [onpath], search_dirs=None, install_sh=script)
        assert r.returncode == 0, r.stderr
        assert r.stdout.strip() == str(want)

    def test_hung_interpreter_times_out_and_search_continues(self, tmp_path):
        d1, d2 = tmp_path / "d1", tmp_path / "d2"
        d1.mkdir()
        hang = d1 / "python3"
        hang.write_text("#!/bin/sh\nexec sleep 30\n")
        hang.chmod(0o755)
        want = _fake_python(d2 / "python3", 3012)
        import time
        t0 = time.monotonic()
        r = _run(tmp_path, [d1, d2], extra_env={"QUOIN_PROBE_TIMEOUT": "1"})
        assert r.returncode == 0, r.stderr
        assert r.stdout.strip() == str(want)
        assert time.monotonic() - t0 < 20

    def test_absent_candidates_not_listed_in_failure(self, tmp_path):
        d = tmp_path / "bin"
        a = _fake_python(d / "python3", 3009)
        r = _run(tmp_path, [d])
        assert r.returncode == 1
        assert str(a) in r.stderr
        assert str(d / "python:") not in r.stderr
        assert "not an executable file" not in r.stderr

    def test_script_syntax_clean_under_system_bash(self):
        r = subprocess.run([BASH, "-n", str(INSTALL_SH)], capture_output=True, text=True)
        assert r.returncode == 0, r.stderr


def _dispatcher_stub(tmp_path: Path) -> Path:
    """Interpreter stub that models the probes install.sh makes.

    State lives in files under $STUB_DIR: `installed` (quoin importable
    unaided), `installed_no_cli` (importable but `-m quoin --version` fails),
    `in_venv`, and `pip_rc` (exit status for `-m pip`; a successful pip marks
    quoin as installed).
    """
    stub = tmp_path / "stub" / "python3"
    stub.parent.mkdir(exist_ok=True)
    stub.write_text(
        """#!/bin/bash
if [[ "$1" == "-c" ]]; then
  case "$2" in
    *base_prefix*) [[ -f "$STUB_DIR/in_venv" ]] && exit 0 || exit 1 ;;
    *sys.version_info*) echo 3012; exit 0 ;;
    *__about__*) echo 9.9.9; exit 0 ;;
    *"import quoin"*)
      if [[ -f "$STUB_DIR/installed" || -d "$PWD/quoin" || "${PYTHONPATH:-}" == */src ]]; then exit 0; fi
      exit 1 ;;
  esac
  exit 0
fi
if [[ "$1" == "-m" && "$2" == "quoin" && "$3" == "--version" ]]; then
  if [[ -f "$STUB_DIR/installed" && ! -f "$STUB_DIR/installed_no_cli" ]]; then echo "quoin 0.0.1"; exit 0; fi
  exit 1
fi
if [[ "$1" == "-m" && "$2" == "pip" ]]; then
  echo "$*" >> "$STUB_DIR/pip.log"
  rc=0; [[ -f "$STUB_DIR/pip_rc" ]] && rc="$(<"$STUB_DIR/pip_rc")"
  [[ "$rc" == 0 ]] && : > "$STUB_DIR/installed"
  exit "$rc"
fi
echo "ARGV:$*"
echo "PYTHONPATH=${PYTHONPATH:-}"
exit 0
"""
    )
    stub.chmod(0o755)
    return stub


def _tier_run(tmp_path, state=(), pip_rc=0, args=("--scope", "user"), cwd=None):
    stub = _dispatcher_stub(tmp_path)
    state_dir = tmp_path / "state"
    state_dir.mkdir(exist_ok=True)
    for name in state:
        (state_dir / name).write_text("")
    (state_dir / "pip_rc").write_text(str(pip_rc))
    r = _run(tmp_path, [], extra_env={"QUOIN_PYTHON": str(stub),
                                      "STUB_DIR": str(state_dir)},
             args=list(args), cwd=cwd)
    log = state_dir / "pip.log"
    return r, (log.read_text() if log.exists() else "")


class TestTierThree:
    def test_pip_failure_falls_back_to_source_tree(self, tmp_path):
        r, log = _tier_run(tmp_path, state=("installed",), pip_rc=1)
        assert r.returncode == 0, r.stderr
        assert "ARGV:-m quoin install" in r.stdout
        assert re.search(r"PYTHONPATH=.*/src\n", r.stdout)
        assert "0.0.1" in r.stderr and "--use-pip" in r.stderr
        assert "-m pip" in log

    def test_explicit_use_pip_failure_is_fatal(self, tmp_path):
        r, _ = _tier_run(tmp_path, state=("installed",), pip_rc=1,
                         args=("--scope", "user", "--use-pip"))
        assert r.returncode == 1
        assert "pip install failed" in r.stderr
        assert "ARGV:" not in r.stdout

    def test_venv_drops_user_flag_and_plain_keeps_it(self, tmp_path):
        (tmp_path / "a").mkdir()
        (tmp_path / "b").mkdir()
        _, log_venv = _tier_run(tmp_path / "a", state=("installed", "in_venv"), pip_rc=1)
        assert "-m pip install" in log_venv and "--user" not in log_venv
        _, log_plain = _tier_run(tmp_path / "b", state=("installed",), pip_rc=1)
        assert "--user" in log_plain

    def test_pip_success_runs_install_without_fallback_warning(self, tmp_path):
        r, log = _tier_run(tmp_path, state=("installed",), pip_rc=0)
        assert r.returncode == 0, r.stderr
        assert "ARGV:-m quoin install" in r.stdout
        assert "could not reinstall" not in r.stderr
        assert "-m pip" in log

    def test_namespace_shadowing_does_not_fake_an_install(self, tmp_path):
        cwd = tmp_path / "work"
        (cwd / "quoin").mkdir(parents=True)
        r, log = _tier_run(tmp_path, state=(), pip_rc=0, cwd=cwd)
        assert r.returncode == 0, r.stderr
        assert re.search(r"PYTHONPATH=.*/src\n", r.stdout)
        assert "ARGV:-m pip" not in r.stdout
        assert log == "", "tier 2 must be chosen, not pip"

    def test_empty_installed_version_is_worded_as_not_installed(self, tmp_path):
        r, _ = _tier_run(tmp_path, state=("installed", "installed_no_cli"), pip_rc=1)
        assert r.returncode == 0, r.stderr
        assert "no 'quoin' command is installed" in r.stderr
        assert "version  " not in r.stderr
        assert "ARGV:-m quoin install" in r.stdout

    def test_real_interpreter_namespace_package_is_not_an_install(self, tmp_path):
        probe = "import quoin; assert quoin.__file__ is not None"
        env = {**os.environ, "PYTHONNOUSERSITE": "1"}
        env.pop("PYTHONPATH", None)
        neutral = subprocess.run([sys.executable, "-c", probe], cwd="/",
                                 capture_output=True, env=env)
        if neutral.returncode == 0:
            pytest.skip("quoin is importable from / for this interpreter")
        cwd = tmp_path / "work"
        (cwd / "quoin").mkdir(parents=True)
        shadowed = subprocess.run([sys.executable, "-c", "import quoin"],
                                  cwd=str(cwd), capture_output=True, env=env)
        assert shadowed.returncode == 0  # a cwd-based probe would be fooled
        assert subprocess.run([sys.executable, "-c", probe], cwd="/",
                              capture_output=True, env=env).returncode != 0
