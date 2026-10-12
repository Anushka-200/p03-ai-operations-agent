# One image for the API, the scheduler and the Telegram bot; compose picks the command.
FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:0.9 /uv /usr/local/bin/uv
WORKDIR /srv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy PYTHONUNBUFFERED=1
# Dependencies first (cached layer), then the code.
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-install-project
COPY app ./app
COPY config ./config
COPY migrations ./migrations
COPY evals ./evals
COPY scripts ./scripts
RUN useradd --system --uid 10001 --no-create-home p03  # the app only reads /srv, so no slow chown of the virtualenv
USER p03
ENV PATH="/srv/.venv/bin:$PATH" PYTHONPATH=/srv
CMD ["uvicorn", "app.api.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8003"]
