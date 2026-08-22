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

# --- Deploy-time image signature verification (issue #500) -------------------
# The operator image this validation session will apply (Phase A of
# docs/validation.md applies deploy/operator-deployment.yaml, whose `image:`
# line is digest-pinned by release.yml, issue #149). CI already proves the
# signature exists (ci.yml cosign-verify-dev-image, #459); this closes the
# loop cluster-side by verifying it against the EXACT pinned digest BEFORE
# anything is applied.
#
# Identity pair MUST match .github/workflows/ci.yml (cosign-verify-dev-image)
# and release.yml's signing identity verbatim (#456/#459) — do not edit one
# without the other.
OPERATOR_DEPLOYMENT_YAML="${OPERATOR_DEPLOYMENT_YAML:-$SCRIPT_DIR/../deploy/operator-deployment.yaml}"
COSIGN_CERTIFICATE_IDENTITY="https://github.com/anchapin/openstudio-server-operator/.github/workflows/release.yml@refs/heads/develop"
COSIGN_CERTIFICATE_OIDC_ISSUER="https://token.actions.githubusercontent.com"

if ! kind get clusters 2>/dev/null | grep -qx "$CLUSTER_NAME"; then
    echo "ERROR: kind cluster '$CLUSTER_NAME' not found. Run scripts/create-kind-cluster.sh first." >&2
    exit 1
fi

kubectl config use-context "kind-$CLUSTER_NAME"

# Skip semantics (deliberate): the kind dev flow must NOT hard-require cosign.
# - cosign NOT installed -> print a LOUD explicit skip warning and continue.
# - cosign installed + verification FAILS -> abort the deploy (set -e exits
#   on cosign's non-zero); a bad signature is exactly what this gate stops.
# - cosign installed + verification passes -> proceed to kubectl apply.
if command -v cosign >/dev/null 2>&1; then
    OPERATOR_IMAGE="$(python3 - "$OPERATOR_DEPLOYMENT_YAML" <<'PY'
import sys, yaml

doc = yaml.safe_load(open(sys.argv[1]))
for container in doc["spec"]["template"]["spec"]["containers"]:
    image = container.get("image", "")
    if image.startswith("ghcr.io/") and "@sha256:" in image:
        print(image)
        break
PY
)"
    if [ -z "$OPERATOR_IMAGE" ]; then
        echo "ERROR: no digest-pinned operator image found in $OPERATOR_DEPLOYMENT_YAML" >&2
        exit 1
    fi
    echo "Verifying operator image signature (issue #500): $OPERATOR_IMAGE"
    cosign verify \
        --certificate-identity "$COSIGN_CERTIFICATE_IDENTITY" \
        --certificate-oidc-issuer "$COSIGN_CERTIFICATE_OIDC_ISSUER" \
        "$OPERATOR_IMAGE"
    echo "Signature verified against the release workflow identity (#500)."
else
    echo "******************************************************************" >&2
    echo "WARNING (#500): cosign is not installed — SKIPPING deploy-time"    >&2
    echo "signature verification of the digest-pinned operator image."       >&2
    echo "Install cosign (https://github.com/sigstore/cosign) to enforce it;" >&2
    echo "see docs/validation.md#deploy-time-signature-verification-500."    >&2
    echo "******************************************************************" >&2
fi

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
