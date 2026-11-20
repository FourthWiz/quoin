#!/usr/bin/env bash
# worktreecreate.sh — WorktreeCreate hook for quoin nested-git worktree dispatch.
#
# Fires when the harness tries to create a worktree (isolation: "worktree").
# For single-repo nested-git layouts (project root has no .git; one child has .git):
#   - Reads sidecar JSON from <project_root>/.workflow_artifacts/.dispatch-hint.json
#   - Runs git_root_for_dispatch.py to resolve the nested git root
#   - Creates a worktree IN the nested git root using `git worktree add`
#   - Prints the created worktree path to stdout (harness picks this up as the
#     created worktree path)
#
# For skip cases (multi-repo, no nested git, stale sidecar, or any error):
#   - Exits 0 with NO stdout output
#   - The harness sees a missing path → worktree creation fails
#   - The §0 block in calling skill catches the worktree-class error and retries
#     the Agent dispatch WITHOUT isolation: "worktree" (Phase 2 retry at cheap tier)
#
# Key contract (from https://code.claude.com/docs/en/hooks):
#   - Command hooks: print created worktree path to stdout for success
#   - Exit 0 + no stdout = harness fails worktree creation (our skip path)
#   - Exit non-zero = abort (we never exit non-zero — always fail-OPEN)
#
# Fail-OPEN: any error → exit 0 with no output (harness fails worktree).
# The parent skill's §0 Phase 2 retry handles the failure gracefully.

set +e
set -u

# ── read hook input ───────────────────────────────────────────────────────────
HOOK_INPUT=""
HOOK_INPUT="$(cat 2>/dev/null)" || true

# Extract cwd from hook input JSON (contains project root / session cwd)
PROJECT_ROOT=""
PROJECT_ROOT="$(printf '%s' "$HOOK_INPUT" | jq -r '.cwd // empty' 2>/dev/null)" || true
[[ -z "$PROJECT_ROOT" ]] && exit 0

# Extract the worktree_path and branch_name from hook input (harness-generated)
WORKTREE_PATH=""
WORKTREE_PATH="$(printf '%s' "$HOOK_INPUT" | jq -r '.worktree_path // empty' 2>/dev/null)" || true
BRANCH_NAME=""
BRANCH_NAME="$(printf '%s' "$HOOK_INPUT" | jq -r '.branch_name // empty' 2>/dev/null)" || true
BASE_REF=""
BASE_REF="$(printf '%s' "$HOOK_INPUT" | jq -r '.base_ref // empty' 2>/dev/null)" || true

# Set to 1 when we synthesize worktree_path/branch_name the harness omitted (audit signal).
SELFGEN=0

# ── read sidecar ──────────────────────────────────────────────────────────────
SIDECAR="$PROJECT_ROOT/.workflow_artifacts/.dispatch-hint.json"
[[ ! -f "$SIDECAR" ]] && exit 0

# ── stale sidecar check (reject if mtime > 60s) ───────────────────────────────
NOW="$(date +%s 2>/dev/null)" || true
if [[ -n "$NOW" ]]; then
    SIDECAR_MTIME=""
    SIDECAR_MTIME="$(stat -f %m "$SIDECAR" 2>/dev/null || stat -c %Y "$SIDECAR" 2>/dev/null)" || true
    if [[ -n "$SIDECAR_MTIME" ]] && [[ "$NOW" -gt $(( SIDECAR_MTIME + 60 )) ]]; then
        # Stale — skip without consuming the sidecar
        exit 0
    fi
fi

# ── audit log path ────────────────────────────────────────────────────────────
AUDIT_DIR="$PROJECT_ROOT/.workflow_artifacts/memory"
AUDIT="$AUDIT_DIR/worktree-hook-audit.log"
mkdir -p "$AUDIT_DIR" 2>/dev/null || true

# ── resolve nested git root ───────────────────────────────────────────────────
# Find the git_root_for_dispatch.py script (deployed next to this hook or in scripts/)
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" 2>/dev/null && pwd)" || SCRIPT_DIR=""
GIT_ROOT_SCRIPT=""

# Try the deployed scripts location first
if [[ -f "$HOME/.claude/scripts/git_root_for_dispatch.py" ]]; then
    GIT_ROOT_SCRIPT="$HOME/.claude/scripts/git_root_for_dispatch.py"
elif [[ -n "$SCRIPT_DIR" ]] && [[ -f "$SCRIPT_DIR/../scripts/git_root_for_dispatch.py" ]]; then
    GIT_ROOT_SCRIPT="$(cd "$SCRIPT_DIR/../scripts" && pwd)/git_root_for_dispatch.py"
fi

if [[ -z "$GIT_ROOT_SCRIPT" ]]; then
    echo "$(date -u +%FT%TZ) rc=err result=no-script" >> "$AUDIT" 2>/dev/null || true
    exit 0
fi

RESOLVED_GIT_ROOT=""
RESOLVED_GIT_ROOT="$(python3 "$GIT_ROOT_SCRIPT" --sidecar "$SIDECAR" 2>/dev/null)"
RC=$?

# ── consume sidecar (single-shot, regardless of outcome) ─────────────────────
rm -f "$SIDECAR" 2>/dev/null || true

# ── log audit entry ───────────────────────────────────────────────────────────
echo "$(date -u +%FT%TZ) rc=$RC result=${RESOLVED_GIT_ROOT:-skip}" >> "$AUDIT" 2>/dev/null || true

# ── bound git worktree add with QUOIN_SUBPROCESS_TIMEOUT ─────────────────────
# Wrap git in `timeout` so a hung git-worktree-add on a slow Drive mount cannot stall
# the hook indefinitely. Fail-OPEN: if the `timeout` binary is absent (e.g. stock
# macOS ships `gtimeout` under coreutils), run git unwrapped rather than failing.
WT_TIMEOUT="${QUOIN_SUBPROCESS_TIMEOUT:-30}"
GIT_TIMEOUT_BIN="$(command -v timeout 2>/dev/null || true)"
git_wt() {
    if [[ -n "$GIT_TIMEOUT_BIN" ]]; then
        "$GIT_TIMEOUT_BIN" "${WT_TIMEOUT}s" git "$@"
    else
        git "$@"
    fi
}

# ── act on result ────────────────────────────────────────────────────────────
case "$RC" in
  0)
    # Single nested repo resolved — create the worktree IN the nested git root.
    # The harness has already generated WORKTREE_PATH and BRANCH_NAME; we use them
    # but anchor the worktree inside the nested repo (RESOLVED_GIT_ROOT).
    if [[ -z "$WORKTREE_PATH" ]] || [[ -z "$BRANCH_NAME" ]]; then
        # The harness omitted worktree_path/branch_name — the observed 100%-failure case on
        # Google-Drive-synced nested-git layouts (T-01 spike root cause). Self-generate them so
        # isolation actually works, unless explicitly disabled via QUOIN_WORKTREE_SELFGEN=0
        # (which restores the old skip behaviour).
        if [[ "${QUOIN_WORKTREE_SELFGEN:-1}" == "0" ]]; then
            echo "$(date -u +%FT%TZ) rc=skip result=missing-worktree-path-or-branch selfgen=0" >> "$AUDIT" 2>/dev/null || true
            exit 0
        fi
        SELFGEN=1
        SELFGEN_TOKEN="$(date +%s 2>/dev/null)-$$"
        [[ -z "$BRANCH_NAME" ]] && BRANCH_NAME="quoin/wt-${SELFGEN_TOKEN}"
        if [[ -z "$WORKTREE_PATH" ]]; then
            # Anchor OUTSIDE the Drive-synced tree — validated in the T-01 spike: a /tmp-based
            # path works and avoids Drive bloat/GC concerns (R-06). Fall back to a project-local
            # .worktrees/ dir only if no temp base is writable.
            WT_BASE="${TMPDIR:-/tmp}/quoin-worktrees"
            if ! mkdir -p "$WT_BASE" 2>/dev/null; then
                WT_BASE="$PROJECT_ROOT/.worktrees"
                mkdir -p "$WT_BASE" 2>/dev/null || true
            fi
            WORKTREE_PATH="$WT_BASE/wt-${SELFGEN_TOKEN}"
        fi
    fi

    # Build the git worktree add command.
    # Try with -b (create new branch) first; fall back to branch-only form if branch already exists.
    GIT_RC=0
    GIT_OUTPUT=""
    if [[ -n "$BASE_REF" ]]; then
        # With base ref: create new branch from base ref
        GIT_OUTPUT="$(git_wt -C "$RESOLVED_GIT_ROOT" worktree add -b "$BRANCH_NAME" "$WORKTREE_PATH" "$BASE_REF" 2>&1)" || GIT_RC=$?
    else
        # Without base ref: try -b first (new branch from HEAD), then bare checkout (branch exists)
        GIT_OUTPUT="$(git_wt -C "$RESOLVED_GIT_ROOT" worktree add -b "$BRANCH_NAME" "$WORKTREE_PATH" 2>&1)"
        GIT_RC=$?
        if [[ "$GIT_RC" -ne 0 ]]; then
            # Branch may already exist — try without -b
            GIT_RC=0
            GIT_OUTPUT="$(git_wt -C "$RESOLVED_GIT_ROOT" worktree add "$WORKTREE_PATH" "$BRANCH_NAME" 2>&1)" || GIT_RC=$?
        fi
    fi

    echo "$(date -u +%FT%TZ) git-rc=$GIT_RC git-root=$RESOLVED_GIT_ROOT worktree=$WORKTREE_PATH selfgen=$SELFGEN" >> "$AUDIT" 2>/dev/null || true

    if [[ "$GIT_RC" -ne 0 ]]; then
        # git worktree add failed — log and skip (Phase 2 retry handles it)
        echo "$(date -u +%FT%TZ) git-error: $GIT_OUTPUT" >> "$AUDIT" 2>/dev/null || true
        exit 0
    fi

    # Success — print the created worktree path to stdout
    printf '%s\n' "$WORKTREE_PATH"
    ;;

  1|2)
    # No nested repo (exit 1) or multi-repo (exit 2) — skip
    # Exit 0 with no stdout → harness fails worktree creation → Phase 2 retry
    ;;

  *)
    # Error (exit 3 or other) — skip
    echo "$(date -u +%FT%TZ) rc=$RC result=error-from-helper" >> "$AUDIT" 2>/dev/null || true
    ;;
esac

exit 0
