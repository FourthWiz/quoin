Expected: promote. Signals: frequency_3plus (3 files), user_marked_yes. Entry: jq hook pattern.

The three insights files (insights-2026-04-01.md, insights-2026-03-15.md, insights-2026-03-01.md) all contain
the pattern "jq must be installed before hooks can parse stdin". The first file has `**Promote?:** yes`
(user_marked_yes signal, weight 5). All three files share the "jq hook" keyword cluster, triggering
frequency_3plus (weight 3) when scored together. Combined promote_score >= 3 (promote_min_score threshold).

Tests call collect_entries(fixture_dir, scan_days=365) to include all files regardless of mtime.

Format note: the insights files here use the legacy "### Insight N:" entry-heading format, kept to
exercise that parsing path. The format writers produce today is "## <HH:MM> — <context>"; see
capture_insight_format/.
