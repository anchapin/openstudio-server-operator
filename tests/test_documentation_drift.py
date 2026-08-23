"""Documentation drift gate (issue #410): the kind-validation runbook must not
embed the legacy pre-#150 ``openstudio`` Redis password in a ``redis://`` URL.
Issue #409 extends the same gate to the REST contract doc
(``docs/contracts/openstudio-server-v3.11.0-rest.md``), which still cited the
literal as the *default* Redis password after #150/#219 rotated it into the
``openstudio-rotated`` placeholder. Issue #408 adds the README layout drift
gate: every file under ``deploy/`` must appear in README.md's
``## Repository layout`` tree with a per-file issue citation, so a new
manifest fails the build until it is documented. Issue #399 adds the
audit-policy gate: ``docs/audit-policy.md`` must keep a kube-apiserver
``audit.k8s.io/v1`` fragment whose four resource rules cover the operator's
entire mutating API surface, and ``docs/onboarding.md`` must keep
cross-linking it, so the SOC2/PCI recipe cannot silently rot out from
under the acceptance criteria.

Why this test exists
--------------------
``scripts/check_redis_password_unique.sh`` (issue #150) guards the *manifest*
password positions (``--requirepass``, ``stringData.password``, ``REDIS_URL``
env values) against the publicly-known ``openstudio`` literal — but it parses
YAML value positions only, so prose and fenced code blocks in
``docs/kind-validation.md`` are invisible to it. Four verification snippets in
that runbook still carried ``redis://:openstudio@queue...`` URLs after #150
landed. A reader who copies one into a fresh kind cluster gets an auth error
(the committed placeholder is ``openstudio-rotated``); a reader who copies one
into a pre-#150 cluster silently re-introduces the literal the shell guard
exists to keep out of source. The contract doc (the REST ground truth per
AGENTS.md) had the same defect in prose (issue #409).

Issue #485 extends the #408 README layout gate to the full inventory drift
shape: every file under ``deploy/`` must be named in AGENTS.md's hand-written
``deploy/`` Layout bullet (the inventory #465 rebuilt) AND in README.md's
Repository-layout ``deploy/`` subtree — in BOTH directions. An undocumented
manifest (added to ``deploy/`` but missing from a doc) and a phantom entry
(doc names a manifest that no longer exists) both fail the build, with the
offending file(s) and doc named in the failure message.

Issue #586 applies the same two-directional shape to the src/ package:
every top-level module under ``src/openstudio_operator/`` (minus
``__init__.py``, which the README subtree has never listed) must appear in
README.md's Repository-layout ``src/openstudio_operator/`` subtree, and every
module-shaped entry in that subtree must exist on disk. The subtree drifted
exactly because no gate watched it: ``_cr_cache.py`` (#497) and ``_retry.py``
(#416) shipped with no rows, and the ``_time.py`` row named a nonexistent
``parse_utc`` symbol — a doc that misnames a function actively misroutes
contributors. The nested ``handlers/`` sub-entries are indented one level
deeper and are deliberately NOT part of this top-level module inventory.

Historical capture logs are the one legitimate home for the literal: a capture
transcribed before #150 is authentic evidence and may keep it — but only when
the block is explicitly introduced by a leading ``PRE-#150`` marker line so no
reader can mistake it for a current recipe. These tests fail the build when the
legacy ``openstudio@`` credential literal appears anywhere in either doc
outside the exemption window of such a marker.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
KIND_VALIDATION_DOC = REPO_ROOT / "docs" / "kind-validation.md"
CONTRACTS_DOC = REPO_ROOT / "docs" / "contracts" / "openstudio-server-v3.11.0-rest.md"
README_DOC = REPO_ROOT / "README.md"
AGENTS_DOC = REPO_ROOT / "AGENTS.md"
DEPLOY_DIR = REPO_ROOT / "deploy"
README_LAYOUT_HEADING = "## Repository layout"
AUDIT_POLICY_DOC = REPO_ROOT / "docs" / "audit-policy.md"
ONBOARDING_DOC = REPO_ROOT / "docs" / "onboarding.md"

# Broader than the original `redis://:openstudio@` form (issue #409): the
# acceptance criterion greps for the bare credential literal so a Mongo-style
# citation cannot sneak past either. The rotated placeholder never matches —
# `openstudio-rotated@` has `-rotated` between the literal and the `@`.
LEGACY_URL_SUBSTRING = "openstudio@"
HISTORICAL_CAPTURE_MARKER = "PRE-#150"
EXEMPTION_WINDOW_LINES = 8

ROTATED_REDIS_URL = "redis://:openstudio-rotated@queue:6379"
ROTATION_SCRIPTS = ("scripts/rotate_redis_password.sh", "scripts/rotate_mongo_password.sh")

FENCE = "```"
LEGACY_DBSIZE_LINE = (
    "$ redis-cli -u redis://:openstudio@queue.openstudio-server.svc.cluster.local:6379 DBSIZE"
)
ROTATED_CLIENT_LINE = (
    "c = ReadOnlyRedisClient("
    "'redis://:openstudio-rotated@queue.openstudio-server.svc.cluster.local:6379')"
)


def legacy_password_urls_outside_capture_labels(lines: list[str]) -> list[tuple[int, str]]:
    """Return ``(line_number, line)`` pairs for legacy Redis-password URLs that
    are not covered by a leading historical-capture marker.

    A URL line is exempt when a ``PRE-#150`` marker line appears within
    ``EXEMPTION_WINDOW_LINES`` lines *above* it (the marker is a leading
    comment on the capture block, per issue #410's labeling convention).
    """
    marker_line_numbers = [
        number for number, line in enumerate(lines) if HISTORICAL_CAPTURE_MARKER in line
    ]
    offenders: list[tuple[int, str]] = []
    for number, line in enumerate(lines):
        if LEGACY_URL_SUBSTRING not in line:
            continue
        labeled = any(
            0 <= number - marker <= EXEMPTION_WINDOW_LINES for marker in marker_line_numbers
        )
        if not labeled:
            offenders.append((number + 1, line.rstrip()))
    return offenders


def test_kind_validation_doc_has_no_legacy_redis_password_urls() -> None:
    content = KIND_VALIDATION_DOC.read_text(encoding="utf-8")
    offenders = legacy_password_urls_outside_capture_labels(content.splitlines())
    assert not offenders, (
        "docs/kind-validation.md embeds the legacy pre-#150 Redis password in a "
        "redis:// URL outside a labeled historical-capture section (issue #410). "
        "Scrub the snippet to the `openstudio-rotated` placeholder, or prefix the "
        "capture block with a `PRE-#150 capture log, not a current recipe` marker "
        f"line. Offending lines: {offenders!r}"
    )


def test_unlabeled_legacy_url_is_flagged() -> None:
    lines = [FENCE, LEGACY_DBSIZE_LINE, FENCE]
    offenders = legacy_password_urls_outside_capture_labels(lines)
    assert [number for number, _ in offenders] == [2]
    assert LEGACY_URL_SUBSTRING in offenders[0][1]


def test_legacy_url_is_exempt_only_within_capture_label_window() -> None:
    marker_line = "# PRE-#150 capture log, not a current recipe — capture evidence below."
    legacy_url = "$ redis-cli -u redis://:openstudio@queue.example:6379 DBSIZE"

    within_window = [marker_line] + [""] * (EXEMPTION_WINDOW_LINES - 1) + [legacy_url]
    assert legacy_password_urls_outside_capture_labels(within_window) == []

    beyond_window = [marker_line] + [""] * EXEMPTION_WINDOW_LINES + [legacy_url]
    offenders = legacy_password_urls_outside_capture_labels(beyond_window)
    assert [number for number, _ in offenders] == [len(beyond_window)]


def test_rotated_placeholder_url_is_never_flagged() -> None:
    lines = [ROTATED_CLIENT_LINE]
    assert legacy_password_urls_outside_capture_labels(lines) == []


def test_contracts_doc_has_no_legacy_credential_urls() -> None:
    content = CONTRACTS_DOC.read_text(encoding="utf-8")
    offenders = legacy_password_urls_outside_capture_labels(content.splitlines())
    assert not offenders, (
        "docs/contracts/openstudio-server-v3.11.0-rest.md cites the legacy "
        "pre-#150 credential literal `openstudio@...` outside a labeled "
        "historical-capture section (issue #409). Cite the `openstudio-rotated` "
        "placeholder with the rotate-before-applying caveat, or prefix the "
        "capture block with a `PRE-#150 capture log, not a current recipe` "
        f"marker line. Offending lines: {offenders!r}"
    )


def test_contracts_doc_cites_rotated_placeholder_and_rotation_scripts() -> None:
    content = CONTRACTS_DOC.read_text(encoding="utf-8")
    assert ROTATED_REDIS_URL in content, (
        "The contract doc's queue-fabric section must cite the committed "
        "`openstudio-rotated` Redis placeholder URL, not a bare literal "
        "(issues #409 / #150)."
    )
    for script in ROTATION_SCRIPTS:
        assert script in content, (
            f"The contract doc must point readers at `{script}` "
            "(rotate-before-applying caveat, issues #409 / #150 / #219)."
        )


def readme_layout_section_lines() -> list[str]:
    """Return the lines of README.md's ``## Repository layout`` section only.

    Scoping to the section (up to the next ``## `` heading) means a manifest
    name mentioned elsewhere in the README (a test command, a runbook pointer)
    cannot mask layout drift (issue #408).
    """
    lines = README_DOC.read_text(encoding="utf-8").splitlines()
    section: list[str] = []
    in_section = False
    for line in lines:
        if line.startswith(README_LAYOUT_HEADING):
            in_section = True
            continue
        if in_section and line.startswith("## "):
            break
        if in_section:
            section.append(line)
    assert section, "README.md must contain a '## Repository layout' section"
    return section


def test_readme_layout_lists_every_deploy_file() -> None:
    """Issues #408 / #485: the README layout tree must enumerate every file
    under ``deploy/`` so load-bearing manifests (the cluster-scoped admission
    policy, the network policy, the credential Secrets, the quota and priority
    classes) are not invisible to first-time contributors. The directory is
    globbed at test time, so a future manifest that ships undocumented fails
    here until the README catches up. Since #485 the gate is exact and
    bidirectional: the ``deploy/`` subtree must list each manifest by name
    (all files, not just ``*.yaml``), and every name it lists must exist on
    disk — a phantom entry pointing at a deleted manifest fails too.
    """
    assert_deploy_inventory_matches(
        readme_deploy_subtree_entry_names(
            README_DOC.read_text(encoding="utf-8").splitlines()
        ),
        "README.md",
    )


# Issue #485 — the deploy/ inventory drift gate. AGENTS.md's ``deploy/``
# Layout bullet is maintained by hand (rebuilt in #465); nothing else keeps it
# honest. A backticked token inside that bullet counts as a listed manifest
# only when it is manifest-shaped: a bare ``*.yaml``/``*.json`` filename with
# no directory component. That excludes the bullet's cross-references
# (``tests/test_monitoring_artifacts.py`` — has a slash) and inline literals
# (``Recreate``, ``secrets: [get]``, ``spec.redisCredentials.secretRef``) while
# still catching a manifest-shaped name that is not a real ``deploy/`` file.
AGENTS_DEPLOY_BULLET_PREFIX = "- `deploy/`"
MANIFEST_NAME = re.compile(r"[\w.-]+\.(?:yaml|json)")
README_DEPLOY_DIR_LINE = "├── deploy/"
README_SUBTREE_ENTRY = re.compile(r"^│\s*[├└]──\s*(\S+)")


def deploy_dir_file_names() -> set[str]:
    """Every file currently shipping under ``deploy/``, globbed at test time
    (issue #485) — the inventory is derived from disk, never hardcoded."""
    return {path.name for path in DEPLOY_DIR.iterdir() if path.is_file()}


def agents_deploy_inventory_names(bullet: str) -> set[str]:
    """Return the manifest-shaped backticked names in one AGENTS.md-style
    ``deploy/`` Layout bullet (issue #485). Pure over the bullet line."""
    tokens = re.findall(r"`([^`]+)`", bullet)
    return {token for token in tokens if MANIFEST_NAME.fullmatch(token)}


def agents_deploy_inventory_bullet() -> str:
    """Return AGENTS.md's single ``deploy/`` Layout bullet line (issue #485)."""
    bullets = [
        line
        for line in AGENTS_DOC.read_text(encoding="utf-8").splitlines()
        if line.startswith(AGENTS_DEPLOY_BULLET_PREFIX)
    ]
    assert len(bullets) == 1, (
        "AGENTS.md must have exactly one Layout bullet starting "
        f"`{AGENTS_DEPLOY_BULLET_PREFIX}` — the deploy/ inventory #465 rebuilt "
        f"and #485 gates (found {len(bullets)})."
    )
    return bullets[0]


def readme_deploy_subtree_entry_names(lines: list[str]) -> set[str]:
    """Return the per-file entry names in a Repository-layout tree's
    ``deploy/`` subtree (issue #485). Pure over the tree lines.

    Scoping to the subtree — the ``├── deploy/`` line down to the next
    top-level ``├──``/``└──`` entry — means a manifest mentioned elsewhere in
    the README cannot mask inventory drift, and a non-deploy file named
    elsewhere in the tree cannot raise a phantom alarm.
    """
    start = next(
        (i for i, line in enumerate(lines) if README_DEPLOY_DIR_LINE in line), None
    )
    assert start is not None, (
        "README.md's Repository-layout tree must contain a "
        f"`{README_DEPLOY_DIR_LINE}` directory line (issue #408)."
    )
    names: set[str] = set()
    for line in lines[start + 1 :]:
        if line.startswith(("├──", "└──")):
            break
        match = README_SUBTREE_ENTRY.match(line)
        if match:
            names.add(match.group(1))
    return names


def assert_deploy_inventory_matches(documented: set[str], doc: str) -> None:
    """Issue #485 acceptance: the names a doc lists for ``deploy/`` must equal
    the directory contents exactly — both directions, with the offending
    file(s) and the doc named in the failure message."""
    on_disk = deploy_dir_file_names()
    undocumented = sorted(on_disk - documented)
    phantom = sorted(documented - on_disk)
    assert not undocumented and not phantom, (
        f"{doc} deploy/ inventory drifted from deploy/ on disk (issue #485). "
        f"Manifest(s) missing from {doc}: {undocumented}. "
        f"Phantom entrie(s) in {doc} naming no on-disk file: {phantom}. "
        "Update the doc inventory and the manifest together — the inventory "
        "is derived from `ls deploy/`, never hardcoded here."
    )


def test_agents_deploy_inventory_matches_deploy_dir() -> None:
    """Issue #485: AGENTS.md's hand-written ``deploy/`` Layout bullet (the
    inventory #465 rebuilt) must name every file under ``deploy/`` — and no
    manifest-shaped name it lists may be missing from disk. Without this gate
    a new manifest lands undocumented and the inventory silently rots again.
    """
    assert_deploy_inventory_matches(
        agents_deploy_inventory_names(agents_deploy_inventory_bullet()), "AGENTS.md"
    )


def test_agents_inventory_names_from_synthetic_bullet() -> None:
    bullet = (
        "- `deploy/` — `crd.yaml` · `operator-deployment.yaml` (single-replica, "
        "`Recreate`) · `release: prometheus` · `tests/test_monitoring_artifacts.py` "
        "· `spec.redisCredentials.secretRef` · `grafana-dashboard.json`"
    )
    assert agents_deploy_inventory_names(bullet) == {
        "crd.yaml",
        "operator-deployment.yaml",
        "grafana-dashboard.json",
    }


def test_readme_deploy_subtree_names_from_synthetic_tree() -> None:
    lines = [
        "```",
        "├── deploy/                     # CRD, RBAC, alerting",
        "│   ├── crd.yaml                # OpenStudioClusterManager CRD",
        "│   ├── phantom-manifest.yaml   # listed but never shipped",
        "│   └── grafana-dashboard.json  # dashboard (#468)",
        "├── docs/                       # runbooks",
        "│   └── nested-doc.md",
        "└── pyproject.toml",
        "```",
    ]
    assert readme_deploy_subtree_entry_names(lines) == {
        "crd.yaml",
        "phantom-manifest.yaml",
        "grafana-dashboard.json",
    }


# Primary introducing issue per manifest (from AGENTS.md §Layout and the
# file headers). Foundational files (crd.yaml, rbac.yaml,
# operator-deployment.yaml) predate the issue-tracking convention and are
# intentionally absent.
DEPLOY_INTRODUCING_ISSUES = {
    "keda-scaledobject.yaml": "#77",
    "redis-credentials-secret.yaml": "#77",
    "mongo-credentials-secret.yaml": "#219",
    "storage-cronjob.yaml": "#78",
    "network-policy.yaml": "#112",
    "pod-delete-admission-policy.yaml": "#293",
    "secret-read-admission-policy.yaml": "#572",
    "deployment-patch-admission-policy.yaml": "#573",
    "priority-class.yaml": "#414",
    "resource-quota.yaml": "#400",
}


def test_readme_deploy_entries_cite_introducing_issue() -> None:
    """Issue #408 acceptance: each documented deploy/ entry that has a known
    introducing issue must cite an issue number on its layout line.
    """
    section_lines = readme_layout_section_lines()
    offenders: list[str] = []
    for basename, issue in sorted(DEPLOY_INTRODUCING_ISSUES.items()):
        entry_lines = [line for line in section_lines if basename in line]
        if not entry_lines:
            offenders.append(f"{basename}: not listed in README layout")
            continue
        if not re.search(r"#\d+", entry_lines[0]):
            offenders.append(f"{basename}: layout comment cites no issue (expected {issue})")
    assert not offenders, (
        "README.md §Repository layout deploy/ entries must cite the "
        f"introducing issue per file (issue #408). Offenders: {offenders}"
    )


# Issue #399 — the kube-apiserver audit recipe. Each tuple is a (name, markers)
# pair: every marker string must appear in docs/audit-policy.md's fenced
# fragment. The exact quoting matches the committed fragment by design (same
# convention as ROTATED_REDIS_URL above): a reformatted fragment that drops a
# rule or un-scopes a verb fails here instead of shipping a weakened audit
# trail.
AUDIT_POLICY_FRAGMENT_HEADER = ("apiVersion: audit.k8s.io/v1", "kind: Policy")
AUDIT_POLICY_RESOURCE_RULES = {
    "openstudioclustermanagers (CR + status subresource)": (
        'group: "energy.nrel.gov"',
        'resources: ["openstudioclustermanagers", "openstudioclustermanagers/status"]',
    ),
    "apps/deployments (patch only)": (
        'verbs: ["patch"]',
        'resources: ["deployments"]',
    ),
    "batch/jobs (create/delete)": (
        'verbs: ["create", "delete"]',
        'resources: ["jobs"]',
    ),
    "core/pods (delete only)": (
        'verbs: ["delete"]',
        'resources: ["pods"]',
    ),
}


def test_audit_policy_doc_keeps_the_four_resource_rules() -> None:
    """Issue #399 acceptance: ``docs/audit-policy.md`` must ship a
    kube-apiserver ``audit.k8s.io/v1`` Policy fragment whose four resource
    rules cover the operator's entire mutating API surface (the OSCM CR and
    its ``.status``, Deployment rolling-restart patches, archival-Job
    create/delete, soft-stop pod deletes) at ``RequestResponse`` level.
    """
    content = AUDIT_POLICY_DOC.read_text(encoding="utf-8")
    for marker in AUDIT_POLICY_FRAGMENT_HEADER:
        assert marker in content, (
            f"docs/audit-policy.md must contain the audit Policy header line "
            f"`{marker}` (issue #399)."
        )
    assert "level: RequestResponse" in content, (
        "docs/audit-policy.md must record the operator's surface at "
        "RequestResponse level (issue #399 acceptance criterion)."
    )
    for rule_name, markers in AUDIT_POLICY_RESOURCE_RULES.items():
        for marker in markers:
            assert marker in content, (
                f"docs/audit-policy.md fragment lost the `{rule_name}` rule "
                f"(expected marker `{marker}`). If the rule moved or was "
                "reworded, update AUDIT_POLICY_RESOURCE_RULES in the same "
                "commit — do not delete the rule (issue #399 is the "
                "SOC2/PCI audit fence)."
            )


def test_onboarding_cross_links_audit_policy_doc() -> None:
    """Issue #399 acceptance: ``docs/onboarding.md`` must keep the audit-policy
    recipe discoverable next to the kind-validation walkthrough pointer."""
    content = ONBOARDING_DOC.read_text(encoding="utf-8")
    assert "./audit-policy.md" in content, (
        "docs/onboarding.md must cross-link docs/audit-policy.md (issue #399) "
        "— keep it next to the kind-validation row in 'Pointers to other docs'."
    )


# Issue #586 — the src/ inventory drift gate. README.md's hand-maintained
# ``src/openstudio_operator/`` subtree is the only place the package's module
# map is written for contributors; nothing else kept it honest, so it drifted
# the week #416 and #497 landed (missing rows, a nonexistent `parse_utc`
# symbol in the `_time.py` row). Mirrors the #485 deploy gate: the inventory
# is globbed from disk at test time and compared in BOTH directions.
SRC_PACKAGE_DIR = REPO_ROOT / "src" / "openstudio_operator"
README_SRC_PACKAGE_LINE = "├── src/openstudio_operator/"
# Depth-1 entries only: ``│   ├── name.py`` (│ + exactly three spaces). The
# nested handlers/ sub-subtree indents one level deeper (``│       ├── ...``)
# and must NOT be collected as top-level module names; the trailing ``.py``
# requirement also keeps the ``handlers/`` directory row itself out.
README_SRC_MODULE_ENTRY = re.compile(r"^│   [├└]──\s*(\S+\.py)")


def src_package_module_names() -> set[str]:
    """Every top-level module under ``src/openstudio_operator/``, globbed at
    test time (issue #586) — the inventory is derived from disk, never
    hardcoded. ``__init__.py`` is excluded: the README subtree documents the
    package's modules, not its packaging marker, and has never listed it."""
    return {
        path.name for path in SRC_PACKAGE_DIR.glob("*.py") if path.name != "__init__.py"
    }


def readme_src_subtree_entry_names(lines: list[str]) -> set[str]:
    """Return the depth-1 ``*.py`` entry names in a Repository-layout tree's
    ``src/openstudio_operator/`` subtree (issue #586). Pure over the tree
    lines.

    Scoping to the subtree — the ``├── src/openstudio_operator/`` line down
    to the next top-level ``├──``/``└──`` entry — means a module mentioned
    elsewhere in the README (a test command, a runbook pointer) cannot mask
    inventory drift, and the nested ``handlers/`` sub-entries are not
    mistaken for top-level modules.
    """
    start = next(
        (i for i, line in enumerate(lines) if README_SRC_PACKAGE_LINE in line), None
    )
    assert start is not None, (
        "README.md's Repository-layout tree must contain a "
        f"`{README_SRC_PACKAGE_LINE}` directory line (issue #586)."
    )
    names: set[str] = set()
    for line in lines[start + 1 :]:
        if line.startswith(("├──", "└──")):
            break
        match = README_SRC_MODULE_ENTRY.match(line)
        if match:
            names.add(match.group(1))
    return names


def assert_src_inventory_matches(documented: set[str], doc: str) -> None:
    """Issue #586 acceptance: the module names a doc lists for
    ``src/openstudio_operator/*.py`` must equal the on-disk top-level modules
    exactly — both directions, with the offending file(s) and the doc named
    in the failure message."""
    on_disk = src_package_module_names()
    undocumented = sorted(on_disk - documented)
    phantom = sorted(documented - on_disk)
    assert not undocumented and not phantom, (
        f"{doc} src/openstudio_operator/ module inventory drifted from "
        f"src/openstudio_operator/ on disk (issue #586). "
        f"Module(s) missing from {doc}: {undocumented}. "
        f"Phantom entrie(s) in {doc} naming no on-disk module: {phantom}. "
        "Update the doc tree and the module together — the inventory is "
        "derived from `ls src/openstudio_operator/*.py`, never hardcoded here."
    )


def test_readme_layout_lists_every_src_module() -> None:
    """Issue #586: the README layout tree must enumerate every top-level
    module under ``src/openstudio_operator/`` so shared-utility seams
    (``_cr_cache.py`` #497, ``_retry.py`` #416) are not invisible to
    contributors. The directory is globbed at test time, so a future module
    that ships undocumented fails here until the README catches up — and a
    phantom row naming a deleted module fails too."""
    assert_src_inventory_matches(
        readme_src_subtree_entry_names(
            README_DOC.read_text(encoding="utf-8").splitlines()
        ),
        "README.md",
    )


def test_readme_src_subtree_names_from_synthetic_tree() -> None:
    lines = [
        "```",
        "├── deploy/                     # CRD, RBAC, alerting",
        "│   └── crd.yaml                # CRD",
        "├── src/openstudio_operator/",
        "│   ├── _constants.py           # constants (#165)",
        "│   ├── phantom_module.py       # listed but never shipped",
        "│   └── handlers/               # handlers",
        "│       ├── analysis_sla.py     # nested — not a top-level module",
        "│       └── worker_recycler.py  # nested too",
        "├── tests/                      # tests",
        "└── pyproject.toml",
        "```",
    ]
    assert readme_src_subtree_entry_names(lines) == {
        "_constants.py",
        "phantom_module.py",
    }
