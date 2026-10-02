"""The deterministic workflow gate.

Given a task, an optional stage and a gated phase, `evaluate` runs a fixed list
of checks over the recorded evidence, the run records, the phase's artifacts
and the current tree, and returns a verdict. Nothing a model wrote in prose can
change it: the optional explanation is carried on the result and is never given
to a check. The module reads files and runs the artifact validator as a
subprocess; it never starts a model and never asks for approval.

Part one (this top half): loading the shared core scripts, resolving paths,
validating artifacts and parsing verdicts. Part two: `evaluate`. Part three:
the audit-file writer.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import re
import shlex
import stat
import subprocess
import sys
import time
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from . import errors, evidence, runstore

VALIDATOR_TIMEOUT_S = 30.0
MAX_DETAIL_BYTES = 1024
MAX_TEXT_BYTES = 1024 * 1024
MAX_ENVELOPE_BYTES = 256 * 1024
MAX_EXPLANATION_BYTES = 8 * 1024
DEFAULT_MAX_CRITIC_ROUNDS = 5
CRITIC_VERDICTS = ("PASS", "REVISE")
REVIEW_VERDICTS = ("APPROVED", "CHANGES_REQUESTED", "BLOCKED")

REASON_CODES = (
    "artifact-added", "artifact-invalid", "artifact-missing", "artifact-removed", "boundary-unrecorded",
    "boundary-violation", "continuation-not-validated", "critic-missing", "critic-not-converged",
    "envelope-invalid", "evidence-incomplete", "evidence-missing", "input-hash-changed", "path-unresolved",
    "repo-added", "repo-content-changed", "repo-content-unverifiable", "repo-dirty-changed",
    "repo-head-changed", "repo-missing", "review-not-approved", "run-evidence-partial",
    "run-not-completed", "state-invalid", "tests-failed", "verdict-unparseable",
)
WARNING_CODES = (
    "boundary-unverified", "critic-not-run", "ledger-appended-during-run", "run-evidence-absent",
    "tests-not-configured",
)


def _redact(text: str) -> str:
    """The same masking `launch_env.Redactor()` applies with no registered
    values; kept local so this module does not import the launcher."""
    return errors.SECRET_SHAPE_RE.sub("<redacted>", text)


class GateRefused(Exception):
    """The request cannot be evaluated at all; `code` is a stable identifier."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class PathUnresolved(Exception):
    """A stage or task path could not be resolved safely."""

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


# ---------------------------------------------------------------------------
# core scripts
# ---------------------------------------------------------------------------

_CORE_CACHE: Dict[str, Any] = {}


def load_core(source_dir, name: str):
    """A pure-function core script loaded by file path, cached per path.
    `GateRefused("source-unavailable")` when it cannot be loaded."""
    path = Path(source_dir) / "core" / "scripts" / (name + ".py")
    key = str(path.resolve())
    module = _CORE_CACHE.get(key)
    if module is None:
        spec = importlib.util.spec_from_file_location("_quoin_core_" + name, str(path))
        if spec is None or spec.loader is None:
            raise GateRefused("source-unavailable")
        module = importlib.util.module_from_spec(spec)
        old = sys.dont_write_bytecode
        sys.dont_write_bytecode = True
        try:
            spec.loader.exec_module(module)
        except (ImportError, OSError, SyntaxError):
            raise GateRefused("source-unavailable") from None
        finally:
            sys.dont_write_bytecode = old
        _CORE_CACHE[key] = module
    return module


def _first_lines(text: str, limit: int = MAX_DETAIL_BYTES) -> str:
    lines = [ln for ln in text.splitlines() if ln.startswith("FAIL")] or [ln for ln in text.splitlines() if ln.strip()]
    out = "; ".join(lines)
    return _redact(out.encode("utf-8")[:limit].decode("utf-8", "ignore"))


def run_validator(project_root, source_dir, path) -> Optional[str]:
    """None when the artifact passes strict validation, else a short detail:
    the first failing invariants, or `validator-unavailable` when the
    validator itself could not give a verdict (fail closed)."""
    script = Path(source_dir) / "core" / "scripts" / "validate_artifact.py"
    sidecar = Path(source_dir) / "memory" / "format-kit.sections.json"
    if not script.is_file() or not sidecar.is_file():
        return "validator-unavailable"
    env = {k: v for k, v in os.environ.items() if k in ("PATH", "HOME")}
    env["LC_ALL"] = "C"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    try:
        proc = subprocess.run(
            [sys.executable, str(script), "--sections-json", str(sidecar), "--quiet", str(path)],
            env=env, cwd=str(project_root), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, timeout=VALIDATOR_TIMEOUT_S, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return "validator-unavailable"
    if proc.returncode == 0:
        return None
    if proc.returncode == 1:
        return _first_lines(proc.stderr.decode("utf-8", "replace")) or "validation failed"
    return "validator-unavailable"


# ---------------------------------------------------------------------------
# paths
# ---------------------------------------------------------------------------


def _lstat(path: Path):
    try:
        return os.lstat(str(path))
    except OSError:
        return None


def _safe_join(project_root, *parts: str) -> Path:
    """`project_root/parts...`, refusing a symlink at any component below the
    project root. Components that do not exist yet are allowed."""
    current = Path(project_root)
    for index, part in enumerate(parts):
        current = current / part
        info = _lstat(current)
        if info is not None and stat.S_ISLNK(info.st_mode):
            raise PathUnresolved("a symlink at %s" % "/".join(parts[: index + 1]))
    return current


def _confine(project_root, value: Any, within: Path) -> Path:
    """The file a recorded path names, which must lie under `within` with no
    symlink on the way. Absolute and project-relative spellings are accepted;
    anything that climbs out, or is not a plain string, is `PathUnresolved`."""
    if not isinstance(value, str) or not value or "\0" in value:
        raise PathUnresolved("a recorded path is not a usable string")
    if ".." in value.replace("\\", "/").split("/"):
        raise PathUnresolved("a recorded path climbs out of its folder")
    root = str(project_root)
    candidate = value if os.path.isabs(value) else os.path.join(root, value)
    rel = os.path.relpath(os.path.normpath(candidate), root)
    if rel == "." or rel == ".." or rel.startswith(".." + os.sep) or os.path.isabs(rel):
        raise PathUnresolved("a recorded path lies outside the project")
    full = _safe_join(project_root, *rel.split(os.sep))
    inside = os.path.relpath(str(full), str(within))
    if inside == "." or inside == ".." or inside.startswith(".." + os.sep) or os.path.isabs(inside):
        raise PathUnresolved("a recorded path lies outside its folder")
    return full


def task_root(project_root, task: str) -> Path:
    return _safe_join(project_root, ".workflow_artifacts", task)


def stage_dir(project_root, task: str, stage: Optional[int], source_dir) -> Path:
    """The directory holding a stage's artifacts: the task root without a
    stage, else `stage-N/` once the task's architecture lists stage N under
    its stage decomposition (the resolver's own row grammar)."""
    root = task_root(project_root, task)
    if stage is None:
        return root
    arch = root / "architecture.md"
    info = _lstat(arch)
    if info is None or not stat.S_ISREG(info.st_mode):
        raise PathUnresolved("architecture.md is missing at the task root")
    resolver = load_core(source_dir, "path_resolve")
    text = _read_text(arch, MAX_TEXT_BYTES)
    if text is None:
        raise PathUnresolved("architecture.md cannot be read")
    match = resolver.SECTION_RE.search(text)
    if not match:
        raise PathUnresolved("architecture.md has no stage decomposition section")
    following = resolver.NEXT_H2_RE.search(text, match.end())
    body = text[match.end(): following.start() if following else len(text)]
    if not any(int(m.group(1)) == stage for m in resolver.ROW_RE.finditer(body)):
        raise PathUnresolved("stage %d is not listed in the stage decomposition" % stage)
    folder = _safe_join(project_root, ".workflow_artifacts", task, "stage-%d" % stage)
    if resolver.task_path(task, stage, str(Path(project_root).resolve())).name != folder.name:
        raise PathUnresolved("the stage folder name disagrees with the resolver")
    return folder


def _read_text(path: Path, limit: int, *, prefix: bool = False) -> Optional[str]:
    """At most `limit` bytes of a regular file, never through a symlink and
    never blocking on a pipe or device: the file is opened non-blocking and its
    type is checked on the open descriptor. A file larger than `limit` yields
    None unless `prefix` is set, which returns its first `limit` bytes."""
    info = _lstat(path)
    if info is None or not stat.S_ISREG(info.st_mode) or (info.st_size > limit and not prefix):
        return None
    try:
        fd = os.open(str(path), os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
    except OSError:
        return None
    try:
        opened = os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode) or (opened.st_size > limit and not prefix):
            return None
        with os.fdopen(fd, "rb", closefd=False) as handle:
            return handle.read(limit).decode("utf-8", "replace")
    except OSError:
        return None
    finally:
        os.close(fd)


def _read_prefix(path: Path, limit: int) -> Optional[str]:
    return _read_text(path, limit, prefix=True)


def _numbered(directory: Path, pattern: "re.Pattern[str]") -> List[Tuple[int, Path]]:
    found: List[Tuple[int, Path]] = []
    try:
        names = os.listdir(str(directory))
    except OSError:
        return found
    for name in names:
        match = pattern.match(name)
        if match:
            found.append((int(match.group(1)), directory / name))
    return sorted(found)


_REVIEW_RE = re.compile(r"^review-(\d+)\.md$")
_CRITIC_RE = re.compile(r"^critic-response-(\d+)\.md$")


def expected_artifacts(
    project_root, task: str, stage: Optional[int], phase: str, entry: Mapping[str, Any], sdir: Optional[Path],
) -> Tuple[List[Path], List[str]]:
    """`(paths, missing)`: the files the phase must have produced and the
    names of those that are absent."""
    root = Path(project_root)
    paths: List[Path] = []
    missing: List[str] = []
    wanted: List[Path] = []
    if phase == "discover":
        wanted = [root / rel for rel in evidence.DISCOVER_FILES]
    elif phase == "architect":
        wanted = [task_root(project_root, task) / "architecture.md"]
    elif phase == "plan" and sdir is not None:
        wanted = [sdir / "current-plan.md"]
    elif phase == "review" and sdir is not None:
        chosen: Optional[Path] = None
        for item in entry.get("harvested") or []:
            candidate = item.get("path") if isinstance(item, Mapping) else None
            if isinstance(candidate, str) and _REVIEW_RE.match(os.path.basename(candidate)):
                chosen = _confine(project_root, candidate, sdir)
                break
        if chosen is None:
            reviews = _numbered(sdir, _REVIEW_RE)
            chosen = reviews[-1][1] if reviews else None
        if chosen is None:
            return [], ["review-N.md"]
        wanted = [chosen]
    for path in wanted:
        info = _lstat(path)
        if info is not None and stat.S_ISREG(info.st_mode):
            paths.append(path)
        else:
            missing.append(_rel(root, path))
    return paths, missing


def _rel(root: Path, path: Path) -> str:
    try:
        return os.path.relpath(str(path), str(root)).replace(os.sep, "/")
    except ValueError:
        return str(path)


# ---------------------------------------------------------------------------
# verdicts
# ---------------------------------------------------------------------------
#
# A review or critic response states its verdict once, in its Verdict section.
# The parser is an allowlist: it approves only a document in which the section
# holds the value and nothing else, the heading is the one visible Verdict
# heading, and every other structured statement of the verdict agrees.
# Anything else refuses. The rule catches an honest writer's formatting
# mistakes; it does not try to defeat a writer who sets out to mislead.

_LINE_BREAK_RE = re.compile("[\r\x0b\x0c\x1c-\x1e\x85  ]")
_BIDI_RE = re.compile("[؜‎‏‪-‮⁦-⁩]")
_CONTAINER_RE = re.compile(r"^(?:>[ \t]?|[-*+][ \t]+|\d{1,9}[.)][ \t]+)+")
_ATX_PREFIX_RE = re.compile(r"^#{1,6}(?:[ \t]|$)")
_TERMINATOR_RE = re.compile(r"^#{1,2}(?:[ \t]|$)")
_UNDER_RE = re.compile(r"^ {0,3}(?:=+|-+)[ \t]*$")
_BOLD_LINE_RE = re.compile(r"^(\*\*|__)\S.*\1:?$")
_HTML_BLOCK_RE = re.compile(r"^ {0,3}<(?:[A-Za-z/?]|![A-Za-z\[])")
_RAW_TEXT_RE = re.compile(
    r"<(?:script|style|textarea|title|xmp|plaintext|iframe|noscript|noembed|noframes|pre)(?![A-Za-z0-9-])",
    re.IGNORECASE,
)
_TAG_STRIP_RE = re.compile(r"<[^<>]{0,200}>")
_LABEL_RE = re.compile(
    r"^(?:[A-Za-z]+[ \t]+){0,2}[Vv][Ee][Rr][Dd][Ii][Cc][Tt][Ss]?[ \t]*(?:\([^()]{0,60}\)[ \t]*)?"
    r"(?::|=|[–—]|-[ \t]|[Ii][Ss][ \t])[ \t]*(.*)$"
)
_INLINE_LABEL_RE = re.compile(
    r"(?<![A-Za-z0-9])[Vv][Ee][Rr][Dd][Ii][Cc][Tt][Ss]?[ \t]*(?::|=|[–—-])?[ \t]*"
    r"([A-Z][A-Z_]{2,}(?![A-Za-z0-9_]).*)$"
)
_HEAD_CANON_RE = re.compile(r"^## Verdict(?:: ?([A-Z_]+))?$")
_VALUE_LINE_RE = re.compile(
    r"^(?:(\*\*|__|\*|_|`)([A-Z_]+)(?:\.\1|\1\.?)|`?<verdict>([A-Z_]+)</verdict>`?\.?|([A-Z_]+)\.?)$"
)
_THEMATIC_RE = re.compile(r"^([-*_])(?:[ \t]*\1){2,}[ \t]*$")
_UNDERLINE_RE = re.compile(r"^(?:=+|-+)[ \t]*$")
_FENCE_OPEN_RE = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")
_AO_LABEL_RE = re.compile(r"^(?:[A-Za-z]+[ \t]+){0,2}[A-Za-z]+[ \t]*:[ \t]*")
_FRONTMATTER_VERDICT_RE = re.compile(r"^verdict:[ \t]*(\"|'|)([A-Z_]+)\1[ \t]*$")
_WORD_CHAR_RE = re.compile(r"[A-Za-z0-9_]")
_LABEL_VALUE_RE = re.compile(r"^([A-Z_]+)(?![A-Za-z0-9])")
_NON_APPROVING_WORDS = {
    "CHANGES_REQUESTED": re.compile(r"(?<![a-z0-9])changes[ _-]requested(?![a-z0-9])"),
    "BLOCKED": re.compile(r"(?<![a-z0-9])blocked(?![a-z0-9])"),
    "REVISE": re.compile(r"(?<![a-z0-9])revise(?![a-z0-9])"),
}
_APPROVING_VALUES = frozenset({"PASS", "APPROVED"})
_DIMENSION_HEADING = "## Dimension Verdicts"
# The format kit's section tables for reviews, security reviews and critic
# responses (quoin/memory/format-kit.sections.json) are the source of these
# titles; a Verdict section may end only at one of them.
_SECTION_TITLES = frozenset(
    "## " + title
    for title in (
        "For human", "Summary", "Plan Compliance", "Spec Compliance", "Issues Found", "Integration Safety",
        "Test Coverage", "Risk Assessment", "Recommendations", "Dimension Verdicts", "Target", "Issues",
        "What's good", "Scorecard", "Findings", "Scope",
    )
)
_FOLD = {"0": "o", "1": "i", "3": "e", "7": "t", "l": "i", "|": "i", "!": "i"}
# Fast path for ASCII text: look-alike characters fold, letters stay, the rest drops.
_FOLD_TABLE = {
    code: (_FOLD.get(chr(code)) or (chr(code) if "a" <= chr(code) <= "z" else None)) for code in range(128)
}
_MARKUP_TABLE = {ord("*"): None, ord("`"): None}
_CONTAINER_STARTS = tuple(">-*+0123456789")

RECOVERY_SENTENCE = (
    "Edit the artifact so the Verdict section holds only the value and every other statement agrees, "
    "then run `quoin opencode adopt` (editing changes the evidence hash), or re-run the phase."
)

_REFUSALS = {
    "line-break": "the text holds a line-boundary character other than a line feed.",
    "bidi": "the text holds a bidirectional control character.",
    "undecodable": "the text holds bytes that did not decode.",
    "frontmatter-unclosed": "the frontmatter block is not closed.",
    "frontmatter-key": "a frontmatter key naming the verdict is not exactly `verdict: VALUE`.",
    "frontmatter-duplicate": "the frontmatter names the verdict twice.",
    "lookalike": "a line spells the verdict with look-alike characters.",
    "heading-count": "the document does not have exactly one Verdict heading.",
    "heading-spelling": "the Verdict heading is not spelled exactly `## Verdict` or `## Verdict: VALUE`.",
    "heading-value": "the Verdict heading names a value that is not allowed here.",
    "raw-text-before": "an HTML element that can swallow the page appears before the Verdict heading.",
    "html-block-before": "an HTML block appears before the Verdict heading.",
    "heading-hidden": "the Verdict heading sits inside a fence or an HTML comment.",
    "heading-not-after-blank": "the line before the Verdict heading is neither blank nor a rule.",
    "indented": "a line in the Verdict section is indented four or more columns.",
    "setext": "a line in the Verdict section is a heading underline.",
    "section-line": "a line in the Verdict section is not just the value.",
    "section-value": "the Verdict section holds a value that is not allowed here.",
    "value-count": "the Verdict section does not hold exactly one value.",
    "frontmatter-disagree": "the frontmatter verdict differs from the Verdict section.",
    "label-disagree": "a label line names the verdict but does not start with the value.",
    "value-line-elsewhere": "a line outside the Verdict section states a different value.",
    "approving-line-start": "an approving document has a line that starts with a non-approving value.",
    "approving-cell": "an approving document has a table cell that is a non-approving value.",
    "approving-heading": "an approving document has a heading that names a non-approving value.",
    "terminator": "the Verdict section ends at a heading that is not a standard section title.",
}


def _skeleton(text: str) -> str:
    """The letters a reader could take `text` to spell, used only to find
    mentions: look-alike digits and bars fold to letters, any other
    non-ASCII letter, digit or symbol becomes the wildcard `?`, the rest is
    dropped."""
    folded = text.casefold()
    if folded.isascii():
        return folded.translate(_FOLD_TABLE).replace("cl", "d")
    out: List[str] = []
    for ch in folded:
        if ch in _FOLD:
            out.append(_FOLD[ch])
        elif "a" <= ch <= "z":
            out.append(ch)
        elif not ch.isascii() and unicodedata.category(ch)[0] in "LNS":
            out.append("?")
    return "".join(out).replace("cl", "d")


def _upper_skeleton(text: str) -> str:
    """Like `_skeleton` for value words in headings: capitals stay, ASCII
    lower-case letters and digits become `.`, other letters, digits and
    symbols become `?`."""
    out: List[str] = []
    for ch in text:
        if "A" <= ch <= "Z":
            out.append(ch)
        elif ch.isascii():
            if "a" <= ch <= "z" or "0" <= ch <= "9":
                out.append(".")
        elif unicodedata.category(ch)[0] in "LNS":
            out.append("?")
    return "".join(out)


def _skeleton_pattern(word: str) -> "re.Pattern[str]":
    parts = []
    for ch in word.casefold().replace("_", "").replace("l", "i"):
        parts.append("(?:d|\\?)" if ch == "d" else "[%s?]" % ("i" if ch in "il" else ch))
    return re.compile("".join(parts))


def _upper_pattern(value: str) -> "re.Pattern[str]":
    return re.compile("".join("[%s?]" % ch for ch in value.replace("_", "")))


_VERDICT_SKELETON_RE = _skeleton_pattern("verdict")


def _plain(line: str) -> str:
    text = line.strip()
    if text[:1] in _CONTAINER_STARTS:
        text = _CONTAINER_RE.sub("", text)
    if "<" in text:
        text = _TAG_STRIP_RE.sub("", text)
    return text.translate(_MARKUP_TABLE).strip()


def _is_bold_line(line: str) -> bool:
    return _BOLD_LINE_RE.match(line.strip()) is not None


def _heading_shaped(lines: Sequence[str], index: int) -> bool:
    raw = lines[index].strip()
    text = _CONTAINER_RE.sub("", raw) if raw[:1] in _CONTAINER_STARTS else raw
    if _ATX_PREFIX_RE.match(text) or (text[:2].lower() == "<h" and text[2:3] in tuple("123456")):
        return True
    if _BOLD_LINE_RE.match(raw):
        return True
    return (
        bool(text)
        and index + 1 < len(lines)
        and _UNDER_RE.match(lines[index + 1]) is not None
        and _THEMATIC_RE.match(text) is None
    )


def _approving_scan(
    lines: Sequence[str], frontmatter_end: int, head: int, end: int, others: Sequence[str],
) -> Optional[Tuple[str, int]]:
    """For an approving result: the first line outside the section, the
    frontmatter and the dimension table that starts with, or whose table cell
    is, a non-approving value, or whose heading names one."""
    skip = set(range(head, end))
    for index in range(frontmatter_end, len(lines)):
        if lines[index].rstrip() == _DIMENSION_HEADING:
            stop = index + 1
            while stop < len(lines) and not _TERMINATOR_RE.match(lines[stop]):
                stop += 1
            skip.update(range(index, stop))
    for index in range(frontmatter_end, len(lines)):
        if index in skip:
            continue
        line = lines[index]
        text = _plain(line).lstrip("| \t_")
        label = _AO_LABEL_RE.match(text)
        candidates = (text, text[label.end():]) if label else (text,)
        for value in others:
            for candidate in candidates:
                if candidate.startswith(value) and not _WORD_CHAR_RE.match(candidate[len(value):len(value) + 1]):
                    return "approving-line-start", index + 1
        if "|" in line:
            stripped = line.strip()
            cells = stripped.split("|")
            if stripped.startswith("|"):
                cells = cells[1:]
            if stripped.endswith("|"):
                cells = cells[:-1]
            for cell in cells:
                if "<" in cell:
                    cell = _TAG_STRIP_RE.sub("", cell)
                if cell.strip(" \t*_`") in others:
                    return "approving-cell", index + 1
        if _heading_shaped(lines, index):
            folded = line.casefold()
            for value in others:
                if _NON_APPROVING_WORDS[value].search(folded):
                    return "approving-heading", index + 1
    return None


def _parse_verdict(text: str, allowed: Sequence[str]) -> Tuple[Optional[str], str, Optional[int]]:
    """`(value, reason, line)`: the allowed value the artifact states, or
    None with a fixed reason sentence and the 1-based number of the refusing
    line when one line refuses. The reason never quotes the artifact."""

    def refuse(code: str, line: Optional[int] = None) -> Tuple[None, str, Optional[int]]:
        return None, _REFUSALS[code], line

    text = text.replace("\r\n", "\n")
    for pattern, code in ((_LINE_BREAK_RE, "line-break"), (_BIDI_RE, "bidi")):
        found = pattern.search(text)
        if found:
            return refuse(code, text.count("\n", 0, found.start()) + 1)
    if "�" in text:
        return refuse("undecodable", text.count("\n", 0, text.index("�")) + 1)
    lines = text.split("\n")
    upper_values = [_upper_pattern(v) for v in allowed]

    frontmatter_end = 0
    frontmatter: List[str] = []
    if lines and lines[0].rstrip() == "---":
        for index in range(1, len(lines)):
            if lines[index].rstrip() in ("---", "..."):
                frontmatter_end = index + 1
                break
        if not frontmatter_end:
            return refuse("frontmatter-unclosed", 1)
        for index in range(1, frontmatter_end - 1):
            line = lines[index]
            if ":" in line and _VERDICT_SKELETON_RE.search(_skeleton(line.split(":", 1)[0])):
                match = _FRONTMATTER_VERDICT_RE.match(line)
                if not match:
                    return refuse("frontmatter-key", index + 1)
                frontmatter.append(match.group(2))
                if len(frontmatter) > 1:
                    return refuse("frontmatter-duplicate", index + 1)

    heads: List[int] = []
    labels: List[Tuple[int, "re.Match[str]"]] = []
    for index in range(frontmatter_end, len(lines)):
        line = lines[index]
        mention = _VERDICT_SKELETON_RE.search(_skeleton(line)) is not None
        shaped = _heading_shaped(lines, index)
        value_heading = (
            shaped and not _is_bold_line(line) and any(p.search(_upper_skeleton(line)) for p in upper_values)
        )
        if not mention and not value_heading:
            continue
        if line.rstrip() == _DIMENSION_HEADING:
            continue
        if mention and "verdict" not in line.casefold():
            return refuse("lookalike", index + 1)
        label = _LABEL_RE.match(_plain(line)) if mention else None
        if shaped and not (label and _is_bold_line(line)):
            heads.append(index)
            continue
        if label:
            labels.append((index, label))
            continue
        inline = _INLINE_LABEL_RE.search(_plain(line))
        if inline:
            labels.append((index, inline))
    if len(heads) != 1:
        return refuse("heading-count", heads[1] + 1 if len(heads) > 1 else None)
    head = heads[0]
    canon = _HEAD_CANON_RE.match(lines[head].rstrip())
    if not canon:
        return refuse("heading-spelling", head + 1)
    head_value = canon.group(1)
    if head_value is not None and head_value not in allowed:
        return refuse("heading-value", head + 1)

    for index in range(frontmatter_end, head):
        if _RAW_TEXT_RE.search(lines[index]):
            return refuse("raw-text-before", index + 1)
    fence: Optional[Tuple[str, int]] = None
    in_comment = False
    for index in range(frontmatter_end, head):
        line = lines[index]
        if fence:
            stripped = line.strip()
            if (
                len(line) - len(line.lstrip(" ")) <= 3
                and len(stripped) >= fence[1]
                and stripped == fence[0] * len(stripped)
            ):
                fence = None
            continue
        if not in_comment:
            opener = _FENCE_OPEN_RE.match(line)
            if opener and not (opener.group(1)[0] == "`" and "`" in opener.group(2)):
                fence = (opener.group(1)[0], len(opener.group(1)))
                continue
            if _HTML_BLOCK_RE.match(line) and not line.lstrip().startswith("<!--"):
                return refuse("html-block-before", index + 1)
        pos = 0
        while True:
            if in_comment:
                close = line.find("-->", pos)
                if close < 0:
                    break
                in_comment = False
                pos = close + 3
            else:
                open_at = line.find("<!--", pos)
                if open_at < 0:
                    break
                in_comment = True
                pos = open_at + 4
    if fence or in_comment:
        return refuse("heading-hidden", head + 1)
    if head > frontmatter_end and lines[head - 1].strip() and lines[head - 1].strip() != "---":
        return refuse("heading-not-after-blank", head + 1)

    end = len(lines)
    for index in range(head + 1, len(lines)):
        if _TERMINATOR_RE.match(lines[index]):
            end = index
            break
    values = set([head_value] if head_value else [])
    prev_blank = True
    for index in range(head + 1, end):
        raw = lines[index]
        if not raw.strip():
            prev_blank = True
            continue
        lead = raw[: len(raw) - len(raw.lstrip(" \t"))]
        if "\t" in lead or len(lead) >= 4:
            return refuse("indented", index + 1)
        stripped = raw.strip()
        if _UNDERLINE_RE.match(stripped) and not prev_blank:
            return refuse("setext", index + 1)
        if stripped.startswith("="):
            return refuse("setext", index + 1)
        if _THEMATIC_RE.match(stripped):
            prev_blank = False
            continue
        match = _VALUE_LINE_RE.match(stripped)
        if not match:
            return refuse("section-line", index + 1)
        value = match.group(2) or match.group(3) or match.group(4)
        if value not in allowed:
            return refuse("section-value", index + 1)
        values.add(value)
        prev_blank = False
    if len(values) != 1:
        return refuse("value-count", head + 1)
    (only,) = values

    if frontmatter and frontmatter[0] != only:
        return refuse("frontmatter-disagree", 1)
    for index, label in labels:
        match = _LABEL_VALUE_RE.match(label.group(1).strip())
        if not match or match.group(1) != only:
            return refuse("label-disagree", index + 1)
    others = [v for v in allowed if v != only]
    for index in range(frontmatter_end, len(lines)):
        if head <= index < end:
            continue
        match = _VALUE_LINE_RE.match(lines[index].strip())
        if match and (match.group(2) or match.group(3) or match.group(4)) in others:
            return refuse("value-line-elsewhere", index + 1)
    if only in _APPROVING_VALUES:
        found = _approving_scan(lines, frontmatter_end, head, end, others)
        if found:
            return refuse(found[0], found[1])
    if end < len(lines) and lines[end].rstrip() not in _SECTION_TITLES:
        return refuse("terminator", end + 1)
    return only, "", None


def parse_verdict(text: str, allowed: Sequence[str]) -> Optional[str]:
    """The verdict an artifact states, or None.

    The artifact must have one Verdict heading, `## Verdict` or
    `## Verdict: VALUE`, and no other heading that names the verdict or
    spells a value in capitals. The section holds only the value (bare, bold,
    in a code span or in a `<verdict>` tag, with at most one trailing period)
    and ends at a standard section heading or the end of the file. Every other
    structured statement of the verdict agrees with it: the frontmatter key,
    any line labelled "Verdict", and any other line that is just a value. In
    an approving document no line outside the section, the frontmatter and the
    dimension table starts with a non-approving value, no table cell is one,
    and no heading names one. Line breaks other than a line feed, bidirectional
    controls and an HTML block before the heading refuse. Prose belongs in
    another section. The rule catches formatting mistakes by an honest writer;
    it does not try to defeat one who sets out to mislead."""
    return _parse_verdict(text, allowed)[0]


def unparseable_detail(name: str, reason: str, line: Optional[int]) -> str:
    """The `verdict-unparseable` detail: the file name, the refusing line when
    there is one, the reason and the recovery."""
    where = "line %d: " % line if line else ""
    return "%s: %s%s %s" % (name, where, reason, RECOVERY_SENTENCE)


def critic_status(
    entry: Mapping[str, Any], origin: str, sdir: Path, settings: Mapping[str, Any], *, project_root=None,
) -> List[Tuple[str, str, str]]:
    """`(status, code, detail)` items for the plan's critic loop.

    Recorded responses must be `critic-response-N.md` files in the stage
    folder. A coordinator entry trusts only the responses it recorded; any
    other origin falls back to the responses on disk when none were recorded.
    No response is a refusal unless the settings say a critic is not required
    or the plan was adopted, which warns."""
    recorded = [r for r in (entry.get("critic_responses") or []) if isinstance(r, str)]
    if recorded:
        try:
            used = []
            for name in recorded:
                if not _CRITIC_RE.match(os.path.basename(name.replace("\\", "/"))):
                    raise PathUnresolved("a recorded critic response is not named critic-response-N.md")
                used.append(_confine(project_root or sdir, name, sdir))
        except PathUnresolved as exc:
            return [("FAIL", "path-unresolved", exc.detail)]
    elif origin == "coordinator":
        used = []
    else:
        used = [path for _n, path in _numbered(sdir, _CRITIC_RE)]
    if not used:
        if settings.get("critic_required") is False or origin == "adopted":
            return [("WARN", "critic-not-run", "no critic response was recorded")]
        return [("FAIL", "critic-missing", "no critic response was recorded for this plan")]
    out: List[Tuple[str, str, str]] = []
    cap = settings.get("max_critic_rounds")
    cap = cap if isinstance(cap, int) and not isinstance(cap, bool) and cap >= 1 else DEFAULT_MAX_CRITIC_ROUNDS
    text = _read_text(used[-1], MAX_TEXT_BYTES)
    if text is None:
        out.append(("FAIL", "artifact-missing", _rel(Path(project_root or sdir), used[-1])))
    else:
        verdict, reason, line = _parse_verdict(text, CRITIC_VERDICTS)
        if verdict is None:
            out.append(("FAIL", "verdict-unparseable", unparseable_detail(used[-1].name, reason, line)))
        elif verdict == "REVISE":
            out.append(("FAIL", "critic-not-converged", "the last critic response asks for a revision"))
    if len(used) > cap:
        out.append(("FAIL", "critic-not-converged", "%d critic rounds exceed the cap of %d" % (len(used), cap)))
    return out or [("PASS", "", "")]


# ---------------------------------------------------------------------------
# evaluation
# ---------------------------------------------------------------------------

CHECK_STATUSES = ("PASS", "FAIL", "WARN", "SKIP")
Item = Tuple[str, str, str]  # (status, code, detail)


@dataclass(frozen=True)
class Check:
    name: str
    status: str
    codes: Tuple[str, ...] = ()
    details: Tuple[str, ...] = ()
    warn_codes: Tuple[str, ...] = ()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name, "status": self.status, "codes": list(self.codes),
            "warn_codes": list(self.warn_codes), "details": list(self.details),
        }


@dataclass(frozen=True)
class GateResult:
    task: str
    stage: Optional[int]
    phase: str
    verdict: str
    checks: Tuple[Check, ...]
    reasons: Tuple[str, ...]
    warnings: Tuple[str, ...]
    origin: Optional[str]
    evidence_ref: Optional[Mapping[str, Any]]
    explanation: Optional[str]
    evaluated_at: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "task": self.task, "stage": self.stage, "phase": self.phase, "verdict": self.verdict,
            "checks": [c.to_dict() for c in self.checks], "reasons": list(self.reasons),
            "warnings": list(self.warnings), "origin": self.origin,
            "evidence_ref": dict(self.evidence_ref) if self.evidence_ref else None,
            "explanation": self.explanation, "evaluated_at": self.evaluated_at,
        }


def _check(name: str, items: Sequence[Item]) -> Check:
    """Fold a check's items into one result: any failure fails it, else any
    warning warns it, else it passes."""
    fails = [i for i in items if i[0] == "FAIL"]
    warns = [i for i in items if i[0] == "WARN"]
    status = "FAIL" if fails else ("WARN" if warns else "PASS")
    return Check(
        name, status,
        tuple(sorted({c for _s, c, _d in fails})),
        tuple(d for _s, _c, d in fails + warns if d),
        tuple(sorted({c for _s, c, _d in warns})),
    )


def _skip(name: str, why: str) -> Check:
    return Check(name, "SKIP", (), (why,))


def _fail(code: str, detail: str = "") -> Item:
    return ("FAIL", code, detail)


def adopt_command(task: str, stage: Optional[int], phase: str, project_root) -> str:
    stage_part = "" if stage is None else " --stage %d" % stage
    return "quoin opencode adopt --task %s%s --phase %s --project-root %s" % (
        shlex.quote(task), stage_part, shlex.quote(phase), shlex.quote(str(project_root)))


def _now_iso(clock: Callable[[], float]) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(clock()))


def _settings(state: Optional[Mapping[str, Any]]) -> Mapping[str, Any]:
    value = (state or {}).get("settings")
    return value if isinstance(value, Mapping) else {}


def _check_evidence(task, stage, phase, project_root, state_error, entry) -> Check:
    if state_error is not None:
        return _check("evidence", [_fail("state-invalid", state_error)])
    if entry is None:
        return _check("evidence", [_fail("evidence-missing", adopt_command(task, stage, phase, project_root))])
    snapshot = entry.get("evidence")
    if not evidence.well_formed(snapshot) or snapshot.get("coverage") != "full":
        return _check("evidence", [_fail("evidence-incomplete", "the recorded evidence does not cover every file")])
    return _check("evidence", [("PASS", "", "")])


def _check_run_outcome(task, phase, entry, directory) -> Check:
    origin = entry["origin"]
    if origin == "adopted":
        return _check("run-outcome", [("WARN", "run-evidence-absent", "adopted evidence has no recorded run")])
    if origin == "continuation":
        if entry.get("continuation_validation") != "PASS":
            return _check("run-outcome", [_fail("continuation-not-validated", "the continuation was not validated")])
        return _check("run-outcome", [("PASS", "", "")])
    runs = [r for r in (entry.get("runs") or []) if isinstance(r, str)]
    if not runs:
        return _check("run-outcome", [_fail("run-not-completed", "the entry lists no run")])
    items: List[Item] = []
    producers = 0
    for run_id in runs:
        try:
            record = runstore.load_record(directory, run_id) if directory is not None else None
        except runstore.RunStoreError as exc:
            items.append(_fail("run-not-completed", "%s: %s" % (run_id, exc.code)))
            continue
        why = _mismatch(record, task, entry.get("stage"), phase)
        if why is not None:
            items.append(_fail("run-not-completed", "%s: %s" % (run_id, why)))
            continue
        if runstore.normalize_phase(record["request"]["phase"]) in runstore.PLAN_PRODUCER_PHASES:
            producers += 1
        outcome = record.get("outcome") if isinstance(record.get("outcome"), Mapping) else {}
        if outcome.get("state") != "completed":
            items.append(_fail("run-not-completed", "%s: outcome is %s" % (run_id, outcome.get("state"))))
        elif outcome.get("evidence") != "full":
            items.append(_fail("run-evidence-partial", "%s: evidence is %s" % (run_id, outcome.get("evidence"))))
    if phase == "plan" and producers == 0:
        items.append(_fail("run-not-completed", "no plan or thorough_plan run is listed"))
    return _check("run-outcome", items or [("PASS", "", "")])


def _mismatch(record: Optional[Mapping[str, Any]], task: str, stage: Optional[int], phase: str) -> Optional[str]:
    """Why a run record is not a run of this entry, or None when it is."""
    if record is None:
        return "no run record"
    request = record.get("request")
    if record.get("task") != task or not isinstance(request, Mapping):
        return "task differs"
    try:
        if runstore.normalize_stage(request.get("stage")) != stage:
            return "stage differs"
    except ValueError:
        return "stage differs"
    if runstore.normalize_phase(request.get("phase")) not in runstore.RUN_PHASES_FOR[phase]:
        return "phase differs"
    return None


def _check_boundary(entry) -> Check:
    origin, boundary = entry["origin"], entry.get("boundary")
    if boundary == "violation":
        return _check("boundary", [_fail("boundary-violation", "a run touched a path it may not")])
    if boundary == "ok":
        return _check("boundary", [("PASS", "", "")])
    if origin == "coordinator":
        return _check("boundary", [_fail("boundary-unrecorded", "no boundary result was recorded")])
    return _check("boundary", [("WARN", "boundary-unverified", "no boundary result was recorded")])



def precheck(project_root, task: str, stage: Any, phase: str) -> Tuple[Optional[int], str]:
    """The normalized `(stage, phase)`, or `GateRefused` when the request cannot
    name a gated phase of an existing task. Phases accept the hyphen spelling
    the run path accepts, since both share one normalization rule."""
    phase = runstore.normalize_phase(phase)
    try:
        runstore.check_task_name(task)
    except runstore.RunStoreError:
        raise GateRefused("invalid-task-name") from None
    if phase not in runstore.GATED_PHASES:
        raise GateRefused("phase-not-gated")
    info = _lstat(Path(project_root) / ".workflow_artifacts" / task)
    if info is None or not stat.S_ISDIR(info.st_mode):
        raise GateRefused("task-missing")
    try:
        stage = runstore.normalize_stage(stage)
    except ValueError:
        raise GateRefused("invalid-stage") from None
    if stage is not None and phase in runstore.STAGELESS_PHASES:
        raise GateRefused("invalid-stage")
    return stage, phase


def evaluate(
    project_root, task: str, stage: Optional[int], phase: str, *, source_dir,
    explanation: Optional[str] = None,
    runner: Optional[runstore.GitRunner] = None,
    bytes_runner: Optional[runstore.GitBytesRunner] = None,
    clock: Callable[[], float] = time.time,
) -> GateResult:
    """Run every check for one gated phase and return the verdict.

    `explanation` is stored on the result after redaction and truncation; it is
    never passed to a check, so it cannot change the verdict. Raises
    `GateRefused("source-unavailable")` when a core script cannot be loaded,
    which can happen after the pre-checks (while resolving the stage folder or
    validating an envelope)."""
    stage, phase = precheck(project_root, task, stage, phase)

    state_error: Optional[str] = None
    state: Optional[Dict[str, Any]] = None
    directory: Optional[Path] = None
    try:
        directory = runstore.inspect_store(project_root)
        if directory is not None:
            state = runstore.load_workflow_state(directory, task)
    except runstore.RunStoreError as exc:
        state_error = exc.code
    except OSError as exc:
        state_error = type(exc).__name__
    entry = runstore.current_entry(state, stage, phase) if state is not None else None
    settings = _settings(state)

    checks: List[Check] = []
    checks.append(_check_evidence(task, stage, phase, project_root, state_error, entry))
    have = entry is not None and state_error is None
    snapshot = entry.get("evidence") if have else None

    checks.append(_check_run_outcome(task, phase, entry, directory) if have else _skip("run-outcome", "no evidence entry"))
    checks.append(_check_boundary(entry) if have else _skip("boundary", "no evidence entry"))

    tdir = Path(project_root) / ".workflow_artifacts" / task
    sdir: Optional[Path] = None
    path_error: Optional[str] = None
    try:
        sdir = stage_dir(project_root, task, stage, source_dir)
    except PathUnresolved as exc:
        path_error = exc.detail
    paths: List[Path] = []
    if have and path_error is None:
        try:
            paths, missing = expected_artifacts(project_root, task, stage, phase, entry, sdir)
        except PathUnresolved as exc:
            paths, missing, path_error = [], [], exc.detail
    if have and path_error is None:
        exist_items: List[Item] = [_fail("artifact-missing", m) for m in missing]
        checks.append(_check("artifacts-exist", exist_items or ([("PASS", "", "")] if paths else [])) if (paths or missing)
                      else _skip("artifacts-exist", "this phase has no required artifact"))
    elif have:
        checks.append(_check("artifacts-exist", [_fail("path-unresolved", path_error or "")]))
    else:
        checks.append(_skip("artifacts-exist", "no evidence entry"))

    if paths:
        invalid: List[Item] = []
        for path in paths:
            detail = run_validator(project_root, source_dir, path)
            if detail is not None:
                invalid.append(_fail("artifact-invalid", "%s: %s" % (_rel(Path(project_root), path), detail)))
        checks.append(_check("artifacts-valid", invalid or [("PASS", "", "")]))
    else:
        checks.append(_skip("artifacts-valid", "no artifact to validate"))

    if have:
        fresh = evidence.take_snapshot(project_root, task, phase, runner=runner, bytes_runner=bytes_runner, clock=clock)
        task_findings, repo_findings = evidence.compare(snapshot, fresh)
        checks.append(_check("artifact-hashes", [_fail(f.code, f.detail) for f in task_findings] or [("PASS", "", "")]))
        checks.append(_check("repo-state", [_fail(f.code, f.detail) for f in repo_findings] or [("PASS", "", "")]))
    else:
        checks.append(_skip("artifact-hashes", "no evidence entry"))
        checks.append(_skip("repo-state", "no evidence entry"))

    checks.append(_check_envelope(project_root, source_dir, entry, tdir) if have else _skip("envelope", "no evidence entry"))
    checks.append(_check_verdict(project_root, phase, entry, origin_of(entry), sdir, settings, paths) if have
                  else _skip("phase-verdict", "no evidence entry"))
    checks.append(_check_tests(phase, entry, settings) if have else _skip("tests", "no evidence entry"))
    checks.append(_check_ledger(entry) if have else _skip("ledger", "no evidence entry"))

    reasons = tuple(sorted({c for ch in checks if ch.status == "FAIL" for c in ch.codes}))
    warnings = tuple(sorted({c for ch in checks for c in ch.warn_codes}))
    verdict = "FAIL" if any(ch.status == "FAIL" for ch in checks) else "PASS"
    shown = None
    if explanation is not None:
        shown = _redact(explanation).encode("utf-8")[:MAX_EXPLANATION_BYTES].decode("utf-8", "ignore")
    ref = None
    if have:
        ref = {"recorded_at": entry.get("recorded_at"), "taken_at": (snapshot or {}).get("taken_at")}
    return GateResult(
        task=task, stage=stage, phase=phase, verdict=verdict, checks=tuple(checks), reasons=reasons,
        warnings=warnings, origin=entry.get("origin") if have else None, evidence_ref=ref,
        explanation=shown, evaluated_at=_now_iso(clock),
    )


def origin_of(entry: Optional[Mapping[str, Any]]) -> Optional[str]:
    return entry.get("origin") if entry else None


def _check_envelope(project_root, source_dir, entry, within) -> Check:
    recorded = entry.get("envelope_path")
    if not recorded:
        return _skip("envelope", "no envelope was recorded")
    try:
        path = _confine(project_root, recorded, within)
    except PathUnresolved as exc:
        return _check("envelope", [_fail("path-unresolved", exc.detail)])
    text = _read_text(path, MAX_ENVELOPE_BYTES)
    if text is None:
        return _check("envelope", [_fail("envelope-invalid", "the envelope file cannot be read")])
    try:
        messages = load_core(source_dir, "handoff_validate").validate(text, "return")
    except Exception:  # noqa: BLE001 - a validator that cannot run is a refusal
        return _check("envelope", [_fail("envelope-invalid", "validator-unavailable")])
    failures = [m for m in messages if m.startswith("FAIL")]
    if failures:
        return _check("envelope", [_fail("envelope-invalid", _redact(failures[0])[:300])])
    return _check("envelope", [("PASS", "", "")])


def _check_verdict(project_root, phase, entry, origin, sdir, settings, paths) -> Check:
    if phase == "plan":
        if sdir is None:
            return _skip("phase-verdict", "the stage folder is unresolved")
        items = critic_status(entry, origin or "", sdir, settings, project_root=project_root)
        return _check("phase-verdict", [(s, c, d) for s, c, d in items])
    if phase == "review":
        if not paths:
            return _skip("phase-verdict", "no review file")
        text = _read_text(paths[0], MAX_TEXT_BYTES)
        if text is None:
            verdict, reason, line = None, "the file could not be read.", None
        else:
            verdict, reason, line = _parse_verdict(text, REVIEW_VERDICTS)
        if verdict is None:
            detail = unparseable_detail(paths[0].name, reason, line)
            return _check("phase-verdict", [_fail("verdict-unparseable", detail)])
        if verdict != "APPROVED":
            return _check("phase-verdict", [_fail("review-not-approved", "the review verdict is %s" % verdict)])
        return _check("phase-verdict", [("PASS", "", "")])
    return _skip("phase-verdict", "this phase has no verdict file")


def _check_tests(phase, entry, settings) -> Check:
    if phase not in ("implement", "review"):
        return _skip("tests", "this phase does not run tests")
    if not settings.get("test_command"):
        return _check("tests", [("WARN", "tests-not-configured", "no test command is configured")])
    tests = entry.get("tests")
    code = tests.get("exit_code") if isinstance(tests, Mapping) else None
    if not isinstance(code, int) or isinstance(code, bool) or code != 0:
        return _check("tests", [_fail("tests-failed", "the recorded test run did not exit 0")])
    return _check("tests", [("PASS", "", "")])


def _check_ledger(entry) -> Check:
    if entry.get("ledger_lines_appended_during_run"):
        return _check("ledger", [("WARN", "ledger-appended-during-run", "the ledger grew while the run was active")])
    return _check("ledger", [("PASS", "", "")])


# ---------------------------------------------------------------------------
# audit file
# ---------------------------------------------------------------------------

import secrets as _secrets  # noqa: E402

MAX_CELL_CHARS = 300
_ID_SHAPE_RE = re.compile(r"\b[DTRFQS]-\d+\b")
_EVALUATOR_LINE = "evaluator: deterministic"


class GateArtifactError(Exception):
    """The audit file could not be written safely; `code` is a stable identifier."""

    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(message or code)
        self.code = code
        self.message = message or code


class GateArtifactConflict(GateArtifactError):
    """A file of the same name exists and was not written by this gate."""

    def __init__(self, message: str = "") -> None:
        super().__init__("gate-artifact-conflict", message or "an audit file written by another tool exists")


class GateArtifactInvalid(GateArtifactError):
    """The file just written does not pass strict validation."""

    def __init__(self, message: str = "") -> None:
        super().__init__("gate-artifact-invalid", message or "the written audit file failed validation")


def _yaml_string(value: str) -> str:
    """A double-quoted YAML scalar. A value holding a short-ID-shaped token has
    each hyphen escaped so the artifact validator's reference check does not
    read it as a reference."""
    quoted = json.dumps(value)
    if _ID_SHAPE_RE.search(value):
        quoted = quoted.replace("-", "\\x2D")
    return quoted


def _cell(text: str) -> str:
    cleaned = _redact(" ".join(str(text).split())).replace("`", "'").replace("|", "/")
    cleaned = cleaned[:MAX_CELL_CHARS].strip()
    return "`%s`" % (cleaned or "-")


def render_artifact(result: GateResult, *, date: str) -> str:
    stage = "null" if result.stage is None else str(int(result.stage))
    origin = "null" if result.origin is None else _yaml_string(result.origin)
    lines = [
        "---",
        "phase: %s" % _yaml_string(result.phase),
        "date: %s" % _yaml_string(date),
        "task: %s" % _yaml_string(result.task),
        "stage: %s" % stage,
        "verdict: %s" % _yaml_string(result.verdict),
        _EVALUATOR_LINE,
        "origin: %s" % origin,
        "---",
        "",
        "## Automated checks",
        "",
        "| Check | Status | Codes | Details |",
        "|---|---|---|---|",
    ]
    for item in result.checks:
        codes = ", ".join(list(item.codes) + list(item.warn_codes))
        lines.append("| %s | %s | %s | %s |" % (
            _cell(item.name), _cell(item.status), _cell(codes), _cell("; ".join(item.details))))
    passed = sum(1 for c in result.checks if c.status in ("PASS", "WARN"))
    lines += ["", "## Verdict", ""]
    if result.verdict == "PASS":
        lines.append("PASS (%d checks passed, %d warnings)" % (passed, len(result.warnings)))
    else:
        failing = sum(1 for c in result.checks if c.status == "FAIL")
        lines.append("FAIL (%d failing checks: %s)" % (failing, ", ".join(result.reasons)))
    if result.verdict == "FAIL":
        lines += ["", "## Failures requiring attention", ""]
        for number, code in enumerate(result.reasons, 1):
            details = "; ".join(d for c in result.checks if code in c.codes for d in c.details)
            lines.append("%d. %s: %s" % (number, _cell(code), _cell(details)))
    if result.warnings:
        lines += ["", "## Warnings (non-blocking)", ""]
        for code in result.warnings:
            details = "; ".join(d for c in result.checks if code in c.warn_codes for d in c.details)
            lines.append("- %s: %s" % (_cell(code), _cell(details)))
    if result.explanation is not None:
        lines += ["", "## Summary of what was produced", "",
                  "Explanation supplied with this gate (not evaluated by any check):", "", "```"]
        lines += ["    " + ln for ln in _redact(result.explanation).splitlines()]
        lines.append("```")
    return "\n".join(lines) + "\n"


def _checked_dir(project_root, sdir: Path) -> Path:
    """`sdir` made safe to write into: it must lie under the artifact root and
    no component may be a symlink."""
    root = Path(project_root)
    artifacts = root / ".workflow_artifacts"
    rel = os.path.relpath(str(sdir), str(artifacts))
    if rel == ".." or rel.startswith(".." + os.sep) or os.path.isabs(rel):
        raise GateArtifactError("unsafe-path", "the stage folder is outside the artifact root")
    current = artifacts
    for part in ["."] + [p for p in rel.split(os.sep) if p != "."]:
        if part != ".":
            current = current / part
        info = _lstat(current)
        if info is not None and stat.S_ISLNK(info.st_mode):
            raise GateArtifactError("unsafe-path", "a symlink lies on the audit file path")
    return current


def _written_by_gate(path: Path) -> bool:
    text = _read_prefix(path, 8 * 1024) if _lstat(path) else None
    if text is None:
        return False
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return False
    for line in lines[1:]:
        if line.strip() == "---":
            return False
        if line == _EVALUATOR_LINE:
            return True
    return False


def write_artifact(project_root, result: GateResult, sdir: Path, *, source_dir, clock: Callable[[], float] = time.time) -> Path:
    """Write `gate-PHASE-DATE.md` into the stage folder (the task root for a
    stage-less phase), replacing a file this writer made on the same day.
    The date is the UTC date; another runtime that names its audit file by
    local date may pick a different file name near midnight. Another tool's
    file of that name is never touched."""
    folder = _checked_dir(project_root, Path(sdir))
    date = time.strftime("%Y-%m-%d", time.gmtime(clock()))
    target = folder / ("gate-%s-%s.md" % (result.phase, date))
    info = _lstat(target)
    if info is not None:
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise GateArtifactError("unsafe-path", "the audit file path is not a regular file")
        if not _written_by_gate(target):
            raise GateArtifactConflict()
    os.makedirs(str(folder), exist_ok=True)
    tmp = folder / (".%s.%s.tmp" % (target.name, _secrets.token_hex(4)))
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(str(tmp), flags, 0o666)
    try:
        try:
            data = render_artifact(result, date=date).encode("utf-8")
            view = memoryview(data)
            while view:
                view = view[os.write(fd, view):]
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(str(tmp), str(target))
    except BaseException:
        try:
            os.unlink(str(tmp))
        except OSError:
            pass
        raise
    detail = run_validator(project_root, source_dir, target)
    if detail is not None:
        raise GateArtifactInvalid(detail)
    return target


def record_gate(
    project_root, task: str, stage: Optional[int], phase: str, result: GateResult, artifact_path,
    clock: Callable[[], float] = time.time,
) -> bool:
    """Store the verdict on the phase's current entry. False when the phase has
    no entry (a gate with no evidence still writes its audit file)."""
    directory = runstore.inspect_store(project_root)
    if directory is None:
        return False
    state = runstore.load_workflow_state(directory, task)
    if state is None:
        return False
    sha = hashlib.sha256(Path(artifact_path).read_bytes()).hexdigest()
    updated = runstore.update_current_entry(state, stage, phase, gate={
        "verdict": result.verdict, "reasons": list(result.reasons), "warnings": list(result.warnings),
        "artifact": _rel(Path(project_root), Path(artifact_path)), "artifact_sha256": sha,
        "evaluated_at": result.evaluated_at,
    })
    if updated is None:
        return False
    state["updated_at"] = _now_iso(clock)
    runstore.write_workflow_state(directory, state)
    return True
