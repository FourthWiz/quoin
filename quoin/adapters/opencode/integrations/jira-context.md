## When to use

Use this skill when the task names a Jira issue key (for example `ENG-123`) or needs the status, description or discussion of related tickets.

## Syntax

```text
workctl jira issue ENG-123 --json
workctl jira search --query-file {{ARTIFACT_ROOT}}/<task>/jql.txt --limit 10 --json
workctl jira search --query-file {{ARTIFACT_ROOT}}/<task>/jql.txt --limit 10 --cursor <pagination.cursor> --json
```

The search query file holds one JQL expression, for example `project = ENG AND status != Done ORDER BY updated DESC`. The broker passes it to Jira unchanged, so write valid JQL; the file may hold at most 4000 characters.

## What comes back

- `jira issue`: `data.issue` (key, summary, status, type, priority, assignee, reporter, labels, created, updated, plain-text description, attachment metadata, `comment_total`, url) and `data.comments` (id, author, created, plain-text body). When the byte bound trims comments, the last ones in Jira's order are dropped and `pagination.omitted_items` says how many.
- `jira search`: `data.items`, one row per issue (key, summary, status, type, priority, assignee, updated, url). Fetch the full issue with `jira issue` only for the rows you need.
