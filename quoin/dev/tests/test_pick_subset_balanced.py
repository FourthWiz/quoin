"""Offline tests for the tiered-balanced suite, the tier registry and the refresh modes."""
from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

from quoin.benchmarks.scripts import pick_subset

BENCH = Path(pick_subset.__file__).resolve().parent.parent
SCRIPT = BENCH / "scripts" / "pick_subset.py"
SUITE = BENCH / "suite-tiered-balanced.json"
SUITE_SMALL = BENCH / "suite-tiered-small.json"
SNAPSHOT = BENCH / "data" / "swebench-verified-snapshot.json"
PROBLEMS = BENCH / "data" / "swebench-verified-problems.json"
PROBLEMS_BAL = BENCH / "data" / "swebench-verified-problems-tiered-balanced.json"
PROVENANCE = BENCH / "suite-tiered-balanced-provenance.md"
REPO_ROOT = BENCH.parent.parent
REV = "c104f840cc67f8b6eec6f759ebc8b2693d585d4a"

# The tracker preview list, as <org>__<name>-<number> instance ids.
PREVIEW = {
    "easy": [
        "django__django-10999", "django__django-11119", "django__django-11951",
        "django__django-13023", "pylint-dev__pylint-4970", "pytest-dev__pytest-10081",
        "pytest-dev__pytest-7205", "sphinx-doc__sphinx-7889", "sphinx-doc__sphinx-9711",
        "sympy__sympy-12481",
    ],
    "medium": [
        "django__django-12155", "django__django-13516", "django__django-14559",
        "django__django-15252", "django__django-15916", "django__django-16950",
        "matplotlib__matplotlib-25960", "psf__requests-2931", "pydata__xarray-3151",
        "pydata__xarray-7229", "pytest-dev__pytest-5840", "scikit-learn__scikit-learn-25931",
        "sphinx-doc__sphinx-7985", "sympy__sympy-13551", "sympy__sympy-15599",
    ],
    "large": [
        "astropy__astropy-14369", "django__django-11400", "django__django-11885",
        "django__django-12325", "django__django-13344", "django__django-13449",
        "django__django-15128", "django__django-15957", "django__django-16263",
        "pylint-dev__pylint-4551", "pylint-dev__pylint-8898", "pytest-dev__pytest-10356",
        "pytest-dev__pytest-6197", "sphinx-doc__sphinx-8548", "sphinx-doc__sphinx-9461",
    ],
}
LABELS = {"easy": "<15 min fix", "medium": "15 min - 1 hour", "large": "1-4 hours"}


def _snapshot_rows():
    return json.loads(SNAPSHOT.read_text(encoding="utf-8"))["rows"]


# --- registry and selection ---------------------------------------------------------

def test_selection_equals_the_preview_list():
    chosen = pick_subset.select_tiered(_snapshot_rows(), 1729, pick_subset.TIERED_BALANCED_SPEC)
    assert {t: sorted(ids) for t, ids in chosen.items()} == {t: sorted(v) for t, v in PREVIEW.items()}


def test_every_tiered_small_instance_is_in_the_same_balanced_tier():
    rows = _snapshot_rows()
    small = pick_subset.select_tiered(rows, 1729)
    balanced = pick_subset.select_tiered(rows, 1729, pick_subset.TIERED_BALANCED_SPEC)
    for tier, ids in small.items():
        assert set(ids) <= set(balanced[tier])


def test_registry_tiered_small_entry_is_the_module_constants():
    entry = pick_subset.TIERED_SUITES["tiered-small"]
    assert entry["tiers"] is pick_subset.TIERED_SMALL_SPEC
    assert entry["cost"] is pick_subset.TIERED_SMALL_COST
    assert pick_subset.TIERED_SUITES["tiered-balanced"]["tiers"] is pick_subset.TIERED_BALANCED_SPEC


def test_balanced_token_estimates_are_reused_per_tier():
    for tier in ("easy", "medium", "large"):
        for key in ("est_tokens_in", "est_tokens_out"):
            assert (
                pick_subset.TIERED_BALANCED_SPEC[tier][key]
                == pick_subset.TIERED_SMALL_SPEC[tier][key]
            )


def test_tiered_small_verify_still_passes():
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), "--suite", "tiered-small", "--seed", "1729",
         "--verify", str(SUITE_SMALL)],
        capture_output=True, text=True,
    )
    assert proc.returncode == 0, proc.stderr


# --- the committed suite ------------------------------------------------------------

def test_committed_balanced_suite_regenerates_byte_equal():
    suite = pick_subset.build_tiered_suite(SNAPSHOT, PROBLEMS_BAL, 1729, "tiered-balanced")
    assert pick_subset.render_suite(suite) == SUITE.read_text(encoding="utf-8")


def test_balanced_suite_shape():
    doc = json.loads(SUITE.read_text(encoding="utf-8"))
    tasks = doc["tasks"]
    assert len(tasks) == 40
    for tier, n in (("easy", 10), ("medium", 15), ("large", 15)):
        of_tier = [t for t in tasks if t["tier"] == tier]
        assert len(of_tier) == n
        assert all(t["difficulty"] == LABELS[tier] for t in of_tier)
    for t in tasks:
        assert t["description"].strip()
        assert re.fullmatch(r"[0-9a-f]{40}", t["base_commit"])
    assert doc["tiered"]["sub_seeds"] == {"easy": 101, "medium": 102, "large": 103}
    assert doc["seed"] == 1729 and doc["tiered"]["seed"] == 1729
    assert doc["provenance_file"] == "quoin/benchmarks/suite-tiered-balanced-provenance.md"


def test_shared_statements_are_byte_equal_across_problems_files():
    small = json.loads(PROBLEMS.read_text(encoding="utf-8"))["problems"]
    balanced = json.loads(PROBLEMS_BAL.read_text(encoding="utf-8"))["problems"]
    assert len(small) == 9 and len(balanced) == 40
    for iid, text in small.items():
        assert balanced[iid] == text


def test_problems_sha256_recorded_in_the_suite():
    doc = json.loads(SUITE.read_text(encoding="utf-8"))["tiered"]
    assert doc["problems_sha256"] == hashlib.sha256(PROBLEMS_BAL.read_bytes()).hexdigest()
    assert doc["problems_file"] == "quoin/benchmarks/data/" + PROBLEMS_BAL.name
    assert doc["pairwise_review_cap_usd"] == 2 and doc["pairwise_review_total_cap_usd"] == 80
    small = json.loads(SUITE_SMALL.read_text(encoding="utf-8"))["tiered"]
    assert "problems_sha256" not in small and "pairwise_review_cap_usd" not in small


def test_new_suite_file_is_marked_binary_for_line_endings():
    out = subprocess.run(
        ["git", "check-attr", "text", "quoin/benchmarks/suite-tiered-balanced.json"],
        capture_output=True, text=True, cwd=REPO_ROOT,
    ).stdout
    assert out.strip().endswith("text: unset")


# --- provenance ---------------------------------------------------------------------

def _money(pattern, text):
    return int(re.search(pattern, text).group(1).replace(",", ""))


def test_provenance_figures_equal_the_suite_and_the_constants():
    prov = PROVENANCE.read_text(encoding="utf-8")
    cost = pick_subset.TIERED_BALANCED_COST
    review = pick_subset.TIERED_BALANCED_REVIEW
    tiered = json.loads(SUITE.read_text(encoding="utf-8"))["tiered"]
    figures = {
        "cap": _money(r"Per-session cap \| \$([\d,]+)", prov),
        "target": _money(r"Documented target \| \$([\d,]+)", prov),
        "ceiling": _money(r"Enforced ceiling \| \$([\d,]+)", prov),
        "review": _money(r"Pairwise review cap per review \| \$([\d,]+)", prov),
        "review_total": _money(r"Pairwise review total cap \| \$([\d,]+)", prov),
        "wall": _money(r"\*\*([\d,]+) s\*\*", prov),
    }
    expected = {
        "cap": cost["documented_cap_usd"], "target": cost["worst_case_target_usd"],
        "ceiling": cost["worst_case_ceiling_usd"],
        "review": review["pairwise_review_cap_usd"],
        "review_total": review["pairwise_review_total_cap_usd"],
        "wall": pick_subset.TIERED_BALANCED_WALL_CLOCK_SECONDS,
    }
    assert figures == expected
    assert figures["cap"] == tiered["documented_cap_usd"]
    assert figures["target"] == tiered["worst_case_target_usd"]
    assert figures["ceiling"] == tiered["worst_case_ceiling_usd"]
    assert figures["review"] == tiered["pairwise_review_cap_usd"]
    assert figures["review_total"] == tiered["pairwise_review_total_cap_usd"]
    assert figures["target"] == figures["cap"] * 40 * 2
    pilot = _money(r"Pilot \(easy tier\) worst case \| \$([\d,]+)", prov)
    assert pilot == figures["cap"] * 10 * 2


def test_provenance_lists_tasks_mapping_hashes_and_prepare_command():
    prov = PROVENANCE.read_text(encoding="utf-8")
    doc = json.loads(SUITE.read_text(encoding="utf-8"))
    for t in doc["tasks"]:
        assert f"| {t['id']} | {t['tier']} | {t['source_id']} | {t['repo']} |" in prov
    small = json.loads(SUITE_SMALL.read_text(encoding="utf-8"))["tasks"]
    by_source = {t["source_id"]: t["id"] for t in doc["tasks"]}
    for t in small:
        assert f"| {by_source[t['source_id']]} | {t['id']} | {t['source_id']} |" in prov
    assert hashlib.sha256(SNAPSHOT.read_bytes()).hexdigest() in prov
    assert hashlib.sha256(PROBLEMS_BAL.read_bytes()).hexdigest() in prov
    assert "--suite tiered-balanced" in prov and "--prepare-repos" in prov


# --- refresh modes (fixture rows, no pyarrow, no network) ------------------------------

def _fixture_rows():
    rows = []
    for tier, label in LABELS.items():
        for i in range(20):
            rows.append({
                "instance_id": f"org__proj-{tier[0]}{i:03d}".replace(f"-{tier[0]}", "-1"),
                "repo": "org/proj", "base_commit": f"{i:040x}", "difficulty": label,
                "problem_statement": f"statement {tier} {i}",
            })
    for n, r in enumerate(rows):
        r["instance_id"] = f"org__proj-{n + 1}"
    return rows


@pytest.fixture
def refresh_env(tmp_path, monkeypatch):
    rows = _fixture_rows()
    raw = b"fake parquet bytes"
    monkeypatch.setattr(pick_subset, "_read_parquet_rows", lambda path: (rows, raw))
    parquet = tmp_path / "x.parquet"
    parquet.write_bytes(raw)
    return {
        "parquet": parquet, "tmp": tmp_path, "raw": raw,
        "snapshot": tmp_path / "snap.json",
        "small": tmp_path / "small.json", "balanced": tmp_path / "balanced.json",
    }


def _paths(env):
    return {"tiered-small": env["small"], "tiered-balanced": env["balanced"]}


def _full_refresh(env):
    pick_subset.refresh_verified_snapshot(
        env["parquet"], REV, "2026-10-26", 1729, env["snapshot"], _paths(env)
    )


def test_full_refresh_writes_the_snapshot_and_both_problems_files(refresh_env, capsys):
    _full_refresh(refresh_env)
    snap = json.loads(refresh_env["snapshot"].read_text())
    assert snap["row_count"] == 60
    assert len(json.loads(refresh_env["small"].read_text())["problems"]) == 9
    assert len(json.loads(refresh_env["balanced"].read_text())["problems"]) == 40


def _problems_only(env, **over):
    argv = [
        "--refresh-verified-snapshot", "--problems-only", "--suite", "tiered-balanced",
        "--parquet-file", str(env["parquet"]), "--revision", over.get("revision", REV),
        "--snapshot", str(env["snapshot"]),
    ]
    argv += over.get("extra", [])
    return argv


def test_problems_only_with_matching_sha_writes_one_file(refresh_env, monkeypatch):
    _full_refresh(refresh_env)
    snap_bytes = refresh_env["snapshot"].read_bytes()
    refresh_env["balanced"].unlink()
    refresh_env["small"].unlink()
    monkeypatch.setitem(
        pick_subset.TIERED_SUITES["tiered-balanced"], "problems_path", refresh_env["balanced"]
    )
    pick_subset.main(_problems_only(refresh_env))
    assert refresh_env["balanced"].exists() and not refresh_env["small"].exists()
    assert refresh_env["snapshot"].read_bytes() == snap_bytes


def test_problems_only_with_a_different_parquet_sha_is_refused(refresh_env, monkeypatch):
    _full_refresh(refresh_env)
    refresh_env["balanced"].unlink()
    monkeypatch.setattr(pick_subset, "_read_parquet_rows", lambda p: (_fixture_rows(), b"other"))
    with pytest.raises(SystemExit) as exc:
        pick_subset.main(_problems_only(refresh_env, extra=["--problems", str(refresh_env["balanced"])]))
    assert exc.value.code == 2
    assert not refresh_env["balanced"].exists()


def test_problems_only_with_a_different_revision_is_refused(refresh_env):
    _full_refresh(refresh_env)
    refresh_env["balanced"].unlink()
    with pytest.raises(SystemExit) as exc:
        pick_subset.main(
            _problems_only(refresh_env, revision="0" * 40,
                           extra=["--problems", str(refresh_env["balanced"])])
        )
    assert exc.value.code == 2
    assert not refresh_env["balanced"].exists()


def test_suite_without_problems_only_is_refused(refresh_env, capsys):
    with pytest.raises(SystemExit) as exc:
        pick_subset.main([
            "--refresh-verified-snapshot", "--suite", "tiered-balanced",
            "--parquet-file", str(refresh_env["parquet"]), "--revision", REV,
            "--snapshot", str(refresh_env["snapshot"]),
        ])
    assert exc.value.code == 2
    assert "--problems-only" in capsys.readouterr().err
    assert not refresh_env["snapshot"].exists()


def test_problems_only_problems_flag_overrides_the_output_path(refresh_env):
    _full_refresh(refresh_env)
    out = refresh_env["tmp"] / "override.json"
    refresh_env["balanced"].unlink()
    pick_subset.main(_problems_only(refresh_env, extra=["--problems", str(out)]))
    assert out.exists() and not refresh_env["balanced"].exists()


def test_full_refresh_rejects_a_problems_override(refresh_env):
    with pytest.raises(SystemExit) as exc:
        pick_subset.main([
            "--refresh-verified-snapshot", "--parquet-file", str(refresh_env["parquet"]),
            "--revision", REV, "--snapshot", str(refresh_env["snapshot"]),
            "--problems", str(refresh_env["tmp"] / "p.json"),
        ])
    assert exc.value.code == 2
