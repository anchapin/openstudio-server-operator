# Pinned by digest — issue #124 (operator/rclone/python base image drift).
# 3.12-slim @ 876416ecde9aca2bcc90e1fb0c7a9500bbf749f5788b70f82d4c5a5c2357f8b4
# Refresh this digest on Python security releases by running:
#   docker buildx imagetools inspect python:3.12-slim
# and updating both this line and the cosign-trust step in release.yml.
FROM python:3.12-slim@sha256:876416ecde9aca2bcc90e1fb0c7a9500bbf749f5788b70f82d4c5a5c2357f8b4

WORKDIR /app

COPY pyproject.toml ./
COPY README.md ./
COPY src ./src

RUN pip install --no-cache-dir .

# Kopf watches the namespace given at runtime; RBAC + CRD live in deploy/.
CMD ["kopf", "run", "--module", "openstudio_operator.handlers", "--namespace", "openstudio-server"]
