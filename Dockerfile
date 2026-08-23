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
#
# The same install provides the pinned BUILD backend (#576): hatchling
# (plus its packaging / pathspec / pluggy / tomlkit / trove-classifiers
# closure) is hash-pinned inside requirements.txt, so the wheel-build
# toolchain is verified by the same --require-hashes gate as the runtime
# deps. pyproject.toml pins the backend exactly
# (`requires = ["hatchling==1.32.0"]`); the pin pair is drift-gated by
# tests/test_dependency_drift.py.
#
# `--no-build-isolation` (#576): default isolation would silently
# re-download the (unpinned-latest) build backend from PyPI into an
# ephemeral env — executing unverified PyPI code inside the image build
# and defeating both the digest-pin (#124) and the hash-pin provenance
# (cosign/SLSA prove who built, not that the toolchain was clean).
# `--no-deps` stays: the runtime closure is fully satisfied above.
RUN pip install --no-cache-dir --require-hashes -r requirements.txt \
 && pip install --no-cache-dir --no-deps --no-build-isolation .

# Issue #589: image-level non-root default. The deploy manifests have pinned
# runAsUser/fsGroup 1000 since #115/#161, but a manifest is a scheduler-side
# control — it does not travel with the artifact. Everything that runs the
# image outside those manifests (docker run, the release.yml dev-dep assert,
# kind-recipe debugging, downstream embeds) would execute as root. USER 1000
# matches the manifests' UID so file-ownership semantics stay identical; the
# operator writes nothing to disk (status lives in the CR subresource), and
# the Deployment's emptyDir /tmp covers any scratch need under
# readOnlyRootFilesystem.
USER 1000

# Kopf watches the namespace given at runtime; RBAC + CRD live in deploy/.
CMD ["kopf", "run", "--module", "openstudio_operator.handlers", "--namespace", "openstudio-server"]
