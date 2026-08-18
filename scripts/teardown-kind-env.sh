#!/usr/bin/env bash
# Tear down the kind validation environment (issue #19).
#
# Deletes the whole kind cluster — mongo/redis data (emptyDirs), the hostPath
# NFS stand-in contents, and any analyses created during fixture capture all
# live inside the kind node container and are destroyed with it.
set -euo pipefail

CLUSTER_NAME="${KIND_CLUSTER_NAME:-os-operator-validation}"

if kind get clusters 2>/dev/null | grep -qx "$CLUSTER_NAME"; then
    echo "Deleting kind cluster '$CLUSTER_NAME' ..."
    kind delete cluster --name "$CLUSTER_NAME"
else
    echo "kind cluster '$CLUSTER_NAME' not found — nothing to do."
fi
