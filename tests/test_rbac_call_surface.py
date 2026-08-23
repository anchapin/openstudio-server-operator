"""Issue #643 gate: RBAC-vs-client-calls drift check for both Role manifests.

The #228 gate (``test_operator_role_oscm_verbs_are_enumerated_subset`` in
``tests/test_deploy_manifests.py``) is a static allowlist CAP: it pins the
OSCM rules to a fixed subset of verbs, which enshrines the then-current
superset rather than deriving it from usage — dead verbs were protected by
the test instead of flagged by it (#643).

This gate closes that drift class the same way the repo already gates
"exactly one construction site" invariants (``test_singleton_registry_coverage``
AST gates): it AST-scans ``src/openstudio_operator/`` for Kubernetes client
calls (``*_namespaced_*`` attribute CALLS — Protocol/stub ``def``
declarations and docstring mentions are naturally excluded), maps each call
to a (apiGroup, resource, verb) triple, and asserts that every verb granted
by a Role is either

(a) exercised by a call site attributable to the process bound to that
    Role, or
(b) on the explicitly commented framework allowlist below (kopf internals
    the AST scan cannot see).

Direction: granted ⊆ exercised ∪ allowlisted. Any grant with neither is a
dead verb and fails CI — future RBAC widening must either add a real call
site or a deliberate, reviewed allowlist entry.

Process attribution (which Role governs which call site):

* operator Role (``deploy/rbac.yaml``) — the kopf operator process. Every
  module under ``src/openstudio_operator/`` except the pruner-only modules.
* pruner Role (``deploy/storage-cronjob.yaml``) — the storage-prune
  CronJob process: ``prune_entrypoint``, ``retention``, ``archival``.
* ``status_store`` runs in BOTH processes (operator handlers + the pruner's
  ``.status.archivedAnalyses`` RMW), so its calls are credited to both.

File-level attribution is conservative in the safe direction: a mis-placed
file can only cause a loud false FAILURE, never a silent false pass.

Framework allowlist: kopf is pinned ``>=1.37,<1.45`` deliberately (see
``tests/test_singleton_registry_coverage.py`` for the kopf-internals fence);
a kopf bump must re-evaluate the allowlist entries below deliberately.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import yaml

from openstudio_operator._constants import CRD_GROUP, CRD_PLURAL

REPO = Path(__file__).resolve().parents[1]
SRC = REPO / "src" / "openstudio_operator"
DEPLOY = REPO / "deploy"

# Kubernetes python-client method shape: <verb>_namespaced_<resource>.
# Protocol stubs (`def list_namespaced_pod(self, ...)`) are FunctionDefs,
# not ast.Call nodes, so only real invocations match.
_METHOD_RE = re.compile(
    r"^(list|watch|create|patch|delete|deletecollection|read|get|replace|update)"
    r"_namespaced_(.+)$"
)

# Method prefix -> RBAC verb. `read_*` authorizes under `get`;
# `replace_*`/`update_*` under `update`.
_VERB_FOR_PREFIX = {
    "list": "list",
    "watch": "watch",
    "create": "create",
    "patch": "patch",
    "delete": "delete",
    "deletecollection": "deletecollection",
    "read": "get",
    "get": "get",
    "replace": "update",
    "update": "update",
}

# Method-name resource suffix -> (apiGroup, RBAC resource). The OSCM CRD
# identity is imported from _constants (#495) instead of hardcoded, so a
# CRD rename fails loudly here too. Unknown suffixes raise — a NEW call
# surface must be mapped explicitly, never silently dropped.
_RESOURCE_FOR_SUFFIX: dict[str, tuple[str, str]] = {
    "custom_object": (CRD_GROUP, CRD_PLURAL),
    "custom_object_status": (CRD_GROUP, f"{CRD_PLURAL}/status"),
    "deployment": ("apps", "deployments"),
    "pod": ("", "pods"),
    "secret": ("", "secrets"),
    "event": ("", "events"),
    "job": ("batch", "jobs"),
}

# File stems that run in the storage-prune CronJob process (its Role lives
# in deploy/storage-cronjob.yaml, not rbac.yaml).
_PRUNER_PROCESS_STEMS = {"prune_entrypoint", "retention", "archival"}

# File stems whose calls run in BOTH processes — credited to both Roles.
_SHARED_STEMS = {"status_store"}

# Framework-required (apiGroup, resource, verb) grants with NO call site in
# src/ — kopf internals the AST scan cannot see. Every entry must carry a
# justification; widening this set is a deliberate, reviewed act (a kopf
# bump should re-evaluate each entry — see module docstring).
KOPF_FRAMEWORK_ALLOWLIST = {
    # kopf's watching/streaming machinery on the OSCM main resource: the
    # operator code itself only polls (singleton guard ->
    # list_namespaced_custom_object), but kopf streams watches and resolves
    # objects by name under the hood. Documented per #228; #643 keeps these
    # explicit so a future kopf bump re-evaluates them deliberately.
    (CRD_GROUP, CRD_PLURAL, "get"),
    (CRD_GROUP, CRD_PLURAL, "watch"),
    # kopf.event() (openstudio_operator/events.py, the EventEmitter sink)
    # posts Events through kopf's own client (create_namespaced_event inside
    # kopf) — invisible to a scan of OUR source tree.
    ("", "events", "create"),
}

# The prune CronJob runs no kopf machinery — every pruner grant must map
# to a real call site (prune_entrypoint posts Events directly via
# core_api.create_namespaced_event, so no kopf allowlist is needed).
PRUNER_FRAMEWORK_ALLOWLIST: set[tuple[str, str, str]] = set()


def _map_call(attr: str) -> tuple[str, str, str]:
    """Map a ``*_namespaced_*`` method name to (apiGroup, resource, verb)."""
    match = _METHOD_RE.match(attr)
    assert match is not None, f"unmatched client method {attr!r}"
    prefix, suffix = match.groups()
    try:
        group, resource = _RESOURCE_FOR_SUFFIX[suffix]
    except KeyError as exc:
        raise AssertionError(
            f"client method {attr!r} maps to no RBAC resource — add the "
            f"suffix {suffix!r} to _RESOURCE_FOR_SUFFIX so the #643 drift "
            "gate can attribute it"
        ) from exc
    return (group, resource, _VERB_FOR_PREFIX[prefix])


def _scan_call_surface() -> dict[str, set[tuple[str, str, str]]]:
    """AST-scan src/ for real client calls, attributed per process.

    Returns {"operator": {...}, "pruner": {...}} with shared modules
    credited to both.
    """
    surface: dict[str, set[tuple[str, str, str]]] = {
        "operator": set(),
        "pruner": set(),
    }
    for path in sorted(SRC.rglob("*.py")):
        stem = path.stem
        tree = ast.parse(path.read_text())
        calls: set[tuple[str, str, str]] = set()
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and _METHOD_RE.match(node.func.attr)
            ):
                calls.add(_map_call(node.func.attr))
        if stem in _SHARED_STEMS:
            surface["operator"] |= calls
            surface["pruner"] |= calls
        elif stem in _PRUNER_PROCESS_STEMS:
            surface["pruner"] |= calls
        else:
            surface["operator"] |= calls
    return surface


def _load_role(path: Path, role_name: str) -> dict:
    docs = list(yaml.safe_load_all(path.read_text()))
    matches = [d for d in docs if d.get("kind") == "Role" and d["metadata"]["name"] == role_name]
    assert len(matches) == 1, f"expected exactly one Role {role_name!r} in {path.name}"
    return matches[0]


def _granted_triples(role: dict) -> set[tuple[str, str, str]]:
    granted: set[tuple[str, str, str]] = set()
    for rule in role["rules"]:
        group = rule["apiGroups"][0] if rule["apiGroups"] else ""
        for resource in rule["resources"]:
            for verb in rule["verbs"]:
                granted.add((group, resource, verb))
    return granted


def _dead_grants(role_path: Path, role_name: str, process: str,
                 allowlist: set[tuple[str, str, str]]) -> list[tuple[str, str, str]]:
    role = _load_role(role_path, role_name)
    granted = _granted_triples(role)
    exercised = _scan_call_surface()[process]
    return sorted(granted - exercised - allowlist)


def test_operator_role_grants_are_exercised_or_allowlisted():
    """Issue #643: every operator-Role verb maps to a call site or an
    explicit kopf-framework allowlist entry — dead grants fail CI."""
    dead = _dead_grants(
        DEPLOY / "rbac.yaml", "openstudio-operator-role", "operator",
        KOPF_FRAMEWORK_ALLOWLIST,
    )
    assert not dead, (
        "deploy/rbac.yaml grants (apiGroup, resource, verb) triples with no "
        "call site in src/openstudio_operator/ and no framework allowlist "
        "entry (issue #643 — trim the verb, or document it in "
        "KOPF_FRAMEWORK_ALLOWLIST with a justification): "
        f"{dead}"
    )


def test_pruner_role_grants_are_exercised_or_allowlisted():
    """Issue #643, pruner half: the storage-pruner Role in
    deploy/storage-cronjob.yaml must map every verb to a real call site —
    the pruner runs no kopf machinery, so its allowlist is empty."""
    dead = _dead_grants(
        DEPLOY / "storage-cronjob.yaml", "openstudio-storage-pruner-role",
        "pruner", PRUNER_FRAMEWORK_ALLOWLIST,
    )
    assert not dead, (
        "deploy/storage-cronjob.yaml grants (apiGroup, resource, verb) "
        "triples with no call site in the pruner modules "
        "(prune_entrypoint/retention/archival/status_store) — trim the "
        f"verb (issue #643): {dead}"
    )


def test_call_surface_scan_finds_the_known_call_sites():
    """Scanner-rot fence (#643): the AST scan must still find the known
    call floor. If the method-shape regex or the AST walk drifts (e.g. a
    client-library rename), the main gate could pass vacuously — this
    floor assertion makes that failure loud instead."""
    surface = _scan_call_surface()
    operator_floor = {
        (CRD_GROUP, CRD_PLURAL, "list"),                      # singleton guard poll
        (CRD_GROUP, f"{CRD_PLURAL}/status", "get"),           # status_store RMW
        (CRD_GROUP, f"{CRD_PLURAL}/status", "patch"),
        ("apps", "deployments", "get"),                       # _k8s read_namespaced_deployment
        ("apps", "deployments", "patch"),                     # _k8s patch_namespaced_deployment
        ("", "pods", "list"),                                 # analysis_sla / web_background_monitor
        ("", "pods", "delete"),                               # analysis_sla zombie eviction
        ("", "secrets", "get"),                               # client_factory #463/#606 bounded read
    }
    pruner_floor = {
        (CRD_GROUP, CRD_PLURAL, "list"),                      # prune_entrypoint CR resolution
        ("batch", "jobs", "get"),                             # retention read_namespaced_job
        ("batch", "jobs", "create"),                          # retention archival spawn
        ("batch", "jobs", "delete"),                          # retention failed-Job cleanup
        ("", "events", "create"),                             # prune_entrypoint Event posts
    }
    assert operator_floor <= surface["operator"], (
        f"operator scan floor regressed (scanner rot?): missing "
        f"{sorted(operator_floor - surface['operator'])}"
    )
    assert pruner_floor <= surface["pruner"], (
        f"pruner scan floor regressed (scanner rot?): missing "
        f"{sorted(pruner_floor - surface['pruner'])}"
    )


def test_kopf_framework_allowlist_is_the_documented_set():
    """Pin the framework allowlist contents (#643): adding an entry is a
    deliberate, reviewed act — each one must carry a justification comment
    in KOPF_FRAMEWORK_ALLOWLIST, and a kopf bump (pyproject pins
    kopf>=1.37,<1.45) must re-evaluate every entry."""
    assert KOPF_FRAMEWORK_ALLOWLIST == {
        (CRD_GROUP, CRD_PLURAL, "get"),
        (CRD_GROUP, CRD_PLURAL, "watch"),
        ("", "events", "create"),
    }, (
        "KOPF_FRAMEWORK_ALLOWLIST changed — every new entry needs a "
        "justification comment AND an update here, so allowlist growth "
        "shows up in the diff (issue #643)"
    )
    assert PRUNER_FRAMEWORK_ALLOWLIST == set()
