FROM python:3.12-slim AS build
# hatch-vcs reads the version from git.
RUN apt-get update && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*
COPY --from=ghcr.io/astral-sh/uv:0.5 /uv /bin/uv
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src ./src
COPY README.md ./
# .git is mounted for this step only, so it never lands in a layer. The
# writable mount keeps git's own writes, such as a refreshed index, out of
# the checkout. safe.directory because the checkout's owner need not be the
# build's user.
RUN --mount=type=bind,source=.git,target=/app/.git,rw \
    git config --global --add safe.directory /app \
    && uv sync --frozen --no-dev

FROM python:3.12-slim
WORKDIR /app
COPY --from=build /app /app
ENV PATH="/app/.venv/bin:$PATH"
# No USER: under rootless docker, root in the container is the unprivileged
# host user, which is what lets it read a 0600 config. Under rootful docker,
# set `user:` in compose to match the owner of ./config.
# A fresh volume copies this directory's mode, so any `user:` can write the
# run lock there.
RUN mkdir -p /data && chmod 0777 /data
VOLUME ["/data"]
EXPOSE 8080
ENTRYPOINT ["ifl", "--config", "/config/config.toml"]
CMD ["daemon"]
