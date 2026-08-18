#!/usr/bin/env bash
# Capture OpenStudio Server REST API fixtures from a live 3.11.0 instance
# (issue #19). Curls every endpoint in the operator's API contract
# (.agents/skills/_shared/api-contracts/openstudio-server-v3.11.0-rest.md)
# and writes one JSON envelope per endpoint under tests/fixtures/live/.
#
# Read endpoints are always captured. MUTATING endpoints (soft_stop, action,
# requeue) require --mutate; the analysis DELETE cascade additionally
# requires --with-delete. Both print loud warnings before touching state.
#
# Fails loudly when the server is unreachable: this script is meaningless
# without the 3.11.0 stack running (scripts/deploy-openstudio-stack.sh).
#
# Usage:
#   scripts/capture_fixtures.sh [--base-url http://localhost:8080] [--mutate] [--with-delete]
#                               [--analysis-id <id>] [--data-point-id <id>]
#                               [--no-port-forward]
#
# Requires: curl, jq (and kubectl when auto port-forward is used).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
NAMESPACE="${NAMESPACE:-openstudio-server}"

BASE_URL="${BASE_URL:-http://localhost:8080}"
OUT_DIR="$REPO_ROOT/tests/fixtures/live"
DO_MUTATE=0
DO_DELETE=0
ANALYSIS_ID="${ANALYSIS_ID:-}"
DATA_POINT_ID="${DATA_POINT_ID:-}"
AUTO_PF=1
# Sentinel: a non-existent analysis/datapoint id. Live v3.11.0 servers use
# UUID strings as ids (NOT BSON ObjectIds), but any unknown id exercises the
# not-found code paths (which, note, do NOT return 404 — see the probes below).
SENTINEL_ID="ffffffffffffffffffffffff"

while [[ $# -gt 0 ]]; do
    case "$1" in
        --base-url) BASE_URL="$2"; shift 2 ;;
        --mutate) DO_MUTATE=1; shift ;;
        --with-delete) DO_DELETE=1; shift ;;
        --analysis-id) ANALYSIS_ID="$2"; shift 2 ;;
        --data-point-id) DATA_POINT_ID="$2"; shift 2 ;;
        --no-port-forward) AUTO_PF=0; shift ;;
        -h|--help) grep '^#' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "ERROR: unknown argument: $1 (see --help)" >&2; exit 1 ;;
    esac
done

for tool in curl jq; do
    if ! command -v "$tool" >/dev/null 2>&1; then
        echo "ERROR: required tool '$tool' not found on PATH." >&2
        exit 1
    fi
done

if [[ $DO_DELETE -eq 1 && $DO_MUTATE -eq 0 ]]; then
    echo "ERROR: --with-delete implies --mutate; pass both." >&2
    exit 1
fi

PORT_FORWARD_PID=""
cleanup() {
    if [[ -n "$PORT_FORWARD_PID" ]] && kill -0 "$PORT_FORWARD_PID" 2>/dev/null; then
        echo "Stopping port-forward (pid $PORT_FORWARD_PID) ..."
        kill "$PORT_FORWARD_PID" 2>/dev/null || true
    fi
}
trap cleanup EXIT

probe() { curl -sS --max-time 3 -o /dev/null "$BASE_URL" 2>/dev/null; }

if ! probe; then
    if [[ $AUTO_PF -eq 1 && -z "${BASE_URL##http://localhost:8080}" ]] && command -v kubectl >/dev/null 2>&1; then
        echo "Base URL $BASE_URL unreachable — starting kubectl port-forward svc/web 8080:80 ..."
        kubectl -n "$NAMESPACE" port-forward svc/web 8080:80 >/dev/null 2>&1 &
        PORT_FORWARD_PID=$!
        for _ in $(seq 1 30); do
            if probe; then break; fi
            sleep 1
        done
    fi
    if ! probe; then
        cat >&2 <<EOF
ERROR: OpenStudio Server is not reachable at $BASE_URL.
This script captures fixtures from a LIVE 3.11.0 stack and refuses to run
without one. Start it first:
    scripts/create-kind-cluster.sh
    scripts/deploy-openstudio-stack.sh
    kubectl -n $NAMESPACE port-forward svc/web 8080:80
(or pass --base-url pointing at an already-running server).
EOF
        exit 1
    fi
fi

mkdir -p "$OUT_DIR"
TMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TMP_DIR"; cleanup' EXIT

# capture <slug> <method> <path-and-query> [form-data] [accept]
# Writes $OUT_DIR/<slug>.json as:
#   {endpoint, method, http_status, content_type, location?, body}
#
# Content negotiation matters (live-verified): endpoints that have both
# format.html and format.json variants (action, requeue, DELETE) return the
# HTML variant — e.g. a 302 redirect for DELETE — unless Accept prefers
# JSON. The operator's HTTP client negotiates JSON, so we capture that
# (accept defaults to "application/json"). soft_stop is HTML-ONLY (no
# format.json): it is captured with Accept */* so the 302 redirect comes
# through, which is exactly what the operator observes.
# The redirect is recorded, NOT followed (-L is absent on purpose).
capture() {
    local slug="$1" method="$2" path="$3" data="${4:-}" accept="${5:-application/json}"
    local body_file="$TMP_DIR/body" header_file="$TMP_DIR/headers"
    local status ctype location
    local curl_args=(-sS -X "$method" -H "Accept: $accept"
                     -o "$body_file" -D "$header_file"
                     -w '%{http_code}\t%{content_type}' "$BASE_URL$path")
    if [[ -n "$data" ]]; then
        curl_args+=(-H 'Content-Type: application/x-www-form-urlencoded' --data "$data")
    fi
    if ! curl "${curl_args[@]}" > "$TMP_DIR/meta"; then
        echo "ERROR: curl failed for $method $path" >&2
        exit 1
    fi
    status="$(cut -f1 "$TMP_DIR/meta")"
    ctype="$(cut -f2 "$TMP_DIR/meta")"
    location="$(awk 'tolower($1)=="location:" {sub(/\r$/, ""); print $2}' "$header_file" | tail -1)"
    local body
    if jq -e . "$body_file" >/dev/null 2>&1; then
        body="$(cat "$body_file")"
        body="$(jq -n --argjson b "$body" '$b')"
    else
        body="$(jq -R -s . "$body_file")"
    fi
    local envelope
    envelope="$(jq -n \
        --arg endpoint "$path" \
        --arg method "$method" \
        --arg status "$status" \
        --arg ctype "$ctype" \
        --arg location "$location" \
        --argjson body "$body" \
        '{endpoint: $endpoint, method: $method,
          http_status: ($status | tonumber), content_type: $ctype,
          location: (if $location == "" then null else $location end),
          body: $body}')"
    printf '%s\n' "$envelope" > "$OUT_DIR/$slug.json"
    printf '  [%s] %s %s -> %s\n' "$status" "$method" "$path" "$OUT_DIR/$slug.json"
}

echo "Capturing fixtures from $BASE_URL into $OUT_DIR"
echo

echo "== Read endpoints (safe) =="
capture get_analyses GET "/analyses.json"

if [[ -z "$ANALYSIS_ID" ]]; then
    ANALYSIS_ID="$(jq -r '.body | if type == "array" and length > 0 then (.[0]._id // .[0].id | tostring) else "" end' \
        "$OUT_DIR/get_analyses.json")"
fi
if [[ -z "$ANALYSIS_ID" ]]; then
    ANALYSIS_ID="$SENTINEL_ID"
    echo
    echo "WARNING: no analyses exist on the server; per-analysis fixtures below" >&2
    echo "         use sentinel id $SENTINEL_ID and capture ERROR shapes only." >&2
    echo "         Create an analysis (web UI at $BASE_URL or the python client)" >&2
    echo "         and re-run for success-shape fixtures." >&2
fi
echo "Using ANALYSIS_ID=$ANALYSIS_ID"

capture get_analysis_status GET "/analyses/$ANALYSIS_ID/status.json"
capture get_analysis_page_data GET "/analyses/$ANALYSIS_ID/page_data.json"
capture get_data_points_status GET "/data_points/status?status=1&jobs=started"
capture get_data_points GET "/data_points.json"

if [[ -z "$DATA_POINT_ID" ]]; then
    DATA_POINT_ID="$(jq -r '.body | if type == "array" and length > 0 then (.[0]._id // .[0].id | tostring) else "" end' \
        "$OUT_DIR/get_data_points.json")"
fi
if [[ -z "$DATA_POINT_ID" ]]; then
    DATA_POINT_ID="$SENTINEL_ID"
    echo "WARNING: no data points exist; requeue captures the ERROR shape only." >&2
fi
echo "Using DATA_POINT_ID=$DATA_POINT_ID"

# Error-shape probes against a valid-but-unknown id (read-only, always
# captured). Live v3.11.0 truth: mongoid.yml sets raise_not_found_error:
# false, so these do NOT 404 — page_data returns {analysis: null} and status
# returns an empty {analyses: []}, both over HTTP 200.
capture get_analysis_page_data_notfound GET "/analyses/$SENTINEL_ID/page_data.json"
capture get_analysis_status_notfound GET "/analyses/$SENTINEL_ID/status.json"

if [[ $DO_MUTATE -eq 1 ]]; then
    cat <<EOF

WARNING: --mutate given — the next requests CHANGE SERVER STATE:
  * GET  /analyses/$ANALYSIS_ID/soft_stop      (flips run_flag; does not wait)
  * POST /analyses/$ANALYSIS_ID/action stop     (flips run_flag; waits in-flight)
  * POST /data_points/$DATA_POINT_ID/requeue    (moves a Resque job to :requeued)
  * POST /analyses/$ANALYSIS_ID/action start    (starts the analysis)
EOF
    read -r -p "Type 'mutate' to continue: " answer
    if [[ "$answer" != "mutate" ]]; then
        echo "Aborted — no mutating requests were made." >&2
        exit 1
    fi
    echo
    echo "== Mutating endpoints (--mutate) =="
    capture get_analysis_soft_stop GET "/analyses/$ANALYSIS_ID/soft_stop" "" "*/*"
    capture post_analysis_action_stop POST "/analyses/$ANALYSIS_ID/action" "analysis_action=stop"
    capture post_datapoint_requeue POST "/data_points/$DATA_POINT_ID/requeue"
    capture post_analysis_action_start POST "/analyses/$ANALYSIS_ID/action" "analysis_action=start"
else
    echo
    echo "Mutating endpoints (soft_stop, action, requeue) SKIPPED — pass --mutate to capture them."
fi

if [[ $DO_DELETE -eq 1 ]]; then
    cat <<EOF

WARNING: --with-delete given — the next request DESTROYS the analysis
  * DELETE /analyses/$ANALYSIS_ID
    Server-side cascade: data_points dependent:destroy; each dp after_destroy
    rm-rf's its NFS asset dir. THIS IS THE NFS CLEANUP PATH (contract).
EOF
    read -r -p "Type 'delete' to continue: " answer
    if [[ "$answer" != "delete" ]]; then
        echo "Aborted — no DELETE was made." >&2
        exit 1
    fi
    echo
    echo "== Destructive endpoints (--with-delete) =="
    capture delete_analysis DELETE "/analyses/$ANALYSIS_ID"
fi

jq -n \
    --arg base_url "$BASE_URL" \
    --arg captured_at "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
    --arg analysis_id "$ANALYSIS_ID" \
    --arg data_point_id "$DATA_POINT_ID" \
    --argjson mutate "$DO_MUTATE" \
    --argjson with_delete "$DO_DELETE" \
    '{base_url: $base_url, captured_at: $captured_at, analysis_id: $analysis_id,
      data_point_id: $data_point_id, mutate: $mutate, with_delete: $with_delete}' \
    > "$OUT_DIR/capture_meta.json"

echo
echo "Done. Wrote $(find "$OUT_DIR" -maxdepth 1 -name '*.json' | wc -l) fixture files to $OUT_DIR"
echo "Next: scripts/check_fixture_drift.py --live   (diff shapes vs the contract)"
