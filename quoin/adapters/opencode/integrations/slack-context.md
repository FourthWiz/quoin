## When to use

Use this skill when the task points at a Slack discussion (a channel id and thread timestamp, or a permalink) or needs to find what was said about an incident, feature or decision.

## Syntax

```text
workctl slack search --query-file {{ARTIFACT_ROOT}}/<task>/slack-query.txt --limit 10 --json
workctl slack search --query-file {{ARTIFACT_ROOT}}/<task>/slack-query.txt --limit 10 --cursor <pagination.cursor> --json
workctl slack thread C0123ABCD 1759400000.000100 --limit 50 --json
workctl slack thread C0123ABCD 1759400000.000100 --limit 50 --cursor <pagination.cursor> --json
```

The search query file holds Slack search syntax, for example `checkout double charge in:#payments after:2026-10-01`. A thread is addressed by its channel id (starts with C, G or D) and the parent message timestamp; a permalink ending in `/archives/C0123ABCD/p1759400000000100` maps to channel `C0123ABCD` and timestamp `1759400000.000100` (insert the dot before the last six digits). When the link carries `thread_ts=`, it points at a reply: use the `thread_ts` value as the thread timestamp.

## What comes back

- `slack search`: `data.items` (channel id and name, ts, user, text, permalink) and `pagination.total` when Slack reports it.
- `slack thread`: `data.messages` in thread order (ts, user or bot id, text, reply_count on the parent); `provenance.url` is the thread permalink when the broker knows the workspace URL.

User ids such as `U0123` are not names; do not guess who they are.
