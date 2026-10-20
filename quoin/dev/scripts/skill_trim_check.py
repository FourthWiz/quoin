#!/usr/bin/env python3
"""Check that a SKILL.md trim kept every mechanical invariant.

Dev tooling only: not deployed, no network, no model call. Stdlib only, Python
3.8 grammar. Compares each working-tree adapter SKILL.md with the same file at a
git ref and prints one line per violation:

    FAIL NAME: reason

Typical use (from the git root):

    .venv/bin/python quoin/dev/scripts/skill_trim_check.py                # every changed skill
    .venv/bin/python quoin/dev/scripts/skill_trim_check.py gate review    # named skills
    .venv/bin/python quoin/dev/scripts/skill_trim_check.py --ref 4dcd4873 --json

Hard invariants (exit 1): frontmatter, generated spans (section 0 family and
verification blocks), the section-0 overhang, heading lines, fenced code blocks,
HTML comment markers, in-domain caps-token count, the core pointer line, added
memory-doc references, and start_of_day staying untouched. Report-only items
(exit 0): inline-code spans that disappeared, added lines, removed bytes/lines.
Exit 2 on usage or git errors.
"""

from __future__ import annotations

import argparse
import collections
import importlib.util
import json
import re
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SRC_ROOT = HERE.parents[1]  # <git-root>/quoin
GIT_ROOT = SRC_ROOT.parent
DEFAULT_REF = "4dcd4873"
DEFAULT_SKILLS_SUBDIR = "quoin/adapters/claude/skills"
PROTECTED_UNCHANGED = frozenset({"start_of_day"})

CAPS_PATTERNS = [
    r"MUST(?! NOT)", r"ALWAYS", r"NEVER", r"CRITICAL",
    r"MUST NOT", r"REQUIRED", r"DO NOT", r"IMPORTANT",
]

MEMORY_REF_RE = re.compile(r"__QUOIN_HOME__/memory/([A-Za-z0-9_\-./]+\.md)(?:[^\"\n]{0,40}\"([^\"\n]+)\")?")
INLINE_CODE_RE = re.compile(r"`([^`\n]+)`")
COMMENT_RE = re.compile(r"<!--.*?-->", re.S)


class UsageError(Exception):
    pass


def _load_baseline():
    spec = importlib.util.spec_from_file_location("_adapter_baseline", str(HERE / "adapter_baseline.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_BASELINE = _load_baseline()


def anchor_regexes():
    """Heading-anchor regexes owned by the verification injector (never removable)."""
    path = SRC_ROOT / "scripts" / "inject_verification_step.py"
    if not path.is_file():
        return []
    spec = importlib.util.spec_from_file_location("_inject_verification_step", str(path))
    mod = importlib.util.module_from_spec(spec)
    sys.modules.setdefault("_inject_verification_step", mod)
    try:
        spec.loader.exec_module(mod)
    except Exception:
        return []
    found = []
    for value in vars(mod).values():
        if isinstance(value, dict):
            for inner in value.values():
                if isinstance(inner, dict):
                    for k, v in inner.items():
                        if k.endswith("anchor_regex") and isinstance(v, str):
                            found.append(v)
    return found


# ---------------------------------------------------------------------------
# text analysis
# ---------------------------------------------------------------------------

def split_frontmatter(text):
    lines = text.split("\n")
    if not lines or lines[0].strip() != "---":
        return "", text
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            return "\n".join(lines[:i + 1]), "\n".join(lines[i + 1:])
    return "", text


def fence_blocks(lines):
    """Return fenced blocks as strings (open fence through close fence, inclusive)."""
    blocks, cur, in_fence = [], [], False
    for line in lines:
        if line.lstrip().startswith("```"):
            if in_fence:
                cur.append(line)
                blocks.append("\n".join(cur))
                cur, in_fence = [], False
            else:
                in_fence = True
                cur = [line]
        elif in_fence:
            cur.append(line)
    if in_fence:
        blocks.append("\n".join(cur))
    return blocks


def headings(lines):
    out, in_fence = [], False
    for line in lines:
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
            continue
        if not in_fence and re.match(r"^#{1,6} ", line):
            out.append(line)
    return out


def generated_spans(lines):
    """Ordered list of generated span texts (section 0 family plus verification)."""
    s0, ver = _BASELINE.generated_span_lines(lines)
    idx = sorted(s0 | ver)
    spans, cur = [], []
    for i in idx:
        if cur and i != cur[-1] + 1:
            spans.append("\n".join(lines[k] for k in cur))
            cur = []
        cur.append(i)
    if cur:
        spans.append("\n".join(lines[k] for k in cur))
    return spans, s0


def overhang(lines):
    marker = next((i for i, l in enumerate(lines) if _BASELINE.SECTION0_END_MARKER in l), None)
    if marker is None:
        return None
    end = _BASELINE._next_h2(lines, marker)
    return "\n".join(lines[marker + 1:end])


def caps_count(lines, s0):
    text = "\n".join(l for i, l in enumerate(lines) if i not in s0)
    return sum(len(re.findall(p, text)) for p in CAPS_PATTERNS)


def memory_refs(text):
    return set((m.group(1), m.group(2)) for m in MEMORY_REF_RE.finditer(text))


def check_skill(name, before, after, memory_dir=None, allow_heading_removal=(), anchors=None):
    """Return (fails, report) for one skill. Pure function over the two texts."""
    fails, report = [], {}
    if anchors is None:
        anchors = anchor_regexes()
    b_lines, a_lines = before.split("\n"), after.split("\n")

    if name in PROTECTED_UNCHANGED and before != after:
        fails.append("%s must stay untouched in this trim" % name)

    if split_frontmatter(before)[0] != split_frontmatter(after)[0]:
        fails.append("frontmatter changed")

    b_spans, b_s0 = generated_spans(b_lines)
    a_spans, a_s0 = generated_spans(a_lines)
    if b_spans != a_spans:
        fails.append("generated spans changed (%d before, %d after, or content/order differs)" % (len(b_spans), len(a_spans)))

    if overhang(b_lines) != overhang(a_lines):
        fails.append("section-0 overhang changed")

    b_heads, a_heads = headings(b_lines), headings(a_lines)
    allowed = set(allow_heading_removal)
    kept_before = []
    for h in b_heads:
        if h.lstrip("# ").strip() in allowed or h in allowed:
            if any(re.search(rx, h) for rx in anchors):
                fails.append("heading matched by an injector anchor can never be removed: %s" % h)
                kept_before.append(h)
            continue
        kept_before.append(h)
    if kept_before != a_heads:
        gone = list((collections.Counter(kept_before) - collections.Counter(a_heads)).elements())
        new = list((collections.Counter(a_heads) - collections.Counter(kept_before)).elements())
        if gone or new:
            fails.append("headings changed: removed %s, added %s" % (gone[:3], new[:3]))
        else:
            fails.append("heading order changed")

    gone_fences = collections.Counter(fence_blocks(b_lines)) - collections.Counter(fence_blocks(a_lines))
    if gone_fences:
        first = next(iter(gone_fences))
        fails.append("fenced block removed or edited (%d affected), first starts: %s" % (
            sum(gone_fences.values()), first.split("\n", 2)[1][:60] if "\n" in first else first[:60]))

    gone_markers = collections.Counter(COMMENT_RE.findall(before)) - collections.Counter(COMMENT_RE.findall(after))
    new_markers = collections.Counter(COMMENT_RE.findall(after)) - collections.Counter(COMMENT_RE.findall(before))
    if gone_markers or new_markers:
        fails.append("HTML comment markers changed: lost %s, gained %s" % (
            list(gone_markers)[:2], list(new_markers)[:2]))

    cb, ca = caps_count(b_lines, b_s0), caps_count(a_lines, a_s0)
    if ca > cb:
        fails.append("in-domain caps-token count rose from %d to %d" % (cb, ca))

    pointer = "quoin/core/skills/%s.md" % name
    if pointer in before and pointer not in after:
        fails.append("core pointer %s dropped" % pointer)

    if memory_dir is not None:
        mem = Path(memory_dir)
        for fname, heading in sorted(memory_refs(after) - memory_refs(before), key=lambda x: (x[0], x[1] or "")):
            target = mem / fname
            if not target.is_file():
                fails.append("added memory reference to missing file %s" % fname)
                continue
            if heading:
                body = target.read_text(encoding="utf-8")
                if not any(l.startswith("#") and heading in l for l in body.split("\n")):
                    fails.append("added memory reference %s names missing heading \"%s\"" % (fname, heading))

    lost_code = collections.Counter(INLINE_CODE_RE.findall(before)) - collections.Counter(INLINE_CODE_RE.findall(after))
    b_count = collections.Counter(b_lines)
    added = [l for l in a_lines if l.strip() and not b_count.get(l)]
    report = {
        "dropped_inline_code": sorted(lost_code),
        "added_lines": added,
        "removed_bytes": len(before.encode("utf-8")) - len(after.encode("utf-8")),
        "removed_lines": len(b_lines) - len(a_lines),
        "caps_before": cb, "caps_after": ca,
    }
    return fails, report


# ---------------------------------------------------------------------------
# git plumbing and CLI
# ---------------------------------------------------------------------------

def git_show(git_root, ref, relpath):
    proc = subprocess.run(["git", "-C", str(git_root), "show", "%s:%s" % (ref, relpath)],
                          capture_output=True)
    if proc.returncode != 0:
        return None
    return proc.stdout.decode("utf-8")


def git_check_ref(git_root, ref):
    proc = subprocess.run(["git", "-C", str(git_root), "rev-parse", "--verify", "--quiet", ref + "^{commit}"],
                          capture_output=True)
    if proc.returncode != 0:
        raise UsageError("unknown git ref %r" % ref)


def run(args):
    git_root = Path(args.git_root)
    git_check_ref(git_root, args.ref)
    skills_dir = git_root / args.skills_subdir
    if not skills_dir.is_dir():
        raise UsageError("no skills directory at %s" % skills_dir)
    names = list(args.skills) or sorted(p.parent.name for p in skills_dir.glob("*/SKILL.md"))
    memory_dir = Path(args.memory_dir) if args.memory_dir else (git_root / "quoin" / "memory")
    if not memory_dir.is_dir():
        memory_dir = None
    results, fail_lines = {}, []
    for name in names:
        rel = "%s/%s/SKILL.md" % (args.skills_subdir, name)
        path = git_root / rel
        if not path.is_file():
            raise UsageError("no SKILL.md for %s" % name)
        after = path.read_text(encoding="utf-8")
        before = git_show(git_root, args.ref, rel)
        if before is None or before == after:
            continue
        fails, report = check_skill(name, before, after, memory_dir, args.allow_heading_removal)
        results[name] = {"fails": fails, "report": report}
        fail_lines.extend("FAIL %s: %s" % (name, f) for f in fails)
    return results, fail_lines


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("skills", nargs="*")
    ap.add_argument("--ref", default=DEFAULT_REF)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--allow-heading-removal", action="append", default=[], metavar="TEXT")
    ap.add_argument("--git-root", default=str(GIT_ROOT))
    ap.add_argument("--skills-subdir", default=DEFAULT_SKILLS_SUBDIR)
    ap.add_argument("--memory-dir", default=None)
    args = ap.parse_args(argv)
    try:
        results, fail_lines = run(args)
    except UsageError as exc:
        print("skill_trim_check: %s" % exc, file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps({"ref": args.ref, "skills": results, "failures": fail_lines}, indent=2, sort_keys=True))
    else:
        if not results:
            print("no changed skills")
        for line in fail_lines:
            print(line)
        for name, r in sorted(results.items()):
            rep = r["report"]
            print("%s: removed %d B / %d lines, added %d lines, caps %d -> %d, dropped inline-code spans: %d" % (
                name, rep["removed_bytes"], rep["removed_lines"], len(rep["added_lines"]),
                rep["caps_before"], rep["caps_after"], len(rep["dropped_inline_code"])))
            for span in rep["dropped_inline_code"]:
                print("  dropped-inline-code: `%s`" % span)
            for line in rep["added_lines"]:
                print("  added: %s" % line.strip()[:120])
    return 1 if fail_lines else 0


if __name__ == "__main__":
    sys.exit(main())
