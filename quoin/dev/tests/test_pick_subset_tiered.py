"""Offline tests for the tiered suite generator and the vendored SWE-bench Verified data.

The autouse fixture blocks in-process network access. It does not reach
subprocess tests, which rely on the generator containing no network code at
all (checked statically below).
"""
from __future__ import annotations

import ast
import hashlib
import json
import re
import socket
import subprocess
import sys
from pathlib import Path

import pytest

from quoin.benchmarks.scripts import pick_subset

BENCH = Path(pick_subset.__file__).resolve().parent.parent
SCRIPT = BENCH / "scripts" / "pick_subset.py"
SUITE = BENCH / "suite-tiered-small.json"
SNAPSHOT = BENCH / "data" / "swebench-verified-snapshot.json"
PROBLEMS = BENCH / "data" / "swebench-verified-problems.json"
PROVENANCE = BENCH / "suite-tiered-small-provenance.md"
SUITE_V1 = BENCH / "suite-v1.json"

V1_GENERATOR_SHA256 = "76d439330e6d9a067292893614f8b5bcec91c0d018584dec8e2944bf2ee0031b"
SUITE_V1_FILE_SHA256 = "73da0276b4ba0eef12f208176246f5f74d518e324e5c51a95f628446aaf5312b"
TIER_LABELS = {"easy": "<15 min fix", "medium": "15 min - 1 hour", "large": "1-4 hours"}
REV = "c104f840cc67f8b6eec6f759ebc8b2693d585d4a"


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("network access attempted")

    monkeypatch.setattr(socket.socket, "connect", boom)
    monkeypatch.setattr(socket, "create_connection", boom)
    monkeypatch.setattr(socket, "getaddrinfo", boom)


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _suite() -> dict:
    return json.loads(SUITE.read_text(encoding="utf-8"))


def _synthetic(tmp_path, counts=None, revision=REV):
    """Write a small snapshot and a matching problems file; return (snap, problems, rows)."""
    counts = counts or {"<15 min fix": 8, "15 min - 1 hour": 6, "1-4 hours": 4, ">4 hours": 1}
    rows = []
    for li, (label, n) in enumerate(counts.items()):
        for i in range(n):
            rows.append(
                {
                    "instance_id": f"org__repo-{li}-{i:02d}",
                    "repo": "org/repo",
                    "base_commit": f"{i:040x}",
                    "difficulty": label,
                }
            )
    rows.sort(key=lambda r: r["instance_id"])
    snap = pick_subset.snapshot_from_rows(
        rows, revision=revision, source_file="f.parquet", source_sha256="0" * 64,
        source_size=1, fetched_on="2026-01-01",
    )
    chosen = pick_subset.select_tiered(snap["rows"], 1729)
    selected = [i for ids in chosen.values() for i in ids]
    full = [dict(r, problem_statement=f"text for {r['instance_id']}") for r in rows]
    problems = pick_subset.problems_from_rows(full, selected, revision=revision)
    sp, pp = tmp_path / "snap.json", tmp_path / "problems.json"
    sp.write_text(pick_subset.render_suite(snap), encoding="utf-8")
    pp.write_text(pick_subset.render_suite(problems), encoding="utf-8")
    return sp, pp, full


# --- committed suite --------------------------------------------------------

def test_regenerate_equals_committed_bytes():
    built = pick_subset.render_suite(
        pick_subset.build_tiered_suite(SNAPSHOT, PROBLEMS, 1729)
    )
    assert built == SUITE.read_bytes().decode("utf-8")


def test_verify_happy_path_and_tamper(tmp_path):
    with pytest.raises(SystemExit) as ok:
        pick_subset.main(["--suite", "tiered-small", "--seed", "1729", "--verify", str(SUITE)])
    assert ok.value.code == 0

    text = SUITE.read_text(encoding="utf-8")
    i = text.index('"description"', text.index('"tasks"')) + len('"description": "')
    tampered = tmp_path / "tampered.json"
    tampered.write_text(text[:i] + ("X" if text[i] != "X" else "Y") + text[i + 1:], encoding="utf-8")
    with pytest.raises(SystemExit) as bad:
        pick_subset.main(["--suite", "tiered-small", "--seed", "1729", "--verify", str(tampered)])
    assert bad.value.code == 1


def test_other_seed_does_not_match_vendored_problems():
    with pytest.raises(SystemExit) as exc:
        pick_subset.main(["--suite", "tiered-small", "--seed", "1730", "--verify", str(SUITE)])
    assert exc.value.code == 2


def test_cli_verify_subprocess():
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), "--suite", "tiered-small", "--seed", "1729", "--verify", str(SUITE)],
        capture_output=True, text=True,
    )
    assert proc.returncode == 0
    assert "VERIFY OK" in proc.stdout


def test_suite_shape():
    suite = _suite()
    tasks = suite["tasks"]
    assert len(tasks) == 9
    assert [t["tier"] for t in tasks].count("easy") == 4
    assert [t["tier"] for t in tasks].count("medium") == 3
    assert [t["tier"] for t in tasks].count("large") == 2
    assert list(suite) == [
        "schema_version", "description", "generated_by", "seed", "provenance_file", "tiered", "tasks",
    ]
    order = [
        "id", "tier", "source", "source_id", "repo", "base_commit", "difficulty",
        "est_tokens_in", "est_tokens_out", "wall_clock_seconds", "description",
    ]
    for t in tasks:
        assert list(t) == order
        assert t["difficulty"] == TIER_LABELS[t["tier"]]
        assert t["difficulty"] != ">4 hours"
        assert re.match(r"^verified_(easy|medium|large)_\d{3}$", t["id"])
        assert not t["id"].startswith("swebench")
        assert t["source"] == "swebench_verified"
        block = suite["tiered"]["tiers"][t["tier"]]
        for k in ("est_tokens_in", "est_tokens_out", "wall_clock_seconds"):
            assert t[k] == block[k]
        if t["tier"] in ("medium", "large"):
            assert t["wall_clock_seconds"] > 600
    assert suite["tiered"]["sub_seeds"] == {"easy": 101, "medium": 102, "large": 103}
    cost = pick_subset.TIERED_SMALL_COST
    assert suite["tiered"]["worst_case_ceiling_usd"] == cost["worst_case_ceiling_usd"] == 1790
    assert suite["tiered"]["worst_case_target_usd"] == cost["worst_case_target_usd"] == 1620
    assert suite["tiered"]["documented_cap_usd"] == cost["documented_cap_usd"] == 90
    # the cap, worst case and ceiling follow from the documented rule
    assert cost["worst_case_target_usd"] == cost["documented_cap_usd"] * 9 * 2
    assert cost["worst_case_ceiling_usd"] >= cost["worst_case_target_usd"] * 1.1
    for tier, spec in pick_subset.TIERED_SMALL_SPEC.items():
        assert suite["tiered"]["tiers"][tier]["wall_clock_seconds"] == spec["wall_clock_seconds"] == 19200


def test_suite_matches_snapshot_and_problems():
    suite = _suite()
    snap = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    problems = json.loads(PROBLEMS.read_text(encoding="utf-8"))
    by_id = {r["instance_id"]: r for r in snap["rows"]}
    for t in suite["tasks"]:
        row = by_id[t["source_id"]]
        assert (t["repo"], t["base_commit"], t["difficulty"]) == (
            row["repo"], row["base_commit"], row["difficulty"],
        )
        assert t["description"] == problems["problems"][t["source_id"]]
    assert suite["tiered"]["snapshot_sha256"] == _sha(SNAPSHOT.read_bytes())
    assert suite["tiered"]["dataset_revision"] == snap["revision"] == problems["revision"]


def test_vendored_snapshot_is_self_consistent():
    snap = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    rows = snap["rows"]
    assert snap["row_count"] == len(rows) == 500
    recount = {}
    for r in rows:
        recount[r["difficulty"]] = recount.get(r["difficulty"], 0) + 1
    assert snap["difficulty_counts"] == dict(sorted(recount.items()))
    ids = [r["instance_id"] for r in rows]
    assert ids == sorted(set(ids))
    assert set(recount) <= set(pick_subset.KNOWN_VERIFIED_DIFFICULTIES)
    for r in rows:
        assert list(r) == pick_subset.SNAPSHOT_FIELDS
        assert re.match(r"^[0-9a-f]{40}$", r["base_commit"])
    problems = json.loads(PROBLEMS.read_text(encoding="utf-8"))
    assert set(problems["problems"]) == {t["source_id"] for t in _suite()["tasks"]}
    assert snap["revision"] == REV


# --- selection and loaders on synthetic data --------------------------------

def test_selection_is_deterministic_and_offset_sensitive(tmp_path):
    sp, _, _ = _synthetic(tmp_path)
    rows = json.loads(sp.read_text())["rows"]
    a = pick_subset.select_tiered(rows, 1729)
    assert a == pick_subset.select_tiered(rows, 1729)
    assert [len(a[t]) for t in ("easy", "medium", "large")] == [4, 3, 2]
    saved = dict(pick_subset.TIERED_SMALL_SPEC["medium"])
    try:
        pick_subset.TIERED_SMALL_SPEC["medium"]["sub_seed_offset"] = 104
        assert pick_subset.select_tiered(rows, 1729)["medium"] != a["medium"]
    finally:
        pick_subset.TIERED_SMALL_SPEC["medium"].update(saved)


def test_pool_smaller_than_n_raises(tmp_path):
    rows = [
        {"instance_id": "a-1", "repo": "o/r", "base_commit": "0" * 40, "difficulty": "1-4 hours"},
    ]
    with pytest.raises(pick_subset.TieredSuiteError):
        pick_subset.select_tiered(rows, 1729)


def test_synthetic_build_ok_and_sidecar_errors(tmp_path):
    sp, pp, _ = _synthetic(tmp_path)
    suite = pick_subset.build_tiered_suite(sp, pp, 1729)
    assert len(suite["tasks"]) == 9

    data = json.loads(pp.read_text())
    victim = sorted(data["problems"])[0]

    missing = dict(data, problems={k: v for k, v in data["problems"].items() if k != victim})
    mp = tmp_path / "missing.json"
    mp.write_text(json.dumps(missing))
    with pytest.raises(pick_subset.TieredSuiteError, match="missing"):
        pick_subset.build_tiered_suite(sp, mp, 1729)

    extra = dict(data, problems=dict(data["problems"], zzz__extra_1="x"))
    ep = tmp_path / "extra.json"
    ep.write_text(json.dumps(extra))
    with pytest.raises(pick_subset.TieredSuiteError, match="extra"):
        pick_subset.build_tiered_suite(sp, ep, 1729)

    rev = dict(data, revision="f" * 40)
    rp = tmp_path / "rev.json"
    rp.write_text(json.dumps(rev))
    with pytest.raises(pick_subset.TieredSuiteError, match="revision"):
        pick_subset.build_tiered_suite(sp, rp, 1729)


def test_snapshot_validation_errors(tmp_path):
    sp, _, _ = _synthetic(tmp_path)
    snap = json.loads(sp.read_text())

    bad_label = json.loads(json.dumps(snap))
    bad_label["rows"][0]["difficulty"] = "2 weeks"
    p = tmp_path / "label.json"
    p.write_text(json.dumps(bad_label))
    with pytest.raises(pick_subset.TieredSuiteError, match="unknown difficulty"):
        pick_subset.load_verified_snapshot(p)

    bad_count = dict(snap, row_count=snap["row_count"] + 1)
    p2 = tmp_path / "count.json"
    p2.write_text(json.dumps(bad_count))
    with pytest.raises(pick_subset.TieredSuiteError, match="row_count"):
        pick_subset.load_verified_snapshot(p2)


def test_tiered_flags_conflict_with_counts():
    with pytest.raises(SystemExit) as exc:
        pick_subset.main(["--suite", "tiered-small", "--n-humaneval", "5"])
    assert exc.value.code == 2


# --- refresh transforms ------------------------------------------------------

def _fake_rows():
    return [
        {
            "instance_id": iid, "repo": "o/r", "base_commit": "a" * 40, "difficulty": label,
            "problem_statement": f"p {iid}", "patch": "SECRET", "hints_text": "HINT",
        }
        for iid, label in [("b-2", "1-4 hours"), ("a-1", "<15 min fix"), ("c-3", ">4 hours")]
    ]


def test_snapshot_from_rows_shape_and_dropped_fields():
    snap = pick_subset.snapshot_from_rows(
        _fake_rows(), revision=REV, source_file="f", source_sha256="1" * 64,
        source_size=10, fetched_on="2026-01-01",
    )
    assert list(snap) == [
        "dataset", "revision", "split", "source_file", "source_file_sha256", "source_file_size",
        "fetched_on", "generated_by", "fields", "row_count", "difficulty_counts", "rows",
    ]
    assert [r["instance_id"] for r in snap["rows"]] == ["a-1", "b-2", "c-3"]
    assert all(list(r) == pick_subset.SNAPSHOT_FIELDS for r in snap["rows"])
    assert "SECRET" not in json.dumps(snap) and "HINT" not in json.dumps(snap)
    assert snap["difficulty_counts"] == {">4 hours": 1, "1-4 hours": 1, "<15 min fix": 1}


def test_snapshot_from_rows_rejects_duplicates_and_unknown_labels():
    kw = dict(revision=REV, source_file="f", source_sha256="1", source_size=1, fetched_on="d")
    rows = _fake_rows()
    with pytest.raises(pick_subset.TieredSuiteError, match="duplicate"):
        pick_subset.snapshot_from_rows(rows + [dict(rows[0])], **kw)
    with pytest.raises(pick_subset.TieredSuiteError, match="unknown difficulty"):
        pick_subset.snapshot_from_rows([dict(rows[0], difficulty="soon")], **kw)


def test_problems_from_rows_only_selected_and_nonempty():
    out = pick_subset.problems_from_rows(_fake_rows(), ["b-2", "a-1"], revision=REV)
    assert list(out["problems"]) == ["a-1", "b-2"]
    assert "hints_text" not in json.dumps(out["problems"])
    with pytest.raises(pick_subset.TieredSuiteError, match="problem_statement"):
        pick_subset.problems_from_rows([dict(_fake_rows()[0], problem_statement="")], ["b-2"], revision=REV)


@pytest.mark.parametrize("rev", ["main", "abc1234"])
def test_refresh_rejects_unpinned_revision_before_reading(rev, tmp_path):
    with pytest.raises(SystemExit) as exc:
        pick_subset.main([
            "--refresh-verified-snapshot", "--parquet-file", str(tmp_path / "nope.parquet"),
            "--revision", rev,
        ])
    assert exc.value.code == 2


# --- static checks -----------------------------------------------------------

def test_source_parses_as_python_38_and_has_no_network_imports():
    source = SCRIPT.read_text(encoding="utf-8")
    tree = ast.parse(source, feature_version=(3, 8))
    banned = {"urllib", "http", "socket", "requests"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            assert not banned & {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            assert (node.module or "").split(".")[0] not in banned


# --- v1 unchanged ------------------------------------------------------------

def test_v1_output_unchanged(capsys):
    text = pick_subset.render_suite(pick_subset.build_suite(100, 20, 1729))
    assert _sha(text.encode("utf-8")) == V1_GENERATOR_SHA256
    pick_subset.main(["--seed", "1729"])
    assert capsys.readouterr().out == text
    assert _sha(SUITE_V1.read_bytes()) == SUITE_V1_FILE_SHA256


# --- provenance --------------------------------------------------------------

def test_provenance_mentions_recorded_values():
    prov = PROVENANCE.read_text(encoding="utf-8")
    suite = _suite()
    cost = pick_subset.TIERED_SMALL_COST
    wall = pick_subset.TIERED_SMALL_WALL_CLOCK_SECONDS
    needles = [
        REV, "1729", "101", "102", "103", _sha(SNAPSHOT.read_bytes()),
        "$%d" % cost["documented_cap_usd"],
        "$%s" % format(cost["worst_case_target_usd"], ","),
        "$%s" % format(cost["worst_case_ceiling_usd"], ","),
        "%s s" % format(wall, ","),
        "0.9 of the\ncutoff",
        "direct-fix sessions",
    ]
    for t in suite["tasks"]:
        needles += [t["id"], t["source_id"]]
    for n in needles:
        assert n in prov, n


# --- snapshot and problems validation ---------------------------------------

def _rewrite_snapshot(tmp_path, mutate):
    sp, pp, _ = _synthetic(tmp_path)
    snap = json.loads(sp.read_text(encoding="utf-8"))
    mutate(snap["rows"])
    sp.write_text(json.dumps(snap), encoding="utf-8")
    return sp, pp


def _run_main(sp, pp, capsys):
    with pytest.raises(SystemExit) as exc:
        pick_subset.main([
            "--suite", "tiered-small", "--seed", "1729",
            "--snapshot", str(sp), "--problems", str(pp),
            "--output", str(sp.parent / "out.json"),
        ])
    err = capsys.readouterr().err
    return exc.value.code, err


@pytest.mark.parametrize(
    "mutate",
    [
        lambda rows: rows.__setitem__(0, "not a dict"),
        lambda rows: rows[0].pop("repo"),
        lambda rows: rows[0].__setitem__("instance_id", None),
        lambda rows: rows[0].__setitem__("extra", "x"),
        lambda rows: rows[0].__setitem__("repo", "../evil"),
        lambda rows: rows[0].__setitem__("repo", "a/b/c"),
        lambda rows: rows[0].__setitem__("instance_id", "bad id;rm"),
    ],
)
def test_malformed_snapshot_rows_exit_two(tmp_path, capsys, mutate):
    sp, pp = _rewrite_snapshot(tmp_path, mutate)
    code, err = _run_main(sp, pp, capsys)
    assert code == 2
    assert "ERROR:" in err
    assert "Traceback" not in err


@pytest.mark.parametrize("bad", ["", 42])
def test_problems_values_must_be_nonempty_strings(tmp_path, capsys, bad):
    sp, pp, _ = _synthetic(tmp_path)
    data = json.loads(pp.read_text(encoding="utf-8"))
    first = sorted(data["problems"])[0]
    data["problems"][first] = bad
    pp.write_text(json.dumps(data), encoding="utf-8")
    code, err = _run_main(sp, pp, capsys)
    assert code == 2
    assert "ERROR:" in err
    assert "Traceback" not in err


def test_json_data_files_are_byte_pinned_by_gitattributes():
    repo_root = BENCH.parent.parent
    out = subprocess.run(
        ["git", "check-attr", "text", "--", "quoin/benchmarks/data/swebench-verified-snapshot.json"],
        cwd=repo_root, capture_output=True, text=True,
    )
    assert out.returncode == 0
    assert out.stdout.strip().endswith("unset")


def test_provenance_figures_equal_the_generator_constants():
    prov = PROVENANCE.read_text(encoding="utf-8")
    cost = pick_subset.TIERED_SMALL_COST
    figures = {
        "cap": int(re.search(r"Per-session cap \| \$([\d,]+)", prov).group(1).replace(",", "")),
        "target": int(re.search(r"Documented target \| \$([\d,]+)", prov).group(1).replace(",", "")),
        "ceiling": int(re.search(r"Enforced ceiling \| \$([\d,]+)", prov).group(1).replace(",", "")),
        "wall": int(re.search(r"\*\*([\d,]+) s\*\*", prov).group(1).replace(",", "")),
    }
    assert figures == {
        "cap": cost["documented_cap_usd"], "target": cost["worst_case_target_usd"],
        "ceiling": cost["worst_case_ceiling_usd"],
        "wall": pick_subset.TIERED_SMALL_WALL_CLOCK_SECONDS,
    }
    pilot = int(re.search(r"Pilot \(easy tier\) worst case \| \$([\d,]+)", prov).group(1).replace(",", ""))
    assert pilot == cost["documented_cap_usd"] * pick_subset.TIERED_SMALL_SPEC["easy"]["n"] * 2
