#!/usr/bin/env bash
# CI guard — fail the build if the legacy kind-recipe Mongo password literal
# `openstudio` re-appears as a password value in any of the committed
# manifests (issue #219).
#
# Same shape as scripts/check_redis_password_unique.sh (the #150 Redis
# companion): exit 0 = clean; exit 1 = drift / regression detected. Used as a
# CI step so the build fails before the operator image is published if anyone
# reverts the rotation.
#
# The guard parses YAML with python's yaml.safe_load so it checks VALUE
# positions (not just text presence) — comments / image names / namespace
# labels that happen to contain `openstudio` are NOT flagged. Only the
# following positions are checked:
#
#   - scripts/manifests/01-mongo.yaml:  MONGO_INITDB_ROOT_PASSWORD env value
#     (MONGO_INITDB_ROOT_USERNAME is intentionally literal — see #219 for
#      the "username stays literal, only the password rotates" convention
#      that mirrors #150's empty Redis username.)
#   - deploy/mongo-credentials-secret.yaml: stringData.password
#   - scripts/manifests/04-web.yaml / 05-web-background.yaml / 06-worker.yaml:
#     MONGO_USER / MONGO_PASSWORD env entries — the literal `openstudio` as
#     the `value:` is flagged; `valueFrom.secretKeyRef` references are the
#     rotated-by-design pattern and pass through.
#
# Run:
#   bash scripts/check_mongo_password_unique.sh                  # check all
#   bash scripts/check_mongo_password_unique.sh --verbose        # print ok paths too
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd -P)"

if [[ ! -x "$(command -v python3 || true)" ]]; then
    echo "ERROR: python3 not found on PATH (required for YAML parsing)." >&2
    exit 1
fi

VERBOSE=0
for arg in "$@"; do
    case "$arg" in
        -v|--verbose) VERBOSE=1 ;;
        -h|--help)
            sed -n '2,/^set -euo/p' "$0" | sed 's/^# \{0,1\}//'
            exit 0
            ;;
        *)
            echo "ERROR: unknown argument: $arg" >&2
            exit 2
            ;;
    esac
done

LEGACY="openstudio"

python3 - "$REPO_ROOT" "$LEGACY" "$VERBOSE" <<'PY'
import sys, pathlib, yaml

repo_root = pathlib.Path(sys.argv[1])
legacy = sys.argv[2]   # 'openstudio'
verbose = sys.argv[3] == "1"

failures: list[str] = []

def fail(msg: str) -> None:
    failures.append(msg)
    print(f"FAIL: {msg}", file=sys.stderr)

def ok(msg: str) -> None:
    if verbose:
        print(f"OK:   {msg}")

def load_yaml(path: pathlib.Path):
    """Load a (possibly multi-document) YAML file. Returns a list of docs."""
    with path.open() as f:
        return [d for d in yaml.safe_load_all(f) if d is not None]

def check_deployment_env_names(spec: dict, names: tuple[str, ...], where: str) -> None:
    """For each container, look for env entries whose `name` is in `names`
    and fail if the entry has a literal `value: legacy` (the rotated-out
    pattern). Entries with `valueFrom.secretKeyRef` are the new pattern and
    pass through.
    """
    containers = spec.get("template", {}).get("spec", {}).get("containers", [])
    for container in containers:
        for env in container.get("env") or []:
            env_name = env.get("name")
            if env_name not in names:
                continue
            value = env.get("value")
            if value is not None and value == legacy:
                fail(
                    f"{where}: {env_name} has literal value '{legacy}' "
                    f"(issue #219). Use valueFrom.secretKeyRef "
                    f"(name: openstudio-mongo, key: username/password)."
                )

def check_mongo_initdb_root_password(spec: dict, where: str) -> None:
    """Look for `MONGO_INITDB_ROOT_PASSWORD` env value on the mongo container.
    MONGO_INITDB_ROOT_USERNAME is intentionally literal (only the password
    is rotated, mirroring #150's Redis convention) — do NOT flag it.
    """
    containers = spec.get("template", {}).get("spec", {}).get("containers", [])
    for container in containers:
        for env in container.get("env") or []:
            if env.get("name") != "MONGO_INITDB_ROOT_PASSWORD":
                continue
            value = env.get("value")
            if value is not None and value == legacy:
                fail(
                    f"{where}: MONGO_INITDB_ROOT_PASSWORD has literal value "
                    f"'{legacy}' (issue #219). The committed placeholder is "
                    f"'openstudio-rotated' and is rotated per-cluster by "
                    f"scripts/rotate_mongo_password.sh."
                )

def check_secret_password(doc: dict, where: str) -> None:
    """Check stringData.password of a Secret. stringData.username is the
    chart default and is intentionally literal — do NOT flag it.
    """
    if doc.get("kind") != "Secret":
        return
    string_data = doc.get("stringData") or {}
    password = string_data.get("password")
    if password == legacy:
        fail(
            f"{where}: Secret stringData.password uses the legacy literal "
            f"'{legacy}' (issue #219). The committed placeholder is "
            f"'openstudio-rotated' and is rotated per-cluster by "
            f"scripts/rotate_mongo_password.sh."
        )

# --- mongo manifest ----------------------------------------------------------
mongo_manifest = repo_root / "scripts/manifests/01-mongo.yaml"
for doc in load_yaml(mongo_manifest):
    if doc and doc.get("kind") == "Deployment":
        check_mongo_initdb_root_password(
            doc.get("spec", {}),
            f"scripts/manifests/01-mongo.yaml deployment/{doc['metadata']['name']}",
        )
ok("scripts/manifests/01-mongo.yaml: no legacy literal in MONGO_INITDB_ROOT_PASSWORD")

# --- Secret manifest ---------------------------------------------------------
secret_manifest = repo_root / "deploy/mongo-credentials-secret.yaml"
for doc in load_yaml(secret_manifest):
    check_secret_password(
        doc,
        f"deploy/mongo-credentials-secret.yaml secret/{doc['metadata'].get('name', '?')}",
    )
ok("deploy/mongo-credentials-secret.yaml: no legacy literal in password")

# --- web / web-background / worker manifests ---------------------------------
for name in ("04-web.yaml", "05-web-background.yaml", "06-worker.yaml"):
    path = repo_root / "scripts/manifests" / name
    for doc in load_yaml(path):
        if doc and doc.get("kind") == "Deployment":
            check_deployment_env_names(
                doc.get("spec", {}),
                ("MONGO_USER", "MONGO_PASSWORD"),
                f"scripts/manifests/{name} deployment/{doc['metadata']['name']}",
            )
    ok(f"scripts/manifests/{name}: no legacy literal in MONGO_USER/MONGO_PASSWORD")

if failures:
    print(
        f"\nFAIL: {len(failures)} legacy-mongo-password violation(s) found (issue #219).",
        file=sys.stderr,
    )
    print(
        "  The literal 'openstudio' was the publicly-known kind-recipe Mongo\n"
        "  password. It MUST NOT appear as a password value in source. See\n"
        "  scripts/rotate_mongo_password.sh to install a per-cluster random\n"
        "  password (companion to scripts/rotate_redis_password.sh from #150).",
        file=sys.stderr,
    )
    sys.exit(1)

print("OK: no legacy 'openstudio' literal in any Mongo password position (issue #219).")
PY
