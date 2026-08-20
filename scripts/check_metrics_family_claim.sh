#!/usr/bin/env bash
# CI / local guard — verify the metrics-family prose claim in README.md and
# docs/audit-dryrun-idempotency.md agrees with the canonical tuple counts
# in tests/_metrics_inventory.py (issue #387; home moved there by #406).
#
# The Python test in tests/test_metrics_family_prose_claim.py is the
# load-bearing assertion; this shell script is the grep-based mirror of
# the test-count guard pattern (#151 / #220) — it runs as a CI wall so a
# maintainer who only runs the lint path (without pytest) still gets the
# high-signal "claim is stale" failure message. The two gates share the
# same invariant and the same failure message shape.
#
# Exit codes:
#   0 — prose claims agree with the tuple counts.
#   1 — a prose claim disagrees with the tuple counts (or is missing).
#   2 — usage error / missing dependency.
#
# Usage:
#   bash scripts/check_metrics_family_claim.sh
#
# The script is intentionally dependency-free (bash + grep + python). CI
# runs it from the `lint` job on every pull_request event after the
# Python test has run. The tuple lengths are extracted via `python3 -c`
# that parses the AST of `tests/_metrics_inventory.py` directly — no
# package import is required, so the script works without the operator
# editable-install being on the Python path.
set -euo pipefail

# --- Derive tuple counts from the canonical source of truth.
# `tests/_metrics_inventory.py` is the SOLE source of truth for the
# counter / gauge / histogram family names (issue #406 moved the
# tuples out of `tests/test_metrics_endpoint.py`, which now imports
# them). The Python AST walk here is the lightweight alternative to
# `from _metrics_inventory import ...` (which would require the tests
# dir on sys.path). The AST approach reads the three tuples directly
# from the source, so the script works in CI even when only the lint
# path (no `pip install -e '.[dev]'`) has run.
PYTHON_AST_HELPER=$(cat <<'PY'
import ast
import sys

path = "tests/_metrics_inventory.py"
with open(path, "r", encoding="utf-8") as f:
    tree = ast.parse(f.read(), filename=path)

names = {
    "EXPECTED_COUNTER_FAMILIES": None,
    "EXPECTED_GAUGE_FAMILIES": None,
    "EXPECTED_HISTOGRAM_FAMILIES": None,
}
for node in tree.body:
    if isinstance(node, ast.Assign):
        for target in node.targets:
            if isinstance(target, ast.Name) and target.id in names:
                if isinstance(node.value, ast.Tuple):
                    names[target.id] = len(node.value.elts)

for name, value in names.items():
    if value is None:
        sys.stderr.write(f"ERROR: {name} not found in {path}\n")
        sys.exit(2)
    sys.stdout.write(f"{value}\n")
PY
)

TUPLE_COUNTS=$(python3 -c "$PYTHON_AST_HELPER")
COUNTERS=$(echo "$TUPLE_COUNTS" | sed -n '1p')
GAUGES=$(echo "$TUPLE_COUNTS" | sed -n '2p')
HISTOGRAMS=$(echo "$TUPLE_COUNTS" | sed -n '3p')

echo "Tuple counts: $COUNTERS counters + $GAUGES gauges + $HISTOGRAMS histograms"

# --- Inspect every prose location that carries the claim.
FAIL=0
for FILE in README.md docs/audit-dryrun-idempotency.md; do
    if [ ! -f "$FILE" ]; then
        echo "::error::$FILE not found in repo root" >&2
        FAIL=1
        continue
    fi
    # The canonical prose shape: "N counters + M gauges + K histograms".
    # The regex is anchored on word boundaries so a passing mention of
    # "16 counters" in prose doesn't accidentally satisfy the rule. The
    # `?` after the plural noun handles the singular case ("1 counter")
    # for future-proofing.
    CLAIM=$(grep -oE '[0-9]+ counters? \+ [0-9]+ gauges? \+ [0-9]+ histograms?' "$FILE" | head -1 || true)
    if [ -z "$CLAIM" ]; then
        echo "::error::$FILE is missing the 'N counters + M gauges + K histograms' claim" >&2
        FAIL=1
        continue
    fi
    C=$(echo "$CLAIM" | awk '{print $1}')
    G=$(echo "$CLAIM" | awk '{print $4}')
    H=$(echo "$CLAIM" | awk '{print $7}')
    echo "$FILE claim: $CLAIM"
    if [ "$C" != "$COUNTERS" ] || [ "$G" != "$GAUGES" ] || [ "$H" != "$HISTOGRAMS" ]; then
        echo "::error::$FILE metrics-family claim is stale: prose says '$CLAIM', tuple counts are $COUNTERS + $GAUGES + $HISTOGRAMS" >&2
        FAIL=1
    fi
done

if [ "$FAIL" -ne 0 ]; then
    echo "" >&2
    echo "Required prose shape (issue #387):" >&2
    echo "  README.md + docs/audit-dryrun-idempotency.md both carry:" >&2
    echo "  ${COUNTERS} counters + ${GAUGES} gauges + ${HISTOGRAMS} histograms" >&2
    echo "Mirror the existing 'tests/_metrics_inventory.py' tuples —" >&2
    echo "the prose is held to the tuple, not the other way around." >&2
    exit 1
fi

echo "Metrics-family prose claims agree with the tuple counts (issue #387)."
exit 0
