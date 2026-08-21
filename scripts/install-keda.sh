#!/usr/bin/env bash
# Install KEDA (Kubernetes Event-driven Autoscaling) for the
# OpenStudio Server operator validation (#77).
#
# Background: the operator's custom HPA-floor adjuster (#18) was removed
# and replaced with a standard KEDA ScaledObject that scales the
# `worker` Deployment from 0..N based on pending Resque queue items
# (deploy/keda-scaledobject.yaml). KEDA runs under its own ServiceAccount
# (`keda-operator`) and manages the ScaledObject + HPA from outside the
# operator process — so this script is a cluster-prerequisite install, NOT
# an operator-managed step. Run once per cluster (idempotent on re-run).
#
# Two install paths, tried in order:
#   1. helm install keda kedacore/keda (when helm is on PATH)
#   2. kubectl apply of KEDA's official raw manifests (always works)
#
# Both paths install KEDA into the `keda` namespace and produce a
# `keda-operator` Deployment + a `keda-metrics-apiserver` Deployment +
# a `keda-admission-webhooks` Deployment (typical 3-component layout for
# KEDA >= 2.4). The script verifies readiness and prints the installed
# version.
#
# After this script completes:
#   1. Disable the chart's `worker-hpa` (or post-render the manifest to
#      remove it) — see docs/kind-validation.md#phase-4-keda-scaledobject
#   2. Apply deploy/keda-scaledobject.yaml (or scripts/manifests/07-keda-scaledobject.yaml)
#
# Idempotent: re-running prints "already present" and exits 0; the helm
# path upgrades, the kubectl path does NOT (raw manifests are version-pinned
# in KEDA_VERSION below — change to upgrade).
set -euo pipefail

CLUSTER_NAME="${KIND_CLUSTER_NAME:-os-operator-validation}"
KEDA_NAMESPACE="${KEDA_NAMESPACE:-keda}"
KEDA_RELEASE_NAME="${KEDA_RELEASE_NAME:-keda}"
# Pinned to the latest stable at the time of writing (#77). To upgrade,
# change here AND re-run the script — the raw-manifest path always uses
# this exact version, the helm path lets `helm upgrade` track the chart's
# version.
KEDA_VERSION="${KEDA_VERSION:-2.20.2}"

# Resolve the kind context so subsequent kubectl calls target the right
# cluster (no-op if already on it).
if kind get clusters 2>/dev/null | grep -qx "$CLUSTER_NAME"; then
    kubectl config use-context "kind-$CLUSTER_NAME" >/dev/null
fi

# Pre-flight: cluster reachable.
if ! kubectl cluster-info >/dev/null 2>&1; then
    echo "ERROR: kubectl cannot reach a cluster. Check kubeconfig/context." >&2
    exit 1
fi

# Already installed? Print version + exit 0.
if kubectl get namespace "$KEDA_NAMESPACE" >/dev/null 2>&1 \
   && kubectl -n "$KEDA_NAMESPACE" get deployment keda-operator >/dev/null 2>&1; then
    echo "KEDA already present in namespace '$KEDA_NAMESPACE'."
    kubectl -n "$KEDA_NAMESPACE" get deploy
    kubectl -n "$KEDA_NAMESPACE" get svc
    exit 0
fi

# --- Path 1: helm install (preferred) ------------------------------------------
if command -v helm >/dev/null 2>&1; then
    echo "Installing KEDA v${KEDA_VERSION} via helm into namespace '$KEDA_NAMESPACE' ..."
    # Add the KEDA helm repo if not present.
    if ! helm repo list 2>/dev/null | grep -q '^kedacore'; then
        helm repo add kedacore https://kedacore.github.io/charts
        helm repo update kedacore
    fi
    # create namespace if needed (helm does not --create-namespace by default
    # in 3.x for all installs, so be explicit)
    kubectl create namespace "$KEDA_NAMESPACE" --dry-run=client -o yaml | kubectl apply -f - >/dev/null
    helm upgrade --install "$KEDA_RELEASE_NAME" kedacore/keda \
        --namespace "$KEDA_NAMESPACE" \
        --version "$KEDA_VERSION" \
        --wait \
        --timeout 10m
    echo "KEDA installed (helm path). Verifying ..."
else
    # --- Path 2: kubectl apply of KEDA's raw manifests (fallback) -------------
    echo "helm not found; installing KEDA v${KEDA_VERSION} via kubectl apply of raw manifests into namespace '$KEDA_NAMESPACE' ..."
    # Stream directly from release-assets (302 → GitHub-owned CDN); we
    # don't cache on disk so re-runs always use the pinned version.
    KEDA_URL="https://github.com/kedacore/keda/releases/download/v${KEDA_VERSION}/keda-${KEDA_VERSION}.yaml"
    kubectl create namespace "$KEDA_NAMESPACE" --dry-run=client -o yaml | kubectl apply -f - >/dev/null
    if ! curl -fsSL "$KEDA_URL" | kubectl apply -f -; then
        echo "ERROR: failed to apply KEDA manifest from $KEDA_URL" >&2
        exit 1
    fi
    echo "KEDA manifests applied (kubectl path). Verifying ..."
fi

# --- Verify -------------------------------------------------------------------
# KEDA >= 2.4 ships three Deployments; wait for the operator as the
# liveness proxy — the others come up in lockstep.
echo "Waiting for keda-operator rollout (timeout 5m) ..."
kubectl -n "$KEDA_NAMESPACE" rollout status deploy/keda-operator --timeout=5m

echo
echo "KEDA v${KEDA_VERSION} installed in namespace '$KEDA_NAMESPACE'."
echo "Deployments:"
kubectl -n "$KEDA_NAMESPACE" get deploy
echo
echo "Next steps (see docs/kind-validation.md#phase-4-keda-scaledobject):"
echo "  1. Disable the chart's worker-hpa (idempotent):"
echo "     kubectl -n openstudio-server delete hpa worker-hpa --ignore-not-found"
echo "  2. Install the Redis credential Secret — the committed manifest ships"
echo "     only the unusable sentinel CHANGE_ME_RUN_ROTATE_SCRIPT (#462), so"
echo "     rotate to install a real per-cluster password:"
echo "     scripts/rotate_redis_password.sh"
echo "  3. Apply the ScaledObject:"
echo "     kubectl apply -f deploy/keda-scaledobject.yaml"
echo "  4. Watch scaling:"
echo "     kubectl -n openstudio-server get hpa,scaledobject,worker"
echo "     kubectl -n keda logs deploy/keda-operator -f"
