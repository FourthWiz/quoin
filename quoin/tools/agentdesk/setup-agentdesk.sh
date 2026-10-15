#!/usr/bin/env bash
set -euo pipefail

echo "============================================================"
echo "Agent Desk setup"
echo "Ghostty + Zellij + Claude default + Codex on demand"
echo "============================================================"
echo

ZSHRC="$HOME/.zshrc"
AGENTDESK_DIR="$HOME/.config/agentdesk"
AGENTDESK_HELPERS="$AGENTDESK_DIR/agentdesk.zsh"
ZELLIJ_LAYOUT_DIR="$HOME/.config/zellij/layouts"
ZELLIJ_LAYOUT="$ZELLIJ_LAYOUT_DIR/agent-desk.kdl"
ZELLIJ_CONFIG_DIR="$HOME/.config/zellij"
ZELLIJ_CONFIG="$ZELLIJ_CONFIG_DIR/config.kdl"

SOURCE_LINE='[ -f "$HOME/.config/agentdesk/agentdesk.zsh" ] && source "$HOME/.config/agentdesk/agentdesk.zsh"'

timestamp() {
  date +%Y%m%d-%H%M%S
}

write_if_changed() {
  local target="$1"
  local tmp="$2"
  local backup_suffix="$3"

  if [ -f "$target" ] && cmp -s "$target" "$tmp"; then
    echo "Unchanged: $target"
    rm -f "$tmp"
    return 0
  fi

  if [ -f "$target" ]; then
    local backup="${target}.${backup_suffix}.$(timestamp).bak"
    cp "$target" "$backup"
    echo "Backup created: $backup"
  fi

  mv "$tmp" "$target"
  echo "Updated: $target"
}

add_homebrew_shellenv_once() {
  local brew_path="$1"
  local line="eval \"\$(${brew_path} shellenv)\""

  touch "$ZSHRC"

  if grep -Fq "$line" "$ZSHRC"; then
    echo "Homebrew shellenv already present in ~/.zshrc"
  else
    {
      echo
      echo "# Homebrew"
      echo "$line"
    } >> "$ZSHRC"
    echo "Added Homebrew shellenv to ~/.zshrc"
  fi
}

install_homebrew_if_missing() {
  if command -v brew >/dev/null 2>&1; then
    echo "Found Homebrew: $(brew --version | head -n 1)"
    return 0
  fi

  echo "Homebrew not found. Installing Homebrew..."
  /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"

  if [ -x /opt/homebrew/bin/brew ]; then
    eval "$(/opt/homebrew/bin/brew shellenv)"
    add_homebrew_shellenv_once "/opt/homebrew/bin/brew"
  elif [ -x /usr/local/bin/brew ]; then
    eval "$(/usr/local/bin/brew shellenv)"
    add_homebrew_shellenv_once "/usr/local/bin/brew"
  fi

  if ! command -v brew >/dev/null 2>&1; then
    echo "Homebrew installation finished, but brew is still not on PATH."
    echo "Restart the terminal, then rerun this script."
    exit 1
  fi
}

brew_install_if_missing() {
  local formula="$1"
  local command_name="${2:-$formula}"

  if command -v "$command_name" >/dev/null 2>&1; then
    echo "Found: $command_name"
  else
    echo "Installing: $formula"
    brew install "$formula"
  fi
}

brew_cask_install_if_missing() {
  local cask="$1"
  local app_path="$2"

  if [ -e "$app_path" ]; then
    echo "Found app: $app_path"
  elif brew list --cask "$cask" >/dev/null 2>&1; then
    echo "Found cask: $cask"
  else
    echo "Installing cask: $cask"
    brew install --cask "$cask" || {
      echo "Could not install $cask via Homebrew Cask."
      echo "Agent Desk still works in another terminal."
    }
  fi
}

patch_zshrc_once() {
  touch "$ZSHRC"

  if grep -Fq "$SOURCE_LINE" "$ZSHRC"; then
    echo "~/.zshrc already sources Agent Desk helpers"
    return 0
  fi

  local backup="$HOME/.zshrc.agentdesk.backup.$(timestamp)"
  cp "$ZSHRC" "$backup"
  echo "Backup created: $backup"

  {
    echo
    echo "# Agent Desk"
    echo "$SOURCE_LINE"
  } >> "$ZSHRC"

  echo "Added Agent Desk source line to ~/.zshrc"
}

detect_clipboard_backend() {
  local os
  os="$(uname -s)"
  if [ "$os" = "Darwin" ]; then
    echo "pbcopy"
  elif [ -n "${WAYLAND_DISPLAY:-}" ] || [ "${XDG_SESSION_TYPE:-}" = "wayland" ]; then
    echo "wl-copy"
  else
    echo "xclip -selection clipboard"
  fi
}

clipboard_backend_label() {
  case "$1" in
    pbcopy)   echo "macOS (pbcopy)" ;;
    wl-copy)  echo "Linux Wayland (wl-copy)" ;;
    *)        echo "Linux X11 (xclip -selection clipboard)" ;;
  esac
}

echo "Step 1: install base tools"
touch "$ZSHRC"

install_homebrew_if_missing

echo
echo "Installing required CLI tools if missing..."
brew_install_if_missing "zellij" "zellij"
brew_install_if_missing "lazygit" "lazygit"
brew_install_if_missing "fzf" "fzf"

echo
echo "Installing Ghostty if missing..."
brew_cask_install_if_missing "ghostty" "/Applications/Ghostty.app"

echo
echo "Step 2: write/update Agent Desk helper file if changed"

mkdir -p "$AGENTDESK_DIR"
helpers_tmp="$(mktemp)"

# T-03: two-mode strategy — prefer source file when available (quoin install),
# fall back to inline heredoc for standalone / legacy use.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
AGENTDESK_ZSH_SRC="$SCRIPT_DIR/agentdesk.zsh"

if [ -f "$AGENTDESK_ZSH_SRC" ]; then
  # Source file available (installed via quoin or cloned repo)
  cp "$AGENTDESK_ZSH_SRC" "$helpers_tmp"
else
  # Fallback: write inline heredoc (standalone / legacy use)
  cat > "$helpers_tmp" <<'EOF'
# ============================================================
# Agent Desk helpers (legacy inline — install via quoin for full features)
# Ghostty/Zellij + Claude default + Codex on demand
# ============================================================

# IVG-135: work around upstream zellij socket-path length bug (#4211/#2817).
# See agentdesk.zsh for the full comment; kept byte-identical in intent here
# (literal $ZELLIJ_SOCKET_DIR / $(id -u) since this heredoc is single-quoted).
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

agentdesk() {
  local project_root session_name repo_count layout_path custom_name
  local layout_path="$HOME/.config/zellij/layouts/agent-desk.kdl"

  if [ -n "${ZELLIJ:-}" ]; then
    echo "You are already inside a Zellij session."
    echo "Detach with Alt+s, then d (needs Ghostty macos-option-as-alt), or run: zellij action detach"
    return 1
  fi

  custom_name=""

  while [ "$#" -gt 0 ]; do
    case "$1" in
      -n|--name)
        shift
        if [ "$#" -eq 0 ]; then
          echo "Error: --name requires a value."
          return 1
        fi
        custom_name="$1"
        ;;
      -h|--help)
        cat <<'HELP'
Usage:
  agentdesk
  agentdesk <session-name>
  agentdesk --name <session-name>
  agentdesk -n <session-name>

  (prompts to attach to a live same-named session by default; reply 'n' to start new.
   Non-interactive callers always start new, unchanged.)

Install via quoin for --mode and layout options.
HELP
        return 0
        ;;
      *)
        if [ -z "$custom_name" ]; then
          custom_name="$1"
        else
          echo "Error: unexpected argument: $1"
          return 1
        fi
        ;;
    esac
    shift
  done

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

  # NOTE: mirrors the primary agentdesk.zsh collision block (IVG-109) — keep both in sync.
  # ── Resolve session name suffix (attach-or-new prompt, IVG-109, fallback copy) ──
  local resolved_name
  resolved_name="$(_agentdesk_next_session_name "$session_name")"
  if [ "$resolved_name" != "$session_name" ]; then
    # $session_name is currently a live Zellij session (collision).
    if [ -t 0 ]; then
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
      # Non-TTY: preserve exact prior behavior — never prompt, never hang.
      echo "Session '$session_name' already exists; starting new session '$resolved_name' instead."
      echo "(To resume the existing session: agentdesk-attach $session_name)"
      session_name="$resolved_name"
    fi
  fi

  mkdir -p "$project_root/.workflow_artifacts"

  touch "$project_root/.workflow_artifacts/repos.md"

  {
    echo "# Repositories"
    echo
    _detect_repos | while read -r repo; do
      [ -n "$repo" ] && echo "- $repo"
    done
  } > "$project_root/.workflow_artifacts/repos.md"

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

  if [ ! -f "$layout_path" ]; then
    echo "Zellij layout not found:"
    echo "  $layout_path"
    return 1
  fi

  echo "Starting agent desk:"
  echo "  PROJECT_ROOT=$PROJECT_ROOT"
  echo "  SESSION=$session_name"
  echo "  REPOS:"
  cat "$project_root/.workflow_artifacts/repos.md"
  echo

  # IVG-135 (T-03, required): pre-flight guard against a would-be socket path
  # that overflows the AF_UNIX sun_path cap (~104 bytes macOS / ~108 Linux).
  # Best-effort heuristic; see agentdesk.zsh for the full comment.
  _sock_len=$(( ${#ZELLIJ_SOCKET_DIR} + ${#session_name} + 20 ))
  if [ "$_sock_len" -gt 100 ]; then
    echo "agentdesk: session socket path would be too long (~${_sock_len} bytes; limit ~104)." >&2
    echo "  session: $session_name" >&2
    echo "  ZELLIJ_SOCKET_DIR: $ZELLIJ_SOCKET_DIR" >&2
    echo "  Fix: set a shorter ZELLIJ_SOCKET_DIR, or use --name <shorter-name>." >&2
    return 1
  fi

  PROJECT_ROOT="$project_root" zellij --new-session-with-layout "$layout_path" --session "$session_name"
}

agentdesk-attach() {
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
  if [ -z "${PROJECT_ROOT:-}" ]; then
    export PROJECT_ROOT="$PWD"
  fi

  _detect_repos
}

crepo() {
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
EOF
fi

write_if_changed "$AGENTDESK_HELPERS" "$helpers_tmp" "agentdesk-helpers"

echo
echo "Step 3: write/update Zellij layout if changed"

mkdir -p "$ZELLIJ_LAYOUT_DIR"
layout_tmp="$(mktemp)"

cat > "$layout_tmp" <<'EOF'
layout {
    default_tab_template {
        pane size=1 borderless=true {
            plugin location="zellij:tab-bar"
        }

        children

        pane size=2 borderless=true {
            plugin location="zellij:status-bar"
        }
    }

    tab name="main" {
        pane split_direction="vertical" {
            pane name="Claude Code" size="65%" {
                command "zsh"
                args "-lc" "source \"$HOME/.config/agentdesk/agentdesk.zsh\" 2>/dev/null || true; cd \"$PROJECT_ROOT\" && echo 'Claude Code - project root:' \"$PWD\" && if command -v claude >/dev/null 2>&1; then claude; else echo 'claude command not found. Install Claude Code, then run claude here.'; fi; zsh"
            }

            pane split_direction="horizontal" {
                pane name="Tests/logs" size="50%" {
                    command "zsh"
                    args "-lc" "source \"$HOME/.config/agentdesk/agentdesk.zsh\" 2>/dev/null || true; cd \"$PROJECT_ROOT\" && echo 'Tests/logs shell'; zsh"
                }

                pane name="Shell" {
                    command "zsh"
                    args "-lc" "source \"$HOME/.config/agentdesk/agentdesk.zsh\" 2>/dev/null || true; cd \"$PROJECT_ROOT\" && echo 'Shell'; zsh"
                }
            }
        }
    }

    tab name="review" {
        pane split_direction="vertical" {
            pane name="Git review / repos" size="50%" {
                command "zsh"
                args "-lc" "source \"$HOME/.config/agentdesk/agentdesk.zsh\" 2>/dev/null || true; cd \"$PROJECT_ROOT\" && echo 'Project root:' && pwd && echo && echo 'Detected repos:' && repos && echo && echo 'Git status across repos:' && grepos && echo && echo 'Run gitreview to open lazygit for a selected repo.' && echo 'Run gitrootreview only if project root itself is a Git repo.' && zsh"
            }

            pane name="Workflow artifacts" {
                command "zsh"
                args "-lc" "source \"$HOME/.config/agentdesk/agentdesk.zsh\" 2>/dev/null || true; cd \"$PROJECT_ROOT\" && echo 'Project root:' && pwd && echo && echo 'Root folder:' && ls -la && echo && echo 'Workflow artifacts:' && ls -la .workflow_artifacts 2>/dev/null && echo && echo 'Artifact files:' && find .workflow_artifacts -maxdepth 2 -type f -print 2>/dev/null; zsh"
            }
        }
    }

    tab name="repos" {
        pane split_direction="vertical" {
            pane name="Repos list" size="50%" {
                command "zsh"
                args "-lc" "source \"$HOME/.config/agentdesk/agentdesk.zsh\" 2>/dev/null || true; cd \"$PROJECT_ROOT\" && cat .workflow_artifacts/repos.md 2>/dev/null; zsh"
            }

            pane name="Repos status" {
                command "zsh"
                args "-lc" "source \"$HOME/.config/agentdesk/agentdesk.zsh\" 2>/dev/null || true; cd \"$PROJECT_ROOT\" && echo 'Git status across repos:' && grepos; zsh"
            }
        }
    }

    tab name="shell" {
        pane name="Project shell" {
            command "zsh"
            args "-lc" "source \"$HOME/.config/agentdesk/agentdesk.zsh\" 2>/dev/null || true; cd \"$PROJECT_ROOT\" && zsh"
        }
    }

    tab name="Spend" {
        pane name="Token Spend" {
            command "zsh"
            args "-lc" "source \"$HOME/.config/agentdesk/agentdesk.zsh\" 2>/dev/null || true; cd \"$PROJECT_ROOT\" && python3 \"$HOME/.claude/scripts/spend_monitor.py\" --compact --watch"
        }
    }
}
EOF

write_if_changed "$ZELLIJ_LAYOUT" "$layout_tmp" "agentdesk-layout"

echo
echo "Step 3b: configure Zellij copy_command"

_detected_backend="$(detect_clipboard_backend)"
_detected_label="$(clipboard_backend_label "$_detected_backend")"
echo "Detected clipboard backend: $_detected_label"

_chosen_backend="$_detected_backend"
if [ -t 0 ]; then
  echo "  1) $_detected_label  [detected, default]"
  echo "  2) macOS (pbcopy)"
  echo "  3) Linux Wayland (wl-copy)"
  echo "  4) Linux X11 (xclip -selection clipboard)"
  printf "Select clipboard backend [1]: "
  read -r _choice || true
  case "${_choice:-1}" in
    1|"") _chosen_backend="$_detected_backend" ;;
    2)    _chosen_backend="pbcopy" ;;
    3)    _chosen_backend="wl-copy" ;;
    4)    _chosen_backend="xclip -selection clipboard" ;;
    *)    echo "Invalid choice; using detected backend."; _chosen_backend="$_detected_backend" ;;
  esac
fi

manage_zellij_keybinds() {
  local config="${1:-$HOME/.config/zellij/config.kdl}"

  if [ ! -f "$config" ]; then
    echo "Warning: Zellij config not found at $config — skipping keybind merge."
    return 0
  fi

  # Idempotency guard: if sentinel already present, skip.
  if grep -qF '// [agentdesk: freed]' "$config" 2>/dev/null; then
    echo "Unchanged: Zellij keybind merge already applied (sentinel found in $config)"
    return 0
  fi

  local tmp
  tmp="$(mktemp "${config}.keybinds.XXXXXX")"

  # awk script (single-quoted heredoc — no shell expansion inside).
  # Edits performed in one pass:
  #   1. Inside shared_except "locked" { ... } (ends at the bare closing brace):
  #      - Comment out `bind "Alt left" { ... }` and `bind "Alt right" { ... }`
  #      - Inject `bind "Alt s" { SwitchToMode "session"; }` before the closing brace
  #   2. In shared_except "locked" "session" { ... }:
  #      - Comment out `bind "Ctrl o" { SwitchToMode "session"; }`
  awk '
    BEGIN {
      in_shared_locked = 0
      in_shared_locked_session = 0
      injected_alts = 0
    }

    # Detect entry into shared_except "locked" { (NOT followed by more quoted args)
    /^[[:space:]]*shared_except[[:space:]]+"locked"[[:space:]]*\{/ &&
    !/shared_except[[:space:]]+"locked"[[:space:]]+"/ {
      in_shared_locked = 1
      in_shared_locked_session = 0
      print
      next
    }

    # Detect entry into shared_except "locked" "session" {
    /^[[:space:]]*shared_except[[:space:]]+"locked"[[:space:]]+"session"[[:space:]]*\{/ {
      in_shared_locked_session = 1
      in_shared_locked = 0
      print
      next
    }

    # Detect any other shared_except / shared_among block start — exit tracking
    /^[[:space:]]*(shared_except|shared_among)[[:space:]]/ {
      in_shared_locked = 0
      in_shared_locked_session = 0
      print
      next
    }

    # Inside shared_except "locked": inject Alt s and comment out Alt left/right
    in_shared_locked {
      # Inject bind "Alt s" before the closing brace of this block
      if (/^[[:space:]]*\}[[:space:]]*$/) {
        if (!injected_alts) {
          print "        // [agentdesk: added]"
          print "        bind \"Alt s\" { SwitchToMode \"session\"; }"
          injected_alts = 1
        }
        in_shared_locked = 0
        print
        next
      }
      # Comment out Alt left
      if (/bind[[:space:]]+"Alt left"[[:space:]]*\{/) {
        print "        // [agentdesk: freed] " $0
        next
      }
      # Comment out Alt right
      if (/bind[[:space:]]+"Alt right"[[:space:]]*\{/) {
        print "        // [agentdesk: freed] " $0
        next
      }
      print
      next
    }

    # Inside shared_except "locked" "session": comment out Ctrl o → session
    in_shared_locked_session {
      if (/^[[:space:]]*\}[[:space:]]*$/) {
        in_shared_locked_session = 0
        print
        next
      }
      if (/bind[[:space:]]+"Ctrl o"[[:space:]]*\{[[:space:]]*SwitchToMode[[:space:]]+"session"/) {
        print "        // [agentdesk: freed] " $0
        next
      }
      print
      next
    }

    { print }
  ' "$config" > "$tmp"

  write_if_changed "$config" "$tmp" "keybinds"
}

mkdir -p "$ZELLIJ_CONFIG_DIR"

if [ ! -f "$ZELLIJ_CONFIG" ]; then
  printf 'copy_command "%s"\ncopy_on_select true\n' "$_chosen_backend" > "$ZELLIJ_CONFIG"
  echo "Created: $ZELLIJ_CONFIG (with copy_command \"$_chosen_backend\")"
elif grep -qE '^\s*copy_command\s' "$ZELLIJ_CONFIG" 2>/dev/null; then
  _existing_val="$(grep -E '^\s*copy_command\s' "$ZELLIJ_CONFIG" | head -1 | sed 's/.*copy_command[[:space:]]*"\(.*\)".*/\1/')"
  if [ "$_existing_val" = "$_chosen_backend" ]; then
    echo "Unchanged: copy_command already set to \"$_chosen_backend\""
  else
    _cfg_backup="${ZELLIJ_CONFIG}.copy-cmd.$(timestamp).bak"
    cp "$ZELLIJ_CONFIG" "$_cfg_backup"
    echo "Backup created: $_cfg_backup"
    sed -i.tmp "s|^\([[:space:]]*\)copy_command .*|\1copy_command \"$_chosen_backend\"|" "$ZELLIJ_CONFIG"
    rm -f "${ZELLIJ_CONFIG}.tmp"
    echo "Updated copy_command to \"$_chosen_backend\" in $ZELLIJ_CONFIG"
  fi
elif grep -qE '^\s*//\s*copy_command\s' "$ZELLIJ_CONFIG" 2>/dev/null; then
  _cfg_backup="${ZELLIJ_CONFIG}.copy-cmd.$(timestamp).bak"
  cp "$ZELLIJ_CONFIG" "$_cfg_backup"
  echo "Backup created: $_cfg_backup"
  # Use awk to replace only the FIRST commented copy_command line.
  # Zellij's default config has three example lines (x11/wayland/osx); sed would
  # uncomment all three and produce multiple active directives.
  awk -v cmd="$_chosen_backend" '
    !replaced && /^[[:space:]]*\/\/[[:space:]]*copy_command[[:space:]]/ {
      print "copy_command \"" cmd "\""
      replaced = 1
      next
    }
    { print }
  ' "$ZELLIJ_CONFIG" > "${ZELLIJ_CONFIG}.tmp" && mv "${ZELLIJ_CONFIG}.tmp" "$ZELLIJ_CONFIG"
  echo "Uncommented and set copy_command to \"$_chosen_backend\" in $ZELLIJ_CONFIG"
else
  _cfg_backup="${ZELLIJ_CONFIG}.copy-cmd.$(timestamp).bak"
  cp "$ZELLIJ_CONFIG" "$_cfg_backup"
  echo "Backup created: $_cfg_backup"
  printf '\ncopy_command "%s"\n' "$_chosen_backend" >> "$ZELLIJ_CONFIG"
  echo "Appended copy_command \"$_chosen_backend\" to $ZELLIJ_CONFIG"
fi

# ── Keybind merge: free Alt+left, Alt+right, Ctrl+o for Claude Code panes ─────
echo
echo "Step 3b: patching Zellij keybinds (IVG-86)"
manage_zellij_keybinds "$ZELLIJ_CONFIG"

echo
echo "Step 4: patch ~/.zshrc if needed"
patch_zshrc_once

echo
echo "Step 5: checks"

check_command() {
  local cmd="$1"
  local label="$2"

  if command -v "$cmd" >/dev/null 2>&1; then
    echo "OK: $label -> $(command -v "$cmd")"
  else
    echo "MISSING: $label"
  fi
}

check_command "brew" "Homebrew"
check_command "zellij" "Zellij"
check_command "lazygit" "lazygit"
check_command "fzf" "fzf"
check_command "git" "git"
check_command "python3" "python3"

if command -v claude >/dev/null 2>&1; then
  echo "OK: Claude Code -> $(command -v claude)"
else
  echo "MISSING: claude"
  echo "Claude Code is not installed by this script."
fi

if command -v codex >/dev/null 2>&1; then
  echo "OK: Codex -> $(command -v codex)"
else
  echo "MISSING: codex"
  echo "Codex is optional. codexpane/codexcritic require it."
fi

if [ -d "/Applications/Ghostty.app" ]; then
  echo "OK: Ghostty app found"
else
  echo "Ghostty app not found in /Applications. Agent Desk still works in another terminal."
fi

echo
echo "============================================================"
echo "Setup complete."
echo "============================================================"
echo
echo "Run:"
echo "  source ~/.zshrc"
echo
echo "Then from a repo or project root:"
echo "  agentdesk"
echo "  agentdesk my-session"
echo "  agentdesk --name my-session"
echo "  agentdesk --mode solo|duo|trio"
echo "  agentdesk claude codex shell"
echo "  agentdesk claude ccr shell"
echo "  agentdesk --pick                  (force layout picker, even if a layout is saved)"
echo
echo "Useful commands:"
echo "  repos"
echo "  grepos"
echo "  gitreview"
echo "  gitrootreview"
echo "  crepo"
echo "  croot"
echo "  codexpane"
echo "  codexright"
echo "  codexcritic"
echo "  agentdesk-sessions"
echo "  agentdesk-attach <session-name>"
echo "    (agentdesk prompts to attach-or-new when a matching session exists; use agentdesk-attach to resume directly)"
echo "  agentdesk-delete <session-name>"
echo "  agentdesk-delete --force <session-name>"
echo "  agentprompt"
echo "  codexcriticprompt"
echo
