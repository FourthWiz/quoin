#!/bin/sh
# stop.sh — Stop hook: continues an interrupted /run in the session that armed it.
# Decision logic lives in scripts/auto_resume.py. Fail-OPEN: any error -> exit 0, no output.
_ar_lib="$(dirname "$0")/_lib.sh"
[ -r "$_ar_lib" ] || exit 0
. "$_ar_lib" 2>/dev/null || exit 0
root=$(resolve_project_root "$(pwd)" 2>/dev/null) || exit 0
[ -n "$root" ] && [ -d "$root/.workflow_artifacts/memory" ] || exit 0
ls "$root/.workflow_artifacts/memory"/run-continue-arm-*.txt >/dev/null 2>&1 || exit 0
command -v python3 >/dev/null 2>&1 || exit 0
python3 "$(dirname "$0")/../scripts/auto_resume.py" stop --project-root "$root" 2>/dev/null
exit 0
