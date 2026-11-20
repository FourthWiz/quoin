#!/bin/sh
# capture.sh — settings-hook side of the task record (core/scripts/task_record.py).
#
# Usage: capture.sh <post-tool-use|stop|user-prompt-submit|session-start>, with
# the hook input on stdin. SessionStart and UserPromptSubmit set the session
# title and the pointer to the brief in every install. PostToolUse and Stop open
# the record and journal each turn only where the capture mod is not loaded; the
# mod marks its sessions and task_record.py leaves those alone.
#
# Fail-OPEN: any error → exit 0.

root=$(cd "$(dirname "$0")/.." 2>/dev/null && pwd) || exit 0
# The system interpreter starts several times faster than a pyenv shim, and
# this runs on every prompt and every edit.
py=python3
[ -x /usr/bin/python3 ] && py=/usr/bin/python3
"$py" "$root/core/scripts/task_record.py" hook "$1" 2>/dev/null
exit 0
