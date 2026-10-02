Expected: forget. Signals: one_shot, stale_30days, user_marked_no.

The insights file (insights-2026-03-01.md) contains a one-shot, resolved issue with `**Promote?:** no`
(user_marked_no, weight 5). The file is dated 2026-03-01, which is >30 days before the test date
(stale_30days, weight 2) — but since tests use scan_days=365, the file is included in the scan.
The file mtime triggers stale_30days when the file is old enough. user_marked_no alone (weight 5) is
sufficient to reach forget_min_score=2.

Tests call collect_entries(fixture_dir, scan_days=365).

Format note: the insights files here use the legacy "### Insight N:" entry-heading format, kept to
exercise that parsing path. The format writers produce today is "## <HH:MM> — <context>"; see
capture_insight_format/.
