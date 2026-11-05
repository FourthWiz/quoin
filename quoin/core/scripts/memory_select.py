#!/usr/bin/env python3
"""quoin/core/scripts/memory_select.py — selective retrieval for lessons-learned.md.

Returns task-relevant entries from lessons-learned.md using a deterministic
token/substring overlap matcher. Low threshold errs toward over-inclusion (R-04).
Falls back to wholesale read on pathological inputs — selection NEVER fails.

Public API:
  Entry: dataclass(header, applies_to, body, lineno)
  SelectResult: dataclass(selected, fellback_to_wholesale, total, selected_count)
  parse_entries(text: str) -> list[Entry]
  tokenize(s: str) -> set[str]
  score(task_tokens: set[str], entry: Entry) -> int
  select(text: str, task_text: str, threshold: int = SELECT_THRESHOLD) -> SelectResult
  main(argv: list[str] | None = None) -> int

Semantic mode (opt-in, fail-open):
  --build-index   (re)build the local vector index over lessons-learned.md and the
                  forgotten/ archive at <lessons dir>/index/lessons-index.json.
                  Idempotent and incremental: unchanged entries keep their vectors,
                  removed entries are dropped, and the file is rewritten only when
                  its content changes. Re-run it any time; it is safe to repeat.
  --semantic      add vector-similarity hits to the lexical selection. With no
                  index, an unreadable index or one built by a different backend,
                  output is identical to the lexical path.
  --source forgotten
                  search the forgotten/ archive instead of lessons-learned.md
                  (substring match, plus vector hits under --semantic).

  The built-in embedder is stdlib-only: hashed character n-grams, cosine
  similarity. It matches word-form paraphrases ("flaky" vs "flakiness") but is
  not a language model. Set QUOIN_EMBED_BACKEND=fastembed (optional package,
  model from QUOIN_EMBED_MODEL) for model embeddings; if the package is not
  importable the built-in embedder is used.

Exit codes:
  0 — success (selected entries printed, or JSON emitted)
  2 — usage/IO error (bad args, file not found)
  NOTE: there is NO exit 1 — selection always succeeds (degrades to wholesale).

IVG-50 S-1: selective retrieval for lessons-learned.md.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import math
import os
import re
import sys
import zlib
from dataclasses import dataclass, field
from math import ceil
from pathlib import Path

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Default score threshold: 1 shared token = include. Errs toward over-inclusion (R-04).
SELECT_THRESHOLD: int = 1

# Minimum number of results to return before falling back to wholesale.
# If select() would return fewer than this, return all entries.
MIN_RESULTS: int = 5

# Maximum fraction of all entries to return before falling back to wholesale.
# If select() would return more than this fraction, return all entries.
MAX_FRACTION: float = 0.6

# Minimum token length — tokens shorter than this are dropped.
MIN_TOKEN_LEN: int = 3

# Planning/review skill names that trigger always-include when they appear in Applies-to.
PLANNING_SKILL_NAMES: frozenset[str] = frozenset({
    "/plan", "/critic", "/architect", "/review",
})

# Stopwords dropped from token sets.
_STOPWORDS: frozenset[str] = frozenset({
    "the", "and", "for", "that", "this", "with", "from", "are", "was",
    "not", "but", "have", "had", "has", "its", "also", "any", "all",
    "use", "used", "using", "can", "via", "per", "see", "run", "runs",
    "add", "new", "one", "two", "get", "set", "our", "when", "which",
    "into", "each", "will", "does", "then", "they", "them", "been",
    "may", "it", "in", "on", "of", "to", "a", "an", "is", "as", "at",
    "by", "if", "or", "so", "do", "be", "we", "no", "up", "out",
})

# Applies-to line pattern
_APPLIES_TO_RE = re.compile(r"\*\*Applies to:\*\*\s*(.*)")

# Default path for lessons-learned.md, resolved relative to CWD.
_DEFAULT_LESSONS_PATH = Path(".workflow_artifacts") / "memory" / "lessons-learned.md"

# Semantic index: stored next to lessons-learned.md.
INDEX_DIRNAME: str = "index"
INDEX_FILENAME: str = "lessons-index.json"
INDEX_VERSION: int = 1
FORGOTTEN_DIRNAME: str = "forgotten"

# Metadata lines /sleep writes at the top of each forgotten/ archive block.
_FORGOTTEN_META_RE = re.compile(r"^> (Source|Forgotten|Score):")

# Semantic hits added on top of the lexical selection: at most this many,
# each with cosine similarity at least SEMANTIC_MIN_SIM.
SEMANTIC_TOP_K: int = 5
SEMANTIC_MIN_SIM: float = 0.3

# Built-in embedder: character n-gram sizes and hash space.
_NGRAM_SIZES: tuple = (3, 4)
_NGRAM_DIM: int = 1 << 20
_NGRAM_BACKEND_ID: str = "ngram-v1"


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------

@dataclass
class Entry:
    """A single lessons-learned entry."""
    header: str       # The ## header line (date + task name)
    applies_to: str   # Text after **Applies to:**
    body: str         # Full entry text (including header)
    lineno: int       # 1-based line number of the ## header in the source


@dataclass
class SelectResult:
    """Result of select()."""
    selected: list[Entry] = field(default_factory=list)
    fellback_to_wholesale: bool = False
    total: int = 0
    selected_count: int = 0

    @property
    def selected_headers(self) -> list[str]:
        return [e.header for e in self.selected]


# ---------------------------------------------------------------------------
# Core helpers
# ---------------------------------------------------------------------------

def parse_entries(text: str) -> list[Entry]:
    """Split *text* into Entry objects, one per ## header.

    Skips the template/comment block at the top of lessons-learned.md:
    - Lines beginning with <!-- ... --> (HTML comments)
    - The literal template example starting with ``## <date>``

    Only entries whose header matches ``## <date>`` (date = 4 digits) or
    ``## YYYY-MM-DD`` format (real entries) are kept.
    """
    entries: list[Entry] = []
    lines = text.splitlines(keepends=True)
    total_lines = len(lines)

    # Find all ## header positions
    positions: list[int] = []  # 0-based line indices of ## headers
    for i, line in enumerate(lines):
        if line.startswith("## "):
            positions.append(i)

    for idx, pos in enumerate(positions):
        # Determine end of this entry
        end_pos = positions[idx + 1] if idx + 1 < len(positions) else total_lines

        header_line = lines[pos].rstrip("\n").rstrip("\r")

        # Skip the template comment block: "## <date> — <task-name>" placeholder
        # (the literal angle-bracket form used in the comment block at the top)
        if "<date>" in header_line:
            continue

        # Reconstruct the entry body
        body = "".join(lines[pos:end_pos])

        # Extract Applies-to text
        applies_to = ""
        match = _APPLIES_TO_RE.search(body)
        if match:
            applies_to = match.group(1).strip()

        entries.append(Entry(
            header=header_line.lstrip("# ").strip(),  # strip "## " prefix
            applies_to=applies_to,
            body=body,
            lineno=pos + 1,  # 1-based
        ))

    return entries


def tokenize(s: str) -> set[str]:
    """Lowercase, split on non-alphanumeric, drop stopwords and short tokens.

    Deterministic: same input → same output. No stemming, no LLM.
    """
    tokens = re.split(r"[^a-z0-9]+", s.lower())
    return {
        t for t in tokens
        if len(t) >= MIN_TOKEN_LEN and t not in _STOPWORDS
    }


def score(task_tokens: set[str], entry: Entry) -> int:
    """Count shared tokens between task_tokens and the entry's applies_to + header.

    Primary signal: Applies-to tag + header (cheap, high-signal).
    Entry body is NOT scored — keeps over-inclusion bounded per R-04 design.
    """
    entry_tokens = tokenize(entry.applies_to + " " + entry.header)
    return len(task_tokens & entry_tokens)


def _always_include(entry: Entry, task_tokens: set[str]) -> bool:
    """Return True if this entry must always be included regardless of score.

    Always-include rules (R-04 — bias toward keeping a lesson):
    1. Entry's Applies-to line names a planning/review skill literally.
    2. Entry's header task-name shares any token with the task text.
    """
    # Rule 1: planning/review skill named in applies_to
    applies_lower = entry.applies_to.lower()
    for skill in PLANNING_SKILL_NAMES:
        if skill in applies_lower:
            return True

    # Rule 2: header task-name tokens overlap with task tokens
    header_tokens = tokenize(entry.header)
    if header_tokens & task_tokens:
        return True

    return False


def select(
    text: str,
    task_text: str,
    threshold: int = SELECT_THRESHOLD,
) -> SelectResult:
    """Select task-relevant entries from *text* (a lessons-learned.md blob).

    Returns entries with score >= threshold, PLUS always-include entries.
    Falls back to wholesale if result count < MIN_RESULTS or > MAX_FRACTION * total.

    The wholesale fallback guarantees pathological inputs degrade safely to
    the existing behavior, never to under-inclusion (R-04).
    """
    entries = parse_entries(text)
    total = len(entries)
    result = SelectResult(total=total)

    if total == 0:
        result.fellback_to_wholesale = True
        result.selected_count = 0
        return result

    # Tokenize the task description
    task_tokens = tokenize(task_text) if task_text.strip() else set()

    # Score each entry and apply always-include rules
    selected: list[Entry] = []
    for entry in entries:
        s = score(task_tokens, entry)
        ai = _always_include(entry, task_tokens) if task_tokens else False
        if s >= threshold or ai:
            selected.append(entry)

    selected_count = len(selected)

    # Wholesale fallback conditions:
    # 1. Task text empty or garbage (no tokens) → always wholesale
    # 2. Fewer than MIN_RESULTS selected
    # 3. More than MAX_FRACTION of all entries selected (no useful filtering)
    if (
        not task_tokens
        or selected_count < MIN_RESULTS
        or selected_count > ceil(MAX_FRACTION * total)
    ):
        result.fellback_to_wholesale = True
        result.selected = list(entries)
        result.selected_count = total
        return result

    result.selected = selected
    result.selected_count = selected_count
    return result


# ---------------------------------------------------------------------------
# Semantic index (opt-in, fail-open)
# ---------------------------------------------------------------------------

def parse_forgotten(text: str, name: str) -> list[Entry]:
    """Split a forgotten/<date>.md archive file into Entry records.

    Blocks are separated by lines consisting of ``---``. Each block starts with
    ``> Source:`` / ``> Forgotten:`` / ``> Score:`` metadata lines followed by
    the verbatim forgotten text. The header is ``forgotten/<name>#<n>`` (1-based
    block number); ``applies_to`` holds the ``> Source:`` anchor.
    """
    entries: list[Entry] = []
    lines = text.splitlines(keepends=True)
    block: list[str] = []
    start = 0
    count = 0

    def _flush(block_lines: list[str], first_lineno: int) -> None:
        nonlocal count
        body = "".join(block_lines)
        if not body.strip():
            return
        count += 1
        anchor = ""
        for ln in block_lines:
            if ln.startswith("> Source:"):
                anchor = ln[len("> Source:"):].strip()
                break
        entries.append(Entry(
            header=f"forgotten/{name}#{count}",
            applies_to=anchor,
            body=body,
            lineno=first_lineno,
        ))

    def _is_separator(i: int) -> bool:
        # "---" ends a block only when the next non-blank line opens a new
        # block (or the file ends): the verbatim text may contain "---" too.
        if lines[i].strip() != "---":
            return False
        for nxt in lines[i + 1:]:
            if nxt.strip():
                return nxt.startswith("> Source:")
        return True

    for i, line in enumerate(lines):
        if _is_separator(i):
            _flush(block, start + 1)
            block = []
            start = i + 1
            continue
        if not block:
            if not line.strip():
                continue  # blank lines between blocks
            start = i
        block.append(line)
    _flush(block, start + 1)
    return entries


def _embed_text(entry: Entry) -> str:
    """Text embedded for an entry: the body without forgotten/ metadata lines,
    stripped so that blank lines added around it (appends) do not change it."""
    return "".join(
        ln for ln in entry.body.splitlines(keepends=True)
        if not _FORGOTTEN_META_RE.match(ln)
    ).strip()


def _normalize(vec: dict) -> dict:
    norm = math.sqrt(sum(v * v for v in vec.values()))
    if norm == 0:
        return {}
    return {k: round(v / norm, 4) for k, v in vec.items()}


class NgramBackend:
    """Stdlib-only embedder: hashed character n-grams of each content word.

    Words are lowercased, stopwords and short tokens dropped, then padded as
    ``#word#`` and cut into 3- and 4-grams. Counts are log-scaled and the vector
    is L2-normalised, so the dot product of two vectors is their cosine.
    """

    id = _NGRAM_BACKEND_ID

    def embed(self, texts: list[str]) -> list[dict]:
        out = []
        for text in texts:
            counts: dict = {}
            for word in tokenize(text):
                padded = f"#{word}#"
                for n in _NGRAM_SIZES:
                    for i in range(len(padded) - n + 1):
                        h = zlib.crc32(padded[i:i + n].encode("utf-8")) % _NGRAM_DIM
                        counts[h] = counts.get(h, 0) + 1
            out.append(_normalize(
                {str(k): 1.0 + math.log(c) for k, c in counts.items()}
            ))
        return out


class FastembedBackend:
    """Optional model embedder via the ``fastembed`` package (not a dependency)."""

    def __init__(self) -> None:
        # Optional package, loaded dynamically so it is never a hard dependency.
        text_embedding = importlib.import_module("fastembed").TextEmbedding
        model = os.environ.get("QUOIN_EMBED_MODEL", "BAAI/bge-small-en-v1.5")
        self.id = f"fastembed:{model}"
        self._model = text_embedding(model_name=model)

    def embed(self, texts: list[str]) -> list[dict]:
        out = []
        for vec in self._model.embed(list(texts)):
            out.append(_normalize({str(i): float(v) for i, v in enumerate(vec)}))
        return out


def active_backend():
    """Return the embedder selected by QUOIN_EMBED_BACKEND (default: built-in).

    An unknown name, or an optional backend whose package cannot be imported or
    initialised, falls back to the built-in n-gram embedder.
    """
    name = os.environ.get("QUOIN_EMBED_BACKEND", "").strip().lower()
    if name == "fastembed":
        try:
            return FastembedBackend()
        except Exception as exc:  # noqa: BLE001 — fail-open to built-in
            print(
                f"memory_select: fastembed backend unavailable ({exc}); "
                "using built-in n-gram embedder",
                file=sys.stderr,
            )
    return NgramBackend()


def _cosine(a: dict, b: dict) -> float:
    if len(a) > len(b):
        a, b = b, a
    return sum(v * b.get(k, 0.0) for k, v in a.items())


def _entry_key(entry: Entry) -> str:
    return hashlib.sha256(_embed_text(entry).encode("utf-8")).hexdigest()


def default_index_path(lessons_path: Path) -> Path:
    return lessons_path.parent / INDEX_DIRNAME / INDEX_FILENAME


def collect_entries(lessons_path: Path) -> list[tuple[str, Entry]]:
    """All indexable entries as (source, Entry): lessons first, then forgotten/."""
    out: list[tuple[str, Entry]] = []
    if lessons_path.exists():
        text = lessons_path.read_text(encoding="utf-8")
        out.extend(("lessons", e) for e in parse_entries(text))
    forgotten_dir = lessons_path.parent / FORGOTTEN_DIRNAME
    if forgotten_dir.is_dir():
        for f in sorted(forgotten_dir.glob("*.md")):
            text = f.read_text(encoding="utf-8")
            out.extend(
                (f"{FORGOTTEN_DIRNAME}/{f.name}", e)
                for e in parse_forgotten(text, f.name)
            )
    return out


def load_index(index_path: Path, backend_id: str):
    """Return {key: vector} from the index, or None when it is unusable.

    Unusable means: missing, unreadable, malformed, wrong version, or built by a
    different backend. Callers treat None as "no index" (fail-open).
    """
    try:
        data = json.loads(index_path.read_text(encoding="utf-8"))
        if data.get("version") != INDEX_VERSION or data.get("backend") != backend_id:
            return None
        vectors = {}
        for item in data["entries"]:
            vec = item["vector"]
            if not isinstance(vec, dict):
                return None
            vectors[str(item["key"])] = {str(k): float(v) for k, v in vec.items()}
        return vectors
    except Exception:  # noqa: BLE001 — any problem means "no index"
        return None


def build_index(lessons_path: Path, index_path: Path, backend=None) -> dict:
    """Build or refresh the index; return counts {entries, added, reused, removed, written}.

    Vectors of entries whose text is unchanged are reused from the existing
    index (same backend only). The output is deterministic, so a second run
    with no changes leaves the file byte-identical and is not rewritten.
    """
    backend = backend or active_backend()
    old = load_index(index_path, backend.id) or {}

    items = []
    seen = set()
    for source, entry in collect_entries(lessons_path):
        key = _entry_key(entry)
        if (source, key) in seen:
            continue
        seen.add((source, key))
        items.append((source, key, entry))

    missing = sorted({k for _, k, _ in items if k not in old})
    texts = {}
    for _, k, e in items:
        if k in missing and k not in texts:
            texts[k] = _embed_text(e)
    fresh = dict(zip(missing, backend.embed([texts[k] for k in missing])))

    records = []
    for source, key, entry in items:
        records.append({
            "source": source,
            "key": key,
            "header": entry.header,
            "vector": old[key] if key in old else fresh[key],
        })
    records.sort(key=lambda r: (r["source"] != "lessons", r["source"], r["key"]))

    payload = json.dumps(
        {"version": INDEX_VERSION, "backend": backend.id, "entries": records},
        sort_keys=True, separators=(",", ":"),
    ) + "\n"

    current_keys = {k for _, k, _ in items}
    stats = {
        "entries": len(records),
        "added": len(missing),
        "reused": len(current_keys) - len(missing),
        "removed": len(set(old) - current_keys),
        "written": False,
    }
    try:
        existing = index_path.read_text(encoding="utf-8")
    except OSError:
        existing = None
    if existing != payload:
        index_path.parent.mkdir(parents=True, exist_ok=True)
        # The index is a local, rebuildable cache: keep it out of version control.
        ignore = index_path.parent / ".gitignore"
        if not ignore.exists():
            ignore.write_text("*\n", encoding="utf-8")
        tmp = index_path.with_name(index_path.name + ".tmp")
        tmp.write_text(payload, encoding="utf-8")
        os.replace(tmp, index_path)
        stats["written"] = True
    return stats


def semantic_rank(
    entries: list[Entry], query: str, vectors: dict, backend,
) -> list[tuple[float, Entry]]:
    """Rank *entries* by cosine similarity to *query*, highest first.

    Entries missing from the index (added since the last build) are embedded on
    the fly, so a stale index still covers every entry.
    """
    if not query.strip() or not entries:
        return []
    keys = [_entry_key(e) for e in entries]
    missing = [i for i, k in enumerate(keys) if k not in vectors]
    extra = backend.embed(
        [query] + [_embed_text(entries[i]) for i in missing]
    )
    qvec, extra_vecs = extra[0], extra[1:]
    local = dict(vectors)
    for i, vec in zip(missing, extra_vecs):
        local[keys[i]] = vec
    ranked = [(_cosine(qvec, local[k]), e) for k, e in zip(keys, entries)]
    ranked.sort(key=lambda t: (-t[0], t[1].lineno))
    return ranked


def semantic_hits(
    entries: list[Entry], query: str, vectors: dict, backend,
) -> list[tuple[float, Entry]]:
    """Top SEMANTIC_TOP_K entries with similarity >= SEMANTIC_MIN_SIM."""
    ranked = semantic_rank(entries, query, vectors, backend)
    return [t for t in ranked if t[0] >= SEMANTIC_MIN_SIM][:SEMANTIC_TOP_K]


def select_semantic(
    text: str,
    task_text: str,
    vectors: dict,
    backend,
    threshold: int = SELECT_THRESHOLD,
) -> tuple[SelectResult, dict]:
    """Lexical selection plus vector hits; returns (result, {lineno: similarity}).

    The lexical selection is computed first (with its wholesale fallback). If it
    filtered, the best semantic hits are added (result kept in file order), up
    to the MAX_FRACTION cap, so semantic mode is never less selective.
    """
    lexical = select(text, task_text, threshold=threshold)
    entries = parse_entries(text)
    hits = semantic_hits(entries, task_text, vectors, backend)
    sims = {e.lineno: round(s, 4) for s, e in hits}
    if lexical.fellback_to_wholesale or not hits:
        return lexical, sims

    # Add the best new hits, never so many that the union would trip the
    # MAX_FRACTION wholesale rule: semantic mode must not be less selective.
    total = len(entries)
    chosen = {e.lineno for e in lexical.selected}
    room = ceil(MAX_FRACTION * total) - len(chosen)
    for _, e in hits:
        if room <= 0:
            break
        if e.lineno not in chosen:
            chosen.add(e.lineno)
            room -= 1
    union = [e for e in entries if e.lineno in chosen]
    sims = {e.lineno: round(s, 4) for s, e in hits if e.lineno in chosen}
    return SelectResult(union, False, total, len(union)), sims


def search_forgotten(
    lessons_path: Path, pattern: str, vectors, backend,
) -> list[tuple[str, Entry, dict]]:
    """Forgotten blocks matching *pattern*: case-insensitive substring hits, plus
    vector hits when *vectors* is not None. Returns (source, entry, scores).
    """
    items = [
        (src, e) for src, e in collect_entries(lessons_path) if src != "lessons"
    ]
    needle = pattern.lower()
    found: dict = {}
    for src, e in items:
        if needle and needle in _embed_text(e).lower():
            found[(src, e.lineno)] = (src, e, {"score": 1})
    if vectors is not None:
        source_of = {id(e): src for src, e in items}
        try:
            hits = semantic_hits([e for _, e in items], pattern, vectors, backend)
        except Exception as exc:  # noqa: BLE001 — fail-open to substring hits
            print(f"memory_select: semantic search failed ({exc}); "
                  "using substring match only", file=sys.stderr)
            hits = []
        for sim, e in hits:
            src = source_of[id(e)]
            rec = found.setdefault((src, e.lineno), (src, e, {"score": 0}))
            rec[2]["similarity"] = round(sim, 4)
    return [found[k] for k in sorted(found)]


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="memory_select",
        description=(
            "Select task-relevant entries from lessons-learned.md using "
            "deterministic token overlap. Falls back to wholesale read on "
            "pathological inputs. Never exits 1 — selection always succeeds."
        ),
    )
    parser.add_argument(
        "--task-text",
        default=None,
        metavar="TEXT",
        help="Task description text to match against lessons-learned entries.",
    )
    parser.add_argument(
        "--file",
        default=None,
        metavar="PATH",
        help=(
            "Path to lessons-learned.md. "
            f"Default: {_DEFAULT_LESSONS_PATH} (relative to CWD)."
        ),
    )
    parser.add_argument(
        "--threshold",
        type=int,
        default=SELECT_THRESHOLD,
        metavar="N",
        help=f"Minimum score to include an entry (default: {SELECT_THRESHOLD}).",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        dest="as_json",
        help=(
            'Output result as JSON: '
            '{"selected":[{"header":...,"lineno":...,"score":...}...],'
            '"fellback_to_wholesale":bool,"total":N,"selected_count":M}.'
        ),
    )
    parser.add_argument(
        "--ids-only",
        action="store_true",
        dest="ids_only",
        help="Output only entry headers (one per line), not full body text.",
    )
    parser.add_argument(
        "--semantic",
        action="store_true",
        help=(
            "Add vector-similarity hits from the local index. Without a usable "
            "index the output is identical to the lexical path."
        ),
    )
    parser.add_argument(
        "--build-index",
        action="store_true",
        dest="build_index",
        help=(
            "Rebuild the semantic index (idempotent, incremental) and exit. "
            "Does not need --task-text."
        ),
    )
    parser.add_argument(
        "--index",
        default=None,
        metavar="PATH",
        help=f"Index path. Default: <lessons dir>/{INDEX_DIRNAME}/{INDEX_FILENAME}.",
    )
    parser.add_argument(
        "--source",
        choices=("lessons", "forgotten"),
        default="lessons",
        help=(
            "Entries to search: lessons-learned.md (default) or the forgotten/ "
            "archive (substring match on --task-text, plus vector hits under --semantic)."
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """CLI entry point. Returns exit code (0=success, 2=usage/IO error).

    Note: there is NO exit code 1. Selection always succeeds (degrades to wholesale).
    """
    parser = _build_parser()
    args = parser.parse_args(argv)
    if not args.build_index and args.task_text is None:
        parser.error("the following arguments are required: --task-text")

    # Resolve file path
    if args.file:
        lessons_path = Path(args.file).resolve()
    else:
        lessons_path = (Path.cwd() / _DEFAULT_LESSONS_PATH).resolve()
    index_path = (
        Path(args.index).resolve() if args.index else default_index_path(lessons_path)
    )

    # Index builds and forgotten/ searches need only the memory directory;
    # lessons selection needs the lessons file itself.
    needs_file = not args.build_index and args.source == "lessons"
    if not (lessons_path.exists() if needs_file else lessons_path.parent.is_dir()):
        print(
            f"error: lessons-learned.md not found at {lessons_path}. "
            "Pass --file or run from the project root.",
            file=sys.stderr,
        )
        return 2

    if args.build_index:
        backend = active_backend()
        try:
            stats = build_index(lessons_path, index_path, backend)
        except (OSError, UnicodeDecodeError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        print(
            f"index: {index_path} backend={backend.id} entries={stats['entries']} "
            f"added={stats['added']} reused={stats['reused']} "
            f"removed={stats['removed']} "
            f"{'written' if stats['written'] else 'unchanged'}"
        )
        return 0

    backend = None
    vectors = None
    if args.semantic and index_path.exists():
        backend = active_backend()
        vectors = load_index(index_path, backend.id)

    if args.source == "forgotten":
        try:
            found = search_forgotten(lessons_path, args.task_text, vectors, backend)
        except (OSError, UnicodeDecodeError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        if args.as_json:
            print(json.dumps({"selected": [
                dict({"header": e.header, "source": src, "lineno": e.lineno,
                      "anchor": e.applies_to}, **scores)
                for src, e, scores in found
            ]}))
        elif args.ids_only:
            for _, e, _ in found:
                print(e.header)
        else:
            print("".join(e.body for _, e, _ in found), end="")
        return 0

    try:
        text = lessons_path.read_text(encoding="utf-8")
    except (OSError, PermissionError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    sims: dict = {}
    result = None
    if vectors is not None:
        try:
            result, sims = select_semantic(
                text, args.task_text, vectors, backend, threshold=args.threshold,
            )
        except Exception as exc:  # noqa: BLE001 — fail-open to lexical
            print(f"memory_select: semantic selection failed ({exc}); "
                  "using lexical selection", file=sys.stderr)
            result, sims = None, {}
    if result is None:
        result = select(text, args.task_text, threshold=args.threshold)

    if args.as_json:
        # Include score in each entry's JSON output
        task_tokens = tokenize(args.task_text) if args.task_text.strip() else set()
        selected_json = []
        for e in result.selected:
            item = {
                "header": e.header,
                "lineno": e.lineno,
                "score": score(task_tokens, e),
            }
            if e.lineno in sims:
                item["similarity"] = sims[e.lineno]
            selected_json.append(item)
        print(json.dumps({
            "selected": selected_json,
            "fellback_to_wholesale": result.fellback_to_wholesale,
            "total": result.total,
            "selected_count": result.selected_count,
        }))
        return 0

    if args.ids_only:
        for entry in result.selected:
            print(entry.header)
        return 0

    # Default: print concatenated entry bodies (what a planning skill would read)
    print("".join(e.body for e in result.selected), end="")
    return 0


if __name__ == "__main__":
    sys.exit(main())
