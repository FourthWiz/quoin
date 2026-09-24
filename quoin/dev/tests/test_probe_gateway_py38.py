from __future__ import annotations

import ast
import shutil
import subprocess
import sys

import pytest

import _opencode_helpers as helpers

MODULES = ["fake_openai_server.py", "probe_gateway.py"]

_SUBSCRIPT_BUILTINS = {"list", "dict", "set", "tuple", "frozenset", "type"}
_DENYLIST_ATTRS = {"removeprefix", "removesuffix"}
_DENYLIST_MODULES = {"zoneinfo", "tomllib", "graphlib"}
_DENYLIST_CALLS = {"cache"}  # functools.cache(


def _read(name):
    return (helpers.OPENCODE_DIR / name).read_text(encoding="utf-8")


@pytest.mark.parametrize("name", MODULES)
def test_ast_parses_under_feature_version_38(name):
    src = _read(name)
    ast.parse(src, feature_version=(3, 8))


@pytest.mark.parametrize("name", MODULES)
def test_future_annotations_present(name):
    src = _read(name)
    tree = ast.parse(src)
    found = False
    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and node.module == "__future__":
            if any(alias.name == "annotations" for alias in node.names):
                found = True
                break
    assert found, "%s is missing 'from __future__ import annotations'" % (name,)


class _RuntimeModernSyntaxVisitor(ast.NodeVisitor):
    def __init__(self):
        self.violations = []

    def _flag(self, node, why):
        self.violations.append("line %d: %s" % (getattr(node, "lineno", -1), why))

    def visit_Subscript(self, node):
        # allow subscripts inside annotation contexts; a runtime Subscript as a
        # bare expression/assignment value is what we actually want to flag.
        if isinstance(node.value, ast.Name) and node.value.id in _SUBSCRIPT_BUILTINS:
            parent_is_annotation = getattr(node, "_quoin_in_annotation", False)
            if not parent_is_annotation:
                self._flag(node, "runtime subscripted builtin %r" % (node.value.id,))
        self.generic_visit(node)

    def visit_BinOp(self, node):
        if isinstance(node.op, ast.BitOr):
            self._flag(node, "X | Y outside annotation position")
        self.generic_visit(node)

    def visit_Attribute(self, node):
        if node.attr in _DENYLIST_ATTRS:
            self._flag(node, "denylisted attribute %r" % (node.attr,))
        self.generic_visit(node)

    def visit_Call(self, node):
        if isinstance(node.func, ast.Attribute) and node.func.attr in _DENYLIST_CALLS:
            self._flag(node, "denylisted call %r(" % (node.func.attr,))
        self.generic_visit(node)

    def visit_Import(self, node):
        for alias in node.names:
            if alias.name.split(".")[0] in _DENYLIST_MODULES:
                self._flag(node, "denylisted module %r" % (alias.name,))
        self.generic_visit(node)

    def visit_ImportFrom(self, node):
        if node.module and node.module.split(".")[0] in _DENYLIST_MODULES:
            self._flag(node, "denylisted module %r" % (node.module,))
        self.generic_visit(node)

    def visit_FunctionDef(self, node):
        self._mark_annotations(node)
        self.generic_visit(node)

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_AnnAssign(self, node):
        _mark_subtree(node.annotation)
        self.generic_visit(node)

    def _mark_annotations(self, node):
        for target in (node.returns,):
            _mark_subtree(target)
        for arg in list(node.args.args) + list(node.args.kwonlyargs) + list(node.args.posonlyargs):
            _mark_subtree(arg.annotation)


def _mark_subtree(node):
    if node is None:
        return
    for child in ast.walk(node):
        child._quoin_in_annotation = True


@pytest.mark.parametrize("name", MODULES)
def test_no_runtime_modern_syntax(name):
    src = _read(name)
    tree = ast.parse(src)
    visitor = _RuntimeModernSyntaxVisitor()
    visitor.visit(tree)
    assert not visitor.violations, "\n".join(visitor.violations)


@pytest.mark.parametrize("name", MODULES)
def test_mutation_flags_runtime_list_bracket(name, tmp_path):
    src = _read(name)
    mutated = src + "\n\n_QUOIN_TEST_SCRATCH: list = []\n_QUOIN_TEST_SCRATCH2 = list[str]()\n"
    tree = ast.parse(mutated)
    visitor = _RuntimeModernSyntaxVisitor()
    visitor.visit(tree)
    assert any("runtime subscripted builtin" in v for v in visitor.violations)


def _find_real_python38():
    candidates = []
    which38 = shutil.which("python3.8")
    if which38:
        candidates.append(which38)
    candidates.append("/usr/bin/python3")
    which3 = shutil.which("python3")
    if which3:
        candidates.append(which3)
    for candidate in candidates:
        try:
            result = subprocess.run(
                [candidate, "-c", "import sys; print(sys.version_info[0], sys.version_info[1])"],
                capture_output=True, text=True, timeout=10,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        if result.returncode == 0 and result.stdout.strip() == "3 8":
            return candidate
    return None


@pytest.fixture(scope="module")
def real_python38():
    interp = _find_real_python38()
    if interp is None:
        pytest.skip("no real Python 3.8 interpreter found (checked python3.8, /usr/bin/python3, python3)")
    return interp


def test_real_38_imports_both_modules(real_python38):
    for name in MODULES:
        path = helpers.OPENCODE_DIR / name
        mod_name = "quoin_py38_check_" + name.replace(".py", "")
        program = (
            "import importlib.util, sys\n"
            "spec = importlib.util.spec_from_file_location(%r, %r)\n"
            "mod = importlib.util.module_from_spec(spec)\n"
            "sys.modules[%r] = mod\n"
            "spec.loader.exec_module(mod)\n"
            "print('ok')\n"
        ) % (mod_name, str(path), mod_name)
        result = subprocess.run(
            [real_python38, "-B", "-c", program],
            capture_output=True, text=True, timeout=15,
            env={"PYTHONDONTWRITEBYTECODE": "1", "PATH": "/usr/bin:/bin"},
        )
        assert result.returncode == 0, result.stderr
        assert "ok" in result.stdout


def test_real_38_retry_after_paths(real_python38):
    path = helpers.OPENCODE_DIR / "probe_gateway.py"
    program = (
        "import importlib.util, sys, datetime\n"
        "spec = importlib.util.spec_from_file_location('quoin_py38_probe', %r)\n"
        "mod = importlib.util.module_from_spec(spec)\n"
        "sys.modules['quoin_py38_probe'] = mod\n"
        "spec.loader.exec_module(mod)\n"
        "now = datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc)\n"
        "assert mod.parse_retry_after('garbage', now) is None\n"
        "future = (now + datetime.timedelta(seconds=5)).strftime('%%a, %%d %%b %%Y %%H:%%M:%%S GMT')\n"
        "assert mod.parse_retry_after(future, now) == 5\n"
        "print('ok')\n"
    ) % (str(path),)
    result = subprocess.run(
        [real_python38, "-B", "-c", program],
        capture_output=True, text=True, timeout=15,
        env={"PYTHONDONTWRITEBYTECODE": "1", "PATH": "/usr/bin:/bin"},
    )
    assert result.returncode == 0, result.stderr
    assert "ok" in result.stdout


def test_real_38_help_exits_0(real_python38):
    result = subprocess.run(
        [real_python38, "-B", str(helpers.OPENCODE_DIR / "probe_gateway.py"), "--help"],
        capture_output=True, text=True, timeout=15,
        env={"PYTHONDONTWRITEBYTECODE": "1", "PATH": "/usr/bin:/bin"},
    )
    assert result.returncode == 0


def test_real_38_full_default_ok_run(real_python38, tmp_path):
    fake_server = helpers.load_module(helpers.OPENCODE_DIR / "fake_openai_server.py", "quoin_opencode_fake_server_py38test")
    with fake_server.FakeProviderServer() as server:
        out = tmp_path / "record.json"
        env = {"QUOIN_PROBE_TEST_KEY": "sk-test-py38-key", "PYTHONDONTWRITEBYTECODE": "1", "PATH": "/usr/bin:/bin"}
        result = subprocess.run(
            [
                real_python38, "-B", str(helpers.OPENCODE_DIR / "probe_gateway.py"),
                "--base-url", server.base_url, "--model", "default_ok",
                "--credential-env", "QUOIN_PROBE_TEST_KEY", "--provider", "fake",
                "--output", str(out), "--timeout", "5",
            ],
            capture_output=True, text=True, timeout=30, env=env,
        )
        assert result.returncode == 0, result.stderr
        import json

        record = json.loads(out.read_text())
        assert record["verdict"]["status"] == "qualified"
