FROM python:3.12-slim

WORKDIR /app

COPY pyproject.toml ./
COPY src ./src

RUN pip install --no-cache-dir .

# Kopf watches the namespace given at runtime; RBAC + CRD live in deploy/.
CMD ["kopf", "run", "--module", "openstudio_operator.handlers", "--namespace", "openstudio-server"]
