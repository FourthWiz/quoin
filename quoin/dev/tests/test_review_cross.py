"""Cross-repository review support: the stage plan from the interface graph,
branch-wide impact, the review pack (which carries untouched consumers), sibling
PR linking, and the review-cross skill's registration."""

import json
import os
import re
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

PKG = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PKG / "core" / "scripts"))
import impact  # noqa: E402
import link_prs  # noqa: E402
import review_cross_pack as pack  # noqa: E402

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "interface_project"
REPOS = ("shared-schema", "order-service", "web-client")


def _git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", *args],
                          check=True, capture_output=True, text=True).stdout


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "proj"
    shutil.copytree(FIXTURE, root)
    for name in REPOS:
        _git(root / name, "init", "-q", "-b", "main")
        _git(root / name, "add", ".")
        _git(root / name, "commit", "-q", "-m", "init")
    (root / ".workflow_artifacts").mkdir()
    return root


def _rename_route_on_branch(project, branch="feat/by-id"):
    service = project / "order-service"
    _git(service, "checkout", "-q", "-b", branch)
    routes = service / "app" / "routes.py"
    routes.write_text(routes.read_text().replace('"/orders/{order_id}"', '"/orders/by-id/{order_id}"'))
    _git(service, "commit", "-qam", "move the order route")
    return branch


def test_plan_puts_providers_before_their_consumers(project):
    stages = impact.plan_stages(project, ["shared-schema"])
    assert [s["repo"] for s in stages] == ["shared-schema", "order-service", "web-client"]
    assert stages[0]["reason"] == "named in the task"
    assert [s["repo"] for s in impact.plan_stages(project, ["order-service"])] == ["order-service", "web-client"]
    assert [s["repo"] for s in impact.plan_stages(project, ["web-client"])] == ["web-client"]
    assert impact.plan_stages(project, ["no-such-repo"]) == []


def test_branch_impact_uses_each_repository_default_branch(project):
    branch = _rename_route_on_branch(project)
    result = impact.impact(project, None, branch=branch)
    assert result["changed"] == {"order-service": ["app/routes.py"]}
    assert result["check"] == ["web-client"]
    (project / "order-service" / "app" / "extra.py").write_text("X = 1\n")
    assert impact.impact(project, None, branch=branch)["changed"]["order-service"] == ["app/extra.py", "app/routes.py"]


def test_pack_carries_the_untouched_consumer_with_line_numbers(project):
    branch = _rename_route_on_branch(project)
    text = pack.build(project, branch)
    assert "Repositories to check: web-client" in text
    assert "#### web-client/src/api.ts" in text
    assert re.search(r"\n\s+4  .*fetch\(`\$\{BASE\}/orders/\$\{id\}`\)", text)
    assert '+@app.get("/orders/by-id/{order_id}")' in text


def test_pack_without_changes_says_so(project):
    _git(project / "order-service", "checkout", "-q", "-b", "feat/empty")
    text = pack.build(project, "feat/empty")
    assert "No repository has changes" in text


@pytest.fixture
def fake_gh(tmp_path, monkeypatch):
    bodies = tmp_path / "bodies"
    bodies.mkdir()
    gh = tmp_path / "bin" / "gh"
    gh.parent.mkdir()
    logic = tmp_path / "fake_gh.py"
    logic.write_text(f"""import sys, pathlib, re
store = pathlib.Path({str(bodies)!r})
args = sys.argv[1:]
url = args[2]
key = re.sub(r"\\W+", "_", url)
if args[:2] == ["pr", "view"]:
    print((store / key).read_text() if (store / key).exists() else "Original body.")
elif args[:2] == ["pr", "edit"]:
    (store / key).write_text(sys.stdin.read())
""")
    # A shell wrapper: an interpreter path with a space cannot go on a #! line.
    gh.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{logic}" "$@"\n')
    gh.chmod(gh.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("PATH", f"{gh.parent}{os.pathsep}{os.environ['PATH']}")
    return bodies


def test_link_prs_adds_one_related_section_to_each(fake_gh):
    urls = ["https://github.com/acme/order-service/pull/7", "https://github.com/acme/web-client/pull/12"]
    assert link_prs.link(urls) == {"linked": urls, "failed": []}
    link_prs.link(urls)
    bodies = {p.name: p.read_text() for p in fake_gh.iterdir()}
    service = next(v for k, v in bodies.items() if "order_service" in k)
    client = next(v for k, v in bodies.items() if "web_client" in k)
    for body in (service, client):
        assert body.startswith("Original body.\n\n## Related pull requests\n")
        assert body.count("## Related pull requests") == 1
    assert "- web-client#12: https://github.com/acme/web-client/pull/12" in service and "order-service#7" not in service
    assert "- order-service#7: https://github.com/acme/order-service/pull/7" in client


def test_link_prs_keeps_sections_that_follow():
    body = "Summary.\n\n## Related pull requests\n\n- old\n\n## Tests\n\nall pass\n"
    out = link_prs.with_section(body, ["https://github.com/a/b/pull/1"])
    assert "- old" not in out and "## Tests\n\nall pass" in out and out.endswith("- b#1: https://github.com/a/b/pull/1\n")


def test_link_prs_command_line_rejects_bad_input():
    run = lambda *a: subprocess.run([sys.executable, str(PKG / "core" / "scripts" / "link_prs.py"), *a],  # noqa: E731
                                    capture_output=True, text=True)
    assert run("https://github.com/a/b/pull/1").returncode == 2
    assert run("https://github.com/a/b/pull/1", "not-a-url").returncode == 2


def test_review_cross_skill_is_registered_as_a_forked_opus_skill():
    skill = (PKG / "adapters" / "claude" / "skills" / "review-cross" / "SKILL.md").read_text()
    head = skill.split("\n---", 1)[0]
    assert re.search(r"^name: review-cross$", head, re.M)
    assert re.search(r"^model: opus$", head, re.M) and re.search(r"^context: fork$", head, re.M)
    entries = json.loads((PKG / "core" / "workflow" / "skills.json").read_text())["skills"]
    assert any(e["name"] == "review-cross" for e in entries)
    assert (PKG / "core" / "skills" / "review-cross.md").is_file()


def test_review_cross_is_a_known_phase_marker():
    import run_phase
    import status_graph

    assert "review-cross" in run_phase.PHASES
    assert status_graph._LIGHT_MARKER_PHASE["review-cross"] == "review"
