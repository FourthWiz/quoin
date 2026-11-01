#!/bin/sh
# userpromptsubmit.sh - UserPromptSubmit hook for quoin
# Deployed to ~/.claude/hooks/ by bash install.sh
#
# Run continuation sentinels only: a typed prompt takes control back by
# removing the session's run-continue arm file, and a typed /run or /run_hard records
# consent when a run-state record exists. The decision logic that reads these
# sentinels lives in scripts/auto_resume.py; this hook only maintains them.
#
# Contract: reads session_id, transcript_path, cwd and prompt from the stdin
# JSON. Never opens the transcript. No stdout on any path. Fail-OPEN: every
# error ends in exit 0. For payloads up to 8 KB the script is fork-free apart
# from one cat for stdin, an rm when the arm file exists and a touch on the
# consent path; a larger payload adds one awk pass that extracts the fields.
#
# The project root walk is an inline copy of resolve_project_root in _lib.sh
# (outermost project artifacts directory strictly below $HOME, else nearest
# .git, else the start dir). Sourcing the library would cost parse time on
# every prompt.
#
# Shebang assertion: head -1 ... | grep -qE '^#!/bin/sh( |$)'

STDIN=$(cat) || exit 0
exec >/dev/null 2>&1

# Byte semantics from here on. In a multibyte locale every prefix or suffix
# removal over the payload costs time proportional to its length, which made a
# large pasted prompt slow enough to hit the hook timeout before the arm file
# was removed. The session-id character class below is also ASCII-only this way.
LC_ALL=C
export LC_ALL

# field KEY [novalue]: _ok=1 and _r=<text after the opening quote of the first
# string value of "KEY">; unless a second argument is given, _v=<that value up
# to the next quote>. An occurrence of the key name that is not followed by a
# colon and a string (a value that happens to equal the key, a null, a number)
# is skipped in favour of the next occurrence. A missing key leaves _ok empty.
field() {
  _ok=""; _v=""; _r=""
  _s=$STDIN
  while :; do
    case "$_s" in *"\"$1\""*) ;; *) return 0 ;; esac
    _s=${_s#*"\"$1\""}
    _r=$_s
    while :; do case "$_r" in [[:space:]]*) _r=${_r#?} ;; *) break ;; esac; done
    case "$_r" in :*) _r=${_r#:} ;; *) continue ;; esac
    while :; do case "$_r" in [[:space:]]*) _r=${_r#?} ;; *) break ;; esac; done
    case "$_r" in \"*) _r=${_r#\"} ;; *) continue ;; esac
    break
  done
  [ -n "$2" ] || _v=${_r%%\"*}
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

if [ "${#STDIN}" -gt 8192 ]; then
  # Large payload: the builtin prefix and suffix removals above cost time
  # quadratic in the payload length or in how far a key sits from the start
  # (tens of seconds for 100 KB on macOS /bin/sh and on dash, whichever order
  # the keys come in), so one awk pass extracts the same fields instead. It keeps
  # the first usable occurrence of each key and the same 4160-blank bound.
  # JSON whitespace is spelled out as [ \t\r\n] because mawk 1.3.3 has no
  # POSIX bracket classes. Fail direction: if awk is missing or fails on a
  # payload over 8 KB, the fields come back empty, the session id check exits,
  # and the arm file is kept (awk is POSIX-required, so this is not expected).
  _x=$(printf '%s' "$STDIN" | awk 'BEGIN { RS = "\001" }
    function val(k,   r) {
      if (!match($0, "\"" k "\"[ \t\r\n]*:[ \t\r\n]*\"")) return ""
      r = substr($0, RSTART + RLENGTH); sub(/".*/, "", r); return "1" r
    }
    { s = val("session_id"); t = val("transcript_path"); c = val("cwd"); p = ""
      if (match($0, "\"prompt\"[ \t\r\n]*:[ \t\r\n]*\"")) {
        p = substr($0, RSTART + RLENGTH, 4224); b = p; sub(/^[ \t]*/, "", b)
        n = length(p) - length(b); if (n > 4160) n = 4160
        p = "1" substr(p, n + 1, 32); gsub(/\n/, " ", p)
      }
      printf "%s\n%s\n%s\n%s\n", s, t, c, p }')
  { IFS= read -r sid; IFS= read -r tp; IFS= read -r cwd; IFS= read -r ptxt; } <<EOT
$_x
EOT
  sid=${sid#1}; tp=${tp#1}; cwd=${cwd#1}
  _big=1
else
  _big=""
  field session_id; sid=$_v
  field transcript_path; tp=$_v
fi
case "$sid" in ''|*[!A-Za-z0-9-]*) exit 0 ;; esac
case "$tp" in */subagents/*) exit 0 ;; esac

if [ -n "$_big" ]; then
  _ok=""; [ -n "$ptxt" ] && _ok=big
  ptxt=${ptxt#1}
else
  field prompt novalue
  ptxt=""
fi
if [ "$_ok" = 1 ]; then
  # Leading blanks are dropped with a bounded number of steps (64 blocks of 64,
  # then up to 64 singles), so a prompt that is mostly whitespace cannot make
  # the hook slow. A prompt led by more than 4160 blanks (64 * 64 + 64) keeps
  # the rest and is then handled as a plain prompt: it still disarms, but neither
  # the /run consent nor the task-notification exemption applies (both fail
  # safe). The prompt text is only ever matched by case patterns and never
  # copied piecemeal.
  ptxt=$_r
  _n=0
  while [ "$_n" -lt 64 ]; do
    case "$ptxt" in
      [[:blank:]][[:blank:]][[:blank:]][[:blank:]][[:blank:]][[:blank:]][[:blank:]][[:blank:]][[:blank:]][[:blank:]][[:blank:]][[:blank:]][[:blank:]][[:blank:]][[:blank:]][[:blank:]][[:blank:]][[:blank:]][[:blank:]][[:blank:]][[:blank:]][[:blank:]][[:blank:]][[:blank:]][[:blank:]][[:blank:]][[:blank:]][[:blank:]][[:blank:]][[:blank:]][[:blank:]][[:blank:]][[:blank:]][[:blank:]][[:blank:]][[:blank:]][[:blank:]][[:blank:]][[:blank:]][[:blank:]][[:blank:]][[:blank:]][[:blank:]][[:blank:]][[:blank:]][[:blank:]][[:blank:]][[:blank:]][[:blank:]][[:blank:]][[:blank:]][[:blank:]][[:blank:]][[:blank:]][[:blank:]][[:blank:]][[:blank:]][[:blank:]][[:blank:]][[:blank:]][[:blank:]][[:blank:]][[:blank:]][[:blank:]]*) ptxt=${ptxt#????????????????????????????????????????????????????????????????} ;;
      *) break ;;
    esac
    _n=$((_n + 1))
  done
  _n=0
  while [ "$_n" -lt 64 ]; do
    case "$ptxt" in [[:blank:]]*) ptxt=${ptxt#?} ;; *) break ;; esac
    _n=$((_n + 1))
  done
fi
case "$ptxt" in
  '<task-notification>'*) exit 0 ;;
esac

[ -n "$_big" ] || { field cwd; cwd=$_v; }
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
  '/run'[[:space:]]*|'/run\'*|'/run"'*|'/run_hard'*)
    for _f in "$mem"/run-state-*.json; do
      if [ -e "$_f" ]; then touch "$mem/run-continue-consent-$sid.txt"; fi
      break
    done
    ;;
esac
exit 0
