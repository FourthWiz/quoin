"""repo_map.py — token-budgeted, graph-ranked symbol map of a repository.

Parses source files into definitions and identifier references, builds a
file-level reference graph (file A -> file B when A uses a name B defines),
ranks files with weighted PageRank, spreads each file's rank over the symbols
it is referenced through, and renders the top symbols grouped by file. The
number of symbols shown is the largest that keeps the whole output, including
frontmatter, within the token budget (estimated as characters / 4).

Parsing uses tree-sitter when the `tree_sitter` package and a grammar package
for the language (for example `tree_sitter_python`) are importable. Otherwise it
falls back to the standard library: `ast` for Python and regular expressions
for the other languages. The map is advisory; a missing map changes nothing.

Portable-core: stdlib only at import time, no `import quoin`.

Exit codes (CLI):
  0 — map printed or written
  2 — argparse / invocation error
  3 — repository unreadable or output not writable (fail-open: callers skip)
"""

import argparse
import ast
import datetime
import importlib
import json
import math
import os
import re
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

ENV_TOKENS = "QUOIN_REPOMAP_TOKENS"
DEFAULT_TOKENS = 1000
MIN_TOKENS = 100
MAX_FILE_BYTES = 512 * 1024
MAX_FILES = 5000

_PRUNE_NAMES = frozenset({
    ".git", "node_modules", ".venv", "venv", "__pycache__", ".idea", ".vscode",
    ".workspaces", ".workflow_artifacts", "dist", "build", "target", ".tox",
    ".mypy_cache", ".pytest_cache",
})

_LANG_BY_EXT = {
    ".py": "python",
    ".js": "javascript", ".jsx": "javascript", ".mjs": "javascript", ".cjs": "javascript",
    ".ts": "typescript", ".tsx": "tsx",
    ".go": "go",
    ".rs": "rust",
    ".java": "java",
    ".kt": "kotlin",
    ".rb": "ruby",
    ".sh": "bash", ".bash": "bash",
    ".c": "c", ".h": "c",
    ".cc": "cpp", ".cpp": "cpp", ".hpp": "cpp", ".hh": "cpp",
    ".cs": "c_sharp",
    ".php": "php",
}

# tree-sitter node types that define a named symbol, per grammar. The symbol name
# is the node's `name` field (or, failing that, its first identifier child).
_TS_DEF_TYPES = {
    "python": {"function_definition": "def", "class_definition": "class"},
    "javascript": {
        "function_declaration": "function", "class_declaration": "class",
        "method_definition": "method", "generator_function_declaration": "function",
    },
    "typescript": {
        "function_declaration": "function", "class_declaration": "class",
        "method_definition": "method", "interface_declaration": "interface",
        "type_alias_declaration": "type", "enum_declaration": "enum",
        "abstract_class_declaration": "class",
    },
    "go": {
        "function_declaration": "func", "method_declaration": "func",
        "type_spec": "type",
    },
    "rust": {
        "function_item": "fn", "struct_item": "struct", "enum_item": "enum",
        "trait_item": "trait", "type_item": "type", "mod_item": "mod",
    },
    "java": {
        "class_declaration": "class", "interface_declaration": "interface",
        "enum_declaration": "enum", "method_declaration": "method",
        "record_declaration": "record",
    },
    "kotlin": {"class_declaration": "class", "function_declaration": "fun",
               "object_declaration": "object"},
    "ruby": {"method": "def", "class": "class", "module": "module",
             "singleton_method": "def"},
    "bash": {"function_definition": "function"},
    "c": {"function_definition": "function", "struct_specifier": "struct"},
    "cpp": {"function_definition": "function", "class_specifier": "class",
            "struct_specifier": "struct"},
    "c_sharp": {"class_declaration": "class", "interface_declaration": "interface",
                "method_declaration": "method", "struct_declaration": "struct",
                "enum_declaration": "enum"},
    "php": {"function_definition": "function", "class_declaration": "class",
            "method_declaration": "method", "interface_declaration": "interface"},
}
_TS_DEF_TYPES["tsx"] = _TS_DEF_TYPES["typescript"]

# Grammar module and the attribute holding its language for each language key.
_TS_GRAMMARS = {
    "python": ("tree_sitter_python", "language"),
    "javascript": ("tree_sitter_javascript", "language"),
    "typescript": ("tree_sitter_typescript", "language_typescript"),
    "tsx": ("tree_sitter_typescript", "language_tsx"),
    "go": ("tree_sitter_go", "language"),
    "rust": ("tree_sitter_rust", "language"),
    "java": ("tree_sitter_java", "language"),
    "kotlin": ("tree_sitter_kotlin", "language"),
    "ruby": ("tree_sitter_ruby", "language"),
    "bash": ("tree_sitter_bash", "language"),
    "c": ("tree_sitter_c", "language"),
    "cpp": ("tree_sitter_cpp", "language"),
    "c_sharp": ("tree_sitter_c_sharp", "language"),
    "php": ("tree_sitter_php", "language_php"),
}

_ID = r"[A-Za-z_$][A-Za-z0-9_$]*"
_JS_DEFS = [
    (re.compile(r"^\s*(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s*\*?\s*(" + _ID + ")"), "function"),
    (re.compile(r"^\s*(?:export\s+)?(?:default\s+)?(?:abstract\s+)?class\s+(" + _ID + ")"), "class"),
    (re.compile(r"^\s*(?:export\s+)?(?:const|let|var)\s+(" + _ID + r")\s*=\s*(?:async\s+)?(?:function\b|\([^)]*\)\s*=>|" + _ID + r"\s*=>)"), "function"),
]
_TS_EXTRA = [
    (re.compile(r"^\s*(?:export\s+)?interface\s+(" + _ID + ")"), "interface"),
    (re.compile(r"^\s*(?:export\s+)?type\s+(" + _ID + r")\s*[=<]"), "type"),
    (re.compile(r"^\s*(?:export\s+)?(?:const\s+)?enum\s+(" + _ID + ")"), "enum"),
]
_REGEX_DEFS = {
    "javascript": _JS_DEFS,
    "typescript": _JS_DEFS + _TS_EXTRA,
    "tsx": _JS_DEFS + _TS_EXTRA,
    "go": [
        (re.compile(r"^func\s+(?:\([^)]*\)\s*)?(" + _ID + ")"), "func"),
        (re.compile(r"^type\s+(" + _ID + ")"), "type"),
    ],
    "rust": [
        (re.compile(r"^\s*(?:pub(?:\([^)]*\))?\s+)?(?:async\s+)?(?:unsafe\s+)?fn\s+(" + _ID + ")"), "fn"),
        (re.compile(r"^\s*(?:pub(?:\([^)]*\))?\s+)?(struct|enum|trait|type|mod)\s+(" + _ID + ")"), None),
    ],
    "java": [
        (re.compile(r"^\s*(?:(?:public|private|protected|static|final|abstract|sealed)\s+)*(class|interface|enum|record)\s+(" + _ID + ")"), None),
        (re.compile(r"^\s+(?:(?:public|private|protected|static|final|abstract|synchronized)\s+)+[\w<>\[\], ?]+\s+(" + _ID + r")\s*\("), "method"),
    ],
    "kotlin": [
        (re.compile(r"^\s*(?:\w+\s+)*(class|interface|object)\s+(" + _ID + ")"), None),
        (re.compile(r"^\s*(?:\w+\s+)*fun\s+(?:<[^>]*>\s*)?(?:[\w.]+\.)?(" + _ID + ")"), "fun"),
    ],
    "ruby": [
        (re.compile(r"^\s*(class|module)\s+([A-Z]\w*)"), None),
        (re.compile(r"^\s*def\s+(?:self\.)?(" + _ID + r"[?!=]?)"), "def"),
    ],
    "bash": [
        (re.compile(r"^\s*(?:function\s+)?(" + _ID + r")\s*\(\s*\)\s*\{?"), "function"),
        (re.compile(r"^\s*function\s+(" + _ID + ")"), "function"),
    ],
    "c": [
        (re.compile(r"^(?:[A-Za-z_][\w\s\*]*\s+\**)(" + _ID + r")\s*\([^;]*$"), "function"),
        (re.compile(r"^\s*(?:typedef\s+)?struct\s+(" + _ID + ")"), "struct"),
    ],
    "cpp": [
        (re.compile(r"^\s*(?:template\s*<[^>]*>\s*)?(class|struct)\s+(" + _ID + ")"), None),
        (re.compile(r"^(?:[A-Za-z_][\w:<>\s\*&]*\s+[\*&]*)(" + _ID + r"(?:::" + _ID + r")?)\s*\([^;]*$"), "function"),
    ],
    "c_sharp": [
        (re.compile(r"^\s*(?:(?:public|private|protected|internal|static|sealed|abstract|partial)\s+)*(class|interface|struct|enum|record)\s+(" + _ID + ")"), None),
        (re.compile(r"^\s+(?:(?:public|private|protected|internal|static|virtual|override|async)\s+)+[\w<>\[\], ?]+\s+(" + _ID + r")\s*\("), "method"),
    ],
    "php": [
        (re.compile(r"^\s*(?:(?:abstract|final)\s+)?(class|interface|trait)\s+(" + _ID + ")"), None),
        (re.compile(r"^\s*(?:(?:public|private|protected|static)\s+)*function\s+(" + _ID + ")"), "function"),
    ],
}
_IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_KEYWORDS = frozenset(
    "if else elif for while do return def class function func fn struct enum "
    "trait type mod pub let const var import from export default new this self "
    "true false null none nil and or not in is as with try except catch finally "
    "raise throw public private protected static final void int string bool "
    "async await yield break continue switch case package interface extends "
    "implements module end then fi done esac echo local".split()
)


def est_tokens(text: str) -> int:
    return math.ceil(len(text) / 4)


def resolve_budget(flag_value=None) -> int:
    """Budget from --tokens, else QUOIN_REPOMAP_TOKENS, else the default."""
    raw = flag_value if flag_value is not None else os.environ.get(ENV_TOKENS)
    try:
        value = int(raw) if raw is not None else DEFAULT_TOKENS
    except (TypeError, ValueError):
        value = DEFAULT_TOKENS
    if value <= 0:
        value = DEFAULT_TOKENS
    return max(value, MIN_TOKENS)


# ---------------------------------------------------------------------------
# File enumeration
# ---------------------------------------------------------------------------

def list_source_files(repo: Path) -> list:
    """Repo-relative POSIX paths of supported source files, at most MAX_FILES.

    Uses `git ls-files` when the repo is a git checkout (so ignored files stay
    out), else walks the tree pruning vendored and build directories.
    """
    names = None
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo), "ls-files", "-z", "--cached", "--others",
             "--exclude-standard"],
            capture_output=True, timeout=30, check=False,
        )
        if proc.returncode == 0:
            names = [n for n in proc.stdout.decode("utf-8", "replace").split("\0") if n]
    except (OSError, subprocess.SubprocessError):
        names = None
    if not names:
        names = []
        for root, dirs, files in os.walk(repo):
            dirs[:] = sorted(d for d in dirs if d not in _PRUNE_NAMES)
            for fname in sorted(files):
                rel = os.path.relpath(os.path.join(root, fname), repo)
                names.append(rel.replace(os.sep, "/"))
    out = []
    for rel in sorted(set(names)):
        parts = rel.split("/")
        if any(p in _PRUNE_NAMES for p in parts[:-1]):
            continue
        if Path(rel).suffix.lower() not in _LANG_BY_EXT:
            continue
        full = repo / rel
        try:
            if not full.is_file() or full.stat().st_size > MAX_FILE_BYTES:
                continue
        except OSError:
            continue
        out.append(rel)
        if len(out) >= MAX_FILES:
            break
    return out


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------

_TS_PARSERS: dict = {}


def _ts_parser(lang: str):
    """A tree-sitter parser for `lang`, or None when unavailable."""
    if lang in _TS_PARSERS:
        return _TS_PARSERS[lang]
    parser = None
    spec = _TS_GRAMMARS.get(lang)
    if spec is not None:
        try:
            ts = importlib.import_module("tree_sitter")
            grammar = importlib.import_module(spec[0])
            language = ts.Language(getattr(grammar, spec[1])())
            parser = ts.Parser(language)
        except Exception:
            parser = None
    _TS_PARSERS[lang] = parser
    return parser


_JS_FUNCTION_VALUES = frozenset({"arrow_function", "function_expression", "function",
                                  "generator_function"})


def _ts_def_name(node, lang: str):
    """The identifier node naming a definition, or None."""
    if lang in ("c", "cpp") and node.type == "function_definition":
        # The name sits under declarator -> function_declarator -> declarator,
        # possibly wrapped in pointer/reference declarators or a qualified name.
        cur = node.child_by_field_name("declarator")
        while cur is not None and not cur.type.endswith("identifier"):
            nxt = cur.child_by_field_name("declarator") or cur.child_by_field_name("name")
            if nxt is None:
                nxt = next((c for c in cur.children
                            if c.type.endswith("identifier") or "declarator" in c.type), None)
            cur = nxt
        return cur
    name_node = node.child_by_field_name("name")
    if name_node is None:
        name_node = next((c for c in node.children if c.type.endswith("identifier")), None)
    return name_node


def _ts_extract(parser, lang: str, source: bytes):
    tree = parser.parse(source)
    def_types = _TS_DEF_TYPES.get(lang, {})
    defs = []
    refs: dict = defaultdict(int)
    def_name_nodes = set()
    stack = [tree.root_node]
    while stack:
        node = stack.pop()
        kind = def_types.get(node.type)
        if (kind is None and node.type == "variable_declarator"
                and lang in ("javascript", "typescript", "tsx")):
            value = node.child_by_field_name("value")
            if value is not None and value.type in _JS_FUNCTION_VALUES:
                kind = "function"
        if kind is not None:
            name_node = _ts_def_name(node, lang)
            if name_node is not None and name_node.text:
                name = name_node.text.decode("utf-8", "replace")
                defs.append((name, kind, name_node.start_point[0] + 1))
                def_name_nodes.add((name_node.start_byte, name_node.end_byte))
        if node.type.endswith("identifier") and node.child_count == 0:
            if (node.start_byte, node.end_byte) not in def_name_nodes and node.text:
                refs[node.text.decode("utf-8", "replace")] += 1
        stack.extend(reversed(node.children))
    return defs, dict(refs)


def _py_extract(text: str):
    tree = ast.parse(text)
    defs = []
    refs: dict = defaultdict(int)

    def visit_body(body, in_class):
        for node in body:
            if isinstance(node, ast.ClassDef):
                defs.append((node.name, "class", node.lineno))
                visit_body(node.body, True)
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                defs.append((node.name, "def", node.lineno))

    visit_body(tree.body, False)
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            refs[node.id] += 1
        elif isinstance(node, ast.Attribute):
            refs[node.attr] += 1
    return defs, dict(refs)


def _regex_extract(lang: str, text: str):
    defs = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        for pattern, kind in _REGEX_DEFS.get(lang, ()):
            m = pattern.match(line)
            if not m:
                continue
            if kind is None:
                defs.append((m.group(2), m.group(1), lineno))
            else:
                name = m.group(1)
                if name in _KEYWORDS:
                    continue
                defs.append((name, kind, lineno))
            break
    refs: dict = defaultdict(int)
    for tok in _IDENT_RE.findall(text):
        if tok not in _KEYWORDS:
            refs[tok] += 1
    defined = defaultdict(int)
    for name, _kind, _line in defs:
        defined[name] += 1
    for name, n in defined.items():
        left = refs.get(name, 0) - n
        if left > 0:
            refs[name] = left
        else:
            refs.pop(name, None)
    return defs, dict(refs)


def extract(path: Path, lang: str, backend: str = "auto"):
    """(defs, refs, used_backend) for one file; defs are (name, kind, line)."""
    try:
        source = path.read_bytes()
    except OSError:
        return [], {}, "none"
    if backend in ("auto", "tree-sitter"):
        parser = _ts_parser(lang)
        if parser is not None:
            try:
                defs, refs = _ts_extract(parser, lang, source)
                return defs, refs, "tree-sitter"
            except Exception:
                pass
    text = source.decode("utf-8", "replace")
    if lang == "python":
        try:
            defs, refs = _py_extract(text)
            return defs, refs, "fallback"
        except (SyntaxError, ValueError, RecursionError):
            pass
    defs, refs = _regex_extract(lang, text)
    return defs, refs, "fallback"


# ---------------------------------------------------------------------------
# Ranking
# ---------------------------------------------------------------------------

# References only connect files whose languages can call each other.
_LANG_FAMILY = {"typescript": "javascript", "tsx": "javascript", "cpp": "c"}

_TEST_PATH_RE = re.compile(
    r"(^|/)(tests?|__tests__|spec|fixtures?)/|(^|/)test_[^/]*$|_test\.[^/]+$|\.(test|spec)\.[^/]+$"
)


def is_test_path(rel: str) -> bool:
    return bool(_TEST_PATH_RE.search(rel))


def _name_weight(name: str, n_definers: int) -> float:
    """How much a reference to `name` says about the files that define it.

    Long compound identifiers are specific; private, short or widely defined
    names (`get`, `path`, `run`) mostly add noise, and a name defined in several
    files spreads its weight across them.
    """
    weight = 1.0
    compound = "_" in name.strip("_") or any(c.isupper() for c in name[1:])
    if len(name) >= 8 and compound:
        weight *= 10.0
    elif len(name) < 5 and not compound:
        weight *= 0.1
    if name.startswith("_"):
        weight *= 0.1
    if n_definers > 5:
        weight *= 0.1
    return weight / max(n_definers, 1)


def pagerank(nodes, edges, personalization=None, damping=0.85, iters=40):
    """Weighted PageRank. `edges` maps src -> {dst: weight}."""
    nodes = list(nodes)
    if not nodes:
        return {}
    if personalization:
        total = sum(personalization.get(n, 0.0) for n in nodes)
    else:
        total = 0.0
    if total > 0:
        base = {n: personalization.get(n, 0.0) / total for n in nodes}
    else:
        base = {n: 1.0 / len(nodes) for n in nodes}
    rank = dict(base)
    out_weight = {n: sum(edges.get(n, {}).values()) for n in nodes}
    for _ in range(iters):
        dangling = sum(rank[n] for n in nodes if out_weight[n] == 0)
        new = {n: (1 - damping) * base[n] + damping * dangling * base[n] for n in nodes}
        for src, dsts in edges.items():
            ow = out_weight.get(src, 0)
            if not ow:
                continue
            share = damping * rank[src] / ow
            for dst, w in dsts.items():
                new[dst] += share * w
        rank = new
    return rank


def build_map(repo: Path, backend: str = "auto", focus=None):
    """Rank every definition in `repo`.

    Returns (ranked, stats) where ranked is a list of
    (score, file, line, kind, name) sorted best first.
    """
    files = list_source_files(repo)
    defs_by_file = {}
    refs_by_file = {}
    langs = {}
    backends = set()
    for rel in files:
        lang = _LANG_BY_EXT[Path(rel).suffix.lower()]
        langs[rel] = lang
        defs, refs, used = extract(repo / rel, lang, backend)
        if used != "none":
            backends.add(used)
        defs_by_file[rel] = defs
        refs_by_file[rel] = refs

    definers = defaultdict(set)
    for rel, defs in defs_by_file.items():
        for name, _kind, _line in defs:
            definers[name].add(rel)
    referencing = defaultdict(int)
    for refs in refs_by_file.values():
        for name in refs:
            if name in definers:
                referencing[name] += 1
    family = {rel: _LANG_FAMILY.get(lang, lang) for rel, lang in langs.items()}
    n_files = max(len(files), 1)

    edges: dict = defaultdict(lambda: defaultdict(float))
    edge_names: dict = defaultdict(lambda: defaultdict(float))
    for src, refs in refs_by_file.items():
        for name, count in refs.items():
            targets = definers.get(name)
            if not targets:
                continue
            weight = (_name_weight(name, len(targets)) * math.sqrt(count)
                      * math.log(1 + n_files / referencing[name]))
            for dst in targets:
                if dst == src or family[dst] != family[src]:
                    continue
                w = weight * (0.1 if is_test_path(dst) else 1.0)
                edges[src][dst] += w
                edge_names[(src, dst)][name] += w

    personalization = None
    if focus:
        focus_set = {f.replace(os.sep, "/").removeprefix("./") for f in focus}
        personalization = {f: (1.0 if f in focus_set else 0.0) for f in files}
        if not any(personalization.values()):
            personalization = None
    rank = pagerank(files, edges, personalization)

    symbol_score: dict = defaultdict(float)
    for (src, dst), names in edge_names.items():
        total = sum(edges[src].values())
        if not total:
            continue
        for name, w in names.items():
            symbol_score[(dst, name)] += rank[src] * w / total

    ranked = []
    for rel, defs in defs_by_file.items():
        seen = set()
        for name, kind, line in sorted(defs, key=lambda d: d[2]):
            if name in seen:
                continue
            seen.add(name)
            score = symbol_score.get((rel, name), 0.0) + rank.get(rel, 0.0) * 1e-3
            ranked.append((score, rel, line, kind, name))
    ranked.sort(key=lambda r: (-r[0], r[1], r[2], r[4]))
    if not backends:
        used_backend = "none"
    elif backends == {"tree-sitter"}:
        used_backend = "tree-sitter"
    elif backends == {"fallback"}:
        used_backend = "fallback"
    else:
        used_backend = "mixed"
    stats = {"files": len(files), "symbols": len(ranked), "backend": used_backend,
             "truncated": len(files) >= MAX_FILES}
    return ranked, stats


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def _git_head(repo: Path) -> str:
    try:
        proc = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"],
                              capture_output=True, text=True, timeout=10, check=False)
        if proc.returncode == 0 and proc.stdout.strip():
            return proc.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    return "none"


def render(ranked, stats, k, budget, repo_name, head, updated, tokens_field,
           compact=False):
    top = ranked[:k]
    by_file: dict = defaultdict(list)
    file_order = []
    for _score, rel, line, kind, name in top:
        if rel not in by_file:
            file_order.append(rel)
        by_file[rel].append((line, kind, name))
    lines = [
        "---",
        f"path: {repo_name}",
        f"hash: {head}",
        f"updated: {updated}",
        "updated_by: repo_map.py",
        f"tokens: {tokens_field}",
        "---",
        f"# Repo map: {repo_name}",
    ]
    if not compact:
        capped = f" (file limit {MAX_FILES} reached)" if stats.get("truncated") else ""
        lines += ["", (
            f"Top {len(top)} of {stats['symbols']} symbols from {stats['files']} files"
            f"{capped}, ranked by cross-file references (budget {budget} tokens, parser "
            f"{stats['backend']}). Files are listed most central first; entries are "
            "`line kind name`.")]
    if not top and not compact:
        lines += ["", "No supported source files with definitions were found."]
    for rel in file_order:
        lines += ["", rel]
        for line, kind, name in sorted(by_file[rel]):
            lines.append(f"  {line} {kind} {name}")
    return "\n".join(lines) + "\n"


def render_within_budget(ranked, stats, budget, repo_name, head, updated):
    """Render the most symbols that fit in `budget` tokens.

    When not even the full header fits, the explanatory paragraph is dropped;
    with the name capped, that header stays under MIN_TOKENS.
    """
    placeholder = "9" * (len(str(budget)) + 1)
    repo_name = repo_name[:60]
    compact = est_tokens(render(ranked, stats, 0, budget, repo_name, head, updated,
                                placeholder)) > budget

    def fits(k):
        text = render(ranked, stats, k, budget, repo_name, head, updated, placeholder,
                      compact)
        return est_tokens(text) <= budget

    lo, hi = 0, len(ranked)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if fits(mid):
            lo = mid
        else:
            hi = mid - 1
    draft = render(ranked, stats, lo, budget, repo_name, head, updated, placeholder,
                   compact)
    return render(ranked, stats, lo, budget, repo_name, head, updated,
                  str(est_tokens(draft)), compact)


def generate(repo: Path, budget: int, backend: str = "auto", focus=None,
             repo_name=None) -> str:
    ranked, stats = build_map(repo, backend, focus)
    updated = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return render_within_budget(ranked, stats, budget, repo_name or repo.name,
                                _git_head(repo), updated)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--repo", required=True, help="repository root to map")
    parser.add_argument("--tokens", type=int, default=None,
                        help=f"token budget (default ${ENV_TOKENS} or {DEFAULT_TOKENS})")
    parser.add_argument("--out", default=None,
                        help="write the map to this file instead of stdout")
    parser.add_argument("--name", default=None,
                        help="repo name shown in the map (default: directory name)")
    parser.add_argument("--focus", action="append", default=[],
                        help="repo-relative file to rank around (repeatable)")
    parser.add_argument("--backend", choices=("auto", "tree-sitter", "fallback"),
                        default="auto")
    parser.add_argument("--format", choices=("text", "json"), default="text",
                        help="with --out, how to report the result on stdout")
    args = parser.parse_args(argv)

    repo = Path(args.repo).expanduser()
    if not repo.is_dir():
        print(f"repo_map: not a directory: {repo}", file=sys.stderr)
        return 3
    budget = resolve_budget(args.tokens)
    try:
        text = generate(repo.resolve(), budget, args.backend, args.focus, args.name)
    except Exception as exc:
        print(f"repo_map: failed to map {repo}: {exc}", file=sys.stderr)
        return 3
    if args.out is None:
        sys.stdout.write(text)
        return 0
    out = Path(args.out)
    try:
        out.parent.mkdir(parents=True, exist_ok=True)
        tmp = out.with_name(out.name + ".tmp")
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, out)
    except OSError as exc:
        print(f"repo_map: cannot write {out}: {exc}", file=sys.stderr)
        return 3
    tokens = est_tokens(text)
    if args.format == "json":
        print(json.dumps({"out": str(out), "tokens": tokens, "budget": budget}))
    else:
        print(f"repo_map: wrote {out} ({tokens}/{budget} tokens)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
