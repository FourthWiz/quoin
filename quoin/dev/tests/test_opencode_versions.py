"""The OpenCode version policy and the single source of the qualified list."""
from __future__ import annotations

import importlib.util
import json
import re
import shutil
import types
from pathlib import Path

import pytest

from quoin.opencode_adapter import driver, manifest, paths, plugin_artifact, versions

REPO_ROOT = Path(__file__).resolve().parents[3]
SOURCE_DIR = REPO_ROOT / "quoin"
QUALIFIED = ["1.18.32", "1.20.0"]


@pytest.mark.parametrize(
    "version, status, personal, work, schema",
    [
        ("1.18.32", versions.QUALIFIED, True, True, "1.18.32"),
        ("1.20.0", versions.QUALIFIED, True, True, "1.20.0"),
        ("1.21.4", versions.NEWER, True, False, "1.20.0"),
        ("1.19.0", versions.UNQUALIFIED, True, False, "1.18.32"),
        ("1.18.31", versions.BELOW_FLOOR, False, False, None),
        (None, versions.UNKNOWN, False, False, None),
        ("1.21.0-beta.1", versions.UNKNOWN, False, False, None),
    ],
)
def test_judge(version, status, personal, work, schema):
    verdict = versions.judge(version, QUALIFIED)
    assert verdict.status == status
    assert verdict.launches("personal") is personal
    assert verdict.launches("work") is work
    assert verdict.schema_version == schema
    assert verdict.floor == "1.18.32" and verdict.highest == "1.20.0"
    assert verdict.unqualified_runtime is (status in (versions.NEWER, versions.UNQUALIFIED))
    assert (verdict.warning() is not None) is verdict.unqualified_runtime


def test_an_unknown_classification_is_held_to_the_work_rule():
    assert not versions.judge("1.21.4", QUALIFIED).launches("corporate")


def test_versions_compare_numerically_and_the_list_is_validated():
    assert versions.judge("1.100.0", ["1.9.0"]).status == versions.NEWER
    assert versions.judge("1.9.0", ["1.100.0", "1.9.0"]).floor == "1.9.0"
    for bad in ([], ["1.18"], ["1.18.32", "1.18.32"]):
        with pytest.raises(versions.QualifiedVersionsError):
            versions.judge("1.18.32", bad)


def test_refusal_messages_are_actionable():
    work = versions.refusal_message(versions.judge("1.21.4", QUALIFIED), "work")
    assert "1.21.4" in work and "1.18.32, 1.20.0" in work and "qualify" in work
    old = versions.refusal_message(versions.judge("1.0.0", QUALIFIED), "every")
    assert "floor 1.18.32" in old and "upgrade" in old


# ------------------------------------------------- single source of truth


def test_every_place_naming_an_opencode_release_agrees_with_the_list():
    """`qualified-versions.json` is the only list; each other place that names
    an OpenCode release must name a qualified one (or be derived from it)."""
    from quoin.benchmarks.harness import opencode_cells

    qualified = manifest.read_qualified_versions(SOURCE_DIR)
    assert manifest.check_source_dir(SOURCE_DIR) == []  # manifest, compatibility.md, schema subsets
    assert manifest.read_pinned_version(SOURCE_DIR) == qualified[-1]
    assert list(opencode_cells.QUALIFIED_OPENCODE_VERSIONS) == qualified
    assert opencode_cells.PINNED_OPENCODE_VERSION == qualified[-1]
    assert plugin_artifact.PLUGIN_OPENCODE_VERSION in qualified
    package = json.loads((SOURCE_DIR / "dev" / "opencode-plugin" / "package.json").read_text(encoding="utf-8"))
    assert package["quoin"]["opencode"] == plugin_artifact.PLUGIN_OPENCODE_VERSION
    assert package["devDependencies"]["@opencode-ai/plugin"] == plugin_artifact.PLUGIN_OPENCODE_VERSION
    schema = json.loads(paths.native_schema_path().read_text(encoding="utf-8"))
    assert schema["$id"] == "urn:quoin:opencode:native-config:%s" % qualified[-1]


def test_no_source_file_hard_codes_a_qualified_release():
    """Python sources read the list instead of repeating a release, except the
    plugin's tested release, which is checked above."""
    qualified = manifest.read_qualified_versions(SOURCE_DIR)
    pattern = re.compile(r"""["'](%s)["']""" % "|".join(re.escape(v) for v in qualified))
    allowed = {REPO_ROOT / "src" / "quoin" / "opencode_adapter" / "plugin_artifact.py"}
    roots = [REPO_ROOT / "src" / "quoin" / "opencode_adapter", SOURCE_DIR / "benchmarks" / "harness"]
    offenders = [
        str(path.relative_to(REPO_ROOT))
        for root in roots for path in sorted(root.rglob("*.py"))
        if path not in allowed and pattern.search(path.read_text(encoding="utf-8"))
    ]
    assert offenders == []


# ------------------------------------------- unqualified release in between


def test_a_release_between_qualified_ones_is_checked_against_the_lower_subset():
    floor = manifest.read_qualified_versions(SOURCE_DIR)[0]
    verdict = versions.judge(floor + "1", [floor, "999.0.0"])  # e.g. 1.18.321
    assert verdict.status == versions.UNQUALIFIED and verdict.schema_version == floor
    host = types.SimpleNamespace(_refusal=lambda cat, code, msg, **kw: driver.PrepareRefused(cat, code, msg))
    compiled = json.loads((SOURCE_DIR / "adapters" / "opencode" / "fixtures" / "compiled" / "work.opencode.json")
                          .read_text(encoding="utf-8"))
    driver.OpenCodeDriver._check_schema_subset(host, verdict, compiled)
    with pytest.raises(driver.PrepareRefused) as info:
        driver.OpenCodeDriver._check_schema_subset(host, verdict, {"unknown_top_level_key": 1})
    assert info.value.code == "opencode-version-unqualified" and floor in info.value.message


# ------------------------------------------------- the qualification helper


def _qualify_module():
    path = SOURCE_DIR / "dev" / "scripts" / "qualify_opencode.py"
    spec = importlib.util.spec_from_file_location("qualify_opencode", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_qualify_helper_adds_a_release_everywhere_the_drift_checks_look(tmp_path):
    qualify = _qualify_module()
    source = tmp_path / "quoin"
    adapter = source / "adapters" / "opencode"
    real = SOURCE_DIR / "adapters" / "opencode"
    adapter.mkdir(parents=True)
    for name in ("qualified-versions.json", "compatibility.md", "release-acceptance.md"):
        shutil.copy(real / name, adapter / name)
    shutil.copytree(real / "schemas", adapter / "schemas")
    before = manifest.read_qualified_versions(source)

    assert qualify.add(source, "999.0.0") == 0
    after = manifest.read_qualified_versions(source)
    assert after == before + ["999.0.0"]
    assert manifest.qualified_drift(source, after) == []
    schema = json.loads((adapter / "schemas" / paths.native_schema_file("999.0.0")).read_text(encoding="utf-8"))
    assert schema["$id"].endswith(":999.0.0")
    assert "| OpenCode | %s |" % ", ".join(after) in (adapter / "release-acceptance.md").read_text(encoding="utf-8")

    assert qualify.add(source, "999.0.0") == 3
    assert qualify.add(source, "0.0.1") == 2
    assert qualify.add(source, "1.2") == 2
    assert qualify.add(source, "1.2.3\n") == 2
