#!/usr/bin/env bash
#
# wave-state-helpers.sh — shared helpers for wave-state.json namespacing and atomic writes
#
# This library provides:
#   - get_repo_slug: derive a filesystem-safe slug from git remote origin URL
#   - get_state_file: return the namespaced state file path (with legacy fallback)
#   - atomic_write_json: atomically write JSON to the state file using write-then-rename

# Derive repo slug from git remote origin URL
# Strips protocol, host, path prefix, and .git suffix
# Example: https://github.com/anchapin/openstudio-server-operator.git → openstudio-server-operator
get_repo_slug() {
  local origin_url
  origin_url=$(git remote get-url origin 2>/dev/null) || return 1

  # Handle both HTTPS and SSH URLs
  # HTTPS: https://github.com/owner/repo.git
  # SSH: git@github.com:owner/repo.git
  local slug
  if [[ "$origin_url" =~ ^git@ ]]; then
    # SSH format: git@host:owner/repo.git
    slug=$(echo "$origin_url" | sed -E 's|.*:([^/]+)/([^/.]+)(\.git)?$|\2|')
  else
    # HTTPS format: https://host/owner/repo.git
    slug=$(echo "$origin_url" | sed -E 's|.*/([^/]+)\.git$|\1|')
  fi

  # Sanitize: only alphanumeric, dash, underscore
  slug=$(echo "$slug" | tr -cd '[:alnum:]_-')
  echo "$slug"
}

# Get the namespaced state file path
# Returns: ../worktrees/wave-state.<repo-slug>.json
# Falls back to legacy ../worktrees/wave-state.json if namespaced doesn't exist
get_state_file() {
  local worktrees_dir="${1:-../worktrees}"
  local slug
  slug=$(get_repo_slug) || {
    echo "ERROR: Could not derive repo slug from git remote" >&2
    return 1
  }

  local namespaced="${worktrees_dir}/wave-state.${slug}.json"
  local legacy="${worktrees_dir}/wave-state.json"

  # Prefer namespaced; fall back to legacy for backward compatibility (one release)
  if [[ -f "$namespaced" ]]; then
    echo "$namespaced"
  elif [[ -f "$legacy" ]]; then
    echo "WARNING: Using legacy wave-state.json (deprecated, will be removed in next release)" >&2
    echo "$legacy"
  else
    # Neither exists — return namespaced path for new writes
    echo "$namespaced"
  fi
}

# Atomically write JSON to the state file
# Uses write-then-rename with a temp file in the same directory (atomic on POSIX)
# Usage: atomic_write_json <jq_filter> <state_file_path>
# Example: atomic_write_json '.issues["42"].status = "merged"' "$(get_state_file)"
atomic_write_json() {
  local jq_filter="$1"
  local state_file="$2"

  if [[ -z "$jq_filter" || -z "$state_file" ]]; then
    echo "Usage: atomic_write_json <jq_filter> <state_file_path>" >&2
    return 1
  fi

  local dir
  dir=$(dirname "$state_file")
  local tmp_file
  tmp_file=$(mktemp "${dir}/.wave-state.XXXXXX.json") || return 1

  # Read current state (or empty object if file doesn't exist)
  local current_state="{}"
  if [[ -f "$state_file" ]]; then
    current_state=$(cat "$state_file")
  fi

  # Apply jq filter and write to temp file
  echo "$current_state" | jq "$jq_filter" > "$tmp_file" || {
    rm -f "$tmp_file"
    return 1
  }

  # Atomic rename (O_EXCL semantics on POSIX — fails if destination exists and is busy)
  mv "$tmp_file" "$state_file" || {
    rm -f "$tmp_file"
    return 1
  }
}

# Read a value from the state file
# Usage: read_state_value <jq_filter> <state_file_path>
# Example: read_state_value '.issues["42"].pr' "$(get_state_file)"
read_state_value() {
  local jq_filter="$1"
  local state_file="$2"

  if [[ -z "$jq_filter" || -z "$state_file" ]]; then
    echo "Usage: read_state_value <jq_filter> <state_file_path>" >&2
    return 1
  fi

  if [[ ! -f "$state_file" ]]; then
    return 1
  fi

  jq -r "$jq_filter" "$state_file"
}

# Initialize a new wave-state.json with the given plan
# Usage: init_wave_state <plan_json> <state_file_path>
init_wave_state() {
  local plan_json="$1"
  local state_file="$2"

  if [[ -z "$plan_json" || -z "$state_file" ]]; then
    echo "Usage: init_wave_state <plan_json> <state_file_path>" >&2
    return 1
  fi

  local dir
  dir=$(dirname "$state_file")
  local tmp_file
  tmp_file=$(mktemp "${dir}/.wave-state.XXXXXX.json") || return 1

  echo "$plan_json" > "$tmp_file" || {
    rm -f "$tmp_file"
    return 1
  }

  mv "$tmp_file" "$state_file" || {
    rm -f "$tmp_file"
    return 1
  }
}

# Export functions for sourcing
export -f get_repo_slug
export -f get_state_file
export -f atomic_write_json
export -f read_state_value
export -f init_wave_state