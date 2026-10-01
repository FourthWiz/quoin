"""Every refusal and doctor finding maps to one of the seven categories."""
from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from quoin.opencode_adapter import categories, doctor, driver, errors

REPO_ROOT = Path(__file__).resolve().parents[3]
SOURCE_DIR = REPO_ROOT / "quoin"
ADAPTER = REPO_ROOT / "src" / "quoin" / "opencode_adapter"


def test_categories_match_the_driver_refusal_categories():
    assert categories.CATEGORIES == driver.REFUSAL_CATEGORIES
    assert set(categories.LABELS) == set(categories.CATEGORIES)
    assert all(label and label == label.lower() for label in categories.LABELS.values())


def test_every_finding_id_has_a_category():
    assert set(doctor.MESSAGES) <= set(categories.FINDING_CATEGORIES)
    assert set(categories.FINDING_CATEGORIES.values()) <= set(categories.CATEGORIES)
    for finding_id in doctor.MESSAGES:
        assert categories.category_for(finding_id) in categories.CATEGORIES


def test_unmapped_finding_id_raises():
    with pytest.raises(KeyError):
        categories.category_for("not-a-finding")


def test_every_config_class_has_a_category():
    for cls in set(errors.MESSAGE_CLASS.values()) | set(errors.REJECTION_CLASSES):
        assert categories.config_class_category(cls) in categories.CATEGORIES
    assert set(categories.CONFIG_CLASS_CATEGORIES) == set(errors.REJECTION_CLASSES)


def test_policy_denial_is_exactly_the_drivers_policy_rejection_classes():
    policy = {
        cls for cls in errors.REJECTION_CLASSES
        if categories.config_class_category(cls) == "policy-denial"
    }
    assert policy == driver._POLICY_CLASSES & errors.REJECTION_CLASSES
    assert len(policy) == 4
    # a resolution reason, not a rejection class
    assert "provider-excluded" not in errors.REJECTION_CLASSES


def _literal_category_arguments():
    found = []
    for name in ("driver.py", "launch_env.py"):
        tree = ast.parse((ADAPTER / name).read_text())
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            called = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
            if called in ("refuse", "_refuse", "PrepareRefused", "LaunchRefused") and node.args:
                first = node.args[0]
                if isinstance(first, ast.Constant) and isinstance(first.value, str):
                    found.append((name, node.lineno, first.value))
    return found


def test_every_literal_refusal_category_is_known():
    found = _literal_category_arguments()
    assert len(found) > 30
    for name, line, value in found:
        assert value in categories.CATEGORIES, (name, line, value)


def test_module_imports_nothing_from_the_adapter_at_module_level():
    tree = ast.parse((ADAPTER / "categories.py").read_text())
    for node in tree.body:
        if isinstance(node, ast.ImportFrom):
            assert node.level == 0 and node.module in ("__future__", "typing")
        if isinstance(node, ast.Import):
            assert all(not a.name.startswith("quoin") for a in node.names)


def test_smoke_report_json_carries_a_valid_category():
    findings = doctor.run_smoke(SOURCE_DIR)
    payload = json.loads(doctor.render_json(findings, doctor.report_status(findings)))
    assert payload["findings"]
    assert {f["category"] for f in payload["findings"]} <= set(categories.CATEGORIES)


def test_every_message_id_renders_with_its_category():
    findings = [doctor.Finding(id=i, severity="info", message="x") for i in sorted(doctor.MESSAGES)]
    payload = json.loads(doctor.render_json(findings, "healthy"))
    assert [f["category"] for f in payload["findings"]] == [
        categories.category_for(f["id"]) for f in payload["findings"]
    ]
