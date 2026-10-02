#!/usr/bin/env python3
"""
sleep_score.py — importance scoring module for /sleep memory consolidation.

Scans daily insights files, scores each entry using promote/forget signals
from CLAUDE.md, and outputs NDJSON decisions for /sleep to act on.

No writes performed by this module — all writes are performed by the /sleep
SKILL.md body on user confirmation.

Public API (importable):
  load_config(claude_md_path: str = None, *, signals_yaml_path: str = None) -> dict
  collect_entries(scan_dir: str, scan_days: int = 30) -> List[RawEntry]
  score_entries(entries: List[RawEntry], config: dict) -> List[ScoredEntry]
  dedup_against_lessons(entries: List[ScoredEntry], lessons_text: str) -> List[ScoredEntry]
  RawEntry  (dataclass)
  ScoredEntry  (dataclass)

CLI:
  python3 sleep_score.py [--dry-run] [--scan-dir PATH] [--scan-days N]
                         [--lessons-file PATH] [--output json|text] [--help]
  Exit: 0 always (corpus-size warning emitted to stderr, not exit 1).
"""

import argparse
import fnmatch
import json
import os
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

# ---------------------------------------------------------------------------
# Hardcoded defaults — used when pyyaml absent or config block not found.
# These match the sleep_importance_signals YAML in quoin/CLAUDE.md exactly.
# NOTE: _source sentinel is intentionally ABSENT from hardcoded defaults so
# test_default_weights_present can distinguish a live YAML parse (has
# _source: claude_md) from the fallback (no _source key).
# ---------------------------------------------------------------------------
DEFAULT_CONFIG = {
    "promote": {
        "frequency_3plus": 3,
        "cross_task_2plus": 2,
        "cost_bearing": 2,
        "user_marked_yes": 5,
        "structural_fit": 1,
        "survival": 1,
    },
    "forget": {
        "one_shot": 2,
        "resolved_and_shipped": 2,
        "sub_threshold_cost": 1,
        "stale_30days": 2,
        "user_marked_no": 5,
        "duplicate": 3,
    },
    "thresholds": {
        "promote_min_score": 3,
        "promote_max_forget": 0,
        "forget_min_score": 2,
        "forget_max_promote": 0,
        "forget_quiet_floor": 4,
        "scan_window_days": 30,
        "cost_bearing_floor_usd": 0.50,
        "stale_days": 30,
    },
}


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class RawEntry:
    text: str
    source_path: str
    source_start_line: int
    source_end_line: int
    promote_tag: bool   # True if block contains "Promote?: yes" (case-insensitive)
    no_tag: bool        # True if block contains "Promote?: no" (case-insensitive)
    protected: bool = False  # S-3: True if source matches read_only or archived pattern


@dataclass
class ScoredEntry:
    text: str
    source_path: str
    source_lines: str   # f"{source_start_line}..{source_end_line}"
    promote_score: int
    forget_score: int
    bucket: str         # "promote" | "forget" | "middle"
    protected: bool = False  # S-3: carried from RawEntry; demotes "forget" → "middle"


# ---------------------------------------------------------------------------
# load_config
# ---------------------------------------------------------------------------

def load_config(claude_md_path: Optional[str] = None, *, signals_yaml_path: Optional[str] = None) -> dict:
    """Load sleep_importance_signals config.

    Search order:
    1. signals_yaml_path if explicitly provided — OR home default when claude_md_path is None
    2. Parse YAML block from claude_md_path (backward-compat fallback)
    3. Hardcoded DEFAULT_CONFIG (final fallback)

    Precedence rationale: if caller passes claude_md_path positionally (no signals_yaml_path),
    we try CLAUDE.md first to preserve backward-compat. The home-default YAML path is only
    consulted when no CLAUDE.md path is provided (typical CLI default case).

    Unknown keys in thresholds (including _source: claude_md) are silently
    ignored — load_config() uses .get() for all threshold lookups.

    Returns dict with keys: promote, forget, thresholds.
    """
    # Path 1: standalone YAML file
    if signals_yaml_path is not None:
        yaml_path = Path(signals_yaml_path)
    elif claude_md_path is None:
        # No CLAUDE.md path provided → try home default
        yaml_path = Path(os.path.expanduser("__QUOIN_HOME__/memory/sleep-signals.yaml"))
    else:
        yaml_path = None  # caller passed CLAUDE.md positionally → try CLAUDE.md first

    if yaml_path is not None and yaml_path.exists():
        try:
            import yaml  # pyyaml — soft dependency; imported inside body only
            parsed = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
            if isinstance(parsed, dict):
                signals = parsed.get("sleep_importance_signals", parsed)
                if isinstance(signals, dict):
                    return {
                        "promote": signals.get("promote", DEFAULT_CONFIG["promote"]),
                        "forget": signals.get("forget", DEFAULT_CONFIG["forget"]),
                        "thresholds": signals.get("thresholds", DEFAULT_CONFIG["thresholds"]),
                    }
        except ImportError:
            print(
                "[sleep_score: pyyaml not installed; trying CLAUDE.md fallback]",
                file=sys.stderr,
            )
        except Exception:
            pass  # Fall through to CLAUDE.md parse

    # Path 2: parse YAML block from claude_md_path (backward-compat fallback)
    if claude_md_path is not None:
        try:
            text = Path(claude_md_path).read_text(encoding="utf-8")
        except (OSError, IOError):
            return DEFAULT_CONFIG

        # Find the heading
        heading_pattern = r"### /sleep importance signals"
        heading_match = re.search(heading_pattern, text)
        if not heading_match:
            return DEFAULT_CONFIG

        # Find the ```yaml fenced block after the heading
        after_heading = text[heading_match.end():]
        fence_pattern = r"```yaml\s*\n(.*?)```"
        fence_match = re.search(fence_pattern, after_heading, re.DOTALL)
        if not fence_match:
            return DEFAULT_CONFIG

        yaml_text = fence_match.group(1)

        try:
            import yaml  # pyyaml — soft dependency; imported inside body only
            parsed = yaml.safe_load(yaml_text)
        except ImportError:
            print(
                "[sleep_score: pyyaml not installed; using hardcoded default weights]",
                file=sys.stderr,
            )
            return DEFAULT_CONFIG
        except Exception:
            return DEFAULT_CONFIG

        if not isinstance(parsed, dict):
            return DEFAULT_CONFIG

        # Extract the sleep_importance_signals sub-dict if nested
        signals = parsed.get("sleep_importance_signals", parsed)
        if not isinstance(signals, dict):
            return DEFAULT_CONFIG

        return {
            "promote": signals.get("promote", DEFAULT_CONFIG["promote"]),
            "forget": signals.get("forget", DEFAULT_CONFIG["forget"]),
            "thresholds": signals.get("thresholds", DEFAULT_CONFIG["thresholds"]),
        }

    # Path 3: hardcoded defaults
    return DEFAULT_CONFIG


# ---------------------------------------------------------------------------
# S-3: memory-maintenance pattern loader (duplicated from memory_check.py — Q-B)
# The YAML schema is the single source of truth; the ~15-line parser is duplicated
# here to avoid a new cross-script import (no core twin for sleep_score.py).
# ---------------------------------------------------------------------------

def _load_maintenance_patterns(pattern_file: Optional[str]) -> dict:
    """Load memory-maintenance.yaml pattern config.

    Stdlib-only line parser — no PyYAML. Tolerates comments and blank lines.
    Returns dict with keys archived, read_only, ignore (each a list of str globs).
    Returns all-empty lists when pattern_file is None, not found, or unreadable.
    """
    empty: dict = {"archived": [], "read_only": [], "ignore": []}
    if pattern_file is None:
        return empty
    try:
        text = Path(pattern_file).read_text(encoding="utf-8")
    except (OSError, IOError):
        return empty

    result: dict = {"archived": [], "read_only": [], "ignore": []}
    current_key: Optional[str] = None

    for raw_line in text.splitlines():
        comment_pos = raw_line.find("#")
        line = raw_line[:comment_pos].rstrip() if comment_pos >= 0 else raw_line.rstrip()
        if not line.strip():
            continue
        if not line[0].isspace() and line.rstrip().endswith(":"):
            key = line.rstrip().rstrip(":").strip()
            current_key = key if key in result else None
            continue
        stripped = line.lstrip()
        if stripped.startswith("- ") and current_key is not None:
            value = stripped[2:].strip().strip('"').strip("'")
            if value:
                result[current_key].append(value)

    return result


def _is_protected(basename: str, patterns: dict) -> bool:
    """Return True if basename matches read_only or archived (S-3 protected sources)."""
    for glob in patterns.get("archived", []):
        if fnmatch.fnmatch(basename, glob):
            return True
    for glob in patterns.get("read_only", []):
        if fnmatch.fnmatch(basename, glob):
            return True
    return False


def _is_ignored(basename: str, patterns: dict) -> bool:
    """Return True if basename matches the ignore list."""
    for glob in patterns.get("ignore", []):
        if fnmatch.fnmatch(basename, glob):
            return True
    return False


# ---------------------------------------------------------------------------
# collect_entries
# ---------------------------------------------------------------------------

def collect_entries(scan_dir: str, scan_days: int = 30,
                    patterns: Optional[dict] = None) -> List[RawEntry]:
    """Scan insights-*.md files and parse individual entries.

    Two-pass algorithm per D-07:

    Pass 1 (heading-based — canonical):
      Entries sit at the shallowest heading level the file uses:
      "## " if the file has any (the /capture_insight entry format), otherwise
      "### " if it has ≥2. Headings inside fenced code blocks are ignored.
      Discard the block before the first entry heading (preamble).
      Each entry heading + following text until the next entry heading
      (or EOF) = one entry; deeper headings stay inside their entry.

    Pass 2 (separator-based — fallback):
      If the file has no "## " and <2 "### " headings, split on lines
      matching ^---\\s*$. Strip surrounding blank lines from each block.

    Args:
        scan_dir: directory to scan for insights-*.md files.
        scan_days: only consider files modified within this many days.
        patterns: optional maintenance pattern dict from _load_maintenance_patterns().
                  ignore-matched files are skipped entirely; protected-source entries
                  have their ``protected`` field set to True (S-3).

    Returns:
        List of RawEntry objects.
    """
    _patterns = patterns or {}
    scan_path = Path(scan_dir)
    if not scan_path.exists():
        print(
            f"[sleep_score: scan_dir does not exist: {scan_dir}]",
            file=sys.stderr,
        )
        return []

    now = datetime.now(timezone.utc).timestamp()
    cutoff = now - (scan_days * 86400)

    entries: List[RawEntry] = []

    # Glob insights files in scan_dir (and one level deep)
    insight_globs = list(scan_path.glob("insights-*.md")) + list(
        scan_path.glob("*/insights-*.md")
    )

    for filepath in sorted(insight_globs):
        # S-3: skip ignore-matched source files entirely
        if _is_ignored(filepath.name, _patterns):
            continue

        try:
            mtime = filepath.stat().st_mtime
        except OSError:
            continue

        if mtime < cutoff:
            continue

        try:
            content = filepath.read_text(encoding="utf-8")
        except (OSError, IOError):
            continue

        abs_path = str(filepath.resolve())
        file_entries = _parse_file(content, abs_path)

        # S-3: mark entries from protected source files
        is_prot = _is_protected(filepath.name, _patterns)
        if is_prot:
            for e in file_entries:
                e.protected = True

        entries.extend(file_entries)

    return entries


def _parse_file(content: str, abs_path: str) -> List[RawEntry]:
    """Parse a single insights file into RawEntry objects."""
    lines = content.splitlines()

    h2_line_indices: List[int] = []
    h3_line_indices: List[int] = []
    in_fence = False
    for i, line in enumerate(lines):
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
        elif in_fence:
            # A heading quoted inside a code block is entry content, not a boundary.
            continue
        elif line.startswith("## "):
            h2_line_indices.append(i)
        elif line.startswith("### "):
            h3_line_indices.append(i)

    # Entries sit at the shallowest heading level the file uses. /capture_insight
    # and ad-hoc writers produce "## " entries; older files use "### ". A deeper
    # heading inside an entry belongs to that entry.
    if h2_line_indices:
        return _parse_heading_based(lines, abs_path, h2_line_indices)
    if len(h3_line_indices) >= 2:
        return _parse_heading_based(lines, abs_path, h3_line_indices)
    return _parse_separator_based(lines, abs_path)


def _parse_heading_based(lines: List[str], abs_path: str, heading_indices: List[int]) -> List[RawEntry]:
    """Pass 1: split on entry headings. Discard preamble before first heading."""
    entries: List[RawEntry] = []

    # Build blocks: each block starts at an entry heading and ends before the
    # next (or EOF). Preamble (before the first entry heading) is discarded.
    block_starts = heading_indices  # 0-based line indices of entry heading lines

    for i, start_idx in enumerate(block_starts):
        end_idx = block_starts[i + 1] - 1 if i + 1 < len(block_starts) else len(lines) - 1

        block_lines = lines[start_idx:end_idx + 1]
        # A "---" rule between entries is layout, not entry text. The source
        # line range still covers it so the whole block moves together.
        while block_lines and re.match(r"^(---)?\s*$", block_lines[-1]):
            block_lines = block_lines[:-1]
        text = "\n".join(block_lines).strip()

        if len(text) < 10:
            continue

        entry = _make_raw_entry(
            text=text,
            source_path=abs_path,
            source_start_line=start_idx + 1,  # 1-based
            source_end_line=end_idx + 1,       # 1-based
        )
        entries.append(entry)

    return entries


def _parse_separator_based(lines: List[str], abs_path: str) -> List[RawEntry]:
    """Pass 2: split on ^---\\s*$ lines (fallback for files with no entry headings)."""
    entries: List[RawEntry] = []

    # Find separator line indices
    sep_indices = [i for i, line in enumerate(lines) if re.match(r"^---\s*$", line)]

    if not sep_indices:
        # No separators — treat entire file as one block
        text = "\n".join(lines).strip()
        if len(text) >= 10:
            entry = _make_raw_entry(
                text=text,
                source_path=abs_path,
                source_start_line=1,
                source_end_line=len(lines),
            )
            entries.append(entry)
        return entries

    # Each segment is between consecutive separators
    for k in range(len(sep_indices) + 1):
        if k == 0:
            seg_start = 0
            seg_end = sep_indices[0]  # exclusive
        elif k <= len(sep_indices):
            sep_idx = sep_indices[k - 1]
            seg_start = sep_idx + 1
            seg_end = sep_indices[k] if k < len(sep_indices) else len(lines)
        else:
            break

        block_lines = lines[seg_start:seg_end]

        # Strip surrounding blank lines
        while block_lines and not block_lines[0].strip():
            block_lines = block_lines[1:]
            seg_start += 1
        while block_lines and not block_lines[-1].strip():
            block_lines = block_lines[:-1]
            seg_end -= 1

        text = "\n".join(block_lines).strip()
        if len(text) < 10:
            continue

        entry = _make_raw_entry(
            text=text,
            source_path=abs_path,
            source_start_line=seg_start + 1,  # 1-based
            source_end_line=seg_end,           # 1-based
        )
        entries.append(entry)

    return entries


def _make_raw_entry(text: str, source_path: str, source_start_line: int, source_end_line: int) -> RawEntry:
    """Create a RawEntry, extracting Promote? tags from the text.

    Matches both plain form (Promote?: yes) and markdown bold form (**Promote?:** yes).
    Case-insensitive. The tag may appear anywhere in the block.
    """
    # Match: optional leading **, then "Promote?:", optional **, then whitespace, then yes/no
    promote_tag = bool(re.search(r"(?i)\*{0,2}Promote\?:\*{0,2}\s*yes", text))
    no_tag = bool(re.search(r"(?i)\*{0,2}Promote\?:\*{0,2}\s*no", text))
    return RawEntry(
        text=text,
        source_path=source_path,
        source_start_line=source_start_line,
        source_end_line=source_end_line,
        promote_tag=promote_tag,
        no_tag=no_tag,
    )


# ---------------------------------------------------------------------------
# task-slug inference (module-level so the lessons guard can reuse it via CLI)
# ---------------------------------------------------------------------------

def _task_name_from_path(path: str) -> str:
    """Extract task slug from filename like insights-2026-04-25-task.md or
    sessions/2026-04-25-task.md.

    Module-level (IVG-119 T-12): consumed by both /sleep's append-heading synthesis
    and the lessons guard's --candidate-slug, so guard-slug == written-heading-slug.
    """
    basename = os.path.basename(path)
    # Remove insights- prefix and date portion
    m = re.match(r"insights-\d{4}-\d{2}-\d{2}-?(.*)\.md", basename)
    if m:
        return m.group(1) or "default"
    m = re.match(r"\d{4}-\d{2}-\d{2}-(.+)\.md", basename)
    if m:
        return m.group(1)
    return basename


# ---------------------------------------------------------------------------
# score_entries
# ---------------------------------------------------------------------------

def score_entries(entries: List[RawEntry], config: dict) -> List[ScoredEntry]:
    """Score a list of RawEntry objects and assign buckets.

    Accepts list[RawEntry] directly (per D-08). Computes source_lines
    = f"{entry.source_start_line}..{entry.source_end_line}" internally.

    Args:
        entries: list of RawEntry objects from collect_entries().
        config: dict from load_config() with promote/forget/thresholds keys.

    Returns:
        list of ScoredEntry objects with promote_score, forget_score, bucket.
    """
    promote_weights = config.get("promote", DEFAULT_CONFIG["promote"])
    forget_weights = config.get("forget", DEFAULT_CONFIG["forget"])
    thresholds = config.get("thresholds", DEFAULT_CONFIG["thresholds"])

    promote_min = thresholds.get("promote_min_score", 3)
    promote_max_forget = thresholds.get("promote_max_forget", 0)
    forget_min = thresholds.get("forget_min_score", 2)
    forget_max_promote = thresholds.get("forget_max_promote", 0)
    stale_days = thresholds.get("stale_days", 30)

    # Build source-path frequency map (for frequency_3plus signal)
    path_to_entry_texts: dict = {}
    for entry in entries:
        path_to_entry_texts.setdefault(entry.source_path, []).append(entry.text)

    # source_path encodes task name (e.g. "2026-04-25-auth-refactor.md");
    # _task_name_from_path is now module-level (IVG-119 T-12) so the lessons guard's
    # --slug-from-path yields the SAME slug that /sleep writes as an append heading.

    # Per-entry keyword sets for cross-file frequency check
    def _keywords(text: str) -> set:
        return set(w.lower() for w in re.findall(r"\b[a-zA-Z_-]{4,}\b", text))

    # Build a map: keyword -> set of source_paths containing it
    keyword_to_paths: dict = {}
    for entry in entries:
        for kw in _keywords(entry.text):
            keyword_to_paths.setdefault(kw, set()).add(entry.source_path)

    scored: List[ScoredEntry] = []
    for entry in entries:
        p_score = 0
        f_score = 0

        entry_keywords = _keywords(entry.text)
        entry_task = _task_name_from_path(entry.source_path)

        # --- Promote signals ---

        # frequency_3plus: entry's keywords appear in ≥3 distinct source files
        # (proxy: count how many files contain ≥3 of the same keywords as this entry)
        files_sharing_keywords = set()
        for kw in entry_keywords:
            files_sharing_keywords.update(keyword_to_paths.get(kw, set()))
        files_sharing_keywords.discard(entry.source_path)  # exclude self
        # Count files that share ≥3 keywords with this entry
        files_with_3plus = 0
        for other_path in files_sharing_keywords:
            other_entries = path_to_entry_texts.get(other_path, [])
            other_kws: set = set()
            for t in other_entries:
                other_kws.update(_keywords(t))
            overlap = entry_keywords & other_kws
            if len(overlap) >= 3:
                files_with_3plus += 1
        if files_with_3plus >= 2:  # self + 2 others = appears in ≥3 files
            p_score += promote_weights.get("frequency_3plus", 3)

        # user_marked_yes
        if entry.promote_tag:
            p_score += promote_weights.get("user_marked_yes", 5)

        # cost_bearing: MVP — always 0 (requires cost-ledger correlation)
        # p_score += 0

        # cross_task_2plus: keywords appear in ≥2 different task names
        task_names_with_overlap = set()
        for other_entry in entries:
            if other_entry.source_path == entry.source_path:
                continue
            other_task = _task_name_from_path(other_entry.source_path)
            if other_task == entry_task:
                continue
            other_kws = _keywords(other_entry.text)
            if len(entry_keywords & other_kws) >= 2:
                task_names_with_overlap.add(other_task)
        if len(task_names_with_overlap) >= 1:  # appears in ≥2 task contexts (self + 1 other)
            p_score += promote_weights.get("cross_task_2plus", 2)

        # structural_fit: text contains known taxonomy keywords
        taxonomy = {"v-04", "v-05", "v-06", "v-07", "integration", "hook", "dispatch"}
        text_lower = entry.text.lower()
        if any(kw in text_lower for kw in taxonomy):
            p_score += promote_weights.get("structural_fit", 1)

        # survival: MVP — always 0 (requires multi-day tracking)
        # p_score += 0

        # --- Forget signals ---

        # one_shot: entry's source file appears only once across all source paths
        all_paths = list(path_to_entry_texts.keys())
        if len(all_paths) <= 1:
            f_score += forget_weights.get("one_shot", 2)
        else:
            # Count how many OTHER files share ≥1 keyword with this entry
            files_with_any_overlap = sum(
                1 for p in all_paths
                if p != entry.source_path
                and len(entry_keywords & _keywords(" ".join(path_to_entry_texts[p]))) >= 1
            )
            if files_with_any_overlap == 0:
                f_score += forget_weights.get("one_shot", 2)

        # stale_30days: check file mtime via source_path.
        # Suppressed when user_marked_yes — an explicit promote vote overrides age.
        if not entry.promote_tag:
            try:
                mtime = os.path.getmtime(entry.source_path)
                age_days = (datetime.now(timezone.utc).timestamp() - mtime) / 86400
                if age_days > stale_days:
                    f_score += forget_weights.get("stale_30days", 2)
            except OSError:
                pass

        # user_marked_no
        if entry.no_tag:
            f_score += forget_weights.get("user_marked_no", 5)

        # duplicate: handled by dedup_against_lessons (post-scoring step)
        # resolved_and_shipped: MVP — always 0
        # sub_threshold_cost: MVP — always 0

        # --- Bucket decision ---
        bucket = _bucket(p_score, f_score, promote_min, promote_max_forget, forget_min, forget_max_promote)

        # S-3: carry protected flag; demote soft-forget → middle for protected sources
        if entry.protected and bucket == "forget":
            bucket = "middle"

        scored.append(ScoredEntry(
            text=entry.text,
            source_path=entry.source_path,
            source_lines=f"{entry.source_start_line}..{entry.source_end_line}",
            promote_score=p_score,
            forget_score=f_score,
            bucket=bucket,
            protected=entry.protected,
        ))

    return scored


def _bucket(p_score: int, f_score: int, promote_min: int, promote_max_forget: int,
            forget_min: int, forget_max_promote: int) -> str:
    """Assign bucket based on threshold comparisons."""
    if p_score >= promote_min and f_score <= promote_max_forget:
        return "promote"
    if f_score >= forget_min and p_score <= forget_max_promote:
        return "forget"
    return "middle"


# ---------------------------------------------------------------------------
# dedup_against_lessons
# ---------------------------------------------------------------------------

def dedup_against_lessons(entries: List[ScoredEntry], lessons_text: str) -> List[ScoredEntry]:
    """Filter out promote-bucket entries with ≥3-keyword overlap with lessons_text.

    Overlap check: tokenize each entry's text and each paragraph of lessons_text
    into lowercase word sets (≥4 chars). If the intersection has ≥3 words,
    the entry is considered a duplicate and removed from the promote list.

    Non-promote entries are returned unchanged.

    Args:
        entries: list of ScoredEntry objects.
        lessons_text: full text of lessons-learned.md.

    Returns:
        filtered list (dedup-suppressed promote entries removed).
    """
    def _kws(text: str) -> set:
        return set(w.lower() for w in re.findall(r"\b[a-zA-Z_-]{4,}\b", text))

    # Split lessons into paragraphs
    paragraphs = re.split(r"\n{2,}", lessons_text)
    lessons_kw_sets = [_kws(p) for p in paragraphs if len(p.strip()) >= 10]

    result = []
    for entry in entries:
        if entry.bucket != "promote":
            result.append(entry)
            continue

        entry_kws = _kws(entry.text)
        is_dup = any(len(entry_kws & lkws) >= 3 for lkws in lessons_kw_sets)
        if not is_dup:
            result.append(entry)
        # Else: drop (duplicate of existing lesson)

    return result


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _load_lessons_text(lessons_file: Optional[str]) -> str:
    if not lessons_file:
        return ""
    try:
        return Path(lessons_file).read_text(encoding="utf-8")
    except (OSError, IOError):
        return ""


def _entry_to_dict(entry: ScoredEntry) -> dict:
    return {
        "text": entry.text,
        "source_path": entry.source_path,
        "source_lines": entry.source_lines,
        "promote_score": entry.promote_score,
        "forget_score": entry.forget_score,
        "bucket": entry.bucket,
    }


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="sleep_score.py — importance scoring for /sleep memory consolidation.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Score and print decisions; make NO writes.",
    )
    parser.add_argument(
        "--scan-dir",
        default=".workflow_artifacts/memory/daily/",
        help="Directory to scan for insights-*.md files (default: .workflow_artifacts/memory/daily/).",
    )
    parser.add_argument(
        "--scan-days",
        type=int,
        default=30,
        help="Only consider files modified within last N days (default: 30).",
    )
    parser.add_argument(
        "--lessons-file",
        default=None,
        help="Path to lessons-learned.md for dedup check.",
    )
    parser.add_argument(
        "--output",
        choices=["json", "text"],
        default="json",
        help="Output format: json (NDJSON, default) or text (human-readable summary).",
    )
    parser.add_argument(
        "--claude-md",
        default=None,
        help=(
            "Path to CLAUDE.md for config. Search order: (1) __QUOIN_HOME__/memory/sleep-signals.yaml "
            "when --claude-md is omitted, (2) YAML block parsed from --claude-md path, "
            "(3) hardcoded defaults."
        ),
    )
    parser.add_argument(
        "--signals-yaml",
        default=None,
        help="Explicit path to sleep-signals.yaml (overrides --claude-md and home default).",
    )
    parser.add_argument(
        "--patterns",
        default=None,
        metavar="PATH",
        help=(
            "Path to memory-maintenance.yaml pattern file. "
            "When present, ignore-matched source files are skipped and "
            "read_only/archived sources are never soft-forgotten (S-3)."
        ),
    )

    parser.add_argument(
        "--slug-from-path",
        default=None,
        metavar="SRC",
        help=(
            "Utility sub-mode: print the task slug inferred from SRC (same inference "
            "that builds /sleep append headings) and exit 0. Used by lessons_guard.py "
            "to derive --candidate-slug."
        ),
    )

    args = parser.parse_args(argv)

    # Utility sub-mode: print the inferred slug and exit (no scan, no config).
    if args.slug_from_path is not None:
        print(_task_name_from_path(args.slug_from_path))
        return 0

    # Load config
    config = load_config(args.claude_md, signals_yaml_path=args.signals_yaml)

    # S-3: load maintenance patterns (always-pass; no-op when absent)
    maintenance_patterns = _load_maintenance_patterns(args.patterns)

    # Collect entries
    entries = collect_entries(args.scan_dir, args.scan_days, patterns=maintenance_patterns)

    # Corpus-size guard
    if len(entries) < 5:
        print(
            f"[sleep_score: corpus too thin ({len(entries)} entries); "
            "run after 30 days of production accumulation for calibration]",
            file=sys.stderr,
        )

    if not entries:
        return 0

    # Score entries
    scored = score_entries(entries, config)

    # Dedup promote candidates against lessons-learned
    lessons_text = _load_lessons_text(args.lessons_file)
    if lessons_text:
        scored = dedup_against_lessons(scored, lessons_text)

    # Output
    if args.output == "json":
        for entry in scored:
            print(json.dumps(_entry_to_dict(entry)))
    else:
        # Text summary
        promote = [e for e in scored if e.bucket == "promote"]
        forget = [e for e in scored if e.bucket == "forget"]
        middle = [e for e in scored if e.bucket == "middle"]

        def _preview(text: str, n: int = 80) -> str:
            first_line = text.split("\n")[0].strip()
            return first_line[:n] + ("..." if len(first_line) > n else "")

        if promote:
            print(f"\nPROMOTE ({len(promote)}):")
            for i, e in enumerate(promote, 1):
                print(f"  {i}. [score: P={e.promote_score}, F={e.forget_score}] {_preview(e.text)}")

        if forget:
            print(f"\nSOFT-FORGET ({len(forget)}):")
            for i, e in enumerate(forget, 1):
                print(f"  {i}. [score: P={e.promote_score}, F={e.forget_score}] {_preview(e.text)}")

        if middle:
            print(f"\nMIDDLE-BAND ({len(middle)}):")
            for i, e in enumerate(middle, 1):
                print(f"  {i}. [score: P={e.promote_score}, F={e.forget_score}] {_preview(e.text)}")

        if not scored:
            print("No entries scored.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
