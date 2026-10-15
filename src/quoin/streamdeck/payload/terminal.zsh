#!/bin/zsh
# Arguments stay separate throughout: no interpolation into shell commands.
# PATH comes only from the environment the deck set up (recorded binary dirs).
set -e
mode="$1"
session="$2"
project="$3"

wait_for_enter() {
  if [ -t 0 ]; then
    printf '\nPress Enter to close.'
    read -r reply
  fi
  return 0
}

cd -- "$project"
export PROJECT_ROOT="$project"
case "$mode" in
  new)
    ad="${QUOIN_DECK_AGENTDESK:-}"
    if [ -z "$ad" ] || [ ! -r "$ad" ] || ! grep -q '^AGENTDESK_API_VERSION=' "$ad"; then
      print -u2 'Agent Desk is not installed: run `quoin streamdeck install`.'
      wait_for_enter
      exit 1
    fi
    source "$ad"
    agentdesk --name "$session" --panes codex,codex,shell --no-prompt --if-exists attach
    ;;
  attach) zellij attach "$session" ;;
  claude|codex)
    if command -v "$mode" >/dev/null; then
      exec "$mode"
    fi
    print -u2 "$mode not found in PATH: re-run 'quoin streamdeck install' to record its path."
    wait_for_enter
    exit 127
    ;;
  shell) exec /bin/zsh -l ;;
  git)
    if command -v lazygit >/dev/null; then
      exec lazygit
    else
      git status --short
      git --no-pager diff --stat
      printf '\nPress Enter to close the view.'
      read -r reply
    fi ;;
  *) print -u2 'Unknown action'; exit 2 ;;
esac
