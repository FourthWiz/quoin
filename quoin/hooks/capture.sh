#!/bin/sh
# capture.sh — settings-hook side of the task record (core/scripts/task_record.py).
#
# Usage: capture.sh <post-tool-use|post-bash|stop|user-prompt-submit|session-start>, with
# the hook input on stdin. SessionStart and UserPromptSubmit set the session
# title and the pointer to the brief in every install. PostToolUse and Stop open
# the record and journal each turn only where the capture mod is not loaded; the
# mod marks its sessions and task_record.py leaves those alone. PostToolUse on
# Bash records a PR opened with gh pr create and runs the discovery check after
# a commit; SessionStart also checks, in the background, whether the PRs of
# tasks in review have merged.
#
# Fail-OPEN: any error → exit 0.

root=$(cd "$(dirname "$0")/.." 2>/dev/null && pwd) || exit 0
# The system interpreter starts several times faster than a pyenv shim, and
# this runs on every prompt and every edit.
py=python3
[ -x /usr/bin/python3 ] && py=/usr/bin/python3
if [ "$1" = post-bash ]; then
    # Most shell calls are neither a PR nor a commit; skip Python for those.
    input=$(cat)
    case "$input" in
        *"gh pr create"*|*"git commit"*) ;;
        *) exit 0 ;;
    esac
    printf '%s' "$input" | "$py" "$root/core/scripts/task_record.py" hook "$1" 2>/dev/null
    exit 0
fi
"$py" "$root/core/scripts/task_record.py" hook "$1" 2>/dev/null
exit 0
