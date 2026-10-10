FROM python:3.12-slim AS build
COPY --from=ghcr.io/astral-sh/uv:0.5 /uv /bin/uv
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src ./src
COPY README.md ./
RUN uv sync --frozen --no-dev

FROM python:3.12-slim
WORKDIR /app
COPY --from=build /app /app
ENV PATH="/app/.venv/bin:$PATH"
# No USER: under rootless docker, root in the container is the unprivileged
# host user, which is what lets it read a 0600 config and write the ledger.
# Under rootful docker, set `user:` in compose to match the owner of ./config
# and ./data.
VOLUME ["/data"]
ENTRYPOINT ["ifl", "--config", "/config/config.toml"]
CMD ["observe"]
