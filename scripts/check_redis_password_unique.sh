#!/usr/bin/env bash
# CI guard — fail the build if the legacy kind-recipe Redis password literal
# `openstudio` re-appears as a password value in any of the committed
# manifests (issue #150).
#
# Issue #462 extends the gate: deploy/redis-credentials-secret.yaml must
# carry ONLY the unusable sentinel placeholder
# `CHANGE_ME_RUN_ROTATE_SCRIPT` as stringData.password. The pre-#462
# committed value (`openstudio-rotated`) was a real, publicly-known
# credential — a renamed survivor of the #150 leak class — so the Secret
# check is now "value must equal the sentinel, else fail".
#
# Same shape as scripts/check_editable_install.sh (the venv-drift guard, #71):
# exit 0 = clean; exit 1 = drift / regression detected. Used as a CI step so
# the build fails before the operator image is published if anyone reverts the
# rotation.
#
# The guard parses YAML with python's yaml.safe_load so it checks VALUE
# positions (not just text presence) — comments / image names / namespace
# labels that happen to contain `openstudio` are NOT flagged. Only the
# following positions are checked:
#
#   - scripts/manifests/02-redis.yaml:  redis-server's `--requirepass <arg>`
#   - deploy/redis-credentials-secret.yaml: stringData.password (must be
#     exactly the #462 sentinel)
#   - scripts/manifests/04-web.yaml / 05-web-background.yaml / 06-worker.yaml:
#     REDIS_URL env var (parsed as a URL; the password segment is what matters)
#
# Run:
#   bash scripts/check_redis_password_unique.sh                  # check all
#   bash scripts/check_redis_password_unique.sh --verbose        # print ok paths too
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
SENTINEL="CHANGE_ME_RUN_ROTATE_SCRIPT"

python3 - "$REPO_ROOT" "$LEGACY" "$SENTINEL" "$VERBOSE" <<'PY'
import sys, pathlib, re, urllib.parse, yaml

repo_root = pathlib.Path(sys.argv[1])
legacy = sys.argv[2]    # 'openstudio'
sentinel = sys.argv[3]  # 'CHANGE_ME_RUN_ROTATE_SCRIPT' (issue #462)
verbose = sys.argv[4] == "1"

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

def check_url_password(url: str, where: str) -> None:
    """Extract password segment from a redis:// URL and check against legacy."""
    try:
        parsed = urllib.parse.urlparse(url)
    except ValueError:
        return  # not a URL — env may carry a non-URL value; skip silently
    if parsed.scheme != "redis":
        return
    # redis://[:password@]host[:port][/db]
    # urllib parses the userinfo (everything between // and @) as netloc's
    # `username:password` split, BUT for `redis://:password@host` the password
    # is in `parsed.password` (username empty).
    if parsed.password == legacy:
        fail(f"{where}: REDIS_URL contains the legacy password literal "
             f"'{legacy}' (issue #150)")

def check_deployment_args(spec: dict, where: str) -> None:
    """Look for `redis-server ... --requirepass <value>` in a container's args."""
    containers = spec.get("template", {}).get("spec", {}).get("containers", [])
    for container in containers:
        args = container.get("args") or []
        # args is a list; find `--requirepass` and inspect the following value.
        for i, arg in enumerate(args):
            if arg == "--requirepass" and i + 1 < len(args):
                if args[i + 1] == legacy:
                    fail(f"{where}: redis-server --requirepass uses the legacy "
                         f"password literal '{legacy}' (issue #150)")

def check_env_redact(spec: dict, where: str) -> None:
    """Look for an env entry named REDIS_URL and validate its password."""
    containers = spec.get("template", {}).get("spec", {}).get("containers", [])
    for container in containers:
        for env in container.get("env") or []:
            if env.get("name") == "REDIS_URL":
                value = env.get("value", "")
                check_url_password(value, where)

def check_secret_password(doc: dict, where: str) -> None:
    """Check stringData.password of a Secret."""
    if doc.get("kind") != "Secret":
        return
    string_data = doc.get("stringData") or {}
    password = string_data.get("password")
    if password == legacy:
        fail(f"{where}: Secret stringData.password uses the legacy literal "
             f"'{legacy}' (issue #150)")

def check_secret_password_sentinel(doc: dict, where: str) -> None:
    """Issue #462: the committed deploy/ credential Secret must carry
    exactly the unusable sentinel placeholder. Any other value — the
    `openstudio-rotated` placeholder that survived #150, another
    real-looking literal, or a missing key — is a committed credential
    (CWE-798) and fails the build.
    """
    if doc.get("kind") != "Secret":
        return
    string_data = doc.get("stringData") or {}
    password = string_data.get("password")
    if password != sentinel:
        fail(f"{where}: Secret stringData.password must be the unusable "
             f"sentinel '{sentinel}' (issue #462); got {password!r}. "
             f"Committed real credentials are CWE-798 defaults — install a "
             f"per-cluster password via scripts/rotate_redis_password.sh.")

# --- redis manifest ----------------------------------------------------------
redis_manifest = repo_root / "scripts/manifests/02-redis.yaml"
for doc in load_yaml(redis_manifest):
    if doc and doc.get("kind") == "Deployment":
        check_deployment_args(
            doc.get("spec", {}),
            f"scripts/manifests/02-redis.yaml deployment/{doc['metadata']['name']}",
        )
    if doc and doc.get("kind") == "Secret":
        check_secret_password(
            doc,
            f"scripts/manifests/02-redis.yaml secret/{doc['metadata']['name']}",
        )
ok("scripts/manifests/02-redis.yaml: no legacy literal in --requirepass")

# --- Secret manifest ---------------------------------------------------------
secret_manifest = repo_root / "deploy/redis-credentials-secret.yaml"
for doc in load_yaml(secret_manifest):
    where = f"deploy/redis-credentials-secret.yaml secret/{doc['metadata'].get('name', '?')}"
    check_secret_password(doc, where)
    check_secret_password_sentinel(doc, where)
ok("deploy/redis-credentials-secret.yaml: no legacy literal in password")
ok("deploy/redis-credentials-secret.yaml: password is the #462 sentinel")

# --- web / web-background / worker manifests ---------------------------------
for name in ("04-web.yaml", "05-web-background.yaml", "06-worker.yaml"):
    path = repo_root / "scripts/manifests" / name
    for doc in load_yaml(path):
        if doc and doc.get("kind") == "Deployment":
            check_env_redact(
                doc.get("spec", {}),
                f"scripts/manifests/{name} deployment/{doc['metadata']['name']}",
            )
    ok(f"scripts/manifests/{name}: no legacy literal in REDIS_URL")

if failures:
    print(
        f"\nFAIL: {len(failures)} password-placeholder violation(s) found "
        f"(issues #150/#462).",
        file=sys.stderr,
    )
    print(
        "  The literal 'openstudio' was the publicly-known kind-recipe Redis\n"
        "  password. It MUST NOT appear as a password value in source. See\n"
        "  scripts/rotate_redis_password.sh to install a per-cluster random\n"
        "  password. deploy/redis-credentials-secret.yaml must ship only the\n"
        "  unusable sentinel CHANGE_ME_RUN_ROTATE_SCRIPT (issue #462) — any\n"
        "  other committed value is a publicly-known credential.",
        file=sys.stderr,
    )
    sys.exit(1)

print("OK: no legacy 'openstudio' literal in any Redis password position (issue #150);")
print(f"deploy/redis-credentials-secret.yaml ships the #462 sentinel '{sentinel}'.")
PY