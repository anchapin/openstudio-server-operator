# Pinned by digest — issue #124 (operator/rclone/python base image drift).
# 3.12-slim @ 876416ecde9aca2bcc90e1fb0c7a9500bbf749f5788b70f82d4c5a5c2357f8b4
# Refresh this digest on Python security releases by running:
#   docker buildx imagetools inspect python:3.12-slim
# and updating both this line and the cosign-trust step in release.yml.
FROM python:3.14-slim@sha256:ce40764625a4ff50df3548277632e7f96c4e77fe75fa848aae9885476e7df5a4

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
