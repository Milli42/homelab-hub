# ── Build stage ────────────────────────────────────────────
FROM python:3.13-slim AS builder

WORKDIR /build

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-editable

COPY app/ ./app/

# ── Runtime stage ─────────────────────────────────────────
FROM python:3.13-slim AS runtime

WORKDIR /app

COPY --from=builder /build/.venv /app/.venv
COPY --from=builder /build/app /app/app
COPY scripts/ /app/scripts/

RUN mkdir -p /data/images /data/notes/thumbs /data/recipes/thumbs /data/uploads

ENV PATH="/app/.venv/bin:$PATH"
ENV DATABASE_PATH=/data/homelab_hub.db
ENV IMAGES_DIR=/data/images
ENV NOTES_DIR=/data/notes
ENV RECIPES_DIR=/data/recipes
ENV UPLOADS_DIR=/data/uploads
ENV HOMEPAGE_URL=https://bookmarks.mjaranch.com

EXPOSE 8000

CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
