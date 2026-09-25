"""Name rules and manifest drift checks for the OpenCode adapter.

Expected id sets used by these tests are always local to the test function,
or read from the manifest at test time. This keeps the module free of a
module-level ALL-CAPS collection of skill ids, which the registration
roster census would otherwise pick up and require registering.
"""
from __future__ import annotations

from quoin.opencode_adapter import names


def test_normalize_swaps_underscore_for_hyphen():
    assert names.normalize("end_of_task") == "quoin-end-of-task"
    assert names.normalize("revise-fast") == "quoin-revise-fast"


def test_name_error_rejects_invalid_names():
    assert names.name_error("quoin-a--b") is not None
    assert names.name_error("quoin-Bad") is not None
    assert names.name_error("quoin-x-") is not None
    too_long = "quoin-" + "a" * 60  # 66 chars total
    assert names.name_error(too_long) is not None


def test_name_error_accepts_boundary_length():
    exactly_64 = "quoin-" + "a" * 58
    assert len(exactly_64) == 64
    assert names.name_error(exactly_64) is None


def test_find_collisions_names_both_sources():
    messages = names.find_collisions(
        "command",
        [("quoin-end-of-task", "end_of_task"), ("quoin-end-of-task", "end-of-task")],
    )
    assert len(messages) == 1
    assert "end_of_task" in messages[0]
    assert "end-of-task" in messages[0]


def test_find_collisions_three_or_more_sources_comma_separated():
    messages = names.find_collisions(
        "skill",
        [
            ("quoin-x", "a"),
            ("quoin-x", "b"),
            ("quoin-x", "c"),
        ],
    )
    assert len(messages) == 1
    assert "'a'" in messages[0]
    assert "'b'" in messages[0]
    assert "'c'" in messages[0]


def test_same_name_different_namespaces_is_not_a_collision():
    command_messages = names.find_collisions("command", [("quoin-architect", "architect")])
    agent_messages = names.find_collisions("agent", [("quoin-architect", "architect")])
    assert command_messages == []
    assert agent_messages == []


def test_check_unique_raises_naming_both_sources():
    try:
        names.check_unique(
            "agent",
            [("quoin-gate", "gate"), ("quoin-gate", "gate-role")],
        )
    except names.NameCollisionError as exc:
        assert "gate" in str(exc)
        assert "gate-role" in str(exc)
    else:
        raise AssertionError("expected NameCollisionError")
