#!/usr/bin/env bash
# Create the kind validation cluster (issue #19, decision D13).
#
# Idempotent: exits 0 quickly when the cluster already exists.
#
# Requires on PATH: docker, kind, kubectl.
# If kind/kubectl are missing, see docs/kind-validation.md for one-line installs.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CLUSTER_NAME="${KIND_CLUSTER_NAME:-os-operator-validation}"

for tool in docker kind kubectl; do
    if ! command -v "$tool" >/dev/null 2>&1; then
        echo "ERROR: required tool '$tool' not found on PATH." >&2
        echo "See docs/kind-validation.md (prerequisites) for install instructions." >&2
        exit 1
    fi
done

if ! docker info >/dev/null 2>&1; then
    echo "ERROR: docker daemon is not reachable (docker info failed)." >&2
    exit 1
fi

if kind get clusters 2>/dev/null | grep -qx "$CLUSTER_NAME"; then
    echo "kind cluster '$CLUSTER_NAME' already exists — nothing to do."
else
    echo "Creating kind cluster '$CLUSTER_NAME' ..."
    kind create cluster --name "$CLUSTER_NAME" --config "$SCRIPT_DIR/kind-config.yaml"
fi

kubectl config use-context "kind-$CLUSTER_NAME"
echo "Waiting for the node to report Ready ..."
kubectl wait --for=condition=Ready node --all --timeout=300s

echo
echo "Cluster ready. Next step:"
echo "  scripts/deploy-openstudio-stack.sh"
