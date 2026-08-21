#!/usr/bin/env bash
# Rotate the Mongo password for the kind-recipe OpenStudio Server cluster
# (issue #219).
#
# Background: until #219, the kind recipe's Mongo root password was the
# publicly-known literal `openstudio` — it shipped in
# `scripts/manifests/01-mongo.yaml` (MONGO_INITDB_ROOT_PASSWORD) and as the
# literal `MONGO_PASSWORD` in the web / web-background / worker env blocks.
# Anyone running `scripts/create-kind-cluster.sh` +
# `scripts/deploy-openstudio-stack.sh` from a fresh checkout published that
# password in their cluster's Secrets. The #150 PR closed the same shape of
# leak for Redis; this script is the Mongo companion.
#
# After #219, those files ship the placeholder `openstudio-rotated`
# (post-#462, deploy/mongo-credentials-secret.yaml ships the unusable
# sentinel `CHANGE_ME_RUN_ROTATE_SCRIPT` instead; the
# web/web-background/worker env vars now mount their credentials from the
# `openstudio-mongo` Secret via `valueFrom.secretKeyRef`). This script
# generates a fresh 32-char random password and applies it across the whole
# stack in one shot:
#
#   1. Generates a 32-char hex password (override with `MONGO_PASSWORD=...`).
#   2. Substitutes every committed placeholder -> <password> in
#      scripts/manifests/01-mongo.yaml (MONGO_INITDB_ROOT_PASSWORD value,
#      placeholder `openstudio-rotated`) AND in
#      deploy/mongo-credentials-secret.yaml (stringData.password, unusable
#      sentinel `CHANGE_ME_RUN_ROTATE_SCRIPT`, issue #462). The substituted
#      copies are applied via `kubectl apply -f -` (stdin) so the working
#      tree stays clean — the committed placeholders are preserved.
#   3. Applies the resulting manifests and updates the live
#      `openstudio-mongo` Secret in the `openstudio-server` namespace.
#   4. Prints the password to stdout so you can record it for fixture
#      captures / cross-cluster debugging. (Mind your terminal scrollback.)
#
# Usage:
#   scripts/rotate_mongo_password.sh                 # generate + apply (default)
#   MONGO_PASSWORD=mysecret scripts/rotate_mongo_password.sh   # use a specific one
#   scripts/rotate_mongo_password.sh --print-only    # generate + print, do NOT apply
#   scripts/rotate_mongo_password.sh --namespace foo # target a non-default ns
#
# Requires on PATH: kubectl, openssl (or `/dev/urandom` as fallback).
# The script does NOT create the kind cluster — run `scripts/create-kind-cluster.sh`
# first if needed. It targets whatever cluster your current kubectl context
# points at; pass `--namespace` to override the `openstudio-server` default.
#
# Idempotent: re-running with the same `MONGO_PASSWORD` is a no-op against
# the Secret (kubectl apply is safe to re-run). Re-running with no env override
# picks a NEW random password each time — useful for fresh clusters, but
# destructive for live ones (existing pods will fail to authenticate until
# their manifests are re-applied with the new password).
#
# Note on the username: the Mongo username (`openstudio`) is intentionally
# kept literal in source — it's not a secret, mirroring the #150 convention
# where the Redis username is blank and only the password is rotated. We
# only rotate the password in this script.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd -P)"
NAMESPACE="${NAMESPACE:-openstudio-server}"
SECRET_NAME="openstudio-mongo"
PRINT_ONLY=0

# Files that contain a placeholder (must match the rotation target).
# We use python instead of sed because YAML quoting / multi-line strings can
# trip sed (e.g. the `stringData: { password: ... }` block in the Secret has
# comments interleaved). Two placeholder tokens exist post-#462: the
# kind-recipe manifest keeps `openstudio-rotated` while the deploy/ Secret
# ships the unusable sentinel `CHANGE_ME_RUN_ROTATE_SCRIPT` — the
# substitution below replaces whichever token each file carries.
#
# `scripts/manifests/01-mongo.yaml` holds the MONGO_INITDB_ROOT_PASSWORD
# value that the db pod's bootstrap reads from.
# `deploy/mongo-credentials-secret.yaml` holds the stringData.password that
# the web / web-background / worker pods mount via valueFrom.secretKeyRef.
# The 04-web.yaml / 05-web-background.yaml / 06-worker.yaml env blocks are
# NOT in this list because they reference the Secret by name + key (no
# inlined `value:` to substitute), mirroring the #150 redis-credentials-secret
# -> redis-credential-mount pattern.
ROTATE_FILES=(
    "$REPO_ROOT/scripts/manifests/01-mongo.yaml"
    "$REPO_ROOT/deploy/mongo-credentials-secret.yaml"
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
if [[ -n "${MONGO_PASSWORD:-}" ]]; then
    password="$MONGO_PASSWORD"
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

# Guard against a publicly-known literal accidentally re-appearing as the
# rotation target (would defeat the whole point of the script).
if [[ "$password" == "openstudio" ]]; then
    echo "ERROR: MONGO_PASSWORD=openstudio is the legacy literal (issue #219). Pick a different value." >&2
    exit 1
fi
if [[ "$password" == "CHANGE_ME_RUN_ROTATE_SCRIPT" ]]; then
    echo "ERROR: MONGO_PASSWORD=CHANGE_ME_RUN_ROTATE_SCRIPT is the committed sentinel (issue #462)." >&2
    echo "       It is publicly known — pick a per-cluster random value." >&2
    exit 1
fi

echo "Mongo password (record this if you need to debug live clusters):"
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
# clean (the committed placeholders are preserved across rotations). Python is
# used for the substitution because YAML quoting / multi-line values can trip
# sed.

apply_substituted() {
    local src="$1"
    python3 -c "
import sys, pathlib
src = pathlib.Path(r'''$src''')
text = src.read_text()
new = text
# the kind-recipe manifest carries 'openstudio-rotated'; the deploy/
# Secret carries the #462 sentinel — replace whichever is present.
for placeholder in ('CHANGE_ME_RUN_ROTATE_SCRIPT', 'openstudio-rotated'):
    new = new.replace(placeholder, r'''$password''')
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
echo "Mongo password rotated across db Deployment's MONGO_INITDB_ROOT_PASSWORD"
echo "and the openstudio-mongo Secret (namespace: $NAMESPACE). The web /"
echo "web-background / worker pods read MONGO_USER / MONGO_PASSWORD from the"
echo "Secret via valueFrom.secretKeyRef — no manifest changes required there."
echo
echo "Next step:"
echo "  scripts/deploy-openstudio-stack.sh   # apply the rest of the kind stack"
