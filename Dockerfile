FROM python:3.15-slim@sha256:65a65fc7b30b06cb6df4930fd4650697cb23f4c2698ab81fc87e4b031bc36928 AS base

ENV PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

WORKDIR /app

ARG PACKAGE_VERSION=0.0.0+docker
ENV SETUPTOOLS_SCM_PRETEND_VERSION=${PACKAGE_VERSION}

COPY --from=ghcr.io/astral-sh/uv:0.13.8@sha256:5cc1805e2426484abca4ad46ea783bab281fdb225fd82d8ce6d66a81254bac85 /uv /usr/local/bin/uv

COPY pyproject.toml uv.lock README.md ./
COPY src ./src

RUN uv sync --frozen --no-dev

ENV HOST=0.0.0.0 \
    PORT=8000 \
    MCP_TRANSPORT=http \
    APPWRITE_ENDPOINT=https://cloud.appwrite.io/v1

EXPOSE 8000

CMD ["uv", "run", "--no-sync", "mcp-server-appwrite", "--transport", "http"]
