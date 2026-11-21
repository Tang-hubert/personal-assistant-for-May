FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.13.9 /uv /usr/local/bin/uv

ENV PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/app/.venv \
    UV_NO_SYNC=1 \
    PATH="/app/.venv/bin:${PATH}"

WORKDIR /app

COPY pyproject.toml uv.lock .python-version ./
# Dev group included so `docker compose run --rm app python scripts/test.py` works in the image.
RUN uv sync --frozen

COPY . .

RUN useradd --uid 1000 --create-home app \
    && mkdir -p /app/data \
    && chown app:app /app/data

USER app

CMD ["python", "-m", "src.main"]
