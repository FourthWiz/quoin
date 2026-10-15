# ============================================================
# Agent Desk helpers
# Ghostty/Zellij + Claude default + Codex on demand
# ============================================================

# Version of the public non-interactive interface (--panes, --no-prompt,
# --if-exists). Bumped when that interface changes. Consumers detect it with a
# static grep for '^AGENTDESK_API_VERSION=' rather than sourcing this file,
# because sourcing has side effects (it exports ZELLIJ_SOCKET_DIR).
AGENTDESK_API_VERSION=1

# IVG-135: work around upstream zellij socket-path length bug (#4211/#2817).
# zellij's default socket dir (which can resolve to macOS's long $TMPDIR,
# e.g. /var/folders/<hash>/<hash>/T/) can push the AF_UNIX socket path over
# its ~104-108 byte cap, producing the cryptic "session name must be less
# than 0 characters" error regardless of the actual session name length.
# NOTE: this export takes effect in EVERY interactive shell that sources
# agentdesk.zsh via ~/.zshrc, not only agentdesk invocations — see D-01 for
# the full blast-radius statement and the rejected per-function-scoping
# alternative. Never clobbers a pre-set ZELLIJ_SOCKET_DIR (guarded below),
# and only creates the directory it itself chose (never a user override).
if [ -z "${ZELLIJ_SOCKET_DIR:-}" ]; then
  _base="${XDG_RUNTIME_DIR:-/tmp}"
  export ZELLIJ_SOCKET_DIR="${_base}/zellij-agentdesk-$(id -u 2>/dev/null || echo 0)"
  mkdir -p "$ZELLIJ_SOCKET_DIR" 2>/dev/null || true
  unset _base
fi

_agentdesk_realpath() {
  python3 - "$1" <<'PY'
import os
import sys
print(os.path.realpath(os.path.abspath(os.path.expanduser(sys.argv[1]))))
PY
}

_agentdesk_session_name() {
  local root="$1"
  local name

  name="$(
    basename "$root" \
      | tr -c '[:alnum:]_.-' '-' \
      | sed 's/-\{2,\}/-/g' \
      | sed 's/^-//' \
      | sed 's/-$//'
  )"

  if [ -z "$name" ]; then
    name="agentdesk"
  fi

  echo "$name"
}

_sanitize_session_name() {
  local raw="$1"
  local name

  name="$(
    printf "%s" "$raw" \
      | tr -c '[:alnum:]_.-' '-' \
      | sed 's/-\{2,\}/-/g' \
      | sed 's/^-//' \
      | sed 's/-$//'
  )"

  echo "$name"
}

_zellij_session_exists() {
  local session_name="$1"

  zellij list-sessions 2>/dev/null \
    | sed 's/\x1b\[[0-9;]*m//g' \
    | awk '{print $1}' \
    | grep -Fxq "$session_name"
}

_agentdesk_next_session_name() {
  local base="$1"
  local existing
  existing="$(zellij list-sessions 2>/dev/null | sed 's/\x1b\[[0-9;]*m//g' | awk '{print $1}')"
  if ! printf '%s\n' "$existing" | grep -Fxq "$base"; then
    echo "$base"; return 0
  fi
  local n=1 candidate
  while [ "$n" -le 9999 ]; do
    candidate="${base}_${n}"
    if ! printf '%s\n' "$existing" | grep -Fxq "$candidate"; then
      echo "$candidate"; return 0
    fi
    n=$((n+1))
  done
  # bound exhausted — fall back to base_EPOCH so we never collide/hang
  echo "${base}_$(date +%s)"; return 0
}

_detect_repos() {
  if [ -z "${PROJECT_ROOT:-}" ]; then
    export PROJECT_ROOT="$PWD"
  fi

  local root
  root="$(_agentdesk_realpath "$PROJECT_ROOT")"

  if [ -e "$root/.git" ]; then
    echo "."
  fi

  find "$root" -mindepth 2 -maxdepth 4 \( -name .git -type d -o -name .git -type f \) 2>/dev/null \
    | while read -r git_entry; do
        repo_dir="$(dirname "$git_entry")"
        rel="${repo_dir#$root/}"

        if [ "$rel" != "." ] && [ -n "$rel" ]; then
          echo "$rel"
        fi
      done \
    | sort -u
}

_repo_path() {
  local repo="$1"

  if [ -z "${PROJECT_ROOT:-}" ]; then
    export PROJECT_ROOT="$PWD"
  fi

  if [ "$repo" = "." ]; then
    printf "%s\n" "$PROJECT_ROOT"
  else
    printf "%s\n" "$PROJECT_ROOT/$repo"
  fi
}

# ============================================================
# IVG-82: Layout persistence helpers
# State store: $HOME/.config/agentdesk/last-layout
# Key format: name:<sanitized-name> or proj:<percent-encoded-root>
# Value format: __FIXED__ (standard layout) or token1+token2+... (custom)
# ============================================================

_agentdesk_state_file() {
  printf '%s\n' "$HOME/.config/agentdesk/last-layout"
}

# Percent-encode every byte NOT in [A-Za-z0-9].
# Two-step: (1) urllib.parse.quote(safe="") encodes everything except alphanumerics;
# (2) post-process .replace(".","%2E").replace("~","%7E") force-encodes . and ~
# which Python's _ALWAYS_SAFE frozenset would otherwise leave unencoded regardless
# of safe=. Resulting charset: [A-Za-z0-9%_-] — no raw dots, tildes, or backslashes.
# Keys must be backslash-free (awk -v interprets C-escapes; this encoder guarantees it).
_agentdesk_encode_key() {
  local raw="$1"
  python3 -c 'import sys,urllib.parse; s=urllib.parse.quote(sys.argv[1],safe=""); print(s.replace(".","%2E").replace("~","%7E"))' "$raw"
}

# Derive the persistence key for a layout lookup/save.
# custom_name non-empty → name:<sanitized-name> namespace
# else               → proj:<encoded-project-root> namespace
# These are INDEPENDENT namespaces: a name: save never matches a proj: lookup.
_agentdesk_layout_key() {
  local custom_name="$1"
  local project_root="$2"
  if [ -n "$custom_name" ]; then
    printf 'name:%s\n' "$(_sanitize_session_name "$custom_name")"
  else
    printf 'proj:%s\n' "$(_agentdesk_encode_key "$project_root")"
  fi
}

# Set a value for a key in the state file (layout memory and dashboard answers
# share one file; keys are namespaced: name:, proj:, dash:name:, dash:proj:).
# Atomic write via temp file in same directory (same filesystem → atomic mv rename).
# Fail-open: any error → return 0.
# Keys must be backslash-free (guaranteed by encoder); awk -v interprets C-escapes.
_agentdesk_state_set() {
  local key="$1"
  local value="$2"
  local state_file
  state_file="$(_agentdesk_state_file)"
  local state_dir
  state_dir="$(dirname "$state_file")"

  mkdir -p "$state_dir" 2>/dev/null || return 0

  # Read existing content, filtering out any old line for this key (exact first-field match).
  local existing=""
  if [ -f "$state_file" ]; then
    # key must be backslash-free (guaranteed by encoder); awk -v interprets C-escapes.
    existing="$(awk -F'=' -v k="$key" '$1 != k' "$state_file" 2>/dev/null)" || existing=""
  fi

  # Write atomically: temp file in same dir so mv is atomic across same filesystem.
  local tmp_file
  tmp_file="$(mktemp "$state_dir/.last-layout.tmp.XXXXXX" 2>/dev/null)" || return 0

  {
    if [ -n "$existing" ]; then
      printf '%s\n' "$existing"
    fi
    printf '%s=%s\n' "$key" "$value"
  } > "$tmp_file" 2>/dev/null || { rm -f "$tmp_file" 2>/dev/null; return 0; }

  mv "$tmp_file" "$state_file" 2>/dev/null || { rm -f "$tmp_file" 2>/dev/null; return 0; }
  return 0
}

# Save a layout value for a key (wrapper kept for existing callers).
_agentdesk_save_layout() { _agentdesk_state_set "$@"; }

# Read the value for a key from the state file.
# Fail-open: file missing or key absent → echo nothing, return 1.
# key must be backslash-free (guaranteed by encoder); awk -v interprets C-escapes.
# Values may contain '='; sub() strips only the first '=' so values round-trip correctly.
_agentdesk_state_get() {
  local key="$1"
  local state_file
  state_file="$(_agentdesk_state_file)"

  if [ ! -f "$state_file" ]; then
    return 1
  fi

  local value
  # key must be backslash-free (guaranteed by encoder); value may contain '=';
  # sub(/^[^=]*=/,"") strips only up to the first '=', so a=b=c values round-trip correctly.
  value="$(awk -F'=' -v k="$key" '$1==k{sub(/^[^=]*=/,""); print; exit}' "$state_file" 2>/dev/null)"

  if [ -z "$value" ]; then
    return 1
  fi

  printf '%s\n' "$value"
  return 0
}

# Load a saved layout value for a key (wrapper kept for existing callers).
_agentdesk_load_layout() { _agentdesk_state_get "$@"; }

# Key under which the dashboard answer is remembered for a layout key
# (dash:proj:<enc> / dash:name:<name>). Exact-key readers of the layout keys
# never match these lines.
_agentdesk_dash_key() {
  printf 'dash:%s\n' "$1"
}

# Restore a saved layout value to a layout_path.
# __FIXED__ → echoes fixed agent-desk.kdl path, rc=0; caller must NOT rm.
# An optional second argument selects the bar mode (default or compact).
# token+token+... → validates tokens against whitelist, generates temp KDL, echoes path, rc=0.
# Invalid/empty → rc=1.
_agentdesk_layout_from_value() {
  local value="$1" bars="${2:-default}"

  if [ "$value" = "__FIXED__" ]; then
    printf '%s\n' "$HOME/.config/zellij/layouts/agent-desk.kdl"
    return 0
  fi

  # Split on '+' using zsh parameter expansion (NOT read -rA which splits on whitespace).
  # Token whitelist: claude|codex|shell|status|ccr|spend; '+' is a safe separator.
  local -a arr=("${(@s:+:)value}")

  if [ "${#arr[@]}" -eq 0 ]; then
    return 1
  fi

  local tok
  for tok in "${arr[@]}"; do
    case "$tok" in
      claude|codex|shell|status|ccr|spend) ;;
      *)
        return 1
        ;;
    esac
  done

  local gen_path
  gen_path="$(_agentdesk_gen_layout --bars "$bars" "${arr[@]}")" || return 1
  printf '%s\n' "$gen_path"
  return 0
}

# ============================================================
# _agentdesk_pane_cmd <token>
# Returns the zsh -lc command string for the given window type token.
# $PROJECT_ROOT and $HOME are NOT expanded here — they remain literal
# shell variable references for Zellij/zsh to evaluate at runtime.
# Inner double-quotes are emitted as \" so the string survives embedding
# in the KDL `args "-lc" "..."` field (matches the fixed agent-desk.kdl
# escaping). Single-quoted echo strings stay as literal single quotes.
# ============================================================
_agentdesk_pane_cmd() {
  local token="$1"
  case "$token" in
    claude)
      printf '%s' 'source \"$HOME/.config/agentdesk/agentdesk.zsh\" 2>/dev/null || true; cd \"$PROJECT_ROOT\" && echo '\''Claude Code - project root:'\'' \"$PWD\" && if command -v claude >/dev/null 2>&1; then claude; else echo '\''claude not found'\''; fi; zsh'
      ;;
    codex)
      printf '%s' 'source \"$HOME/.config/agentdesk/agentdesk.zsh\" 2>/dev/null || true; cd \"$PROJECT_ROOT\" && echo '\''Codex - project root:'\'' \"$PWD\" && if command -v codex >/dev/null 2>&1; then codex; else echo '\''codex not found'\''; fi; zsh'
      ;;
    shell)
      printf '%s' 'source \"$HOME/.config/agentdesk/agentdesk.zsh\" 2>/dev/null || true; cd \"$PROJECT_ROOT\" && echo '\''Shell'\''; zsh'
      ;;
    status)
      printf '%s' 'source \"$HOME/.config/agentdesk/agentdesk.zsh\" 2>/dev/null || true; cd \"$PROJECT_ROOT\" && python3 \"$HOME/.claude/scripts/status_graph.py\" --compact --watch'
      ;;
    spend)
      printf '%s' 'source \"$HOME/.config/agentdesk/agentdesk.zsh\" 2>/dev/null || true; cd \"$PROJECT_ROOT\" && python3 \"$HOME/.claude/scripts/spend_monitor.py\" --compact --watch'
      ;;
    ccr)
      # The store shape is the launch signal: CCR v3 has no `code`
      # subcommand, and `ccr -v` cannot be used to ask which version this is
      # because a version query migrates (and removes) the v2 config.json.
      printf '%s' 'source \"$HOME/.config/agentdesk/agentdesk.zsh\" 2>/dev/null || true; cd \"$PROJECT_ROOT\" && echo '\''CCR (OpenRouter) - project root:'\'' \"$PWD\" && if command -v ccr >/dev/null 2>&1; then if [ -f \"$HOME/.claude-code-router/config.sqlite\" ]; then ccr default-claude-code; else ccr code; fi; else echo '\''ccr not found'\''; fi; zsh'
      ;;
    dashboard)
      # Internal-only token, not offered by the layout picker. The launcher
      # normally injects the Dashboard tab itself (see the injection block in
      # agentdesk()) and passes the URL-file path to Zellij as a launch-scoped
      # environment value. This case supports `agentdesk claude shell dashboard`
      # for advanced users.
      # CO-UPDATE WARNING: if the command string changes (e.g. new flags), also
      # update _agentdesk_kdl_dashboard_tab.
      printf '%s' 'exec python3 \"$HOME/.claude/scripts/dashboard_server.py\" --no-browser --url-file \"$AGENTDESK_DASHBOARD_URL_FILE\"'
      ;;
  esac
}

# ============================================================
# _agentdesk_pane_name <token>
# Returns the display name for a KDL pane given a window type token.
# ============================================================
_agentdesk_pane_name() {
  local token="$1"
  case "$token" in
    claude)    printf '%s' 'Claude Code' ;;
    codex)     printf '%s' 'Codex' ;;
    shell)     printf '%s' 'Shell' ;;
    status)    printf '%s' 'Status' ;;
    spend)     printf '%s' 'Token Spend' ;;
    ccr)       printf '%s' 'CCR (OpenRouter)' ;;
    dashboard) printf '%s' 'quoin dashboard' ;;  # T-06: internal token
  esac
}

# ============================================================
# KDL emitters
#
# Pure stdout functions that build a generated layout piece by piece. They
# read no files and no environment, so the same arguments always give the
# same bytes. BARS is `default` (tab bar on top, status bar at the bottom) or
# `compact` (a single compact bar at the bottom); anything else is rejected
# with a message on stderr and status 2. Locals are declared once at the top
# of each function: zsh echoes `name=value` when a local is re-declared, and
# that would end up inside a redirected layout file.
# ============================================================
_agentdesk_kdl_check_bars() {
  case "$1" in
    default|compact) return 0 ;;
  esac
  printf 'agentdesk: invalid bar mode "%s" (expected default or compact)\n' "$1" >&2
  return 2
}

# _agentdesk_kdl_bar INDENT top|bottom BARS
_agentdesk_kdl_bar() {
  local ind="$1" where="$2" bars="$3" plugin
  if [ "$bars" = "compact" ]; then
    [ "$where" = "bottom" ] || return 0
    plugin="zellij:compact-bar"
    printf '%*spane size=1 borderless=true {\n' "$ind" ''
  elif [ "$where" = "top" ]; then
    plugin="zellij:tab-bar"
    printf '%*spane size=1 borderless=true {\n' "$ind" ''
  else
    plugin="zellij:status-bar"
    printf '%*spane size=2 borderless=true {\n' "$ind" ''
  fi
  printf '%*splugin location="%s"\n' "$((ind + 4))" '' "$plugin"
  printf '%*s}\n' "$ind" ''
}

# _agentdesk_pane_names TOKEN...
# One display name per line; spend tokens are skipped. A token that occurs
# once keeps its base name. Repeated agents get a letter by occurrence order
# (Claude · A, Claude · B), repeated utilities a number (Shell 1, Shell 2).
_agentdesk_pane_names() {
  local tok base n seen
  local -a toks=()
  local -a letters=(A B C D E F G H I J K L M N O P Q R S T U V W X Y Z)
  local -A total=() counter=()
  for tok in "$@"; do
    [ "$tok" = "spend" ] && continue
    toks+=("$tok")
    total[$tok]=$(( ${total[$tok]:-0} + 1 ))
  done
  for tok in "${toks[@]}"; do
    if [ "${total[$tok]}" -le 1 ]; then
      _agentdesk_pane_name "$tok"
      printf '\n'
      continue
    fi
    seen=$(( ${counter[$tok]:-0} + 1 ))
    counter[$tok]=$seen
    case "$tok" in
      claude|codex|ccr)
        case "$tok" in
          claude) base="Claude" ;;
          codex)  base="Codex" ;;
          ccr)    base="CCR" ;;
        esac
        if [ "$seen" -le 26 ]; then
          n="${letters[$seen]}"
        else
          n="$seen"
        fi
        printf '%s · %s\n' "$base" "$n"
        ;;
      *)
        printf '%s %s\n' "$(_agentdesk_pane_name "$tok")" "$seen"
        ;;
    esac
  done
}

# _agentdesk_kdl_header BARS
# Opens the layout. Zellij drops a tab's hide_floating_panes when a tab uses
# default_tab_template, so the main tab spells its bars out itself. The
# new_tab_template keeps bars on tabs opened later, and the named
# agentdesk_bars template is what the swap layouts use.
_agentdesk_kdl_header() {
  local bars="$1"
  _agentdesk_kdl_check_bars "$bars" || return 2
  printf 'layout {\n'
  printf '    new_tab_template {\n'
  _agentdesk_kdl_bar 8 top "$bars"
  printf '        pane\n'
  _agentdesk_kdl_bar 8 bottom "$bars"
  printf '    }\n'
  printf '    tab_template name="agentdesk_bars" {\n'
  _agentdesk_kdl_bar 8 top "$bars"
  printf '        children\n'
  _agentdesk_kdl_bar 8 bottom "$bars"
  printf '    }\n'
  printf '\n'
}

# _agentdesk_kdl_tab_open BARS NAME [PROPS]
_agentdesk_kdl_tab_open() {
  local bars="$1" tab_name="$2" props="${3:-}"
  _agentdesk_kdl_check_bars "$bars" || return 2
  if [ -n "$props" ]; then
    printf '    tab name="%s" %s {\n' "$tab_name" "$props"
  else
    printf '    tab name="%s" {\n' "$tab_name"
  fi
  _agentdesk_kdl_bar 8 top "$bars"
}

# _agentdesk_kdl_tab_close BARS
_agentdesk_kdl_tab_close() {
  local bars="$1"
  _agentdesk_kdl_check_bars "$bars" || return 2
  _agentdesk_kdl_bar 8 bottom "$bars"
  printf '    }\n'
}

# _agentdesk_kdl_pane INDENT NAME TOKEN [PROPS]
# The name comes first so `pane name="..."` matchers keep working.
_agentdesk_kdl_pane() {
  local ind="$1" pane_label="$2" tok="$3" props="${4:-}"
  if [ -n "$props" ]; then
    printf '%*spane name="%s" %s {\n' "$ind" '' "$pane_label" "$props"
  else
    printf '%*spane name="%s" {\n' "$ind" '' "$pane_label"
  fi
  printf '%*scommand "zsh"\n' "$((ind + 4))" ''
  printf '%*sargs "-lc" "%s"\n' "$((ind + 4))" '' "$(_agentdesk_pane_cmd "$tok")"
  printf '%*s}\n' "$ind" ''
}

# _agentdesk_kdl_spend_floating
# The token-spend monitor lives in a floating pane that starts hidden.
_agentdesk_kdl_spend_floating() {
  printf '        floating_panes {\n'
  _agentdesk_kdl_pane 12 "$(_agentdesk_pane_name spend)" spend 'x="10%" y="10%" width="80%" height="75%"'
  printf '        }\n'
}

# _agentdesk_kdl_row INDENT SIZE FOCUS SIZES ITEM...
# ITEM is "token|name". One item is a plain pane; several sit side by side
# in a vertical split. SIZES is an optional comma list of per-pane sizes.
_agentdesk_kdl_row() {
  local ind="$1" size="$2" focus="$3" sizes="$4"
  shift 4
  local item props idx=0
  local -a szs=()
  [ -n "$sizes" ] && szs=("${(@s:,:)sizes}")
  if [ "$#" -eq 1 ]; then
    props=""
    [ -n "$size" ] && props="size=\"$size\""
    [ -n "$focus" ] && props="${props:+$props }focus=true"
    _agentdesk_kdl_pane "$ind" "${1#*|}" "${1%%|*}" "$props"
    return 0
  fi
  props='split_direction="vertical"'
  [ -n "$size" ] && props="size=\"$size\" $props"
  printf '%*spane %s {\n' "$ind" '' "$props"
  for item in "$@"; do
    idx=$((idx + 1))
    props=""
    [ -n "${szs[$idx]:-}" ] && props="size=\"${szs[$idx]}\""
    if [ "$idx" -eq 1 ] && [ -n "$focus" ]; then
      props="${props:+$props }focus=true"
    fi
    _agentdesk_kdl_pane "$((ind + 4))" "${item#*|}" "${item%%|*}" "$props"
  done
  printf '%*s}\n' "$ind" ''
}

# _agentdesk_kdl_agents_block INDENT SIZE FOCUS ITEM...
# Up to three agents share one row; more are laid out as rows of two.
_agentdesk_kdl_agents_block() {
  local ind="$1" size="$2" focus="$3"
  shift 3
  local props first=1
  local -a pair=()
  if [ "$#" -le 3 ]; then
    _agentdesk_kdl_row "$ind" "$size" "$focus" "" "$@"
    return 0
  fi
  props='split_direction="horizontal"'
  [ -n "$size" ] && props="size=\"$size\" $props"
  printf '%*spane %s {\n' "$ind" '' "$props"
  while [ "$#" -gt 0 ]; do
    pair=("$1")
    [ "$#" -gt 1 ] && pair+=("$2")
    shift "${#pair[@]}"
    if [ "$first" -eq 1 ]; then
      _agentdesk_kdl_row "$((ind + 4))" "" "$focus" "" "${pair[@]}"
      first=0
    else
      _agentdesk_kdl_row "$((ind + 4))" "" "" "" "${pair[@]}"
    fi
  done
  printf '%*s}\n' "$ind" ''
}

# _agentdesk_kdl_agents_tab BARS TOKEN...
# The complete "main" tab. Tiled panes sit in one horizontal container so
# percentage sizes apply to the tiled area only, between the bars: agents on
# top, shells and status in a strip underneath.
_agentdesk_kdl_agents_tab() {
  local bars="$1"
  _agentdesk_kdl_check_bars "$bars" || return 2
  shift
  local tok idx top_size strip_size util_sizes has_status shell_n status_n
  local -a toks=() names=() agents=() utils=() util_toks=()
  for tok in "$@"; do
    [ "$tok" = "spend" ] || toks+=("$tok")
  done
  [ "${#toks[@]}" -eq 0 ] && toks=(shell)
  names=("${(@f)$(_agentdesk_pane_names "${toks[@]}")}")
  idx=0
  for tok in "${toks[@]}"; do
    idx=$((idx + 1))
    case "$tok" in
      claude|codex|ccr) agents+=("$tok|${names[$idx]}") ;;
      *)                utils+=("$tok|${names[$idx]}"); util_toks+=("$tok") ;;
    esac
  done

  _agentdesk_kdl_tab_open "$bars" main 'hide_floating_panes=true' || return 2
  printf '        pane split_direction="horizontal" {\n'
  if [ "${#agents[@]}" -gt 0 ] && [ "${#utils[@]}" -gt 0 ]; then
    top_size="75%"; strip_size="25%"; has_status=""
    shell_n=0; status_n=0
    for tok in "${util_toks[@]}"; do
      [ "$tok" = "status" ] && { has_status=1; status_n=$((status_n + 1)); }
      [ "$tok" = "shell" ] && shell_n=$((shell_n + 1))
    done
    [ -n "$has_status" ] && { top_size="70%"; strip_size="30%"; }
    util_sizes=""
    if [ "${#utils[@]}" -eq 2 ] && [ "$shell_n" -eq 1 ] && [ "$status_n" -eq 1 ]; then
      if [ "${util_toks[1]}" = "shell" ]; then util_sizes="60%,40%"; else util_sizes="40%,60%"; fi
    fi
    _agentdesk_kdl_agents_block 12 "$top_size" 1 "${agents[@]}"
    _agentdesk_kdl_row 12 "$strip_size" "" "$util_sizes" "${utils[@]}"
  elif [ "${#agents[@]}" -gt 0 ]; then
    _agentdesk_kdl_agents_block 12 "" 1 "${agents[@]}"
  elif [ "${#utils[@]}" -le 3 ]; then
    _agentdesk_kdl_row 12 "" 1 "" "${utils[@]}"
  else
    local first=1 item
    for item in "${utils[@]}"; do
      _agentdesk_kdl_row 12 "" "$([ "$first" -eq 1 ] && printf 1)" "" "$item"
      first=0
    done
  fi
  printf '        }\n'
  _agentdesk_kdl_spend_floating
  _agentdesk_kdl_tab_close "$bars"
}

# _agentdesk_kdl_swap_layouts BARS
# Three arrangements to cycle through: even, focus-left, stacked.
# focus-left needs at least two tiled panes besides the bars.
_agentdesk_kdl_swap_layouts() {
  local bars="$1" min_panes
  _agentdesk_kdl_check_bars "$bars" || return 2
  if [ "$bars" = "compact" ]; then min_panes=3; else min_panes=4; fi
  local -a swap_lines=(
    ""
    "    swap_tiled_layout name=\"even\" {"
    "        agentdesk_bars {"
    "            pane split_direction=\"vertical\" {"
    "                children"
    "            }"
    "        }"
    "    }"
    "    swap_tiled_layout name=\"focus-left\" {"
    "        agentdesk_bars min_panes=${min_panes} {"
    "            pane split_direction=\"vertical\" {"
    "                pane size=\"70%\""
    "                pane stacked=true {"
    "                    children"
    "                }"
    "            }"
    "        }"
    "    }"
    "    swap_tiled_layout name=\"stacked\" {"
    "        agentdesk_bars {"
    "            pane stacked=true {"
    "                children"
    "            }"
    "        }"
    "    }"
  )
  print -r -- "${(F)swap_lines[@]}"
}

_agentdesk_kdl_footer() {
  printf '}\n'
}

# _agentdesk_kdl_dashboard_tab LAYOUT URLFILE
# Launcher helper, not an emitter: it reads LAYOUT and expands $HOME. Prints
# the Dashboard tab followed by the closing brace of the layout. Generated
# layouts get explicit bars of their own bar mode; layouts that use a
# default_tab_template (older generated or hand-written ones) get the plain
# tab, which that template decorates.
# CO-UPDATE: keep the command in sync with _agentdesk_pane_cmd dashboard.
_agentdesk_kdl_dashboard_tab() {
  local layout="$1" url_file="$2" bars="default"
  if ! grep -q 'tab_template name="agentdesk_bars"' "$layout" 2>/dev/null; then
    cat <<DASHTAB
    tab name="Dashboard" {
        pane name="quoin dashboard" {
            command "zsh"
            args "-lc" "exec python3 \"$HOME/.claude/scripts/dashboard_server.py\" --no-browser --url-file \"$url_file\""
        }
    }
}
DASHTAB
    return 0
  fi
  grep -q 'zellij:compact-bar' "$layout" && bars="compact"
  _agentdesk_kdl_tab_open "$bars" Dashboard
  cat <<DASHPANE
        pane name="quoin dashboard" {
            command "zsh"
            args "-lc" "exec python3 \"$HOME/.claude/scripts/dashboard_server.py\" --no-browser --url-file \"$url_file\""
        }
DASHPANE
  _agentdesk_kdl_tab_close "$bars"
  printf '}\n'
}

# ============================================================
# _agentdesk_gen_layout [--bars default|compact] <token> [<token> ...]
# Generates a single-tab Zellij KDL layout file for the given window tokens.
# Echoes the path to the temp file. Caller is responsible for cleanup
# (use trap "rm -f $layout_tmp" EXIT INT TERM in the caller).
# ============================================================
_agentdesk_gen_layout() {
  local bars="default" layout_tmp exit_rc=0
  if [ "${1:-}" = "--bars" ]; then
    if [ "$#" -lt 2 ]; then
      echo "agentdesk: --bars requires a value (default or compact)" >&2
      return 1
    fi
    bars="$2"
    shift 2
  fi
  case "$bars" in
    default|compact) ;;
    *)
      printf 'agentdesk: invalid bar mode "%s" (expected default or compact)\n' "$bars" >&2
      return 1
      ;;
  esac

  # mktemp only randomizes *trailing* X's on macOS (BSD mktemp). A suffix after
  # the X's prevents randomisation, creating the same literal path every time and
  # failing with "File exists" on the second call. Fix: generate without suffix,
  # then rename to add .kdl. Uses TMPDIR so sandboxed environments work too.
  layout_tmp="$(mktemp "${TMPDIR:-/tmp}/agentdesk-layout-XXXXXX")" || {
    echo "agentdesk: failed to create temp layout file" >&2
    return 1
  }
  mv -f "$layout_tmp" "$layout_tmp.kdl" || return 1
  layout_tmp="$layout_tmp.kdl"

  {
    _agentdesk_kdl_header "$bars" || exit_rc=1
    _agentdesk_kdl_agents_tab "$bars" "$@" || exit_rc=1
    _agentdesk_kdl_swap_layouts "$bars" || exit_rc=1
    _agentdesk_kdl_footer || exit_rc=1
  } > "$layout_tmp"

  if [ "$exit_rc" -ne 0 ]; then
    rm -f "$layout_tmp"
    echo "agentdesk: failed to generate the layout" >&2
    return 1
  fi

  printf '%s\n' "$layout_tmp"
}

# ============================================================
# _agentdesk_pick_layout
# Interactive layout picker. Prints a menu and reads user selection.
# Echoes space-separated tokens on stdout; echoes nothing for "fixed layout".
# Only call when stdin is a TTY ([ -t 0 ]).
# ============================================================
_agentdesk_pick_layout() {
  printf 'Select a layout:\n' >&2
  printf '  1) Standard (5 tabs: main / review / repos / shell / spend)  [default]\n' >&2
  printf '  2) claude + shell + spend\n' >&2
  printf '  3) claude + claude + shell + spend\n' >&2
  printf '  4) claude + codex + shell + spend\n' >&2
  printf '  5) claude + ccr + shell + spend\n' >&2
  printf '  6) Custom — type comma-separated: e.g. claude, codex, shell, ccr\n' >&2
  printf 'Choice [1]: ' >&2

  local choice
  read -r choice

  # Empty → option 1
  if [ -z "$choice" ] || [ "$choice" = "1" ]; then
    return 0  # echo nothing → caller uses fixed layout
  fi

  case "$choice" in
    2)
      printf 'claude shell\n'
      return 0
      ;;
    3)
      printf 'claude claude shell\n'
      return 0
      ;;
    4)
      printf 'claude codex shell\n'
      return 0
      ;;
    5)
      printf 'claude ccr shell\n'
      return 0
      ;;
    6)
      printf 'Type comma-separated tokens (claude, codex, shell, ccr): ' >&2
      read -r choice
      ;;
    *)
      # Treat any other input as direct comma-separated custom input
      ;;
  esac

  # Parse comma-separated input, strip whitespace, validate tokens
  _agentdesk_parse_custom_tokens "$choice"
}

# ============================================================
# _agentdesk_parse_custom_tokens <comma-separated-string>
# Validates and echoes space-separated tokens.
# Returns 1 if any token is invalid.
# ============================================================
_agentdesk_parse_custom_tokens() {
  local input="$1"
  local -a parsed=()

  # Split on commas using zsh parameter expansion, then strip whitespace from each token.
  # ${(@s:,:)input} is zsh-specific: splits $input on ',' into an array.
  local -a raw_tokens=("${(@s:,:)input}")

  local tok
  for tok in "${raw_tokens[@]}"; do
    # Strip leading/trailing whitespace
    tok="${tok#"${tok%%[! ]*}"}"
    tok="${tok%"${tok##*[! ]}"}"
    [ -z "$tok" ] && continue

    case "$tok" in
      claude|codex|shell|status|ccr|spend)
        parsed+=("$tok")
        ;;
      dashboard)
        # T-06/D-05: internal-only token — not advertised in the picker or --help.
        # Accepted here so advanced users who pass it explicitly are not rejected.
        parsed+=("$tok")
        ;;
      *)
        printf 'Unknown token: "%s". Valid tokens: claude, codex, shell, status, ccr, spend\n' "$tok" >&2
        printf 'Re-enter comma-separated tokens (or press Enter to cancel): ' >&2
        local retry
        read -r retry
        if [ -z "$retry" ]; then
          return 1
        fi
        _agentdesk_parse_custom_tokens "$retry"
        return $?
        ;;
    esac
  done

  if [ "${#parsed[@]}" -eq 0 ]; then
    printf 'No valid tokens provided.\n' >&2
    return 1
  fi

  printf '%s\n' "${parsed[*]}"
  return 0
}

# ============================================================
# _agentdesk_open_url <url>
# Reusable browser-open helper, called by the bounded poller in agentdesk() to
# open the dashboard URL once the dashboard pane has written it.
# ============================================================
_agentdesk_open_url() {
  local url="$1"
  if command -v open >/dev/null 2>&1; then
    open "$url"
  elif command -v xdg-open >/dev/null 2>&1; then
    xdg-open "$url"
  else
    printf 'agentdesk: dashboard running at %s (no browser opener found)\n' "$url" >&2
  fi
}

# ============================================================
# Dashboard decision helpers
#
# The dashboard question is asked once per project (or named session) and the
# answer is remembered in the saved-state file. Nothing here exports a variable
# or starts a server: the decision is printed for the caller, and the server
# runs inside a Zellij pane injected into the layout, so its lifetime is the
# session's lifetime.
# ============================================================

# Ask the dashboard question on the terminal. Prompt goes to stderr so the
# answer ("yes" or "no") is the only thing on stdout.
_agentdesk_dashboard_ask() {
  local reply
  printf 'Open quoin dashboard? [y/N]: ' >&2
  read -r reply
  case "$reply" in
    [Yy]|[Yy][Ee][Ss]) printf 'yes\n' ;;
    *) printf 'no\n' ;;
  esac
}

# _agentdesk_dashboard_resolve <flag> <noprompt> <ask> <pick> <tty> <dash_key>
# Prints "<yes|no> <source>" with source one of
# flag|noprompt|asked|env|stored|default. Precedence:
#   --dashboard/--no-dashboard > --no-prompt (no) > --ask-dashboard (tty) >
#   AGENTDESK_DASHBOARD=1|0 (ignored while a URL-file value is also set, which
#   marks a stale value inherited from an older launcher) > --pick re-ask (tty)
#   > stored answer (tty) > ask (tty) > no.
# Stored answers and questions are interactive-only. Never writes state.
_agentdesk_dashboard_resolve() {
  local flag="$1" noprompt="$2" ask="$3" pick="$4" tty="$5" dash_key="$6"
  local ans stored

  if [ "$flag" = "yes" ] || [ "$flag" = "no" ]; then
    printf '%s flag\n' "$flag"
    return 0
  fi
  if [ -n "$noprompt" ]; then
    printf 'no noprompt\n'
    return 0
  fi
  if [ -n "$ask" ] && [ -n "$tty" ]; then
    ans="$(_agentdesk_dashboard_ask)"
    printf '%s asked\n' "$ans"
    return 0
  fi
  if [ -z "${AGENTDESK_DASHBOARD_URL_FILE:-}" ]; then
    case "${AGENTDESK_DASHBOARD-}" in
      1) printf 'yes env\n'; return 0 ;;
      0) printf 'no env\n'; return 0 ;;
    esac
  fi
  if [ -z "$tty" ]; then
    printf 'no default\n'
    return 0
  fi
  if [ -n "$pick" ]; then
    ans="$(_agentdesk_dashboard_ask)"
    printf '%s asked\n' "$ans"
    return 0
  fi
  stored="$(_agentdesk_state_get "$dash_key")"
  case "$stored" in
    yes|no) printf '%s stored\n' "$stored"; return 0 ;;
  esac
  ans="$(_agentdesk_dashboard_ask)"
  printf '%s asked\n' "$ans"
}

# Create the URL-file path the dashboard pane writes its bound URL to and the
# launcher's poller reads. Prints the path; returns 1 (with a note on stderr)
# when the dashboard server is not installed, e.g. project-scope installs.
_agentdesk_dashboard_server_present() {
  local server_script="$HOME/.claude/scripts/dashboard_server.py"
  if [ ! -f "$server_script" ]; then
    printf 'agentdesk: dashboard server not found at %s; skipping\n' "$server_script" >&2
    return 1
  fi
}

_agentdesk_dashboard_url_file() {
  _agentdesk_dashboard_server_present || return 1
  local url_file
  url_file="$(mktemp "${TMPDIR:-/tmp}/agentdesk-dash-url-XXXXXX")" || return 1
  printf '%s\n' "$url_file"
}

# Print the named-session keys that survive a state cleanup so test leftovers
# can be pruned by hand.
_agentdesk_print_state_names() {
  printf 'Remaining named-session keys:\n'
  if [ -n "$1" ]; then
    printf '%s\n' "$1" | sed 's/^/  /'
  else
    printf '  (none)\n'
  fi
}

# ============================================================
# _agentdesk_clean_state [--dry-run]
#
# Removes saved-state entries that point at test or scratch locations (pytest
# temp dirs, macOS per-user temp folders, agent scratchpads). Keys are
# percent-encoded in the file, so they are decoded before matching. Kept lines
# are written back byte-identical and in order. The original file is copied to
# last-layout.bak.<timestamp> before it is replaced; nothing is written when
# there is nothing to remove or under --dry-run. Never runs automatically, and
# is not safe against an agentdesk launch that saves while it runs.
# ============================================================
_agentdesk_clean_state() {
  local dry_run=""
  [ "${1:-}" = "--dry-run" ] && dry_run=1

  local state_file state_dir tmp="" report
  state_file="$(_agentdesk_state_file)"
  state_dir="$(dirname "$state_file")"

  if [ ! -f "$state_file" ]; then
    printf 'agentdesk: no state file at %s; nothing to clean\n' "$state_file"
    return 0
  fi

  if ! command -v python3 >/dev/null 2>&1; then
    printf 'agentdesk: python3 is required for --clean-state; state left unchanged.\n' >&2
    return 1
  fi

  if [ -z "$dry_run" ]; then
    tmp="$(mktemp "$state_dir/.last-layout.clean.XXXXXX" 2>/dev/null)" || {
      printf 'agentdesk: cannot create a temp file in %s; state left unchanged.\n' "$state_dir" >&2
      return 1
    }
  fi

  report="$(python3 - "$state_file" ${tmp:+"$tmp"} <<'PY'
import sys
from urllib.parse import unquote

state = sys.argv[1]
out = sys.argv[2] if len(sys.argv) > 2 else None
data = open(state, "rb").read()
trailing = data.endswith(b"\n")
lines = data.split(b"\n") if data else []
if trailing:
    lines.pop()
kept, removed, names = [], 0, []
for line in lines:
    if b"=" not in line:
        kept.append(line)
        continue
    key = unquote(line.split(b"=", 1)[0].decode("utf-8", "replace"))
    polluted = (
        "pytest-of-" in key
        or "/private/var/folders/" in key
        or ("/private/tmp/claude-" in key and "/scratchpad" in key)
    )
    if polluted:
        removed += 1
        continue
    kept.append(line)
    if key.startswith("name:"):
        names.append(key)
if out is not None:
    with open(out, "wb") as fh:
        fh.write(b"\n".join(kept) + (b"\n" if trailing and kept else b""))
print("removed=%d" % removed)
print("kept=%d" % len(kept))
for n in names:
    print("n:" + n)
PY
)" || {
    [ -n "$tmp" ] && rm -f "$tmp" 2>/dev/null
    printf 'agentdesk: failed to read the state file; state left unchanged.\n' >&2
    return 1
  }

  local removed kept
  removed="$(printf '%s\n' "$report" | sed -n 's/^removed=//p')"
  kept="$(printf '%s\n' "$report" | sed -n 's/^kept=//p')"
  local names
  names="$(printf '%s\n' "$report" | sed -n 's/^n://p')"

  if [ "${removed:-0}" -eq 0 ]; then
    [ -n "$tmp" ] && rm -f "$tmp" 2>/dev/null
    printf 'agentdesk: already clean (%s entries)\n' "${kept:-0}"
    _agentdesk_print_state_names "$names"
    return 0
  fi

  if [ -n "$dry_run" ]; then
    printf 'agentdesk: would remove %s of %s entries (dry run; nothing changed)\n' \
      "$removed" "$(( removed + kept ))"
    _agentdesk_print_state_names "$names"
    return 0
  fi

  local bak stamp n=0
  stamp="$(date +%Y%m%d-%H%M%S)"
  bak="$state_dir/last-layout.bak.$stamp"
  while [ -e "$bak" ]; do
    n=$(( n + 1 ))
    bak="$state_dir/last-layout.bak.$stamp-$n"
  done
  if ! cp -p "$state_file" "$bak" 2>/dev/null; then
    rm -f "$tmp" 2>/dev/null
    printf 'agentdesk: could not write backup %s; state left unchanged.\n' "$bak" >&2
    return 1
  fi
  if ! mv "$tmp" "$state_file" 2>/dev/null; then
    rm -f "$tmp" 2>/dev/null
    printf 'agentdesk: could not replace the state file; left unchanged. Backup at %s\n' "$bak" >&2
    return 1
  fi
  printf 'agentdesk: removed %s entries; kept %s; backup: %s\n' "$removed" "$kept" "$bak"
  _agentdesk_print_state_names "$names"
  return 0
}

agentdesk() {
  local mode=""
  local custom_name=""
  local force_pick=""
  local pick_flag=""
  local panes_raw="" panes_given="" noprompt="" if_exists=""
  local clean_state="" dry_run=""
  local dash_on="" dash_off="" dash_ask="" compact_bar="" bar_mode="default"
  local -a tokens=()
  local -a panes_tokens=()

  # ── Arg parsing (BEFORE Zellij guard so --help always works) ──────────────
  while [ "$#" -gt 0 ]; do
    case "$1" in
      --mode)
        shift
        if [ "$#" -eq 0 ]; then
          printf 'Error: --mode requires a value (solo, duo, trio).\n' >&2
          return 1
        fi
        case "$1" in
          solo|duo|trio)
            mode="$1"
            ;;
          *)
            printf 'Error: invalid mode "%s". Valid modes: solo, duo, trio\n' "$1" >&2
            return 1
            ;;
        esac
        ;;
      -n|--name)
        shift
        if [ "$#" -eq 0 ]; then
          printf 'Error: --name requires a value.\n' >&2
          return 1
        fi
        custom_name="$1"
        ;;
      --pick)
        force_pick=1
        pick_flag=1
        ;;
      --panes)
        shift
        if [ "$#" -eq 0 ]; then
          printf 'Error: --panes requires a value (e.g. codex,codex,shell).\n' >&2
          return 1
        fi
        panes_raw="$1"
        panes_given=1
        ;;
      --no-prompt)
        noprompt=1
        ;;
      --dashboard)
        dash_on=1
        ;;
      --no-dashboard)
        dash_off=1
        ;;
      --ask-dashboard)
        dash_ask=1
        ;;
      --compact-bar)
        compact_bar=1
        ;;
      --clean-state)
        clean_state=1
        ;;
      --dry-run)
        dry_run=1
        ;;
      --if-exists)
        shift
        if [ "$#" -eq 0 ]; then
          printf 'Error: --if-exists requires a value (attach, new, fail).\n' >&2
          return 1
        fi
        case "$1" in
          attach|new|fail)
            if_exists="$1"
            ;;
          *)
            printf 'Error: invalid --if-exists value "%s". Valid values: attach, new, fail\n' "$1" >&2
            return 1
            ;;
        esac
        ;;
      -h|--help)
        cat <<'HELP'
Usage:
  agentdesk
  agentdesk <session-name>
  agentdesk --name <session-name>
  agentdesk --mode solo|duo|trio
  agentdesk claude [codex] [shell] [...]
  agentdesk --pick
  agentdesk --compact-bar [...]
  agentdesk [--name NAME] --panes LIST [--no-prompt] [--if-exists attach|new|fail]

Modes:
  solo   — one Claude pane full-width
  duo    — Claude on top, a Shell strip below
  trio   — Claude and Codex side by side on top, a Shell strip below

Window types (positional):
  claude   start Claude Code in pane
  codex    start Codex in pane
  shell    plain zsh shell
  status   show workflow pipeline status graph
  ccr      start an open-model CCR (OpenRouter) session in pane
  spend    realtime token-spend monitor (opens as the hidden floating pane)

Layout:
  Agents (claude, codex, ccr) are laid out on top; shells and status share a
  strip below. Token Spend is always available as a hidden floating pane
  (toggle: Alt f, or pane mode then w). Swap between arrangements (even,
  focus-left, stacked) with Alt [ and Alt ]. New panes split the focused pane;
  Zellij's automatic re-arranging is switched off for launched sessions.
  --compact-bar / AGENTDESK_COMPACT_BAR=1
                     one-line bar at the bottom instead of the tab bar and the
                     status bar with mode key hints. Per launch only, never
                     remembered.

Dashboard:
  The "Open quoin dashboard?" question is asked once per project (or named
  session) and the answer is remembered in the same state file.
  --dashboard / --no-dashboard   one-shot override; the remembered answer is
                     not changed and nothing is asked.
  --ask-dashboard    ask again now and remember the new answer. --pick also
                     asks again. With --panes the question is still asked for
                     that launch, but the answer is not remembered.
  AGENTDESK_DASHBOARD=1|0        override the remembered answer without asking.
  --no-prompt never asks and never adds the dashboard unless --dashboard is
  also given. --panes launches never store an answer. Non-interactive launches
  never ask and use only the flag or environment override.

State maintenance:
  agentdesk --clean-state [--dry-run]
                     remove saved entries that point at test or scratch
                     locations (pytest temp dirs, per-user temp folders, agent
                     scratchpads) from ~/.config/agentdesk/last-layout. The file
                     is copied to last-layout.bak.<timestamp> first. --dry-run
                     only reports what would be removed. It never runs
                     automatically; run it when no agentdesk launch is in
                     progress. Cannot be combined with launch arguments.

Layout memory:
  agentdesk remembers your layout choice per project (or per session name).
  On subsequent runs it reuses your last layout silently.
  Use --pick to force the interactive picker and change the layout.
  The layout is stored in: ~/.config/agentdesk/last-layout
  To reset: delete that file, or delete the line for your project key.
  Project key namespace (proj:) and named-session namespace (name:) are
  independent — a named session save never affects a bare project launch.
  Set AGENTDESK_PICK=1 to force the picker via environment variable.
  --panes launches are one-shot and never update this file.

Behavior:
  Re-running with a live same-named session prompts to attach to it (default) or
  start a new suffixed session (_1, _2, …) instead — reply 'n' to start new.
  Non-interactive callers (no TTY, e.g. scripts/CI) skip the prompt and always
  start a new suffixed session, matching the prior default exactly.
  Note: attaching (the default) reconnects to the session as-is — any --mode or
  window-type tokens passed on this invocation are ignored for an attach; reply
  'n' to build the requested layout fresh instead (see MINOR-2, T-01).

Non-interactive launch (AGENTDESK_API_VERSION=1):
  --panes LIST       explicit pane composition: window types joined by ',' or '+'
                     (e.g. codex,codex,shell). Token Spend is still available as the
                     hidden floating pane (toggle: Alt f, or pane mode then w).
                     Not combinable with --mode, positional tokens or --pick.
                     Never remembered in, nor read from, the layout memory file.
  --no-prompt        never ask anything (no dashboard question, no picker, no
                     attach-or-new question), even in a terminal. Without
                     --panes/--mode it uses the saved or fixed layout. The
                     dashboard is not added unless --dashboard is also given.
                     AGENTDESK_PICK is ignored.
  --if-exists attach|new|fail
                     what to do when a same-named session already exists (live or
                     resurrectable). Default: prompt in a terminal, new suffixed
                     session otherwise; with --no-prompt the default is attach.
                     On attach, --panes/--mode are ignored. Attaching needs a
                     terminal (zellij attach fails without one), so callers
                     without a terminal that want a guaranteed result use new or
                     fail.
  Example: agentdesk --name deck --panes codex,codex,shell --no-prompt --if-exists attach

Examples:
  agentdesk
  agentdesk pricing
  agentdesk --mode trio
  agentdesk claude codex shell
  agentdesk claude ccr shell
  agentdesk claude claude
  agentdesk --pick

Resume:
  agentdesk-attach SESSION-NAME   resume an existing session
HELP
        return 0
        ;;
      claude|codex|shell|status|ccr|spend)
        tokens+=("$1")
        ;;
      *)
        if [ "${#tokens[@]}" -eq 0 ] && [ -z "$mode" ]; then
          # Treat as session name (backward compat)
          custom_name="$1"
        else
          printf 'Error: unexpected argument "%s"\n' "$1" >&2
          return 1
        fi
        ;;
    esac
    shift
  done

  # ── State maintenance (BEFORE Zellij guard and any project work) ──────────
  if [ -n "$dry_run" ] && [ -z "$clean_state" ]; then
    printf 'Error: --dry-run is only valid with --clean-state.\n' >&2
    return 1
  fi
  if [ -n "$clean_state" ]; then
    local _cs_conflict=""
    [ -n "$mode" ] && _cs_conflict="--mode"
    [ -z "$_cs_conflict" ] && [ "${#tokens[@]}" -gt 0 ] && _cs_conflict="window-type tokens"
    [ -z "$_cs_conflict" ] && [ -n "$panes_given" ] && _cs_conflict="--panes"
    [ -z "$_cs_conflict" ] && [ -n "$pick_flag" ] && _cs_conflict="--pick"
    [ -z "$_cs_conflict" ] && [ -n "$custom_name" ] && _cs_conflict="a session name"
    [ -z "$_cs_conflict" ] && [ -n "$if_exists" ] && _cs_conflict="--if-exists"
    [ -z "$_cs_conflict" ] && [ -n "$compact_bar" ] && _cs_conflict="--compact-bar"
    [ -z "$_cs_conflict" ] && { [ -n "$dash_on" ] || [ -n "$dash_off" ] || [ -n "$dash_ask" ]; } \
      && _cs_conflict="a dashboard flag"
    if [ -n "$_cs_conflict" ]; then
      printf 'Error: --clean-state cannot be combined with %s.\n' "$_cs_conflict" >&2
      return 1
    fi
    if [ -n "$dry_run" ]; then
      _agentdesk_clean_state --dry-run
    else
      _agentdesk_clean_state
    fi
    return $?
  fi

  # The compact bar is opt-in per launch and never remembered.
  if [ -n "$compact_bar" ] || [ "${AGENTDESK_COMPACT_BAR:-}" = "1" ]; then
    bar_mode="compact"
  fi

  # Apply AGENTDESK_PICK env override (force picker via environment variable).
  if [ -n "${AGENTDESK_PICK:-}" ]; then
    force_pick=1
  fi
  # The environment picker request is inert for the public non-interactive
  # interface, like --pick is inert when --mode/tokens are given.
  if [ -n "$panes_given" ] || [ -n "$noprompt" ]; then
    force_pick=""
  fi

  # ── Conflict checks (BEFORE Zellij guard) ─────────────────────────────────
  if [ -n "$mode" ] && [ "${#tokens[@]}" -gt 0 ]; then
    printf 'Error: --mode and positional window-type tokens are mutually exclusive.\n' >&2
    printf 'Use --mode for preset layouts or positional tokens for custom layouts.\n' >&2
    return 1
  fi

  if [ -n "$custom_name" ] && [ "${#tokens[@]}" -gt 0 ]; then
    printf 'Error: cannot mix session name with window-type tokens.\n' >&2
    printf 'Use --name for a named session with a mode/layout (e.g. agentdesk --mode trio --name my-session).\n' >&2
    return 1
  fi

  if [ -n "$panes_given" ] && [ -n "$mode" ]; then
    printf 'Error: --panes cannot be combined with --mode.\n' >&2
    return 1
  fi

  if [ -n "$panes_given" ] && [ "${#tokens[@]}" -gt 0 ]; then
    printf 'Error: --panes cannot be combined with positional window-type tokens.\n' >&2
    return 1
  fi

  if [ -n "$panes_given" ] && [ -n "$pick_flag" ]; then
    printf 'Error: --panes cannot be combined with --pick.\n' >&2
    return 1
  fi

  if [ -n "$noprompt" ] && [ -n "$pick_flag" ]; then
    printf 'Error: --pick is interactive and cannot be combined with --no-prompt.\n' >&2
    return 1
  fi

  if [ -n "$dash_on" ] && [ -n "$dash_off" ]; then
    printf 'Error: --dashboard and --no-dashboard are mutually exclusive.\n' >&2
    return 1
  fi

  if [ -n "$dash_ask" ] && { [ -n "$dash_on" ] || [ -n "$dash_off" ]; }; then
    printf 'Error: --ask-dashboard cannot be combined with --dashboard or --no-dashboard.\n' >&2
    return 1
  fi

  if [ -n "$dash_ask" ] && [ -n "$noprompt" ]; then
    printf 'Error: --ask-dashboard is interactive and cannot be combined with --no-prompt.\n' >&2
    return 1
  fi

  # ── --panes list parsing (BEFORE Zellij guard; strict, no trimming) ───────
  if [ -n "$panes_given" ]; then
    local _pane _non_spend=0
    local -a _pane_parts
    _pane_parts=("${(@s:,:)${panes_raw//+/,}}")
    if [ "${#_pane_parts[@]}" -eq 0 ]; then
      _pane_parts=("")
    fi
    for _pane in "${_pane_parts[@]}"; do
      case "$_pane" in
        claude|codex|shell|status|ccr|spend)
          panes_tokens+=("$_pane")
          if [ "$_pane" != "spend" ]; then _non_spend=1; fi
          ;;
        "")
          printf 'Error: --panes has an empty entry in "%s".\n' "$panes_raw" >&2
          return 1
          ;;
        *)
          printf 'Error: unknown pane "%s" in --panes. Valid: claude, codex, shell, status, ccr, spend\n' "$_pane" >&2
          return 1
          ;;
      esac
    done
    if [ "$_non_spend" -eq 0 ]; then
      printf 'Error: --panes needs at least one pane other than spend.\n' >&2
      return 1
    fi
  fi

  # ── Zellij guard (AFTER arg parsing) ──────────────────────────────────────
  if [ -n "${ZELLIJ:-}" ]; then
    echo "You are already inside a Zellij session."
    echo "Detach with Alt+s, then d (needs Ghostty macos-option-as-alt), or run: zellij action detach"
    return 1
  fi

  # ── Resolve project root and session name ─────────────────────────────────
  local project_root session_name
  project_root="$(_agentdesk_realpath "$PWD")"
  export PROJECT_ROOT="$project_root"

  if [ -n "$custom_name" ]; then
    session_name="$(_sanitize_session_name "$custom_name")"
  else
    session_name="$(_agentdesk_session_name "$project_root")-agents"
  fi

  if [ -z "$session_name" ]; then
    echo "Error: empty session name after sanitization."
    return 1
  fi

  # ── Compute layout persistence key BEFORE session-name suffixing ──────────
  # Key uses ORIGINAL custom_name (not suffixed), so _1/_2 suffixed sessions
  # still share the same reuse key as the base session name.
  local layout_key
  layout_key="$(_agentdesk_layout_key "$custom_name" "$project_root")"

  # ── Resolve session name suffix (attach-or-new prompt, IVG-109) ───────────
  # Prompts only happen on a terminal and never under --no-prompt.
  local interactive=""
  if [ -t 0 ] && [ -z "$noprompt" ]; then interactive=1; fi

  local resolved_name policy
  resolved_name="$(_agentdesk_next_session_name "$session_name")"
  if [ "$resolved_name" != "$session_name" ]; then
    # $session_name is currently a live Zellij session (collision).
    policy="$if_exists"
    if [ -z "$policy" ] && [ -n "$noprompt" ]; then policy=attach; fi
    if [ -z "$policy" ]; then policy=legacy; fi
    if [ "$policy" = "attach" ]; then
      echo "Attaching to existing session '$session_name'."
      agentdesk-attach "$session_name"
      return $?
    elif [ "$policy" = "fail" ]; then
      printf "Error: session '%s' already exists (--if-exists fail).\n" "$session_name" >&2
      printf '(To resume it: agentdesk-attach %s)\n' "$session_name" >&2
      return 1
    elif [ "$policy" = "new" ]; then
      echo "Session '$session_name' already exists; starting new session '$resolved_name' instead."
      echo "(To resume the existing session: agentdesk-attach $session_name)"
      session_name="$resolved_name"
    elif [ -n "$interactive" ]; then
      printf "Session '%s' already exists. Attach to it, or start a new session '%s'? [A/n]: " \
        "$session_name" "$resolved_name" >&2
      local reply
      read -r reply
      case "$reply" in
        [Nn]|[Nn][Ee][Ww])
          echo "Starting new session '$resolved_name'."
          session_name="$resolved_name"
          ;;
        *)
          # Default (bare Enter, 'a', 'attach', or anything else): attach.
          echo "Attaching to existing session '$session_name'."
          agentdesk-attach "$session_name"
          return $?
          ;;
      esac
    else
      # Non-interactive: preserve exact prior behavior — never prompt, never hang.
      echo "Session '$session_name' already exists; starting new session '$resolved_name' instead."
      echo "(To resume the existing session: agentdesk-attach $session_name)"
      session_name="$resolved_name"
    fi
  fi

  # ── Prepare workflow artifacts ─────────────────────────────────────────────
  mkdir -p "$project_root/.workflow_artifacts"

  touch "$project_root/.workflow_artifacts/repos.md"

  {
    echo "# Repositories"
    echo
    _detect_repos | while read -r repo; do
      [ -n "$repo" ] && echo "- $repo"
    done
  } > "$project_root/.workflow_artifacts/repos.md"

  local repo_count
  repo_count="$(_detect_repos | wc -l | tr -d ' ')"

  if [ "$repo_count" = "0" ]; then
    echo "No Git repo found at current folder or in child folders."
    echo "Starting anyway at project root: $project_root"
  fi

  if ! command -v zellij >/dev/null 2>&1; then
    echo "zellij is not installed. Install it with:"
    echo "  brew install zellij"
    return 1
  fi

  # ── Decide on the dashboard BEFORE layout resolution ──────────────────────
  # The decision is a local: nothing is exported, so it can never leak into the
  # user's shell or a later launch. Only an answer given to a question is
  # remembered (stored with the layout below).
  local dash_decision="" dash_answer="" dash_source="" dash_url_file=""
  local dash_flag=""
  [ -n "$dash_on" ] && dash_flag=yes
  [ -n "$dash_off" ] && dash_flag=no
  local dash_tty=""
  [ -t 0 ] && dash_tty=1
  read -r dash_decision dash_source <<< "$(_agentdesk_dashboard_resolve \
    "$dash_flag" "$noprompt" "$dash_ask" "$force_pick" "$dash_tty" \
    "$(_agentdesk_dash_key "$layout_key")")"
  dash_answer="$dash_decision"
  # The URL temp file is created only at injection time, after every early
  # return, so a failed launch never leaves one behind. Here we only check that
  # the dashboard server is installed.
  if [ "$dash_decision" = "yes" ] && ! _agentdesk_dashboard_server_present; then
    dash_decision=no
  fi

  # ── Resolve layout (D-08 branch order) ────────────────────────────────────
  # Declare ALL locals before the if/elif chain, outside any redirection block.
  # (zsh typeset echoes name=value to stdout when re-declaring inside { } > file)
  local layout_path="" layout_tmp=""
  local saved_value="" saved_value_loaded=""
  local -a effective_tokens=() picked_tokens=()
  local persist_layout=1

  if [ "${#panes_tokens[@]}" -gt 0 ]; then
    # Branch 0: explicit --panes list — one-shot, never loaded or persisted
    layout_tmp="$(_agentdesk_gen_layout --bars "$bar_mode" "${panes_tokens[@]}")"
    layout_path="$layout_tmp"
    persist_layout=""
    # shellcheck disable=SC2064
    trap "rm -f $layout_tmp" EXIT INT TERM

  elif [ -n "$mode" ] || [ "${#tokens[@]}" -gt 0 ]; then
    # Branch 1: explicit mode or tokens — generate KDL at runtime
    if [ -n "$mode" ]; then
      case "$mode" in
        solo)  effective_tokens=(claude) ;;
        duo)   effective_tokens=(claude shell) ;;
        trio)  effective_tokens=(claude codex shell) ;;
      esac
    else
      effective_tokens=("${tokens[@]}")
    fi
    layout_tmp="$(_agentdesk_gen_layout --bars "$bar_mode" "${effective_tokens[@]}")"
    layout_path="$layout_tmp"
    # join OUTSIDE any redirection to avoid typeset-echo leak
    saved_value="${(j:+:)effective_tokens}"
    # Clean up temp file on exit, return, interrupt, or termination
    # shellcheck disable=SC2064
    trap "rm -f $layout_tmp" EXIT INT TERM

  elif [ -n "$force_pick" ]; then
    # Branch 2: --pick or AGENTDESK_PICK=1 — force interactive picker
    # --pick is inert when --mode/tokens are given (branch 1 wins above).
    if [ -n "$interactive" ]; then
      # TTY: show picker
      local picked
      picked="$(_agentdesk_pick_layout)"
      local picker_rc=$?
      if [ $picker_rc -ne 0 ]; then
        printf 'Layout selection cancelled.\n' >&2
        return 1
      fi
      if [ -n "$picked" ]; then
        # Non-empty pick (options 2-6): split space-separated output into array
        read -rA picked_tokens <<< "$picked"
        layout_tmp="$(_agentdesk_gen_layout --bars "$bar_mode" "${picked_tokens[@]}")"
        layout_path="$layout_tmp"
        # join OUTSIDE any redirection to avoid typeset-echo leak
        saved_value="${(j:+:)picked_tokens}"
        # shellcheck disable=SC2064
        trap "rm -f $layout_tmp" EXIT INT TERM
      else
        # Empty pick (option 1 / default): use fixed layout
        layout_path="$HOME/.config/zellij/layouts/agent-desk.kdl"
        saved_value="__FIXED__"
      fi
    else
      # Non-TTY + --pick: skip reuse, go directly to fixed layout (D-07)
      layout_path="$HOME/.config/zellij/layouts/agent-desk.kdl"
      saved_value="__FIXED__"
    fi

  elif saved_value_loaded="$(_agentdesk_load_layout "$layout_key" 2>/dev/null)" && [ -n "$saved_value_loaded" ]; then
    # Branch 3: saved layout exists — try to restore it
    local restored_path
    restored_path="$(_agentdesk_layout_from_value "$saved_value_loaded" "$bar_mode" 2>/dev/null)"
    local restore_rc=$?
    if [ $restore_rc -eq 0 ] && [ -n "$restored_path" ]; then
      layout_path="$restored_path"
      printf 'Reusing last layout (use --pick to change)\n' >&2
      saved_value="$saved_value_loaded"
      # If the restored path is a generated temp file (not the fixed kdl), register trap
      if [ "$restored_path" != "$HOME/.config/zellij/layouts/agent-desk.kdl" ]; then
        layout_tmp="$restored_path"
        # shellcheck disable=SC2064
        trap "rm -f $layout_tmp" EXIT INT TERM
      fi
    else
      # Corrupt saved value — fall through to branch 4/5
      if [ -n "$interactive" ]; then
        local picked
        picked="$(_agentdesk_pick_layout)"
        local picker_rc=$?
        if [ $picker_rc -ne 0 ]; then
          printf 'Layout selection cancelled.\n' >&2
          return 1
        fi
        if [ -n "$picked" ]; then
          read -rA picked_tokens <<< "$picked"
          layout_tmp="$(_agentdesk_gen_layout --bars "$bar_mode" "${picked_tokens[@]}")"
          layout_path="$layout_tmp"
          saved_value="${(j:+:)picked_tokens}"
          # shellcheck disable=SC2064
          trap "rm -f $layout_tmp" EXIT INT TERM
        else
          layout_path="$HOME/.config/zellij/layouts/agent-desk.kdl"
          saved_value="__FIXED__"
        fi
      else
        layout_path="$HOME/.config/zellij/layouts/agent-desk.kdl"
        saved_value="__FIXED__"
      fi
    fi

  elif [ -n "$interactive" ]; then
    # Branch 4: no saved layout + TTY — first-run interactive picker
    local picked
    picked="$(_agentdesk_pick_layout)"
    local picker_rc=$?
    if [ $picker_rc -ne 0 ]; then
      printf 'Layout selection cancelled.\n' >&2
      return 1
    fi
    if [ -n "$picked" ]; then
      read -rA picked_tokens <<< "$picked"
      layout_tmp="$(_agentdesk_gen_layout --bars "$bar_mode" "${picked_tokens[@]}")"
      layout_path="$layout_tmp"
      saved_value="${(j:+:)picked_tokens}"
      # shellcheck disable=SC2064
      trap "rm -f $layout_tmp" EXIT INT TERM
    else
      # Option 1 or empty — use fixed layout
      layout_path="$HOME/.config/zellij/layouts/agent-desk.kdl"
      saved_value="__FIXED__"
    fi

  else
    # Branch 5: non-TTY + no saved layout — use fixed layout silently
    layout_path="$HOME/.config/zellij/layouts/agent-desk.kdl"
    saved_value="__FIXED__"
  fi

  if [ ! -f "$layout_path" ]; then
    echo "Zellij layout not found:"
    echo "  $layout_path"
    return 1
  fi

  # Save layout AFTER existence check passes (D-05): never persist a value
  # whose layout file is missing (would fail every subsequent launch).
  if [ -n "$persist_layout" ]; then
    _agentdesk_save_layout "$layout_key" "$saved_value" 2>/dev/null || true
    if [ "$dash_source" = "asked" ]; then
      _agentdesk_state_set "$(_agentdesk_dash_key "$layout_key")" "$dash_answer" 2>/dev/null || true
    fi
  fi

  echo "Starting agent desk:"
  echo "  PROJECT_ROOT=$PROJECT_ROOT"
  echo "  SESSION=$session_name"
  echo "  REPOS:"
  cat "$project_root/.workflow_artifacts/repos.md"
  echo

  # IVG-135 (T-03, required): pre-flight guard against a would-be socket path
  # that overflows the AF_UNIX sun_path cap (~104 bytes macOS / ~108 Linux).
  # Pinning ZELLIJ_SOCKET_DIR (above) fixes the common-case miscomputation but
  # does not bound session_name itself, so a pathologically long project
  # basename can still overflow. Best-effort heuristic (margin, not exact) —
  # the +20 fudges the "/<zellij-version>/" subdir + separators. Must run
  # AFTER session_name is finalized and BEFORE the create call below.
  _sock_len=$(( ${#ZELLIJ_SOCKET_DIR} + ${#session_name} + 20 ))
  if [ "$_sock_len" -gt 100 ]; then
    echo "agentdesk: session socket path would be too long (~${_sock_len} bytes; limit ~104)." >&2
    echo "  session: $session_name" >&2
    echo "  ZELLIJ_SOCKET_DIR: $ZELLIJ_SOCKET_DIR" >&2
    echo "  Fix: set a shorter ZELLIJ_SOCKET_DIR, or use --name <shorter-name>." >&2
    return 1
  fi

  # ── T-05: Inject Dashboard tab into chosen layout (D-04/CRIT-2 fix) ────────
  # When the dashboard decision above is yes, inject a Dashboard tab into
  # whichever layout was chosen — preserving ALL existing tabs. This is a new
  # temp file; $layout_path is updated to point to it. Otherwise $layout_path is
  # used unchanged.
  #
  # For a restored saved layout the tab is injected into the restored layout
  # path — no regeneration via _agentdesk_gen_layout. The saved layout file on
  # disk is never modified.
  if [ "$dash_decision" = "yes" ]; then
    dash_url_file="$(_agentdesk_dashboard_url_file)" || dash_url_file=""
  fi
  if [ -n "$dash_url_file" ]; then
    local dash_tab_tmp
    # BSD mktemp only randomizes *trailing* X's; a suffix after XXXXXX creates a
    # literal path that collides on the second call ("File exists"). Drop the suffix,
    # then rename to add .kdl — Zellij requires the .kdl extension to identify the
    # file as a layout (same pattern as _agentdesk_gen_layout).
    dash_tab_tmp="$(mktemp "${TMPDIR:-/tmp}/agentdesk-dash-layout-XXXXXX")" || {
      echo "agentdesk: failed to create temp dashboard layout file" >&2
      rm -f "$dash_url_file"
      return 1
    }
    mv -f "$dash_tab_tmp" "${dash_tab_tmp}.kdl" || { rm -f "$dash_tab_tmp" "$dash_url_file"; return 1; }
    dash_tab_tmp="${dash_tab_tmp}.kdl"
    # Strip the final `}` (closing brace of the top-level `layout { ... }` block)
    # and append the Dashboard tab KDL before re-closing.
    # The url-file path is expanded HERE at injection time so the actual path is
    # baked into the KDL — the poller reads the same path.
    sed '$d' "$layout_path" > "$dash_tab_tmp"
    _agentdesk_kdl_dashboard_tab "$layout_path" "$dash_url_file" >> "$dash_tab_tmp"
    # shellcheck disable=SC2064
    trap "rm -f $layout_tmp $dash_tab_tmp" EXIT INT TERM
    layout_path="$dash_tab_tmp"

    # D-03 bounded browser-open poller: spawned BEFORE zellij blocks the foreground.
    # Polls the url-file for ≤10s (50 × 0.2s hard cap), opens the browser, then exits.
    # Cannot orphan — always self-terminates. This is the ONLY `&` in the new flow.
    local _dash_url_file="$dash_url_file"
    ( i=0
      while [ "$i" -lt 50 ]; do
        if [ -f "$_dash_url_file" ] && grep -q '^http' "$_dash_url_file" 2>/dev/null; then
          url="$(grep '^http' "$_dash_url_file" | head -1)"
          _agentdesk_open_url "$url"
          rm -f "$_dash_url_file"  # first rm: success path
          break
        fi
        sleep 0.2
        i=$((i + 1))
      done
      rm -f "$_dash_url_file" ) &  # second rm: timeout path (idempotent; rm -f on missing file is no-op)
  fi

  # AGENTDESK_DASHBOARD_URL_FILE is set for this launch only (empty when the
  # dashboard is off, so a stale exported value never reaches pane commands).
  AGENTDESK_DASHBOARD_URL_FILE="$dash_url_file" PROJECT_ROOT="$project_root" zellij --new-session-with-layout "$layout_path" --session "$session_name" options --auto-layout false
}

agentdesk-attach() {
  case "${1:-}" in
    -h|--help)
      cat <<'HELP'
Usage:
  agentdesk-attach <session-name>    Attach to an existing Zellij session.
HELP
      return 0 ;;
  esac

  local session_name=""

  if [ "$#" -eq 0 ]; then
    agentdesk-sessions
    echo
    echo "Usage: agentdesk-attach <session-name>"
    return 1
  fi

  session_name="$1"

  if ! _zellij_session_exists "$session_name"; then
    echo "No such active session: $session_name"
    echo
    echo "Existing sessions:"
    agentdesk-sessions
    return 1
  fi

  zellij attach "$session_name"
}

repos() {
  case "${1:-}" in
    -h|--help)
      cat <<'HELP'
Usage:
  repos    List detected repos under PROJECT_ROOT (or $PWD).
HELP
      return 0 ;;
  esac

  if [ -z "${PROJECT_ROOT:-}" ]; then
    export PROJECT_ROOT="$PWD"
  fi

  _detect_repos
}

crepo() {
  case "${1:-}" in
    -h|--help)
      cat <<'HELP'
Usage:
  crepo    Fuzzy-pick a repo and cd into it (needs fzf).
HELP
      return 0 ;;
  esac

  if [ -z "${PROJECT_ROOT:-}" ]; then
    export PROJECT_ROOT="$PWD"
  fi

  if ! command -v fzf >/dev/null 2>&1; then
    echo "fzf is not installed. Install it with:"
    echo "  brew install fzf"
    return 1
  fi

  local repo
  repo="$(repos | fzf)"

  [ -z "$repo" ] && return 1

  cd "$(_repo_path "$repo")" || return 1
}

grepos() {
  case "${1:-}" in
    -h|--help)
      cat <<'HELP'
Usage:
  grepos    git status --short for every detected repo.
HELP
      return 0 ;;
  esac

  if [ -z "${PROJECT_ROOT:-}" ]; then
    export PROJECT_ROOT="$PWD"
  fi

  repos | while read -r repo; do
    [ -z "$repo" ] && continue

    echo

    if [ "$repo" = "." ]; then
      echo "===== . ====="
      git -C "$PROJECT_ROOT" status --short 2>/dev/null || true
    else
      echo "===== $repo ====="
      git -C "$PROJECT_ROOT/$repo" status --short 2>/dev/null || true
    fi
  done
}

gitreview() {
  case "${1:-}" in
    -h|--help)
      cat <<'HELP'
Usage:
  gitreview    Fuzzy-pick a repo, open lazygit (or git status) (needs fzf).
HELP
      return 0 ;;
  esac

  if [ -z "${PROJECT_ROOT:-}" ]; then
    export PROJECT_ROOT="$PWD"
  fi

  if ! command -v fzf >/dev/null 2>&1; then
    echo "fzf is not installed. Install it with:"
    echo "  brew install fzf"
    return 1
  fi

  local repo repo_path
  repo="$(repos | fzf --prompt='Select repo for git review: ')"

  [ -z "$repo" ] && return 1

  repo_path="$(_repo_path "$repo")"

  echo "Opening git review for: $repo"
  echo "Path: $repo_path"

  if command -v lazygit >/dev/null 2>&1; then
    (cd "$repo_path" && lazygit)
  else
    git -C "$repo_path" status
  fi
}

gitrootreview() {
  case "${1:-}" in
    -h|--help)
      cat <<'HELP'
Usage:
  gitrootreview    Open lazygit (or git status) at the project-root repo.
HELP
      return 0 ;;
  esac

  if [ -z "${PROJECT_ROOT:-}" ]; then
    export PROJECT_ROOT="$PWD"
  fi

  if [ -e "$PROJECT_ROOT/.git" ]; then
    if command -v lazygit >/dev/null 2>&1; then
      (cd "$PROJECT_ROOT" && lazygit)
    else
      git -C "$PROJECT_ROOT" status
    fi
  else
    echo "Project root is not a Git repo."
    echo "Use gitreview to select a child repo."
  fi
}

croot() {
  case "${1:-}" in
    -h|--help)
      cat <<'HELP'
Usage:
  croot    cd to PROJECT_ROOT (or the enclosing git root).
HELP
      return 0 ;;
  esac

  if [ -n "${PROJECT_ROOT:-}" ] && [ -d "$PROJECT_ROOT" ]; then
    cd "$PROJECT_ROOT" || return 1
  else
    local top
    top="$(git rev-parse --show-toplevel 2>/dev/null)"
    if [ -n "$top" ]; then
      cd "$top" || return 1
    else
      echo "PROJECT_ROOT is not set and current folder is not inside a Git repo."
      return 1
    fi
  fi
}

agentdesk-sessions() {
  case "${1:-}" in
    -h|--help)
      cat <<'HELP'
Usage:
  agentdesk-sessions    List active Zellij sessions.
HELP
      return 0 ;;
  esac

  if ! command -v zellij >/dev/null 2>&1; then
    echo "zellij is not installed."
    return 1
  fi

  zellij list-sessions
}

agentdesk-delete() {
  local force=""
  local session_name=""

  while [ "$#" -gt 0 ]; do
    case "$1" in
      --force|-f)
        force="--force"
        ;;
      -h|--help)
        cat <<'HELP'
Usage:
  agentdesk-delete <session-name>
  agentdesk-delete --force <session-name>
  agentdesk-delete <session-name> --force

Aliases:
  agentdesk-kill
HELP
        return 0
        ;;
      *)
        if [ -z "$session_name" ]; then
          session_name="$1"
        else
          echo "Unexpected argument: $1"
          return 1
        fi
        ;;
    esac
    shift
  done

  if [ -z "$session_name" ]; then
    echo "Please specify the session name to delete."
    echo
    echo "Existing sessions:"
    zellij list-sessions 2>/dev/null || true
    return 1
  fi

  if [ -n "$force" ]; then
    zellij delete-session "$session_name" --force
  else
    zellij delete-session "$session_name"
  fi
}

agentdesk-kill() {
  agentdesk-delete "$@"
}

codexpane() {
  case "${1:-}" in
    -h|--help)
      cat <<'HELP'
Usage:
  codexpane    Open Codex in a floating pane (needs zellij + codex).
HELP
      return 0 ;;
  esac

  local root="${PROJECT_ROOT:-$PWD}"

  if ! command -v zellij >/dev/null 2>&1; then
    echo "zellij is not installed."
    return 1
  fi

  if ! command -v codex >/dev/null 2>&1; then
    echo "codex command not found."
    return 1
  fi

  zellij run \
    --name "Codex" \
    --floating \
    --width "45%" \
    --height "85%" \
    --x "55%" \
    --y "5%" \
    -- zsh -lc "source \"\$HOME/.config/agentdesk/agentdesk.zsh\" 2>/dev/null || true; cd \"$root\" && echo 'Codex - project root:' \"\$PWD\" && codex"
}

codexright() {
  case "${1:-}" in
    -h|--help)
      cat <<'HELP'
Usage:
  codexright    Open Codex in a right-split pane (needs zellij + codex).
HELP
      return 0 ;;
  esac

  local root="${PROJECT_ROOT:-$PWD}"

  if ! command -v zellij >/dev/null 2>&1; then
    echo "zellij is not installed."
    return 1
  fi

  if ! command -v codex >/dev/null 2>&1; then
    echo "codex command not found."
    return 1
  fi

  zellij run \
    --name "Codex" \
    --direction right \
    -- zsh -lc "source \"\$HOME/.config/agentdesk/agentdesk.zsh\" 2>/dev/null || true; cd \"$root\" && echo 'Codex - project root:' \"\$PWD\" && codex"
}

codexcritic() {
  case "${1:-}" in
    -h|--help)
      cat <<'HELP'
Usage:
  codexcritic    Open a Codex critic in a floating pane (needs zellij + codex).
HELP
      return 0 ;;
  esac

  local root="${PROJECT_ROOT:-$PWD}"

  if ! command -v zellij >/dev/null 2>&1; then
    echo "zellij is not installed."
    return 1
  fi

  if ! command -v codex >/dev/null 2>&1; then
    echo "codex command not found."
    return 1
  fi

  zellij run \
    --name "Codex Critic" \
    --floating \
    --width "50%" \
    --height "85%" \
    --x "50%" \
    --y "5%" \
    -- zsh -lc "source \"\$HOME/.config/agentdesk/agentdesk.zsh\" 2>/dev/null || true; cd \"$root\" && echo 'Codex Critic - project root:' \"\$PWD\" && codex"
}

claudepane() {
  case "${1:-}" in
    -h|--help)
      cat <<'HELP'
Usage:
  claudepane    Open Claude Code in a right-split pane (needs zellij + claude).
HELP
      return 0 ;;
  esac

  local root="${PROJECT_ROOT:-$PWD}"

  if ! command -v zellij >/dev/null 2>&1; then
    echo "zellij is not installed."
    return 1
  fi

  if ! command -v claude >/dev/null 2>&1; then
    echo "claude command not found."
    return 1
  fi

  zellij run \
    --name "Claude" \
    --direction right \
    -- zsh -lc "source \"\$HOME/.config/agentdesk/agentdesk.zsh\" 2>/dev/null || true; cd \"$root\" && echo 'Claude Code - project root:' \"\$PWD\" && claude"
}

agentprompt() {
  case "${1:-}" in
    -h|--help)
      cat <<'HELP'
Usage:
  agentprompt    Print the standard agent bootstrap prompt to stdout.
                 Pipe into your agent, e.g. agentprompt | pbcopy
HELP
      return 0 ;;
  esac

  cat <<'PROMPT'
You are working from a project root that may either be a Git repo itself or may contain several child Git repositories.

First read:
- .workflow_artifacts/repos.md
- .workflow_artifacts/current-plan.md if present
- .workflow_artifacts/lessons-learned.md if relevant

Repository convention:
- If repos.md contains ".", the project root itself is the repo.
- If repos.md contains child paths, choose the relevant child repo before editing.

Before editing, identify which repo contains the relevant code.

For every task, maintain:
- the current plan in .workflow_artifacts/current-plan.md
- important review notes in .workflow_artifacts/review.md
- durable lessons in .workflow_artifacts/lessons-learned.md

When making changes, report:
- repo path
- files changed
- tests run from that repo
- cross-repo assumptions
PROMPT
}

codexcriticprompt() {
  case "${1:-}" in
    -h|--help)
      cat <<'HELP'
Usage:
  codexcriticprompt    Print the critic-agent prompt to stdout.
                        Pipe into your agent, e.g. codexcriticprompt | pbcopy
HELP
      return 0 ;;
  esac

  cat <<'PROMPT'
You are the critic agent, not the primary implementation agent.

Read:
- .workflow_artifacts/current-plan.md
- .workflow_artifacts/repos.md
- current git diff across relevant repos

Do not edit files unless explicitly asked.

Return:
- critical risks
- likely bugs
- missing tests
- cross-repo assumptions
- suggested minimal changes
PROMPT
}
