#!/usr/bin/env python3
"""Comment cleanup tooling for Python repos: list candidates, apply deletions, prove equivalence.

Subcommands
  list   <repo> [--include DIR ...] [--exclude GLOB ...]   JSONL of comment blocks on stdout
  apply  <repo> --deletions FILE.json                       delete the given comment lines
  verify <repo> --base REF [--allow-non-py]                 removal-only proof, exit 1 on failure

Deletions file format: [{"file": "pkg/mod.py", "lines": [12, 13, 40]}, ...] with 1-based line
numbers of COMMENT tokens. A full-line comment line is removed entirely; a trailing comment is
stripped from its code line. Entries must be plain tracked .py paths inside the repo (no
absolute paths, `..` or symlinks); any other entry rejects the whole apply with nothing written. Protected comments are never removed: `apply` refuses them and
`verify` fails if one is gone. Files are read and written as bytes in their detected encoding, so
line endings, byte-order marks and coding declarations survive untouched.

The proof for each changed .py file (old = `git show BASE:file`, new = working tree):
  1. ast.dump(old) == ast.dump(new)               code and docstrings unchanged
  2. token stream without COMMENT/NL identical    no code text change
  3. comments(new) is a sub-multiset of comments(old)
  4. protected comments: same multiset in old and new
  5. same encoding and BOM, and every old line maps to a new line unchanged, comment-stripped,
     or dropped (full-line comment or blank line only)
  6. no file mode change
Added, deleted, renamed and copied files fail the proof, as do non-.py changes unless
--allow-non-py is passed. Untracked paths under mutation work dirs are counted and ignored.
"""
from __future__ import annotations

import argparse
import ast
import fnmatch
import importlib.util
import io
import json
import re
import subprocess
import sys
import tokenize
from collections import Counter
from pathlib import Path, PurePosixPath, PureWindowsPath

PROTECTED = [
    ("directive", re.compile(
        r"\bnoqa\b|type:\s*ignore|\bpragma\b|NOSONAR|\bnosec\b|pylint:|ruff:|fmt:\s*(on|off|skip)|"
        r"isort:|mypy:|pyright:|nolint|^#!|-\*-\s*coding|coding[:=]|vim:|^#\s*type:|"
        r"\bflake8\b|\bpytype\b|\byapf\b|skipcq|noinspection|sourcery\s+skip", re.I)),
    ("license", re.compile(r"copyright|licen[cs]e|spdx", re.I)),
    ("ticket-todo", re.compile(r"\b(TODO|FIXME|XXX|HACK)\b.*([A-Z][A-Z0-9]+-\d+|#\d+|https?://)", re.I)),
    ("url", re.compile(r"https?://")),
]

DEFAULT_EXCLUDES = [
    ".venv/*", "*/.venv/*", "build/*", "*.egg-info/*", ".claude/*", "*/__pycache__/*",
    "node_modules/*", "mutants/*", "*/mutants/*", ".mutmut-cache", ".mutmut-cache/*",
]

def is_mutation_workdir(rel: str) -> bool:
    return rel.startswith("mutants/") or rel == ".mutmut-cache" or rel.startswith(".mutmut-cache/")

_PARSE_ERRORS = (SyntaxError, tokenize.TokenError, UnicodeDecodeError, ValueError)


def protected_reason(text: str) -> str | None:
    for name, rx in PROTECTED:
        if rx.search(text):
            return name
    return None


def read_source(data: bytes) -> tuple[str, str, bool]:
    """Decode bytes without newline translation; return (text, encoding, has_bom)."""
    encoding, _ = tokenize.detect_encoding(io.BytesIO(data).readline)
    return data.decode(encoding), encoding, encoding == "utf-8-sig"


def split_lines(text: str) -> list[str]:
    # Breaks only on \n, \r\n and \r, the same boundaries tokenize counts, so list index
    # row - 1 always lines up with a token row. The built-in splitter also breaks on form
    # feed, U+2028 and others, which would shift rows.
    return io.StringIO(text, newline="").readlines()


def _line_ending(line: str) -> str:
    if line.endswith("\r\n"):
        return "\r\n"
    if line.endswith(("\n", "\r")):
        return line[-1]
    return ""


def _tokens(text: str):
    return tokenize.generate_tokens(io.StringIO(text, newline="").readline)


def comment_tokens(text: str):
    """Yield (row, col, string, is_full_line) for each COMMENT token."""
    lines = split_lines(text)
    for tok in _tokens(text):
        if tok.type == tokenize.COMMENT:
            row, col = tok.start
            yield row, col, tok.string, lines[row - 1][:col].strip() == ""


def code_tokens(text: str) -> list[tuple[int, str]]:
    skip = {tokenize.COMMENT, tokenize.NL}
    return [(t.type, t.string) for t in _tokens(text) if t.type not in skip]


def parses_as_code(text: str) -> bool:
    body = text.lstrip("#").strip()
    if not body:
        return False
    if not re.search(r"[=()\[\]{}]|^(import|from|return|def|class|if|for|while|raise|print)\b", body):
        return False
    try:
        ast.parse(body)
        return True
    except SyntaxError:
        if not body.endswith(":"):
            return False
        try:
            ast.parse(body + "\n    pass")
            return True
        except SyntaxError:
            return False


_LINT = None
_LINT_WARNED = False


def residue_hint(text: str, prefixes=()) -> str | None:
    """Best-effort process-vocabulary hint from the shared authored-content lint."""
    global _LINT, _LINT_WARNED
    try:
        if _LINT is None:
            here = Path(__file__).resolve().parent
            if str(here) not in sys.path:
                sys.path.insert(0, str(here))
            path = here / "authored_content_lint.py"
            spec = importlib.util.spec_from_file_location("_repo_comment_cleanup_lint", path)
            mod = importlib.util.module_from_spec(spec)
            sys.modules[spec.name] = mod
            spec.loader.exec_module(mod)
            _LINT = mod
        hit = _LINT.match_taxonomy(text, tuple(prefixes))
        return f"{hit[0]}:{hit[1]}" if hit else None
    except Exception as exc:  # hint is optional; never block listing
        if not _LINT_WARNED:
            _LINT_WARNED = True
            print(f"WARN residue hints unavailable: {exc}", file=sys.stderr)
        return None


def _git(repo: Path, *args: str) -> bytes:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, check=True).stdout


def iter_py_files(repo: Path, includes, excludes):
    roots = [repo / i for i in includes] if includes else [repo]
    raw = _git(repo, "ls-files", "-z", "--", "*.py")
    for name in sorted(n for n in raw.decode("utf-8", "surrogateescape").split("\0") if n):
        path = repo / name
        rel = Path(name).as_posix()
        if not any(path.is_relative_to(r) for r in roots):
            continue
        if any(fnmatch.fnmatch(rel, g) for g in excludes):
            continue
        yield path, rel


def cmd_list(args) -> int:
    repo = Path(args.repo).resolve()
    excludes = DEFAULT_EXCLUDES + (args.exclude or [])
    for path, rel in iter_py_files(repo, args.include, excludes):
        try:
            text, _, _ = read_source(path.read_bytes())
            ast.parse(text)
            comments = list(comment_tokens(text))
        except _PARSE_ERRORS as exc:
            print(f"SKIP {rel}: {type(exc).__name__}: {exc}", file=sys.stderr)
            continue
        lines = split_lines(text)
        block: list[tuple[int, int, str]] = []

        def emit(rec):
            rec["residue"] = residue_hint(rec["text"])
            print(json.dumps(rec))

        def flush():
            if not block:
                return
            start, end = block[0][0], block[-1][0]
            nxt = next((lines[i].strip() for i in range(end, min(end + 6, len(lines)))
                        if lines[i].strip()), "")
            emit({"file": rel, "start": start, "end": end, "kind": "full",
                  "text": "\n".join(b[2] for b in block),
                  "protected": sorted({r for b in block if (r := protected_reason(b[2]))}) or None,
                  "looks_like_code": any(parses_as_code(b[2]) for b in block),
                  "next_code": nxt[:160]})
            block.clear()

        prev_line = None
        for row, col, ctext, full in comments:
            if full and prev_line is not None and row == prev_line + 1 and block and block[-1][1] == col:
                block.append((row, col, ctext))
            else:
                flush()
                if full:
                    block.append((row, col, ctext))
                else:
                    emit({"file": rel, "start": row, "end": row, "kind": "trailing", "text": ctext,
                          "protected": [r] if (r := protected_reason(ctext)) else None,
                          "looks_like_code": parses_as_code(ctext),
                          "next_code": lines[row - 1][:col].strip()[:160]})
            prev_line = row if full else None
        flush()
    return 0


def containment_error(repo: Path, rel: object, tracked: set[str]) -> str | None:
    """Why a deletions-file path may not be written, or None when it is a plain tracked .py file."""
    if not isinstance(rel, str) or not rel:
        return "path must be a non-empty string"
    pure = PurePosixPath(rel)
    if pure.is_absolute() or PureWindowsPath(rel).is_absolute() or PureWindowsPath(rel).drive:
        return "absolute path"
    if ".." in pure.parts or ".." in PureWindowsPath(rel).parts:
        return "'..' component"
    if not rel.endswith(".py"):
        return "not a .py file"
    path = repo / rel
    try:
        resolved = path.resolve()
    except (OSError, RuntimeError) as exc:
        return f"cannot resolve ({exc})"
    if not resolved.is_relative_to(repo):
        return "resolves outside the repository"
    if resolved != repo.joinpath(*pure.parts):
        return "path goes through a symlink"
    if pure.as_posix() not in tracked:
        return "not tracked by git"
    return None


def entry_error(repo: Path, entry: object, tracked: set[str], seen: set[str]) -> str | None:
    """Why a deletions-file entry is unusable, or None. Records the normalized path in `seen`."""
    if not isinstance(entry, dict):
        return "entry must be an object with 'file' and 'lines'"
    why = containment_error(repo, entry.get("file"), tracked)
    if why:
        return why
    lines = entry.get("lines")
    if (not isinstance(lines, list) or not lines
            or not all(isinstance(n, int) and not isinstance(n, bool) and n > 0 for n in lines)):
        return "'lines' must be a non-empty list of positive integers"
    # Two spellings of one file would apply the original line numbers to already-edited text.
    norm = PurePosixPath(entry["file"]).as_posix()
    if norm in seen:
        return "duplicate entry for the same file"
    seen.add(norm)
    return None


def cmd_apply(args) -> int:
    repo = Path(args.repo).resolve()
    spec = json.loads(Path(args.deletions).read_text())
    if not isinstance(spec, list):
        print("ERROR rejected path deletions file must be a JSON list", file=sys.stderr)
        print("applied 0 comment deletions; 1 entries rejected, nothing written")
        return 1
    raw = _git(repo, "ls-files", "-z").decode("utf-8", "surrogateescape")
    tracked = {n for n in raw.split("\0") if n}
    seen: set[str] = set()
    rejected = [f"{entry.get('file') if isinstance(entry, dict) else entry!r}: {why}"
                for entry in spec
                if (why := entry_error(repo, entry, tracked, seen))]
    if rejected:
        for r in rejected:
            print("ERROR rejected path", r, file=sys.stderr)
        print(f"applied 0 comment deletions; {len(rejected)} entries rejected, nothing written")
        return 1
    errors: list[str] = []
    total = 0
    for entry in spec:
        rel = PurePosixPath(entry["file"]).as_posix()
        path = repo / rel
        try:
            text, encoding, _ = read_source(path.read_bytes())
            comments = {row: (col, ctext, full) for row, col, ctext, full in comment_tokens(text)}
        except (OSError, *_PARSE_ERRORS) as exc:
            errors.append(f"{rel}: cannot read or tokenize ({exc})")
            continue
        want = set(entry["lines"])
        missing = sorted(want - comments.keys())
        if missing:
            errors.append(f"{rel}: no comment on lines {missing}")
            continue
        prot = [(r, comments[r][1]) for r in sorted(want) if protected_reason(comments[r][1])]
        if prot:
            errors.append(f"{rel}: refusing protected comments {prot}")
            continue
        lines = split_lines(text)
        drop: set[int] = set()
        for row in sorted(want):
            col, _, full = comments[row]
            if full:
                drop.add(row - 1)
            else:
                lines[row - 1] = lines[row - 1][:col].rstrip() + _line_ending(lines[row - 1])
        # A removed full-line block between two blank runs keeps only the longer run.
        i, n = 0, len(lines)
        while i < n:
            if i in drop:
                j = i
                while j + 1 < n and (j + 1) in drop:
                    j += 1
                a = 0
                while i - 1 - a >= 0 and (i - 1 - a) not in drop and lines[i - 1 - a].strip() == "":
                    a += 1
                b = 0
                while j + 1 + b < n and (j + 1 + b) not in drop and lines[j + 1 + b].strip() == "":
                    b += 1
                if i - a == 0:
                    drop.update(range(j + 1, j + 1 + b))
                else:
                    drop.update(range(j + 1, j + 1 + min(a, b)))
                i = j + 1
            else:
                i += 1
        new_text = "".join(ln for k, ln in enumerate(lines) if k not in drop)
        path.write_bytes(new_text.encode(encoding))
        print(f"TOUCHED {rel}")
        total += len(want)
    for e in errors:
        print("ERROR", e, file=sys.stderr)
    print(f"applied {total} comment deletions in {len(spec) - len(errors)} files; {len(errors)} files refused")
    return 1 if errors else 0


def changed_files(repo: Path, base: str) -> list[list[str]]:
    out = _git(repo, "diff", "--name-status", "-z", base, "--").decode("utf-8", "surrogateescape").split("\0")
    rows: list[list[str]] = []
    i = 0
    while i < len(out) and out[i]:
        status = out[i]
        width = 2 if status[0] in "RC" else 1
        rows.append([status, *out[i + 1:i + 1 + width]])
        i += 1 + width
    untracked = _git(repo, "ls-files", "--others", "--exclude-standard", "-z")
    for name in untracked.decode("utf-8", "surrogateescape").split("\0"):
        if name:
            rows.append(["?", name])
    return rows


def mode_changed_paths(repo: Path, base: str) -> set[str]:
    out = subprocess.run(
        ["git", "-C", str(repo), "-c", "core.quotePath=false", "diff", "--summary", base, "--"],
        capture_output=True, text=True, check=True).stdout
    found = set()
    for line in out.split("\n"):
        m = re.match(r"^ mode change \d+ => \d+ (.+)$", line)
        if m:
            found.add(m.group(1))
    return found


def align_lines(old_text: str, new_text: str) -> tuple[bool, int, int]:
    """Check old lines map onto new lines by equal, comment strip, or drop of a comment or blank line.

    Returns (ok, first unmatched old line, first unmatched new line), both 1-based when not ok.
    The reachable set per old line keeps memory to one row.
    """
    old, new = split_lines(old_text), split_lines(new_text)
    trail: dict[int, int] = {}
    full: set[int] = set()
    for row, col, _, is_full in comment_tokens(old_text):
        if is_full:
            full.add(row - 1)
        else:
            trail[row - 1] = col
    droppable = {i for i, ln in enumerate(old) if i in full or ln.strip() == ""}
    reach = {0}
    # Every old line either survives or is dropped, so after line i the new-side index is at
    # least i + 1 minus the total number of drops; anything lower can never finish.
    spare = len(old) - len(new)
    for i, line in enumerate(old):
        stripped = line[:trail[i]].rstrip() + _line_ending(line) if i in trail else None
        nxt: set[int] = set()
        can_drop = i in droppable
        for j in reach:
            if can_drop:
                nxt.add(j)
            if j < len(new) and (new[j] == line or (stripped is not None and new[j] == stripped)):
                nxt.add(j + 1)
        if spare >= 0:
            nxt = {j for j in nxt if j >= i + 1 - spare}
        if not nxt:
            return False, i + 1, max(reach) + 1
        reach = nxt
    if len(new) in reach:
        return True, 0, 0
    return False, len(old) + 1, max(reach) + 1


def _check_file(old_bytes: bytes, new_bytes: bytes) -> tuple[str | None, int]:
    """Run checks 1-5 for one modified file; return (failure reason or None, comments removed)."""
    try:
        old, old_enc, old_bom = read_source(old_bytes)
        new, new_enc, new_bom = read_source(new_bytes)
        if ast.dump(ast.parse(old)) != ast.dump(ast.parse(new)):
            return "AST differs", 0
        if code_tokens(old) != code_tokens(new):
            return "code token stream differs", 0
        old_c = Counter(t for _, _, t, _ in comment_tokens(old))
        new_c = Counter(t for _, _, t, _ in comment_tokens(new))
    except _PARSE_ERRORS as exc:
        return f"syntax or decode error {exc}", 0
    added = new_c - old_c
    if added:
        return f"comments added or edited: {list(added)[:3]}", 0
    old_p = Counter(t for t in old_c.elements() if protected_reason(t))
    new_p = Counter(t for t in new_c.elements() if protected_reason(t))
    if old_p != new_p:
        return f"protected comments removed: {list(old_p - new_p)[:3]}", 0
    if (old_enc, old_bom) != (new_enc, new_bom):
        return "encoding or BOM changed", 0
    ok, o, n = align_lines(old, new)
    if not ok:
        return f"line alignment: first unmatched old line {o}, new line {n}", 0
    return None, sum((old_c - new_c).values())


def resolve_base(repo: Path, base: str) -> str | None:
    """Resolve a user-supplied base to a commit SHA; None when it is unsafe or unknown."""
    if base.startswith("-"):
        return None
    res = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "--verify", "--end-of-options", f"{base}^{{commit}}"],
        capture_output=True, text=True)
    return res.stdout.strip() if res.returncode == 0 and res.stdout.strip() else None


def _name_set(repo: Path, *args: str) -> set[str]:
    out = _git(repo, "diff", "--name-only", "-z", "--no-renames", *args).decode("utf-8", "surrogateescape")
    return {n for n in out.split("\0") if n}


def cmd_verify(args) -> int:
    repo = Path(args.repo).resolve()
    base = resolve_base(repo, args.base)
    if base is None:
        print(f"FAIL --base {args.base!r} is not a valid commit-ish", file=sys.stderr)
        return 2
    args.base = base
    failures: list[str] = []
    # The commit takes the index, the proof reads the working tree: a path staged differently
    # from its working copy would be committed unproven.
    staged = _name_set(repo, "--cached", base, "--")
    for rel in sorted(staged & _name_set(repo, "--")):
        failures.append(f"{rel}: index differs from working tree")
    checked = removed = ignored = 0
    modes: set[str] | None = None
    for row in changed_files(repo, args.base):
        status, rel = row[0], row[-1]
        if status == "?" and is_mutation_workdir(rel):
            ignored += 1
            continue
        if status == "?":
            status = "A"
        if not rel.endswith(".py"):
            if not args.allow_non_py:
                failures.append(f"{rel}: non-Python file changed ({status})")
            continue
        if status != "M":
            failures.append(f"{rel}: file {status} (only modifications allowed)")
            continue
        old_bytes = _git(repo, "show", f"{args.base}:{rel}")
        new_bytes = (repo / rel).read_bytes()
        checked += 1
        reason, n = _check_file(old_bytes, new_bytes)
        if reason is None:
            if modes is None:
                modes = mode_changed_paths(repo, args.base)
            if rel in modes:
                reason = "mode change"
        if reason:
            failures.append(f"{rel}: {reason}")
        else:
            removed += n
    for f in failures:
        print("FAIL", f)
    if ignored:
        print(f"IGNORED {ignored} mutation work paths")
    print(f"verify: {checked} files checked, {removed} comments removed, {len(failures)} failures")
    return 1 if failures else 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    pl = sub.add_parser("list", help="list comment blocks as JSONL")
    pl.add_argument("repo")
    pl.add_argument("--include", action="append")
    pl.add_argument("--exclude", action="append")
    pa = sub.add_parser("apply", help="delete comment lines named in a deletions file")
    pa.add_argument("repo")
    pa.add_argument("--deletions", required=True)
    pv = sub.add_parser("verify", help="prove the working tree only removed comments")
    pv.add_argument("repo")
    pv.add_argument("--base", required=True)
    pv.add_argument("--allow-non-py", action="store_true")
    args = p.parse_args(argv)
    return {"list": cmd_list, "apply": cmd_apply, "verify": cmd_verify}[args.cmd](args) or 0


if __name__ == "__main__":
    sys.exit(main())
