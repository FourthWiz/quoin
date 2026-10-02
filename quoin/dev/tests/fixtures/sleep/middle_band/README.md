Expected: middle-band. One promote signal, one forget signal, no explicit Promote? tag.

The entry in insights-2026-04-15.md has text that is both somewhat cross-task (subprocess timeout
pattern appears in multiple contexts — mild cross_task signal) AND somewhat one-shot (specific to
one test environment — mild one_shot signal). With no explicit Promote? tag and balanced promote/forget
signals, the entry should land in the middle bucket.

The middle bucket is the fallback: everything that is neither clearly promote (promote_score >= 3 AND
forget_score <= 0) nor clearly forget (forget_score >= 2 AND promote_score <= 0) ends up here.

Tests call collect_entries(fixture_dir, scan_days=365).

Format note: the insights files here use the legacy "### Insight N:" entry-heading format, kept to
exercise that parsing path. The format writers produce today is "## <HH:MM> — <context>"; see
capture_insight_format/.
