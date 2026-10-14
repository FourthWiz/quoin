#!/bin/zsh
# Arguments stay separate throughout: no interpolation into shell commands.
set -e
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"
mode="$1"
session="$2"
project="$3"
source "$HOME/.config/agentdesk/agentdesk.zsh"
cd -- "$project"
export PROJECT_ROOT="$project"
case "$mode" in
  new)
    # Agent Desk rejects --name combined with positional pane types.
    # Seed this new named session's layout, then use its normal startup path.
    _agentdesk_save_layout "$(_agentdesk_layout_key "$session" "$project")" 'codex+codex+shell'
    # A hardware button must not stop at the optional web-dashboard question.
    _agentdesk_open_dashboard() { return 0; }
    agentdesk --name "$session"
    ;;
  attach) zellij attach "$session" ;;
  claude) exec claude ;;
  codex) exec codex ;;
  shell) exec /bin/zsh -l ;;
  git)
    if command -v lazygit >/dev/null; then
      exec lazygit
    else
      git status --short
      git --no-pager diff --stat
      printf '\nНажми Enter, чтобы закрыть просмотр.'
      read -r reply
    fi ;;
  *) print -u2 'Unknown action'; exit 2 ;;
esac
