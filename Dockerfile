FROM python:3.15-slim@sha256:08a4c22c92b79ec821a7ad49768480039ee5b7accac3955228e75366e30abc8e AS base

ENV PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

WORKDIR /app

ARG PACKAGE_VERSION=0.0.0+docker
ENV SETUPTOOLS_SCM_PRETEND_VERSION=${PACKAGE_VERSION}

COPY --from=ghcr.io/astral-sh/uv:0.13.10@sha256:a3ea1357342c1a4bf9a0c53a3deb1b55379aefa037338d8113d5771efa01774f /uv /usr/local/bin/uv

COPY pyproject.toml uv.lock README.md ./
COPY src ./src

RUN uv sync --frozen --no-dev

ENV HOST=0.0.0.0 \
    PORT=8000 \
    MCP_TRANSPORT=http \
    APPWRITE_ENDPOINT=https://cloud.appwrite.io/v1

EXPOSE 8000

CMD ["uv", "run", "--no-sync", "mcp-server-appwrite", "--transport", "http"]
