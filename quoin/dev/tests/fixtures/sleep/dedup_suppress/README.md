Expected: dedup-suppressed. Candidate overlaps >=3 keywords with lessons-learned-fixture.md.

The candidate entry in insights-2026-04-20.md (Insight 1) mentions "pyyaml", "import", "subprocess",
"function", "body" — a cluster of keywords that overlap >= 3 words with the existing lesson in
lessons-learned-fixture.md, which also mentions "pyyaml", "import", "subprocess", "function", "body".

After score_entries() buckets the entry as "promote" (due to user_marked_yes), dedup_against_lessons()
with the text of lessons-learned-fixture.md should filter it out — it is already documented.

Test reads lessons-learned-fixture.md text and passes it to dedup_against_lessons(scored, lessons_text).
Asserts the candidate is NOT in the returned list.

Format note: the insights files here use the legacy "### Insight N:" entry-heading format, kept to
exercise that parsing path. The format writers produce today is "## <HH:MM> — <context>"; see
capture_insight_format/.
