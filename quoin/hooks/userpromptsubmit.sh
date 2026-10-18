#!/bin/sh
# userpromptsubmit.sh - UserPromptSubmit hook for quoin
# Deployed to ~/.claude/hooks/ by bash install.sh
#
# Run continuation sentinels only: a typed prompt takes control back by
# removing the session's run-continue arm file, and a typed /run records
# consent when a run-state record exists. The decision logic that reads these
# sentinels lives in scripts/auto_resume.py; this hook only maintains them.
#
# Contract: reads session_id, transcript_path, cwd and prompt from the stdin
# JSON. Never opens the transcript. No stdout on any path. Fail-OPEN: every
# error ends in exit 0. The script is fork-free apart from one cat for stdin,
# an rm when the arm file exists and a touch on the consent path.
#
# The project root walk is an inline copy of resolve_project_root in _lib.sh
# (outermost .workflow_artifacts strictly below $HOME, else nearest .git, else
# the start dir). Sourcing the library would cost parse time on every prompt.
#
# Shebang assertion: head -1 ... | grep -qE '^#!/bin/sh( |$)'

STDIN=$(cat) || exit 0
exec >/dev/null 2>&1

# field KEY: _ok=1 and _v=<first string value of "KEY"> (up to the next
# quote), _r=<text after the opening quote>. Missing keys and non-string
# values (null, numbers, objects) leave _ok empty.
field() {
  _ok=""; _v=""; _r=""
  case "$STDIN" in *"\"$1\""*) ;; *) return 0 ;; esac
  _r=${STDIN#*"\"$1\""}
  while :; do case "$_r" in [[:space:]]*) _r=${_r#?} ;; *) break ;; esac; done
  case "$_r" in :*) _r=${_r#:} ;; *) return 0 ;; esac
  while :; do case "$_r" in [[:space:]]*) _r=${_r#?} ;; *) break ;; esac; done
  case "$_r" in \"*) _r=${_r#\"} ;; *) return 0 ;; esac
  _v=${_r%%\"*}
  _ok=1
}

# parent DIR: _p=<dirname of DIR>, same results as dirname(1) for the walk.
parent() {
  _p=$1
  while :; do case "$_p" in ?*/) _p=${_p%/} ;; *) break ;; esac; done
  case "$_p" in
    */*)
      _p=${_p%/*}
      while :; do case "$_p" in ?*/) _p=${_p%/} ;; *) break ;; esac; done
      [ -n "$_p" ] || _p=/
      ;;
    *) _p=. ;;
  esac
}

field session_id; sid=$_v
case "$sid" in ''|*[!A-Za-z0-9-]*) exit 0 ;; esac

field transcript_path
case "$_v" in */subagents/*) exit 0 ;; esac

field prompt
ptxt=$_r
while :; do case "$ptxt" in [[:blank:]]*) ptxt=${ptxt#?} ;; *) break ;; esac; done
[ -n "$_ok" ] || ptxt=""
case "$ptxt" in
  '<task-notification>'*) exit 0 ;;
esac

field cwd
cwd=$_v
[ -n "$cwd" ] || cwd=$PWD

ceil=${HOME:-/}
root=""
cur=$cwd
while [ -n "$cur" ]; do
  [ "$cur" = "$ceil" ] && break
  [ "$cur" = "/" ] && break
  [ -d "$cur/.workflow_artifacts" ] && root=$cur
  parent "$cur"
  [ "$_p" = "$cur" ] && break
  cur=$_p
done
if [ -z "$root" ]; then
  cur=$cwd
  while [ -n "$cur" ]; do
    [ "$cur" = "$ceil" ] && break
    [ "$cur" = "/" ] && break
    if [ -e "$cur/.git" ]; then root=$cur; break; fi
    parent "$cur"
    [ "$_p" = "$cur" ] && break
    cur=$_p
  done
fi
[ -n "$root" ] || root=$cwd

mem="$root/.workflow_artifacts/memory"
[ -n "$root" ] && [ -d "$mem" ] || exit 0

[ -e "$mem/run-continue-arm-$sid.txt" ] && rm -f "$mem/run-continue-arm-$sid.txt"

case "$ptxt" in
  '/run'[[:space:]]*|'/run\'*|'/run"'*)
    for _f in "$mem"/run-state-*.json; do
      if [ -e "$_f" ]; then touch "$mem/run-continue-consent-$sid.txt"; fi
      break
    done
    ;;
esac
exit 0
