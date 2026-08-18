# tests/fixtures/live/

Captured by `scripts/capture_fixtures.sh` from a **live kind cluster**
running the real `nrel/openstudio-server:3.11.0` stack (recipe:
`scripts/create-kind-cluster.sh` + `scripts/deploy-openstudio-stack.sh`;
walkthrough + drift findings: `docs/kind-validation.md`).

Each file is one JSON envelope: `{endpoint, method, http_status,
content_type, location?, body}`. `capture_meta.json` records the base URL,
timestamp and which ids were targeted. The committed set was captured with
`--mutate --with-delete` against a minimally-seeded analysis (nested REST
seeding routes are documented in `docs/kind-validation.md`).

Two files are documented **error shapes** (the drift checker reports them
as ERROR — allowed, not FAIL):

- `get_analysis_page_data_notfound.json` — 200 `{analysis: null}` for an
  unknown id (`mongoid.yml` `raise_not_found_error: false` — no 404s).
- `post_datapoint_requeue.json` — 500 on a datapoint that never had a
  Resque job (requeue only dps with a `job_id`).

Re-capture after any stack change with `scripts/capture_fixtures.sh`, then
`scripts/check_fixture_drift.py --live` and update the drift section of
`docs/kind-validation.md`.
