#!/usr/bin/env bash
# Rotate the Redis password for the kind-recipe OpenStudio Server cluster
# (issue #150).
#
# Background: until #150, the kind recipe's Redis password was the publicly-known
# literal `openstudio` — it shipped in `scripts/manifests/02-redis.yaml`,
# `deploy/redis-credentials-secret.yaml`, and the `REDIS_URL` env vars of every
# web / web-background / worker manifest. Anyone running
# `scripts/create-kind-cluster.sh` + `scripts/deploy-openstudio-stack.sh` from
# a fresh checkout published that password in their cluster's Secrets.
#
# After #150, those files ship the placeholder `openstudio-rotated`. This
# script generates a fresh 32-char random password and applies it across the
# whole stack in one shot:
#
#   1. Generates a 32-char hex password (override with `REDIS_PASSWORD=...`).
#   2. Substitutes `openstudio-rotated` -> <password> in
#      scripts/manifests/02-redis.yaml, 04-web.yaml, 05-web-background.yaml,
#      06-worker.yaml, AND in deploy/redis-credentials-secret.yaml. The
#      substituted copies are applied via `kubectl apply -f -` (stdin) so the
#      working tree stays clean — the committed placeholder is preserved.
#   3. Applies the resulting manifests and updates the live
#      `openstudio-redis` Secret in the `openstudio-server` namespace.
#   4. Prints the password to stdout so you can record it for fixture
#      captures / cross-cluster debugging. (Mind your terminal scrollback.)
#
# Usage:
#   scripts/rotate_redis_password.sh                 # generate + apply (default)
#   REDIS_PASSWORD=mysecret scripts/rotate_redis_password.sh   # use a specific one
#   scripts/rotate_redis_password.sh --print-only    # generate + print, do NOT apply
#   scripts/rotate_redis_password.sh --namespace foo # target a non-default ns
#
# Requires on PATH: kubectl, openssl (or `/dev/urandom` as fallback).
# The script does NOT create the kind cluster — run `scripts/create-kind-cluster.sh`
# first if needed. It targets whatever cluster your current kubectl context
# points at; pass `--namespace` to override the `openstudio-server` default.
#
# Idempotent: re-running with the same `REDIS_PASSWORD` is a no-op against
# the Secret (kubectl apply is safe to re-run). Re-running with no env override
# picks a NEW random password each time — useful for fresh clusters, but
# destructive for live ones (existing pods will fail to authenticate until
# their manifests are re-applied with the new password).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd -P)"
NAMESPACE="${NAMESPACE:-openstudio-server}"
SECRET_NAME="openstudio-redis"
PRINT_ONLY=0

# Files that contain the placeholder (must match the rotation target).
# Each entry: <path>:<sed-substitution-args-for-yaml-key-password>
# We use python instead of sed because YAML quoting / multi-line strings can
# trip sed (e.g. the `stringData: { password: openstudio-rotated }` block in
# the Secret has comments interleaved).
ROTATE_FILES=(
    "$REPO_ROOT/scripts/manifests/02-redis.yaml"
    "$REPO_ROOT/scripts/manifests/04-web.yaml"
    "$REPO_ROOT/scripts/manifests/05-web-background.yaml"
    "$REPO_ROOT/scripts/manifests/06-worker.yaml"
    "$REPO_ROOT/deploy/redis-credentials-secret.yaml"
)

usage() {
    sed -n '2,/^set -euo/p' "$0" | sed 's/^# \{0,1\}//' | sed '$d'
    exit "${1:-0}"
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        -h|--help)
            usage 0
            ;;
        --print-only)
            PRINT_ONLY=1
            shift
            ;;
        --namespace)
            NAMESPACE="$2"
            shift 2
            ;;
        --namespace=*)
            NAMESPACE="${1#--namespace=}"
            shift
            ;;
        *)
            echo "ERROR: unknown argument: $1" >&2
            usage 2
            ;;
    esac
done

# --- 1. Generate password ----------------------------------------------------
if [[ -n "${REDIS_PASSWORD:-}" ]]; then
    password="$REDIS_PASSWORD"
else
    if command -v openssl >/dev/null 2>&1; then
        password="$(openssl rand -hex 16)"  # 32 hex chars
    else
        # Fallback when openssl is missing (rare). 32 chars from /dev/urandom,
        # hex-encoded via od. NOT cryptographically as strong as openssl but
        # sufficient as a kind-recipe placeholder rotation.
        password="$(head -c 16 /dev/urandom | od -An -tx1 | tr -d ' \n')"
        # od emits variable-width hex per platform — pad to exactly 32 chars.
        password="$(printf '%-32s' "$password" | tr ' ' '0')"
    fi
fi

# Guard against the legacy literal accidentally re-appearing as the rotation
# target (would defeat the whole point of the script).
if [[ "$password" == "openstudio" ]]; then
    echo "ERROR: REDIS_PASSWORD=openstudio is the legacy literal (issue #150). Pick a different value." >&2
    exit 1
fi

echo "Redis password (record this if you need to debug live clusters):"
echo "  $password"
echo

if [[ "$PRINT_ONLY" == "1" ]]; then
    echo "--print-only set; not applying to the cluster."
    exit 0
fi

# --- 2. Pre-flight -----------------------------------------------------------
for tool in kubectl python3; do
    if ! command -v "$tool" >/dev/null 2>&1; then
        echo "ERROR: required tool '$tool' not found on PATH." >&2
        exit 1
    fi
done

if ! kubectl cluster-info >/dev/null 2>&1; then
    echo "ERROR: kubectl cannot reach a cluster. Check kubeconfig/context." >&2
    echo "Run scripts/create-kind-cluster.sh first if you have not yet." >&2
    exit 1
fi

if ! kubectl get namespace "$NAMESPACE" >/dev/null 2>&1; then
    echo "ERROR: namespace '$NAMESPACE' does not exist in the current cluster." >&2
    echo "Run scripts/deploy-openstudio-stack.sh first (which creates the namespace)." >&2
    exit 1
fi

# --- 3. Substitute + apply each manifest via stdin ---------------------------
# We pipe substituted copies to `kubectl apply -f -` so the working tree stays
# clean (the committed placeholder is preserved across rotations). Python is
# used for the substitution because YAML quoting / multi-line values can trip
# sed.

apply_substituted() {
    local src="$1"
    python3 -c "
import sys, pathlib
src = pathlib.Path(r'''$src''')
text = src.read_text()
new = text.replace('openstudio-rotated', r'''$password''')
if new == text:
    sys.exit(f'WARNING: no placeholder found in {src}; manifest already substituted or stale?')
sys.stdout.write(new)
" | kubectl apply -n "$NAMESPACE" -f -
}

for f in "${ROTATE_FILES[@]}"; do
    if [[ ! -f "$f" ]]; then
        echo "ERROR: manifest not found: $f" >&2
        exit 1
    fi
    echo "Applying $(basename "$(dirname "$f")")/$(basename "$f") with rotated password ..."
    apply_substituted "$f"
done

echo
echo "Redis password rotated across redis Deployment, web/web-background/worker"
echo "REDIS_URL env vars, and the openstudio-redis Secret (namespace: $NAMESPACE)."
echo
echo "Next step:"
echo "  scripts/deploy-openstudio-stack.sh   # apply the rest of the kind stack"