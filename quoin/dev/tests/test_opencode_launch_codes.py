"""The launch refusal code registry covers every refusal the adapter raises.

Refusal constructors are identified by definition and import, never by a bare
name pattern: the two refusal classes, the two factory functions that build
them and the local aliases of the driver's factory. Inside the two launch
modules every refusal call with a literal code must be registered under the
category the call site states, and every call with a computed code must be in
a short table of known dynamic sites. Outside the launch modules no refusal may
be constructed at all, and the few same-named helpers that exist for other
purposes are listed with a reason.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

import _opencode_helpers as helpers
from quoin.opencode_adapter import categories, errors, launch_codes

SRC_DIR = helpers.SOURCE_DIR.parent / "src" / "quoin"
ADAPTER_DIR = SRC_DIR / "opencode_adapter"
LAUNCH_MODULES = ("driver.py", "launch_env.py")
REFUSAL_CLASSES = {"PrepareRefused", "LaunchRefused"}
HELPER_NAMES = {"refuse", "_refuse", "_refusal"}
# (module, dotted scope) of each function that returns a refusal construction.
REFUSAL_FACTORIES = {("driver.py", "OpenCodeDriver._refusal"), ("launch_env.py", "_refuse")}

# Refusal calls whose code is computed. A new unlisted site fails the scan.
DYNAMIC_SITES = {
    ("driver.py", "OpenCodeDriver._refusal"): "the factory itself forwards its parameters",
    ("driver.py", "OpenCodeDriver.prepare"): "re-wraps a launch refusal from the environment builder",
    ("driver.py", "OpenCodeDriver.prepare_interactive"): "re-wraps a launch refusal from the environment builder",
    ("driver.py", "OpenCodeDriver._evaluate_launchable"): "configuration error class, or config-invalid",
    ("driver.py", "OpenCodeDriver._compile_pair"): "compiled- plus a compiler.check reason",
    ("launch_env.py", "_refuse"): "the factory itself forwards its parameters",
}

# Same-named helpers outside the launch modules that are not launch refusals.
NON_LAUNCH_HELPERS = {
    ("gate.py", "_parse_verdict", "refuse"): "returns a (None, reason, line) tuple for artifact verdict parsing",
    ("probe_cli.py", "<module>", "_refuse"): "writes a probe message to stderr and returns exit status 2; probe codes are closed by PROBE_MESSAGES",
    ("cli.py", "_cmd_opencode_test_run", "refuse"): "prints a REFUSED JSON line for the test-run command and returns 2",
}

MIN_LITERAL_CODES = 41
CODE_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")


def _modules():
    for path in sorted(ADAPTER_DIR.glob("*.py")):
        yield path.name, path
    yield "cli.py", SRC_DIR / "cli.py"


def _parse(path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    parents = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[child] = node
    return tree, parents


def _scope(node, parents):
    names = []
    while node in parents:
        node = parents[node]
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.append(node.name)
    return ".".join(reversed(names)) or "<module>"


def _leaf_scope(node, parents):
    while node in parents:
        node = parents[node]
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return node.name
    return "<module>"


def _callee_name(call):
    func = call.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _aliases(tree):
    """Names bound to the driver's factory, e.g. ``refuse = self._refusal``."""
    names = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and isinstance(node.value, ast.Attribute)
            and node.value.attr == "_refusal"
        ):
            names.add(node.targets[0].id)
    return names


def _is_refusal_call(call, aliases):
    func = call.func
    if isinstance(func, ast.Name):
        return func.id in REFUSAL_CLASSES or func.id == "_refuse" or func.id in aliases
    if isinstance(func, ast.Attribute):
        if func.attr in REFUSAL_CLASSES:
            return True
        if func.attr == "_refusal" and isinstance(func.value, ast.Name) and func.value.id == "self":
            return True
        return func.attr == "_refuse" and isinstance(func.value, ast.Name) and func.value.id == "launch_env"
    return False


def _constructs_refusal(func_node):
    return any(
        isinstance(n, ast.Call) and _callee_name(n) in REFUSAL_CLASSES for n in ast.walk(func_node)
    )


def scan_launch_modules():
    """(literal code -> {categories}, call-site count, dynamic sites seen)."""
    literals = {}
    sites = 0
    dynamic = set()
    for name in LAUNCH_MODULES:
        tree, parents = _parse(ADAPTER_DIR / name)
        aliases = _aliases(tree)
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and _is_refusal_call(node, aliases)):
                continue
            scope = _scope(node, parents)
            if len(node.args) >= 2 and isinstance(node.args[1], ast.Constant) and isinstance(node.args[1].value, str):
                category_arg = node.args[0]
                assert isinstance(category_arg, ast.Constant), (name, node.lineno)
                literals.setdefault(node.args[1].value, set()).add(category_arg.value)
                sites += 1
            else:
                dynamic.add((name, scope))
    return literals, sites, dynamic


# ------------------------------------------------------------------ the scan


def test_factories_still_return_a_refusal_construction():
    for module, scope in REFUSAL_FACTORIES:
        tree, parents = _parse(ADAPTER_DIR / module)
        found = [
            n for n in ast.walk(tree)
            if isinstance(n, ast.FunctionDef)
            and (_scope(n, parents) + "." + n.name).lstrip("<module>.") == scope.lstrip("<module>.")
        ]
        assert len(found) == 1, (module, scope)
        returns = [r for r in ast.walk(found[0]) if isinstance(r, ast.Return) and r.value is not None]
        assert returns and all(
            isinstance(r.value, ast.Call) and _callee_name(r.value) in REFUSAL_CLASSES for r in returns
        ), (module, scope)


def test_every_literal_launch_code_is_registered_with_its_category():
    literals, _sites, _dynamic = scan_launch_modules()
    for code, seen in sorted(literals.items()):
        assert launch_codes.is_registered(code), code
        assert seen == {launch_codes.category(code)}, (code, seen)


def test_every_dynamic_refusal_site_is_listed():
    _literals, _sites, dynamic = scan_launch_modules()
    assert dynamic == set(DYNAMIC_SITES), dynamic ^ set(DYNAMIC_SITES)


def test_the_scan_is_not_vacuous():
    literals, sites, _dynamic = scan_launch_modules()
    assert len(literals) >= MIN_LITERAL_CODES
    assert sites >= len(literals)
    assert {"opencode-binary-absent", "plugin-directory-present", "compile-blocked"} <= set(literals)
    assert set(literals) == set(launch_codes.EXISTING_CODES)


# ---------------------------------------------------- nothing escapes


def test_no_module_outside_the_launch_modules_raises_a_launch_refusal():
    helper_defs = set()
    for name, path in _modules():
        if name in LAUNCH_MODULES:
            continue
        tree, parents = _parse(path)
        local_helpers = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in HELPER_NAMES:
                key = (name, _leaf_scope(node, parents), node.name)
                assert key in NON_LAUNCH_HELPERS, "unclassified refusal-named helper: %s" % (key,)
                local_helpers.add(node.name)
                helper_defs.add(key)
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                callee = _callee_name(node)
                assert callee not in REFUSAL_CLASSES, (name, node.lineno, "constructs a refusal class")
                if callee in HELPER_NAMES:
                    assert callee in local_helpers, (name, node.lineno, "calls an unclassified %s" % callee)
                    assert isinstance(node.func, ast.Name), (name, node.lineno)
            elif isinstance(node, ast.ImportFrom):
                imported = {alias.name for alias in node.names}
                assert not (imported & (REFUSAL_CLASSES | HELPER_NAMES)), (
                    name, node.lineno, imported & (REFUSAL_CLASSES | HELPER_NAMES)
                )
    assert helper_defs == set(NON_LAUNCH_HELPERS)


def test_classified_helpers_still_exist_and_are_not_refusals():
    for (module, scope, name), reason in NON_LAUNCH_HELPERS.items():
        assert reason
        path = SRC_DIR / module if module == "cli.py" else ADAPTER_DIR / module
        tree, parents = _parse(path)
        found = [
            n for n in ast.walk(tree)
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
            and n.name == name and _leaf_scope(n, parents) == scope
        ]
        assert len(found) == 1, (module, scope, name)
        assert not _constructs_refusal(found[0]), (module, scope, name)


# ------------------------------------------------ compiled-reason coverage


def _collect_strings(node):
    return {n.value for n in ast.walk(node) if isinstance(n, ast.Constant) and isinstance(n.value, str)}


def compiled_reasons():
    tree, _parents = _parse(ADAPTER_DIR / "compiler.py")
    [check] = [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "check"]
    reasons = set()
    for node in ast.walk(check):
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Attribute) and func.attr == "append" and isinstance(func.value, ast.Name) \
                    and func.value.id == "reasons" and node.args:
                reasons |= _collect_strings(node.args[0])
            if _callee_name(node) == "CheckResult" and len(node.args) >= 2:
                second = node.args[1]
                if isinstance(second, (ast.Tuple, ast.List)) or (
                    isinstance(second, ast.Call) and _callee_name(second) == "tuple"
                ):
                    reasons |= _collect_strings(second)
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            for left, right in ((node.left, node.right), (node.right, node.left)):
                if isinstance(left, ast.Name) and left.id == "reasons" and isinstance(right, (ast.List, ast.Tuple)):
                    reasons |= _collect_strings(right)
    return reasons


def test_every_compiled_reason_is_repaired_or_registered():
    reasons = compiled_reasons()
    assert {"not-private", "flag-mismatch", "stale", "missing"} <= reasons
    for reason in reasons - {"missing", "stale"}:
        assert launch_codes.is_registered("compiled-" + reason), reason
    assert set(launch_codes.COMPILED_REASON_CODES) == {"compiled-" + r for r in reasons - {"missing", "stale"}}


# ---------------------------------------------------------- the registry


def test_config_derived_codes_cover_every_rejection_class():
    assert set(launch_codes.CONFIG_DERIVED_CODES) == errors.REJECTION_CLASSES | {"config-invalid"}
    for code, category in launch_codes.CONFIG_DERIVED_CODES.items():
        if code != "config-invalid":
            assert category == categories.config_class_category(code)


def test_every_code_and_category_is_well_formed():
    for code, category in launch_codes.REGISTRY.items():
        assert CODE_RE.match(code), code
        assert category in categories.CATEGORIES, (code, category)
    assert launch_codes.category("opencode-binary-absent") == "missing-binary"
    with pytest.raises(KeyError):
        launch_codes.category("no-such-code")
    assert not launch_codes.is_registered("no-such-code")


def test_registry_is_read_only_and_groups_do_not_conflict():
    with pytest.raises(TypeError):
        launch_codes.REGISTRY["x"] = "policy-denial"  # type: ignore[index]
    groups = (
        launch_codes.EXISTING_CODES,
        launch_codes.CONFIG_DERIVED_CODES,
        launch_codes.COMPILED_REASON_CODES,
        launch_codes.PLANNED_CODES,
    )
    for first in groups:
        for second in groups:
            for code in set(first) & set(second):
                assert first[code] == second[code], code


def test_planned_codes_are_the_declared_set():
    assert len(launch_codes.PLANNED_CODES) == 39
    assert launch_codes.category("allowed-hosts-unset") == "policy-denial"
    assert launch_codes.category("profile-dir-unsafe") == "invalid-configuration"
    assert launch_codes.category("purge-run-active") == "workflow-validation"


# ----------------------------------------------------------- module hygiene


def test_module_hygiene():
    src = (ADAPTER_DIR / "launch_codes.py").read_text(encoding="utf-8")
    ast.parse(src, feature_version=(3, 10))
    tree = ast.parse(src)
    first = tree.body[1] if isinstance(tree.body[0], ast.Expr) else tree.body[0]
    assert isinstance(first, ast.ImportFrom) and first.module == "__future__"
    adapter_imports = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.level == 1:
            adapter_imports |= {node.module} if node.module else {a.name for a in node.names}
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == "os":
            assert node.attr not in ("environ", "getenv")
    assert adapter_imports == {"categories", "errors"}
