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
# After #150, those files ship the placeholder `openstudio-rotated`
# (post-#462, deploy/redis-credentials-secret.yaml ships the unusable
# sentinel `CHANGE_ME_RUN_ROTATE_SCRIPT` instead). This script generates a
# fresh 32-char random password and applies it across the whole stack in
# one shot:
#
#   1. Generates a 32-char hex password (override with `REDIS_PASSWORD=...`).
#   2. Substitutes every committed placeholder -> <password> in
#      scripts/manifests/02-redis.yaml, 04-web.yaml, 05-web-background.yaml,
#      06-worker.yaml (kind-recipe placeholder `openstudio-rotated`), AND in
#      deploy/redis-credentials-secret.yaml (unusable sentinel
#      `CHANGE_ME_RUN_ROTATE_SCRIPT`, issue #462). The substituted copies
#      are applied via `kubectl apply -f -` (stdin) so the working tree
#      stays clean — the committed placeholders are preserved.
#   3. Applies the resulting manifests and updates the live
#      `openstudio-redis` Secret in the `openstudio-server` namespace.
#   4. Writes the password to a 0600-permission file (default:
#      ./rotated-redis-password.txt in the current directory; override with
#      --out-file PATH) and prints ONLY the path — stdout carries no secret
#      material (issue #499). Use --print-only to force the password onto
#      stdout instead (exposure trade-off documented under Usage).
#
# Usage:
#   scripts/rotate_redis_password.sh                 # generate + apply (default);
#                                                    # password -> 0600 file, path on stdout
#   REDIS_PASSWORD=mysecret scripts/rotate_redis_password.sh   # use a specific one
#   scripts/rotate_redis_password.sh --out-file PATH # write the password to PATH
#                                                    # (-o PATH shorthand) instead of the
#                                                    # default ./rotated-redis-password.txt
#   scripts/rotate_redis_password.sh --print-only    # print the password to stdout; do
#                                                    # NOT apply and do NOT write the file.
#                                                    # EXPOSURE TRADE-OFF: stdout ends up in
#                                                    # terminal scrollback, CI job logs, and
#                                                    # shared session recordings — the exact
#                                                    # leak surfaces issue #499 closes. Use
#                                                    # only when no file with an access
#                                                    # boundary is available.
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
OUT_FILE="${OUT_FILE:-}"

# Files that contain a placeholder (must match the rotation target).
# Each entry: <path>:<placeholder-token-present-in-that-file>
# We use python instead of sed because YAML quoting / multi-line strings can
# trip sed (e.g. the `stringData: { password: ... }` block in the Secret has
# comments interleaved). Two placeholder tokens exist post-#462: the
# kind-recipe manifests keep `openstudio-rotated` while the deploy/ Secret
# ships the unusable sentinel `CHANGE_ME_RUN_ROTATE_SCRIPT` — the
# substitution below replaces whichever token each file carries.
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
        -o|--out-file)
            OUT_FILE="$2"
            shift 2
            ;;
        --out-file=*)
            OUT_FILE="${1#--out-file=}"
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

if [[ "$PRINT_ONLY" == "1" && -n "$OUT_FILE" ]]; then
    echo "ERROR: --out-file and --print-only are mutually exclusive (issue #499)." >&2
    exit 2
fi
if [[ -z "$OUT_FILE" ]]; then
    OUT_FILE="./rotated-redis-password.txt"
fi

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

# Guard against a publicly-known literal accidentally re-appearing as the
# rotation target (would defeat the whole point of the script).
if [[ "$password" == "openstudio" ]]; then
    echo "ERROR: REDIS_PASSWORD=openstudio is the legacy literal (issue #150). Pick a different value." >&2
    exit 1
fi
if [[ "$password" == "CHANGE_ME_RUN_ROTATE_SCRIPT" ]]; then
    echo "ERROR: REDIS_PASSWORD=CHANGE_ME_RUN_ROTATE_SCRIPT is the committed sentinel (issue #462)." >&2
    echo "       It is publicly known — pick a per-cluster random value." >&2
    exit 1
fi

if [[ "$PRINT_ONLY" == "1" ]]; then
    # Explicit escape hatch (issue #499): the password goes to stdout on
    # purpose. The one-line stderr note states the exposure trade-off at use
    # time, not just in --help.
    echo "WARNING: --print-only puts the password on stdout — mind terminal scrollback, CI job logs, and session recordings (issue #499)." >&2
    echo "Redis password (--print-only; not applying to the cluster):"
    echo "$password"
    exit 0
fi

# Default (issue #499): the password goes to a 0600 file; stdout gets the
# path only. umask is scoped to the redirect so the rest of the script keeps
# its inherited umask; the explicit chmod covers a pre-existing file, which
# `>` would otherwise leave at its old (possibly wider) permissions.
(
    umask 077
    printf '%s\n' "$password" > "$OUT_FILE"
)
chmod 600 "$OUT_FILE"
echo "Redis password written to: $OUT_FILE (mode 0600)"
echo

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
# kind-recipe manifests carry 'openstudio-rotated'; the deploy/ Secret
# carries the #462 sentinel — replace whichever is present.
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
echo "Redis password rotated across redis Deployment, web/web-background/worker"
echo "REDIS_URL env vars, and the openstudio-redis Secret (namespace: $NAMESPACE)."
echo "Password retained at: $OUT_FILE (0600) — delete it once recorded."
echo
echo "Next step:"
echo "  scripts/deploy-openstudio-stack.sh   # apply the rest of the kind stack"