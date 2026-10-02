Expected: three entries, one per "## " heading. Signals: per-entry Promote? tags (yes, maybe, no).

The insights file (insights-2026-05-13.md) uses the current writer format: each entry starts with a
"## <HH:MM> — <context>" heading, and entries may be separated by "---" rules. This is the format
/capture_insight and other writers produce today; the sibling fixtures use the legacy "### Insight N:"
format.

The fixture covers the parsing details of that format:
- The file preamble (title and description before the first "## " heading) is discarded.
- A "---" rule between entries is not part of the entry text.
- A nested "### " heading stays inside the entry that contains it.
- A "## " line inside a fenced code block is entry content, not an entry boundary.
- Each entry carries its own Promote? tag, so the three entries are tagged yes, maybe and no.

Tests call collect_entries(fixture_dir, scan_days=365).
