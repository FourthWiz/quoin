Expected: --restore appends entry to insights-2026-03-15-source.md.

The forgotten/2026-03-15.md file contains one block with a > Source: anchor pointing to
insights-2026-03-15-source.md (which EXISTS in this fixture directory). The --restore path
should read the Source: anchor, confirm the original file exists, and append the entry text.

Note: the forgotten file uses a placeholder "FIXTURE_ABS_PATH" that tests must replace
with the actual absolute path to this fixture directory before use.

Format note: the insights files here use the legacy "### Insight N:" entry-heading format, kept to
exercise that parsing path. The format writers produce today is "## <HH:MM> — <context>"; see
capture_insight_format/.
