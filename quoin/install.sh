#!/usr/bin/env bash
# Quoin installer — offline-first thin wrapper that delegates to `quoin install`.
#
# Usage: bash install.sh [--dev] [--upgrade] [--use-pip] [--force-merge]
#                        [--scope user|project[:DIR]] [--claude-md-variant full|slim]
#                        [--allow-hook-merge] [--autocompact-pct N]
#                        [--autocompact-window TOKENS] [--clear-autocompact-env]
#                        [--with-context-tracker] [--remove-context-tracker]
#                        [--print-python] [-h]
#
# Python: any interpreter meeting the project minimum (pyproject.toml
# requires-python, 3.10 by default) is accepted. Set QUOIN_PYTHON=/path/to/python
# to pick one explicitly; --print-python shows which one would be used.
#
# Tier 1 (fast, no network): installed version matches local → exec quoin install
# Tier 2 (offline stdlib):   quoin not installed → PYTHONPATH=src/ exec python -m quoin
# Tier 3 (network, opt-in):  version mismatch or --upgrade/--use-pip → pip install -e .
#
# Agentdesk: the Python installer (quoin install) deploys agentdesk tool files to
# ~/.config/agentdesk/ automatically for user-mode installs. After quoin install
# completes, it will print a hint to run setup-agentdesk.sh for the full setup
# (installs zellij, lazygit, fzf via Homebrew and patches ~/.zshrc). That step
# is intentionally NOT auto-run here — it modifies system state and requires
# explicit user consent.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# ── Argument parsing ──────────────────────────────────────────────────────────
DEV_FLAG=""
FORCE_MERGE_FLAG=""
SCOPE_FLAG=""
CLAUDE_MD_VARIANT_FLAG=""
ALLOW_HOOK_MERGE_FLAG=""
AUTOCOMPACT_PCT_FLAG=""
AUTOCOMPACT_WINDOW_FLAG=""
CLEAR_AUTOCOMPACT_ENV_FLAG=""
WITH_CONTEXT_TRACKER_FLAG=""
REMOVE_CONTEXT_TRACKER_FLAG=""
USE_PIP=0
PIP_UPGRADE_FLAG=""
PRINT_PYTHON=0

# Two-pass arg loop: consume --scope value (which may be a separate token or
# combined as --scope=project:/path).  We collect non-consumed args in REST so
# that unknown flags still get the "ignored" warning.
REST=()
i=0
ARGS=("$@")
while [[ $i -lt ${#ARGS[@]} ]]; do
  arg="${ARGS[$i]}"
  case "$arg" in
    --dev)          DEV_FLAG="--dev" ;;
    --upgrade)      USE_PIP=1; PIP_UPGRADE_FLAG="--upgrade" ;;
    --use-pip)      USE_PIP=1 ;;
    --print-python) PRINT_PYTHON=1 ;;
    --force-merge)  FORCE_MERGE_FLAG="--force-merge" ;;
    --allow-hook-merge) ALLOW_HOOK_MERGE_FLAG="--allow-hook-merge" ;;
    --clear-autocompact-env) CLEAR_AUTOCOMPACT_ENV_FLAG="--clear-autocompact-env" ;;
    --with-context-tracker) WITH_CONTEXT_TRACKER_FLAG="--with-context-tracker" ;;
    --remove-context-tracker) REMOVE_CONTEXT_TRACKER_FLAG="--remove-context-tracker" ;;
    --autocompact-pct=*)
      if [[ -n "${arg#--autocompact-pct=}" ]]; then
        AUTOCOMPACT_PCT_FLAG="--autocompact-pct ${arg#--autocompact-pct=}"
      else
        echo "quoin: --autocompact-pct requires a value (1..100)" >&2
        exit 2
      fi
      ;;
    --autocompact-pct)
      i=$(( i + 1 ))
      if [[ $i -lt ${#ARGS[@]} ]]; then
        AUTOCOMPACT_PCT_FLAG="--autocompact-pct ${ARGS[$i]}"
      else
        echo "quoin: --autocompact-pct requires a value (1..100)" >&2
        exit 2
      fi
      ;;
    --autocompact-window=*)
      if [[ -n "${arg#--autocompact-window=}" ]]; then
        AUTOCOMPACT_WINDOW_FLAG="--autocompact-window ${arg#--autocompact-window=}"
      else
        echo "quoin: --autocompact-window requires a value (100000..1000000)" >&2
        exit 2
      fi
      ;;
    --autocompact-window)
      i=$(( i + 1 ))
      if [[ $i -lt ${#ARGS[@]} ]]; then
        AUTOCOMPACT_WINDOW_FLAG="--autocompact-window ${ARGS[$i]}"
      else
        echo "quoin: --autocompact-window requires a value (100000..1000000)" >&2
        exit 2
      fi
      ;;
    --scope=*)
      if [[ -n "${arg#--scope=}" ]]; then
        SCOPE_FLAG="--scope ${arg#--scope=}"
      else
        echo "quoin: --scope requires a value (user or project[:DIR])" >&2
        exit 2
      fi
      ;;
    --scope)
      i=$(( i + 1 ))
      if [[ $i -lt ${#ARGS[@]} ]]; then
        SCOPE_FLAG="--scope ${ARGS[$i]}"
      else
        echo "quoin: --scope requires a value (user or project[:DIR])" >&2
        exit 2
      fi
      ;;
    --claude-md-variant=*)
      if [[ -n "${arg#--claude-md-variant=}" ]]; then
        CLAUDE_MD_VARIANT_FLAG="--claude-md-variant ${arg#--claude-md-variant=}"
      else
        echo "quoin: --claude-md-variant requires a value (full or slim)" >&2
        exit 2
      fi
      ;;
    --claude-md-variant)
      i=$(( i + 1 ))
      if [[ $i -lt ${#ARGS[@]} ]]; then
        CLAUDE_MD_VARIANT_FLAG="--claude-md-variant ${ARGS[$i]}"
      else
        echo "quoin: --claude-md-variant requires a value (full or slim)" >&2
        exit 2
      fi
      ;;
    -h|--help)
      echo "Usage: bash install.sh [--dev] [--upgrade] [--use-pip] [--force-merge]"
      echo "                       [--scope user|project[:DIR]] [--claude-md-variant full|slim]"
      echo "                       [--allow-hook-merge] [--autocompact-pct N]"
      echo "                       [--autocompact-window TOKENS] [--clear-autocompact-env]"
      echo "                       [--with-context-tracker] [--remove-context-tracker]"
      echo "                       [--print-python]"
      echo "  --dev                Install dev dependencies (pyyaml, pytest)"
      echo "  --upgrade            Re-install via pip before deploying (alias: --use-pip)"
      echo "  --use-pip            Same as --upgrade"
      echo "  --print-python       Print the Python interpreter the installer would use, then exit."
      echo "                       Set QUOIN_PYTHON=/path/to/python to choose one explicitly."
      echo "  --force-merge        Keep first DEV WORKFLOW marker pair; remove extras"
      echo "  --scope user         Install globally under ~/.claude/"
      echo "  --scope project      Install under <CWD>/.claude/ instead of ~/.claude/."
      echo "                       All skills, scripts, hooks, and CLAUDE.md will be"
      echo "                       project-scoped. Hooks register in <project>/.claude/settings.json"
      echo "                       only. Note: for skills, Claude Code personal scope overrides"
      echo "                       project scope — a prior home install shadows project skills."
      echo "                       Run 'quoin doctor --scope project' to detect conflicts."
      echo "  --scope project:/path Install under /path/.claude/ (explicit project root)"
      echo "  --claude-md-variant full|slim  CLAUDE.md variant to merge (default: full)."
      echo "                       'slim' is a project-scope pilot only this wave (IVG-164);"
      echo "                       requires --scope project."
      echo "  --allow-hook-merge   Proceed even if home ~/.claude/settings.json has quoin"
      echo "                       hook stanzas (default: fail-fast to avoid double-fire)"
      echo "  --autocompact-pct N  Opt-in: write CLAUDE_AUTOCOMPACT_PCT_OVERRIDE=N (1..100) to"
      echo "                       settings.json's env block, delegating the auto-compaction"
      echo "                       trigger to the platform. Off by default."
      echo "  --autocompact-window TOKENS"
      echo "                       Opt-in: write CLAUDE_CODE_AUTO_COMPACT_WINDOW=TOKENS"
      echo "                       (100000..1000000, plain integer, no suffix) to settings.json's"
      echo "                       env block. Independent of --autocompact-pct."
      echo "  --clear-autocompact-env"
      echo "                       Remove quoin's two autocompact env keys from settings.json's"
      echo "                       env block. Mutually exclusive with the two flags above."
      echo "  --with-context-tracker"
      echo "                       Opt-in: deploy the context-tracker mod (/ctx pane) to"
      echo "                       skills/context-tracker/. Off by default."
      echo "  --remove-context-tracker"
      echo "                       Remove the context-tracker mod folder. Mutually exclusive with"
      echo "                       --with-context-tracker."
      exit 0
      ;;
    *)  REST+=("$arg") ;;
  esac
  i=$(( i + 1 ))
done

for unknown in "${REST[@]+"${REST[@]}"}"; do
  echo "Warning: unknown argument: $unknown (ignored)" >&2
done

# ── Find Python interpreter ───────────────────────────────────────────────────
# quoin needs the Python minimum declared in pyproject.toml (3.10 by default).
# Any interpreter that meets it is accepted, wherever it lives. Order:
#   1. $QUOIN_PYTHON, if set
#   2. every directory on PATH, in PATH order: python3.N (highest N first),
#      then python3, then python
#   3. well-known install locations that are often not on PATH (Homebrew,
#      python.org, pyenv, uv, asdf, mise, conda, MacPorts); replace the list
#      with QUOIN_PYTHON_SEARCH_DIRS (colon-separated; empty disables it)
#   4. `uv python find` when uv is on PATH
# The first executable file that reports a version at or above the minimum wins.
MIN_MAJOR=3
MIN_MINOR=10
TRIED=()
SEEN_PYTHONS=()
PYTHON=""
PYTHON_VERSION=""
_QUOIN_NUM_RE='^[0-9]+$'
_QUOIN_PY3N_RE='^python3\.[0-9]+$'

_quoin_min_python() {
  local line
  local re='^[[:space:]]*requires-python[[:space:]]*=[[:space:]]*"[[:space:]]*>=[[:space:]]*([0-9]+)\.([0-9]+)'
  [[ -f "$PROJECT_ROOT/pyproject.toml" ]] || return 0
  while IFS= read -r line || [[ -n $line ]]; do
    if [[ $line =~ $re ]]; then
      MIN_MAJOR="${BASH_REMATCH[1]}"
      MIN_MINOR="${BASH_REMATCH[2]}"
      return 0
    fi
  done < "$PROJECT_ROOT/pyproject.toml"
  return 0
}

# Runs the version probe with a time limit so a hung shim cannot stall the
# installer. Output goes to a temp file (not a pipe) so orphaned children of a
# killed interpreter cannot keep the capture open. Sets _QUOIN_PROBE_OUT.
_quoin_run_probe() {
  local cand="$1" limit="${QUOIN_PROBE_TIMEOUT:-10}" tmp pid wd tool=""
  local code="import sys; v=sys.version_info; print(v.major * 1000 + v.minor)"
  _QUOIN_PROBE_OUT=""
  [[ $limit =~ $_QUOIN_NUM_RE ]] || limit=10
  tmp="$(mktemp "${TMPDIR:-/tmp}/quoin-probe.XXXXXX" 2>/dev/null)" || tmp=""
  if [[ -z "$tmp" ]]; then
    _QUOIN_PROBE_OUT="$("$cand" -c "$code" 2>/dev/null)" || _QUOIN_PROBE_OUT=""
    return 0
  fi
  if command -v timeout >/dev/null 2>&1; then
    tool=timeout
  elif command -v gtimeout >/dev/null 2>&1; then
    tool=gtimeout
  fi
  if [[ -n "$tool" ]]; then
    "$tool" "$limit" "$cand" -c "$code" >"$tmp" 2>/dev/null </dev/null || true
  else
    "$cand" -c "$code" >"$tmp" 2>/dev/null </dev/null &
    pid=$!
    ( sleep "$limit"; kill "$pid" 2>/dev/null ) >/dev/null 2>&1 &
    wd=$!
    wait "$pid" 2>/dev/null || true
    kill "$wd" 2>/dev/null || true
    wait "$wd" 2>/dev/null || true
  fi
  IFS= read -r _QUOIN_PROBE_OUT < "$tmp" || true
  rm -f "$tmp" 2>/dev/null || true
  return 0
}

# Sets _QUOIN_PROBED to major*1000+minor; records why on failure.
_quoin_probe() {
  local cand="$1" out
  _QUOIN_PROBED=""
  if [[ -L "$cand" && ! -e "$cand" ]]; then
    TRIED+=("$cand: dangling link")
    return 1
  fi
  # Absent candidates (most python/python3 names per directory) are not worth
  # listing in the failure diagnostic.
  [[ -e "$cand" ]] || return 1
  if [[ ! -f "$cand" || ! -x "$cand" ]]; then
    TRIED+=("$cand: not an executable file")
    return 1
  fi
  _quoin_run_probe "$cand"
  out="$_QUOIN_PROBE_OUT"
  if [[ ! $out =~ $_QUOIN_NUM_RE ]]; then
    TRIED+=("$cand: not runnable or timed out")
    return 1
  fi
  _QUOIN_PROBED="$out"
  return 0
}

# Accepts the candidate when it meets the minimum; sets PYTHON on success.
_quoin_try() {
  local cand="$1" seen
  [[ -n "$cand" ]] || return 1
  for seen in ${SEEN_PYTHONS[@]+"${SEEN_PYTHONS[@]}"}; do
    [[ "$seen" == "$cand" ]] && return 1
  done
  SEEN_PYTHONS+=("$cand")
  _quoin_probe "$cand" || return 1
  if (( _QUOIN_PROBED >= MIN_MAJOR * 1000 + MIN_MINOR )); then
    PYTHON="$cand"
    PYTHON_VERSION="$(( _QUOIN_PROBED / 1000 )).$(( _QUOIN_PROBED % 1000 ))"
    return 0
  fi
  TRIED+=("$cand: version $(( _QUOIN_PROBED / 1000 )).$(( _QUOIN_PROBED % 1000 ))")
  return 1
}

# Try python3.N (highest N first), python3, python inside one directory.
_quoin_try_dir() {
  local dir="$1" f name minor i j tmp ng
  local names=()
  [[ -d "$dir" ]] || return 1
  ng="$(shopt -p nullglob)" || true
  shopt -s nullglob
  for f in "$dir"/python3.*; do
    name="${f##*/}"
    if [[ $name =~ $_QUOIN_PY3N_RE ]]; then
      names+=("$name")
    fi
  done
  eval "$ng"
  # Insertion sort by minor number, descending (no sort -V on bash 3.2 / BSD).
  i=1
  while (( i < ${#names[@]} )); do
    tmp="${names[$i]}"
    j=$(( i - 1 ))
    while (( j >= 0 )) && (( ${names[$j]#python3.} < ${tmp#python3.} )); do
      names[$(( j + 1 ))]="${names[$j]}"
      j=$(( j - 1 ))
    done
    names[$(( j + 1 ))]="$tmp"
    i=$(( i + 1 ))
  done
  for name in ${names[@]+"${names[@]}"} python3 python; do
    if _quoin_try "$dir/$name"; then
      return 0
    fi
  done
  return 1
}

# Expand glob patterns (passed already expanded by the caller) newest-first.
_quoin_try_dirs_reversed() {
  local n=$# i
  local dirs=("$@")
  i=$n
  while (( i > 0 )); do
    i=$(( i - 1 ))
    if _quoin_try_dir "${dirs[$i]}"; then
      return 0
    fi
  done
  return 1
}

_quoin_find_python() {
  local cand dir ng p root
  local pdirs=() extra=()

  # 1. Explicit override
  if [[ -n "${QUOIN_PYTHON:-}" ]]; then
    cand="$QUOIN_PYTHON"
    if [[ "$cand" != */* ]]; then
      cand="$(command -v "$cand" 2>/dev/null || true)"
      [[ -n "$cand" ]] || cand="$QUOIN_PYTHON"
    fi
    if _quoin_try "$cand"; then
      return 0
    fi
    echo "quoin: warning: QUOIN_PYTHON=$QUOIN_PYTHON is not usable (${TRIED[$(( ${#TRIED[@]} - 1 ))]:-no details}); searching for another Python" >&2
  fi

  # 2. Every directory on PATH, in PATH order
  IFS=: read -r -a pdirs <<< "${PATH:-}"
  for dir in ${pdirs[@]+"${pdirs[@]}"}; do
    [[ -n "$dir" ]] || continue
    if _quoin_try_dir "$dir"; then
      return 0
    fi
  done

  # 3. Well-known locations that are often not on PATH
  if [[ -n "${QUOIN_PYTHON_SEARCH_DIRS+set}" ]]; then
    IFS=: read -r -a extra <<< "$QUOIN_PYTHON_SEARCH_DIRS"
    for dir in ${extra[@]+"${extra[@]}"}; do
      [[ -n "$dir" ]] || continue
      if _quoin_try_dir "$dir"; then
        return 0
      fi
    done
  else
    ng="$(shopt -p nullglob)" || true
    shopt -s nullglob
    local home="${HOME:-}"
    for dir in /opt/homebrew/bin /usr/local/bin; do
      if _quoin_try_dir "$dir"; then eval "$ng"; return 0; fi
    done
    for p in \
      "/opt/homebrew/opt/python@3.*/bin" \
      "/usr/local/opt/python@3.*/bin" \
      "/Library/Frameworks/Python.framework/Versions/3.*/bin"; do
      # shellcheck disable=SC2206
      local ex=( $p )
      if (( ${#ex[@]} > 0 )) && _quoin_try_dirs_reversed "${ex[@]}"; then eval "$ng"; return 0; fi
    done
    if [[ -n "$home" ]]; then
      local pyenv_root="${PYENV_ROOT:-$home/.pyenv}"
      local ex2=()
      for dir in "$pyenv_root"/versions/*/bin \
                 "$home"/.local/share/uv/python/*/bin \
                 "$home"/.asdf/installs/python/*/bin \
                 "$home"/.local/share/mise/installs/python/*/bin; do
        ex2+=("$dir")
      done
      if (( ${#ex2[@]} > 0 )) && _quoin_try_dirs_reversed "${ex2[@]}"; then eval "$ng"; return 0; fi
      for root in "$home/miniconda3" "$home/anaconda3" "$home/miniforge3" \
                  "$home/opt/anaconda3" "$home/opt/miniconda3"; do
        if _quoin_try_dir "$root/bin"; then eval "$ng"; return 0; fi
      done
    fi
    for root in /opt/conda /opt/miniconda3 /opt/anaconda3 /opt/miniforge3 \
                /opt/homebrew/Caskroom/miniconda/base \
                /opt/homebrew/Caskroom/miniforge/base; do
      if _quoin_try_dir "$root/bin"; then eval "$ng"; return 0; fi
    done
    # Named conda environments under each conda root
    local envdirs=()
    for root in ${home:+"$home/miniconda3" "$home/anaconda3" "$home/miniforge3" \
                  "$home/opt/anaconda3" "$home/opt/miniconda3"} \
                /opt/conda /opt/miniconda3 /opt/anaconda3 /opt/miniforge3 \
                /opt/homebrew/Caskroom/miniconda/base \
                /opt/homebrew/Caskroom/miniforge/base; do
      for dir in "$root"/envs/*/bin; do
        envdirs+=("$dir")
      done
    done
    if (( ${#envdirs[@]} > 0 )) && _quoin_try_dirs_reversed "${envdirs[@]}"; then eval "$ng"; return 0; fi
    for dir in /opt/local/bin /home/linuxbrew/.linuxbrew/bin \
               ${home:+"$home/.linuxbrew/bin" "$home/.local/bin"}; do
      if _quoin_try_dir "$dir"; then eval "$ng"; return 0; fi
    done
    eval "$ng"
  fi

  # 4. uv-managed interpreters
  if command -v uv >/dev/null 2>&1; then
    cand="$(uv python find ">=${MIN_MAJOR}.${MIN_MINOR}" 2>/dev/null || true)"
    if [[ -n "$cand" ]] && _quoin_try "$cand"; then
      return 0
    fi
  fi
  return 1
}

_quoin_min_python
if ! _quoin_find_python; then
  echo "quoin: Python ${MIN_MAJOR}.${MIN_MINOR}+ required (pyproject.toml requires-python >=${MIN_MAJOR}.${MIN_MINOR}) but no suitable interpreter was found." >&2
  if (( ${#TRIED[@]} > 0 )); then
    echo "quoin: interpreters tried:" >&2
    for _t in "${TRIED[@]}"; do
      echo "  $_t" >&2
    done
  else
    echo "quoin: no python interpreters were found on PATH or in the usual install locations." >&2
  fi
  echo "quoin: install Python ${MIN_MAJOR}.${MIN_MINOR}+ or set QUOIN_PYTHON=/path/to/python3 and re-run." >&2
  exit 1
fi
# Probes below run from another directory, so the interpreter path must not
# depend on the current one.
case "$PYTHON" in
  /*) ;;
  ./*) PYTHON="$PWD/${PYTHON#./}" ;;
  *)   PYTHON="$PWD/$PYTHON" ;;
esac
echo "quoin: using Python ${PYTHON_VERSION} at ${PYTHON}" >&2

if [[ "$PRINT_PYTHON" -eq 1 ]]; then
  echo "$PYTHON"
  exit 0
fi

# ── Interactive scope prompt (when --scope not provided) ─────────────────────
if [[ -z "$SCOPE_FLAG" ]]; then
  if [[ -t 0 ]]; then
    echo ""
    echo "Where should quoin install?"
    echo "  g) Global  — ~/.claude/  (all Claude Code sessions on this machine)"
    echo "  p) Project — ./.claude/  (this project only)"
    echo ""
    while true; do
      read -rp "Choose [g/p] (default: g): " _scope_answer
      case "${_scope_answer:-g}" in
        g|G|global|user)  SCOPE_FLAG="--scope user";    break ;;
        p|P|project)      SCOPE_FLAG="--scope project"; break ;;
        *) echo "Please enter 'g' for global or 'p' for project." ;;
      esac
    done
  else
    echo "quoin: non-interactive mode — defaulting to --scope user (global ~/.claude/)" >&2
    SCOPE_FLAG="--scope user"
  fi
fi

# Build forwarded args for `quoin install` (array preserves paths with spaces)
INSTALL_ARGS=("install" "--source-dir" "$SCRIPT_DIR")
[[ -n "$DEV_FLAG" ]]              && INSTALL_ARGS+=("$DEV_FLAG")
[[ -n "$FORCE_MERGE_FLAG" ]]      && INSTALL_ARGS+=("$FORCE_MERGE_FLAG")
# Forward --scope flag (stored as "--scope value" string; split into two tokens)
if [[ -n "$SCOPE_FLAG" ]]; then
  # Split "--scope value" into separate array elements (handles project:/path with colon)
  read -r _scope_key _scope_val <<< "$SCOPE_FLAG"
  INSTALL_ARGS+=("$_scope_key" "$_scope_val")
fi
if [[ -n "$CLAUDE_MD_VARIANT_FLAG" ]]; then
  read -r _variant_key _variant_val <<< "$CLAUDE_MD_VARIANT_FLAG"
  INSTALL_ARGS+=("$_variant_key" "$_variant_val")
fi
[[ -n "$ALLOW_HOOK_MERGE_FLAG" ]] && INSTALL_ARGS+=("$ALLOW_HOOK_MERGE_FLAG")
if [[ -n "$AUTOCOMPACT_PCT_FLAG" ]]; then
  read -r _pct_key _pct_val <<< "$AUTOCOMPACT_PCT_FLAG"
  INSTALL_ARGS+=("$_pct_key" "$_pct_val")
fi
if [[ -n "$AUTOCOMPACT_WINDOW_FLAG" ]]; then
  read -r _window_key _window_val <<< "$AUTOCOMPACT_WINDOW_FLAG"
  INSTALL_ARGS+=("$_window_key" "$_window_val")
fi
[[ -n "$CLEAR_AUTOCOMPACT_ENV_FLAG" ]] && INSTALL_ARGS+=("$CLEAR_AUTOCOMPACT_ENV_FLAG")
[[ -n "$WITH_CONTEXT_TRACKER_FLAG" ]] && INSTALL_ARGS+=("$WITH_CONTEXT_TRACKER_FLAG")
[[ -n "$REMOVE_CONTEXT_TRACKER_FLAG" ]] && INSTALL_ARGS+=("$REMOVE_CONTEXT_TRACKER_FLAG")

# ── Get versions ──────────────────────────────────────────────────────────────
# Every "is quoin importable on its own" probe runs from a neutral directory.
# `python -c` puts the current directory on sys.path, and both the project root
# and the repo root contain a `quoin/` directory without __init__.py, which
# would import as an empty namespace package and fake an installed quoin.
LOCAL_VERSION="$(cd / && PYTHONPATH="$PROJECT_ROOT/src" "$PYTHON" -c \
  'from quoin.__about__ import __version__; print(__version__)' 2>/dev/null || true)"

INSTALLED_VERSION="$(cd / && "$PYTHON" -m quoin --version 2>/dev/null | \
  awk '{print $2}' || true)"

# ── Tier 1: installed version matches local — no pip needed ───────────────────
if [[ -n "$INSTALLED_VERSION" && -n "$LOCAL_VERSION" \
      && "$INSTALLED_VERSION" == "$LOCAL_VERSION" && "$USE_PIP" -eq 0 ]]; then
  exec "$PYTHON" -m quoin "${INSTALL_ARGS[@]}"
fi

# ── Tier 2: quoin not normally importable — use PYTHONPATH src/ fallback ─────
if ! (cd / && "$PYTHON" -c 'import quoin; assert quoin.__file__ is not None' 2>/dev/null); then
  if (cd / && PYTHONPATH="$PROJECT_ROOT/src" "$PYTHON" -c 'import quoin; assert quoin.__file__ is not None' 2>/dev/null); then
    exec env PYTHONPATH="$PROJECT_ROOT/src" "$PYTHON" -m quoin "${INSTALL_ARGS[@]}"
  fi
fi

# ── Tier 3: pip install (version mismatch, empty install, or explicit --upgrade)
if [[ "$USE_PIP" -eq 1 ]] \
   || [[ -z "$INSTALLED_VERSION" ]] \
   || [[ "$INSTALLED_VERSION" != "$LOCAL_VERSION" ]]; then
  # pip refuses --user inside a virtual environment.
  PIP_USER=(--user)
  if "$PYTHON" -c 'import sys; sys.exit(0 if sys.prefix != sys.base_prefix else 1)' 2>/dev/null; then
    PIP_USER=()
  fi

  if ! "$PYTHON" -m pip install ${PIP_USER[@]+"${PIP_USER[@]}"} $PIP_UPGRADE_FLAG -e "$PROJECT_ROOT"; then
    if [[ "$USE_PIP" -eq 1 ]]; then
      echo "quoin: pip install failed for $PYTHON (the interpreter may be externally managed, have no pip, or be offline); --use-pip/--upgrade was requested, so the install stops here. Re-run without that flag to deploy from the source tree." >&2
      exit 1
    fi
    if (cd / && PYTHONPATH="$PROJECT_ROOT/src" "$PYTHON" -c 'import quoin; assert quoin.__file__ is not None' 2>/dev/null); then
      if [[ -n "$INSTALLED_VERSION" ]]; then
        _stale_note="the 'quoin' command on PATH is still version $INSTALLED_VERSION"
      else
        _stale_note="no 'quoin' command is installed for this Python"
      fi
      echo "quoin: could not reinstall via pip (externally managed Python, no pip, or offline); deploying from the source tree. ${_stale_note}. Re-run with --use-pip after fixing pip to update it." >&2
      exec env PYTHONPATH="$PROJECT_ROOT/src" "$PYTHON" -m quoin "${INSTALL_ARGS[@]}"
    fi
    echo "wrapper logic error — pip install failed and the source tree is not importable; INSTALLED_VERSION=$INSTALLED_VERSION, LOCAL_VERSION=$LOCAL_VERSION; please file an issue with this output" >&2
    exit 1
  fi

  # Post-pip import gate (MAJ-2 round-4 fix)
  if ! (cd / && "$PYTHON" -c 'import quoin; assert quoin.__file__ is not None' 2>/dev/null); then
    IMPORTABLE=no
    echo "wrapper logic error — INSTALLED_VERSION=$INSTALLED_VERSION, LOCAL_VERSION=$LOCAL_VERSION, src/quoin importable=$IMPORTABLE; please file an issue with this output" >&2
    exit 1
  fi

  exec "$PYTHON" -m quoin "${INSTALL_ARGS[@]}"
fi

# ── Defensive abort (should be unreachable) ──────────────────────────────────
IMPORTABLE="$(cd / && PYTHONPATH="$PROJECT_ROOT/src" "$PYTHON" -c 'import quoin' 2>/dev/null \
  && echo yes || echo no)"
echo "wrapper logic error — INSTALLED_VERSION=$INSTALLED_VERSION, LOCAL_VERSION=$LOCAL_VERSION, src/quoin importable=$IMPORTABLE; please file an issue with this output" >&2
exit 1
