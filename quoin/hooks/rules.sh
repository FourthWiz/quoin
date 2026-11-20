#!/bin/sh
# rules.sh — SessionStart hook that adds the short workflow rules to the session.
#
# The plugin cannot write ~/.claude/CLAUDE.md, so the rules arrive as
# additionalContext instead. Registered for startup, resume, clear and compact:
# a compaction or /clear drops earlier context, and CLAUDE.md would be re-read
# at those points too. The text is identical on every call, so it never
# invalidates the prompt cache between requests.
#
# Fail-OPEN: any error → exit 0, no output.

root=$(cd "$(dirname "$0")/.." 2>/dev/null && pwd) || exit 0
rules="$root/CLAUDE.slim.md"
[ -f "$rules" ] || exit 0

python3 - "$rules" "$root" <<'PY' 2>/dev/null || exit 0
import json, sys
path, root = sys.argv[1], sys.argv[2]
text = open(path, encoding="utf-8").read().replace("${CLAUDE_PLUGIN_ROOT}", root)
print(json.dumps({"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": text}}))
PY
exit 0
