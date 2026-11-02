## When to use

Use this skill when the task depends on an email the user received or sent: a request, a report, a decision or an attachment list.

## Syntax

```text
workctl mail search --query-file {{ARTIFACT_ROOT}}/<task>/mail-query.txt --limit 10 --json
workctl mail search --query-file {{ARTIFACT_ROOT}}/<task>/mail-query.txt --limit 10 --cursor <pagination.cursor> --json
workctl mail read <message id from a search result> --json
```

The query file holds the mail service's own search syntax: Gmail operators such as `from:finance@example.com subject:invoice newer_than:30d` for a Gmail backend, or plain search terms for Microsoft 365. Run `mail search` first and `mail read` only for the messages you need.

## What comes back

- `mail search`: `data.items` (id, thread id, from, to, subject, date, snippet; Microsoft 365 adds `has_attachments` and a url).
- `mail read`: `data.message` (from, to, cc, subject, date, body, attachment metadata). `body_format` says what the body is: `text`, `html-as-text` (HTML rendered to plain text) or `snippet` (no readable body part, only the service's preview).

Mail often contains personal data. Quote only what the task needs, never forward or store whole messages, and leave signatures, addresses and unrelated threads out of summaries and artifacts.
