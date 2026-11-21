#!/usr/bin/env python3
"""interfaces.py - the interface graph of a multi-repository project.

Scans the project's repositories (the same ones the discovery map lists) for
the places where one repository provides something another uses, and records
each as an interface: its kind, the provider repository and the path globs
that define it, and the consumer repositories with the paths that use it.

Kinds and how each is found (code files only; docs, tests and vendored or
built folders are skipped):

  package  a manifest's package name (pyproject.toml, package.json, go.mod)
           that another repository's manifest depends on; consumers are the
           manifest and the files that import it
  http     a route defined with a framework decorator or call
           (@app.get("/x"), router.post("/x"), HandleFunc("/x")) whose path a
           client call in another repository requests (fetch, axios,
           requests, httpx); path parameters match any segment
  event    a topic one repository publishes (publish/emit/produce) and another
           subscribes to (subscribe/consume/listen/on_event)
  schema   a schema file (.proto, .graphql, .avsc, *.schema.json, OpenAPI or
           Swagger documents, .thrift) whose name another repository's code
           mentions

Deterministic and standard library only (Python 3.9+), so /discover can run it
and its result can be checked.

Subcommands:
  scan    print the interfaces as JSON
  render  write .workflow_artifacts/memory/dependencies-map.md from the
          discovery map's interfaces (or a fresh scan when the map has none)
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))
import generate_discovery_map as gdm  # noqa: E402

KINDS = ("http", "schema", "package", "event")
CODE_EXT = {".py", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".go", ".rb", ".java", ".kt", ".rs", ".cs", ".php", ".swift"}
SKIP_DIRS = {
    ".git", "node_modules", ".venv", "venv", "env", "dist", "build", "out", "target", "vendor", "__pycache__",
    ".workflow_artifacts", ".next", ".nuxt", ".tox", ".mypy_cache", ".pytest_cache", ".ruff_cache", "coverage",
    ".serena", ".claude", "site-packages",
}
TEST_PATH = re.compile(
    r"(^|/)(tests?|__tests__|__mocks__|spec|specs|fixtures)(/|$)|(_test|\.test|\.spec|_spec)\.[A-Za-z]+$|(^|/)test_[^/]*$"
)
MAX_FILE_BYTES = 512_000
MAX_FILES = 20_000

SCHEMA_FILE = re.compile(
    r"(\.proto|\.graphql|\.gql|\.avsc|\.thrift|\.schema\.json)$|(^|/)(openapi|swagger)[^/]*\.(json|ya?ml)$", re.I
)

ROUTE_DEFS = (
    re.compile(r"@\w+(?:\.\w+)*\.(?:get|post|put|patch|delete|route|api_route)\(\s*[rf]?[\"']([^\"']+)[\"']"),
    re.compile(r"\b(?:app|router|server|api|routes)\.(?:get|post|put|patch|delete|all|route)\(\s*[\"'`]([^\"'`]+)[\"'`]"),
    re.compile(r"\bHandleFunc\(\s*\"([^\"]+)\""),
)
CLIENT_CALL = re.compile(
    r"\b(?:fetch|axios(?:\.\w+)?|requests\.\w+|httpx\.\w+|\w*[cC]lient\.(?:get|post|put|patch|delete|request)|ky(?:\.\w+)?)"
    r"\(\s*[fr]?([\"'`])(.*?)\1"
)
PUBLISH = re.compile(r"\b(?:publish|emit|produce|publish_event|send_event|dispatch_event)\(\s*[\"'`]([A-Za-z0-9_.:/-]+)[\"'`]")
SUBSCRIBE = re.compile(r"\b(?:subscribe|consume|listen|on_event|add_listener|handle_event)\(\s*[\"'`]([A-Za-z0-9_.:/-]+)[\"'`]")


# --- files -----------------------------------------------------------------


def iter_code_files(repo_dir: Path):
    """Yield (repo-relative posix path, text) for the repository's code files."""
    count = 0
    for root, dirs, files in os.walk(repo_dir):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS and not d.startswith("."))
        for name in sorted(files):
            path = Path(root) / name
            rel = path.relative_to(repo_dir).as_posix()
            if path.suffix.lower() not in CODE_EXT or TEST_PATH.search(rel):
                continue
            try:
                if path.stat().st_size > MAX_FILE_BYTES:
                    continue
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            count += 1
            if count > MAX_FILES:
                return
            yield rel, text


def iter_schema_files(repo_dir: Path):
    for root, dirs, files in os.walk(repo_dir):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS and not d.startswith("."))
        for name in sorted(files):
            rel = (Path(root) / name).relative_to(repo_dir).as_posix()
            if SCHEMA_FILE.search(rel) and not TEST_PATH.search(rel):
                yield rel


# --- manifests -------------------------------------------------------------


def norm_package(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _toml_section(text: str, header: str) -> str:
    match = re.search(rf"^\[{re.escape(header)}\]\s*$(.*?)(?=^\[|\Z)", text, re.M | re.S)
    return match.group(1) if match else ""


def _req_name(spec: str) -> str | None:
    match = re.match(r"\s*([A-Za-z0-9][A-Za-z0-9_.-]*)", spec)
    return match.group(1) if match else None


def read_manifests(repo_dir: Path) -> dict:
    """``{"provides": [(name, manifest, lang)], "depends": {norm name: [manifests]}}``."""
    provides, depends = [], {}

    def dep(name: str | None, manifest: str) -> None:
        if name:
            depends.setdefault(norm_package(name), [])
            if manifest not in depends[norm_package(name)]:
                depends[norm_package(name)].append(manifest)

    pyproject = repo_dir / "pyproject.toml"
    if pyproject.is_file():
        text = pyproject.read_text(encoding="utf-8", errors="replace")
        project = _toml_section(text, "project")
        poetry = _toml_section(text, "tool.poetry")
        name = re.search(r"^\s*name\s*=\s*[\"']([^\"']+)", project or poetry, re.M)
        if name:
            provides.append((name.group(1), "pyproject.toml", "python"))
        deps = re.search(r"^\s*dependencies\s*=\s*\[(.*?)\]", project, re.M | re.S)
        for spec in re.findall(r"[\"']([^\"']+)[\"']", deps.group(1) if deps else ""):
            dep(_req_name(spec), "pyproject.toml")
        for key in re.findall(r"^\s*([A-Za-z0-9][A-Za-z0-9_.-]*)\s*=", _toml_section(text, "tool.poetry.dependencies"), re.M):
            if key.lower() != "python":
                dep(key, "pyproject.toml")
    for req in sorted(repo_dir.glob("requirements*.txt")):
        for line in req.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.split("#", 1)[0].strip()
            if line and not line.startswith("-"):
                dep(_req_name(line), req.name)
    package_json = repo_dir / "package.json"
    if package_json.is_file():
        try:
            data = json.loads(package_json.read_text(encoding="utf-8", errors="replace"))
        except ValueError:
            data = {}
        if isinstance(data.get("name"), str):
            provides.append((data["name"], "package.json", "js"))
        for section in ("dependencies", "devDependencies", "peerDependencies", "optionalDependencies"):
            for key in (data.get(section) or {}):
                dep(key, "package.json")
    go_mod = repo_dir / "go.mod"
    if go_mod.is_file():
        text = go_mod.read_text(encoding="utf-8", errors="replace")
        module = re.search(r"^module\s+(\S+)", text, re.M)
        if module:
            provides.append((module.group(1), "go.mod", "go"))
        for name in re.findall(r"^\s*(?:require\s+)?([a-z0-9.-]+\.[a-z]+/\S+)\s+v\S+", text, re.M):
            dep(name, "go.mod")
    return {"provides": provides, "depends": depends}


def _package_paths(repo_dir: Path, name: str, manifest: str, lang: str) -> tuple[list[str], re.Pattern | None]:
    """Globs defining the package and the import pattern consumers use."""
    paths = [manifest]
    if lang == "python":
        module = re.sub(r"[-.]", "_", name)
        for candidate in (module, f"src/{module}"):
            if (repo_dir / candidate).is_dir():
                paths.append(f"{candidate}/**")
                break
        else:
            paths.append("src/**" if (repo_dir / "src").is_dir() else "**/*.py")
        return paths, re.compile(rf"^\s*(?:from|import)\s+{re.escape(module)}\b", re.M)
    if lang == "js":
        for candidate in ("src", "lib"):
            if (repo_dir / candidate).is_dir():
                paths.append(f"{candidate}/**")
                break
        else:
            paths.append("**")
        quoted = re.escape(name)
        return paths, re.compile(rf"""(?:from\s+|require\(\s*|import\s+)["']{quoted}(?:/[^"']*)?["']""")
    paths.append("**/*.go")
    return paths, re.compile(rf"\"{re.escape(name)}(?:/[^\"]*)?\"")


# --- paths -----------------------------------------------------------------


def norm_route(raw: str) -> str | None:
    """A route or requested URL reduced to ``/a/{}/b``; None when it is not one."""
    url = raw.strip()
    url = re.sub(r"^[a-z][a-z0-9+.-]*://[^/]+", "", url, flags=re.I)
    url = re.sub(r"^(?:\$\{[^}]*\}|\{[^}]*\}|<[^>]*>)+", "", url)
    url = url.split("?", 1)[0].split("#", 1)[0]
    if not url.startswith("/"):
        return None
    segments = []
    for seg in url.strip("/").split("/"):
        if not seg:
            continue
        if re.fullmatch(r"\$\{[^}]*\}|\{[^}]*\}|:[A-Za-z_]\w*|<[^>]*>|\*", seg):
            segments.append("{}")
        elif re.fullmatch(r"[A-Za-z0-9_.~-]+", seg):
            segments.append(seg)
        else:
            return None
    if not segments or not any(re.search(r"[A-Za-z]", s) for s in segments if s != "{}"):
        return None
    return "/" + "/".join(segments)


def glob_regex(pattern: str) -> re.Pattern:
    out, i = [], 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif pattern[i] == "*":
            out.append("[^/]*")
            i += 1
        elif pattern[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(pattern[i]))
            i += 1
    return re.compile("".join(out) + r"\Z")


def glob_match(path: str, pattern: str) -> bool:
    return bool(glob_regex(pattern).match(path))


# --- scan ------------------------------------------------------------------


def repositories(project_root: Path) -> list[dict]:
    """``[{"name", "path", "dir"}]`` as the discovery map lists them."""
    out = []
    for repo in gdm._enumerate_repos(project_root):
        directory = project_root if repo["path"] == "." else project_root / repo["path"]
        out.append({"name": repo["name"], "path": repo["path"], "dir": directory})
    return out


def _add_consumer(consumers: dict, repo: str, path: str) -> None:
    consumers.setdefault(repo, [])
    if path not in consumers[repo]:
        consumers[repo].append(path)


def _interface(iid: str, name: str, kind: str, provider: str, provider_paths, consumers: dict) -> dict:
    return {
        "id": iid,
        "name": name,
        "kind": kind,
        "provider": provider,
        "provider_paths": sorted(set(provider_paths)),
        "consumers": [{"repo": repo, "paths": sorted(paths)} for repo, paths in sorted(consumers.items())],
    }


def scan(project_root: str | os.PathLike) -> list[dict]:
    root = Path(project_root).resolve()
    repos = repositories(root)
    files = {r["name"]: list(iter_code_files(r["dir"])) for r in repos}
    manifests = {r["name"]: read_manifests(r["dir"]) for r in repos}
    found: list[dict] = []

    # packages
    for repo in repos:
        for name, manifest, lang in manifests[repo["name"]]["provides"]:
            paths, importer = _package_paths(repo["dir"], name, manifest, lang)
            consumers: dict = {}
            for other in repos:
                if other["name"] == repo["name"]:
                    continue
                declared = manifests[other["name"]]["depends"].get(norm_package(name))
                if not declared:
                    continue
                for manifest_path in declared:
                    _add_consumer(consumers, other["name"], manifest_path)
                for rel, text in files[other["name"]]:
                    if importer is not None and importer.search(text):
                        _add_consumer(consumers, other["name"], rel)
            if consumers:
                found.append(_interface(f"package:{norm_package(name)}", name, "package", repo["name"], paths, consumers))

    # http
    routes: dict = {}
    for repo in repos:
        for rel, text in files[repo["name"]]:
            for pattern in ROUTE_DEFS:
                for raw in pattern.findall(text):
                    norm = norm_route(raw)
                    if norm:
                        routes.setdefault(norm, {}).setdefault(repo["name"], set()).add(rel)
    groups: dict = {}
    for repo in repos:
        for rel, text in files[repo["name"]]:
            for _quote, raw in CLIENT_CALL.findall(text):
                norm = norm_route(raw)
                for provider, defining in (routes.get(norm) or {}).items() if norm else ():
                    if provider == repo["name"]:
                        continue
                    group = groups.setdefault((provider, "/" + norm.strip("/").split("/")[0]), {"paths": set(), "consumers": {}})
                    group["paths"].update(defining)
                    _add_consumer(group["consumers"], repo["name"], rel)
    for (provider, prefix), group in sorted(groups.items()):
        found.append(_interface(f"http:{provider}:{prefix}", f"HTTP {prefix}", "http", provider, group["paths"], group["consumers"]))

    # events
    published: dict = {}
    subscribed: dict = {}
    for repo in repos:
        for rel, text in files[repo["name"]]:
            for topic in PUBLISH.findall(text):
                published.setdefault(topic, {}).setdefault(repo["name"], set()).add(rel)
            for topic in SUBSCRIBE.findall(text):
                subscribed.setdefault(topic, {}).setdefault(repo["name"], set()).add(rel)
    for topic, providers in sorted(published.items()):
        for provider, paths in sorted(providers.items()):
            consumers = {}
            for repo, rels in (subscribed.get(topic) or {}).items():
                if repo != provider:
                    for rel in rels:
                        _add_consumer(consumers, repo, rel)
            if consumers:
                found.append(_interface(f"event:{provider}:{topic}", topic, "event", provider, paths, consumers))

    # schemas
    for repo in repos:
        for rel in iter_schema_files(repo["dir"]):
            base = rel.rsplit("/", 1)[-1]
            if len(base) < 6:
                continue
            consumers = {}
            for other in repos:
                if other["name"] == repo["name"]:
                    continue
                for orel, text in files[other["name"]]:
                    if base in text:
                        _add_consumer(consumers, other["name"], orel)
            if consumers:
                found.append(_interface(f"schema:{repo['name']}:{rel}", base, "schema", repo["name"], [rel], consumers))

    return sorted(found, key=lambda i: i["id"])


# --- discovery map ---------------------------------------------------------


def map_path(project_root: Path) -> Path:
    return project_root / ".workflow_artifacts" / "discovery-map.json"


def load_interfaces(project_root: str | os.PathLike) -> list[dict]:
    """The discovery map's interfaces, or a fresh scan for a version 1 or missing map."""
    root = Path(project_root).resolve()
    try:
        data = json.loads(map_path(root).read_text(encoding="utf-8"))
        if data.get("schema_version", 1) >= 2 and isinstance(data.get("interfaces"), list):
            return data["interfaces"]
    except (OSError, ValueError, AttributeError):
        pass
    return scan(root)


GENERATED_HEADER = "<!-- Generated by quoin interfaces.py from the discovery map. Do not edit; rerun /discover. -->"


def render(interfaces: list[dict]) -> str:
    lines = [GENERATED_HEADER, "", "# Dependencies map", ""]
    if not interfaces:
        lines += ["No interfaces between the project's repositories were found.", ""]
        return "\n".join(lines)
    lines += [
        "Where one repository provides something another uses. Change the provider side and the",
        "consumers listed need checking.",
        "",
        "| Interface | Kind | Provider | Defined in | Consumers |",
        "|---|---|---|---|---|",
    ]
    for item in interfaces:
        defined = ", ".join(f"`{p}`" for p in item["provider_paths"])
        users = "; ".join(f"{c['repo']} ({', '.join(f'`{p}`' for p in c['paths'])})" for c in item["consumers"])
        lines.append(f"| {item['name']} | {item['kind']} | {item['provider']} | {defined} | {users} |")
    lines.append("")
    return "\n".join(lines)


def write_dependencies_map(project_root: str | os.PathLike) -> Path:
    root = Path(project_root).resolve()
    out = root / ".workflow_artifacts" / "memory" / "dependencies-map.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render(load_interfaces(root)), encoding="utf-8")
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    for name in ("scan", "render"):
        p = sub.add_parser(name)
        p.add_argument("--project-root", default=".")
    args = parser.parse_args(argv)
    if args.cmd == "scan":
        print(json.dumps(scan(args.project_root), indent=2))
    else:
        print(write_dependencies_map(args.project_root))
    return 0


if __name__ == "__main__":
    sys.exit(main())
