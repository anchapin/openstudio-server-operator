# Pinned by digest — issue #124 (operator/rclone/python base image drift).
# 3.12-slim @ 876416ecde9aca2bcc90e1fb0c7a9500bbf749f5788b70f82d4c5a5c2357f8b4
# Refresh this digest on Python security releases by running:
#   docker buildx imagetools inspect python:3.12-slim
# and updating both this line and the cosign-trust step in release.yml.
FROM python:3.12-slim@sha256:876416ecde9aca2bcc90e1fb0c7a9500bbf749f5788b70f82d4c5a5c2357f8b4

WORKDIR /app

COPY pyproject.toml ./
COPY requirements.lock ./
COPY README.md ./
COPY src ./src

# Install pinned runtime dependencies from the hash-verified lockfile (#173),
# then the operator package itself without re-resolving deps. The lockfile
# pins kopf / kubernetes / redis / requests / prometheus-client to exact
# versions with --hash=sha256:... annotations; a release-tag run that
# rebuilds the image twice will produce two byte-identical `pip freeze`
# outputs (the supply-chain integrity goal #11).
RUN pip install --no-cache-dir --require-hashes -r requirements.lock \
 && pip install --no-cache-dir --no-deps .

# Kopf watches the namespace given at runtime; RBAC + CRD live in deploy/.
CMD ["kopf", "run", "--module", "openstudio_operator.handlers", "--namespace", "openstudio-server"]
