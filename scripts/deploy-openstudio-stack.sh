#!/usr/bin/env bash
# Deploy the OpenStudio Server 3.11.0 validation stack onto the kind cluster
# (issue #19). Applies every manifest under scripts/manifests/ and waits for
# rollouts.
#
# Idempotent: kubectl apply is safe to re-run; rollout status just verifies.
#
# NOTE: the first run pulls nrel/openstudio-server:3.11.0 (~2 GB compressed)
# into the kind node. On a slow link, consider pre-pulling on the host and
# `kind load docker-image` instead (see docs/kind-validation.md).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CLUSTER_NAME="${KIND_CLUSTER_NAME:-os-operator-validation}"
NAMESPACE="${NAMESPACE:-openstudio-server}"
# Long timeout: first boot includes the 3.11.0 image pull + Rails asset boot
# + mongoid index creation.
ROLLOUT_TIMEOUT="${ROLLOUT_TIMEOUT:-1800s}"

if ! kind get clusters 2>/dev/null | grep -qx "$CLUSTER_NAME"; then
    echo "ERROR: kind cluster '$CLUSTER_NAME' not found. Run scripts/create-kind-cluster.sh first." >&2
    exit 1
fi

kubectl config use-context "kind-$CLUSTER_NAME"

echo "Applying manifests from $SCRIPT_DIR/manifests ..."
kubectl apply -f "$SCRIPT_DIR/manifests/"

echo
echo "Waiting for rollouts (timeout $ROLLOUT_TIMEOUT; first run pulls ~2 GB of images) ..."
for deployment in db redis web web-background worker; do
    echo "--- rollout status deployment/$deployment ---"
    kubectl -n "$NAMESPACE" rollout status "deployment/$deployment" --timeout="$ROLLOUT_TIMEOUT"
done

echo
echo "Stack summary:"
kubectl -n "$NAMESPACE" get deploy,svc,pvc,hpa

echo
echo "Stack is up. Next steps:"
echo "  1. kubectl -n $NAMESPACE port-forward svc/web 8080:80"
echo "  2. scripts/capture_fixtures.sh --base-url http://localhost:8080"
echo "See docs/kind-validation.md for the full walkthrough (including the"
echo "Phase 1 dryRun:true operator smoke test for issue #20's runbook)."
