"""Interface graph: the fixture's planted provider and consumer pairs are found
and nothing else, impact.py names the repositories a diff touches, gen_rules.py
writes path-scoped rules, and version 1 discovery maps still validate."""

import copy
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

PKG = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PKG / "core" / "scripts"))
import gen_rules  # noqa: E402
import generate_discovery_map as gdm  # noqa: E402
import impact  # noqa: E402
import interfaces as ifx  # noqa: E402
import validate_discovery_map as vdm  # noqa: E402

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "interface_project"
V1_MAP = Path(__file__).resolve().parent / "fixtures" / "discovery-map" / "example-map.json"
REPOS = ("shared-schema", "order-service", "web-client")
PLANTED = {
    ("package", "shared-schema", "order-service"),
    ("schema", "shared-schema", "web-client"),
    ("http", "order-service", "web-client"),
    ("event", "order-service", "web-client"),
}


def _git(repo, *args):
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", *args],
                   check=True, capture_output=True)


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "proj"
    shutil.copytree(FIXTURE, root)
    for name in REPOS:
        _git(root / name, "init", "-q", "-b", "main")
        _git(root / name, "add", ".")
        _git(root / name, "commit", "-q", "-m", "init")
    (root / ".workflow_artifacts" / "memory").mkdir(parents=True)
    return root


def _pairs(graph):
    return {(i["kind"], i["provider"], c["repo"]) for i in graph for c in i["consumers"]}


def test_graph_holds_every_planted_pair_and_no_other(project):
    assert _pairs(ifx.scan(project)) == PLANTED


def test_graph_paths(project):
    graph = {i["kind"]: i for i in ifx.scan(project)}
    assert graph["package"]["provider_paths"] == ["acme_schema/**", "pyproject.toml"]
    assert graph["package"]["consumers"] == [{"repo": "order-service", "paths": ["app/routes.py", "pyproject.toml"]}]
    assert graph["http"]["name"] == "HTTP /orders" and graph["http"]["provider_paths"] == ["app/routes.py"]
    assert graph["http"]["consumers"] == [{"repo": "web-client", "paths": ["src/api.ts"]}]
    assert graph["event"]["name"] == "order.created"
    assert graph["schema"]["provider_paths"] == ["schemas/order.schema.json"]
    assert graph["schema"]["consumers"] == [{"repo": "web-client", "paths": ["src/validate.ts"]}]


def test_decoys_produce_nothing(project):
    every_path = {p for i in ifx.scan(project) for c in i["consumers"] for p in c["paths"]}
    assert "tests/test_routes.py" not in every_path
    names = {i["name"] for i in ifx.scan(project)}
    assert "order.audited" not in names and not any("/health" in n or "/publish" in n for n in names)


@pytest.mark.parametrize("raw, want", [
    ("/orders/{order_id}", "/orders/{}"),
    ("${BASE}/orders/${id}", "/orders/{}"),
    ("http://localhost:8000/orders?x=1", "/orders"),
    ("{API}/users/:id/", "/users/{}"),
    ("/v1/{}", "/v1/{}"),
    ("orders", None),
    ("/", None),
    ("/{id}", None),
])
def test_route_normalisation(raw, want):
    assert ifx.norm_route(raw) == want


@pytest.mark.parametrize("path, pattern, want", [
    ("acme_schema/order.py", "acme_schema/**", True),
    ("a/b/c.py", "**/*.py", True),
    ("c.py", "**/*.py", True),
    ("src/x.ts", "src/*.ts", True),
    ("src/a/x.ts", "src/*.ts", False),
    ("pyproject.toml", "acme_schema/**", False),
])
def test_glob_match(path, pattern, want):
    assert ifx.glob_match(path, pattern) is want


def test_impact_of_a_provider_change_names_the_consumer(project):
    routes = project / "order-service" / "app" / "routes.py"
    routes.write_text(routes.read_text().replace('@app.post("/orders")', '@app.post("/orders/new")'))
    _git(project / "order-service", "commit", "-qam", "rename route")
    result = impact.impact(project, "HEAD~1")
    assert result["changed"] == {"order-service": ["app/routes.py"]}
    provider_side = {i["name"] for i in result["interfaces"] if i["side"] == "provider"}
    assert provider_side == {"HTTP /orders", "order.created"}
    assert result["check"] == ["web-client"]
    assert result["repositories"] == ["order-service", "web-client"]
    assert "Repositories to check: web-client" in impact.render(result)


def test_impact_of_a_package_change_names_its_dependent(project):
    model = project / "shared-schema" / "acme_schema" / "order.py"
    model.write_text(model.read_text() + "    currency: str = 'USD'\n")
    result = impact.impact(project, "HEAD")
    assert result["check"] == ["order-service"]


def test_impact_of_an_unrelated_change_touches_no_interface(project):
    (project / "shared-schema" / "README.md").write_text("changed\n")
    result = impact.impact(project, "HEAD")
    assert result["interfaces"] == [] and result["check"] == []
    assert "No interface between repositories is touched." in impact.render(result)


def test_rules_are_path_scoped_and_regenerated_cleanly(project):
    out = gen_rules.generate(project)
    assert out["interfaces"] == 4 and len(out["written"]) == 4
    rules = project / ".claude" / "rules"
    http = (rules / "quoin-interface-http-order-service-orders.md").read_text()
    assert http.startswith('---\npaths:\n  - "order-service/app/routes.py"\n---\n')
    assert gen_rules.HEADER in http and "- web-client: web-client/src/api.ts" in http
    assert gen_rules.generate(project)["written"] == []

    (rules / "mine.md").write_text("Hand-written rule.\n")
    (rules / "quoin-interface-gone.md").write_text(gen_rules.HEADER + "\n")
    assert gen_rules.generate(project)["removed"] == ["quoin-interface-gone.md"]
    assert (rules / "mine.md").read_text() == "Hand-written rule.\n"


def test_rules_rerun_only_when_discovery_is_dirty(project):
    assert gen_rules.is_dirty(project) is False
    gen_rules.generate(project)
    marker = project / ".workflow_artifacts" / "memory" / "discovery-dirty"
    marker.write_text("2026-11-21 task: structure\n")
    later = time.time() + 5
    os.utime(marker, (later, later))
    assert gen_rules.is_dirty(project) is True
    out = gen_rules.refresh(project)
    assert out["interfaces"] == 4
    assert json.loads((project / ".workflow_artifacts" / "discovery-map.json").read_text())["schema_version"] == 2
    assert (project / ".workflow_artifacts" / "memory" / "dependencies-map.md").is_file()


def test_dependencies_map_is_rendered_from_the_graph(project):
    path = ifx.write_dependencies_map(project)
    text = path.read_text()
    assert text.startswith(ifx.GENERATED_HEADER)
    assert "| HTTP /orders | http | order-service | `app/routes.py` | web-client (`src/api.ts`) |" in text


def test_version_2_map_validates_with_its_interfaces(project):
    data = gdm.generate_map(project)
    assert data["schema_version"] == 2 and _pairs(data["interfaces"]) == PLANTED
    assert vdm.validate(data) == []


def test_version_1_maps_still_validate():
    data = json.loads(V1_MAP.read_text())
    assert data["schema_version"] == 1 and "interfaces" not in data
    assert vdm.validate(data) == []


def test_interfaces_are_checked(project):
    data = gdm.generate_map(project)
    v1 = copy.deepcopy(data)
    v1["schema_version"] = 1
    assert any("only a schema_version 2 map" in e for e in vdm.validate(v1))
    bad = copy.deepcopy(data)
    bad["interfaces"][0]["kind"] = "rpc"
    bad["interfaces"][1]["consumers"][0]["repo"] = "ghost"
    bad["interfaces"][2]["provider_paths"] = ["/abs/path"]
    errors = vdm.validate(bad)
    assert any("DM-06" in e and "kind" in e for e in errors)
    assert any("'ghost' is not a repo" in e for e in errors)
    assert any("DM-09" in e and "provider_paths" in e for e in errors)


def test_session_start_reruns_rules_when_discovery_is_dirty(project, monkeypatch, tmp_path):
    import task_record

    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    spawned = []
    monkeypatch.setattr(task_record, "_spawn", spawned.append)
    task_record.hook("session-start", {"session_id": "s1", "cwd": str(project), "source": "startup"})
    assert not any("gen_rules.py" in arg for argv in spawned for arg in argv)
    (project / ".workflow_artifacts" / "memory" / "discovery-dirty").write_text("x\n")
    task_record.hook("session-start", {"session_id": "s1", "cwd": str(project), "source": "startup"})
    assert any(argv[1].endswith("gen_rules.py") and "--if-dirty" in argv for argv in spawned)


def test_command_lines(project):
    run = lambda script, *a: subprocess.run(  # noqa: E731
        [sys.executable, str(PKG / "core" / "scripts" / script), *a], capture_output=True, text=True, check=True).stdout
    assert len(json.loads(run("interfaces.py", "scan", "--project-root", str(project)))) == 4
    assert json.loads(run("impact.py", "--diff", "HEAD", "--project-root", str(project), "--json"))["changed"] == {}
    assert json.loads(run("gen_rules.py", "--project-root", str(project), "--if-dirty"))["skipped"]
