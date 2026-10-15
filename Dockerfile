# Pinned by digest so that a build is repeatable. Dependabot updates the pins.
FROM ghcr.io/astral-sh/uv:0.13.1@sha256:4a1f3666816dafadd20ceb71eac9a7d56d7ef30279f7d64048519ab901ada336 AS uv

FROM python:3.12.15-slim-trixie@sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1 AS build
# hatch-vcs reads the version from git.
RUN apt-get update && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*
COPY --from=uv /uv /bin/uv
WORKDIR /src
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/app/.venv
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
# The whole tree, .git included, so that the version can say whether the
# build had uncommitted changes. Only the venv leaves this stage.
COPY . .
RUN uv sync --frozen --no-dev --no-editable

FROM python:3.12.15-slim-trixie@sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1
WORKDIR /app
COPY --from=build /app/.venv /app/.venv
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
