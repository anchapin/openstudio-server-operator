# Pinned by digest — issue #124 (operator/rclone/python base image drift).
# 3.12-slim-bookworm @ a116514e19457bcb7af7efe9c3dd0b9b71e85b317694e7882a1c52aa15a78134
# Issue #564: moved from python:3.12-slim (trixie) to the bookworm variant.
# The 2026-08 trixie digests still ship 4 gate-visible HIGHs (util-linux
# CVE-2026-53612/53613/53614/53615 — fixed in 2.41.5-0+deb13u1 upstream but
# not yet rebuilt into any published digest), while bookworm scans clean at
# the #481 trivy floor (CRITICAL,HIGH with --ignore-unfixed, the ci.yml
# audit-job flags) with .trivyignore empty. Same Python 3.12 line, so no
# test-matrix change; the 3.12-vs-3.14 evidence is on issue #564.
# Refresh this digest on Python security releases by running:
#   docker buildx imagetools inspect python:3.12-slim-bookworm
# and updating this line. (release.yml cosign signs the built image's own
# digest — it carries no base-digest reference.)
FROM python:3.12-slim-bookworm@sha256:a116514e19457bcb7af7efe9c3dd0b9b71e85b317694e7882a1c52aa15a78134

WORKDIR /app

COPY pyproject.toml ./
COPY requirements.txt ./
COPY README.md ./
COPY src ./src

# Install pinned runtime dependencies from the hash-verified runtime-only
# lockfile (#479): requirements.txt is compiled WITHOUT the dev extra, so
# pytest / hypothesis / responses / fakeredis / ruff and their transitives
# never enter the production image — every dev tool is executable attack
# surface inside a container whose API token carries pods/delete + jobs
# verbs. Hash pins keep the #11 supply-chain goal: a release-tag run that
# rebuilds the image twice produces two byte-identical `pip freeze`
# outputs. requirements.lock (--extra=dev) remains the CI test-env
# lockfile; refresh both together (see AGENTS.md).
RUN pip install --no-cache-dir --require-hashes -r requirements.txt \
 && pip install --no-cache-dir --no-deps .

# Kopf watches the namespace given at runtime; RBAC + CRD live in deploy/.
CMD ["kopf", "run", "--module", "openstudio_operator.handlers", "--namespace", "openstudio-server"]
