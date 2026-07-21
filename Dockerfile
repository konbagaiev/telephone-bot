# Image for the app service (ADR-017). Runs the ASGI app; the same image is used
# as a one-off to run migrations on deploy (`alembic upgrade head`).
FROM python:3.11-slim

WORKDIR /app

# Install dependencies first (declared in pyproject) for layer caching, then the
# source. `-e .` so `src` resolves to /app/src unambiguously for both uvicorn and
# alembic (which reads alembic.ini + migrations/ from /app).
COPY pyproject.toml ./
COPY src ./src
COPY migrations ./migrations
COPY alembic.ini ./
# The runtime configuration (questionnaires + policy) travels with the image
# (ADR-016: config is YAML in git). Only the tracked example dir — never the
# git-ignored real data that may sit beside it locally.
COPY data/example ./data/example
RUN pip install --no-cache-dir -e .

EXPOSE 8000

CMD ["uvicorn", "src.app:app", "--host", "0.0.0.0", "--port", "8000"]
