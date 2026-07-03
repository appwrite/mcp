FROM python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

WORKDIR /app

COPY --from=ghcr.io/astral-sh/uv:0.11.22 /uv /usr/local/bin/uv

COPY pyproject.toml uv.lock README.md ./
COPY src ./src

RUN uv sync --frozen --no-dev

ENV HOST=0.0.0.0 \
    PORT=8000 \
    MCP_TRANSPORT=http \
    APPWRITE_ENDPOINT=https://cloud.appwrite.io/v1

EXPOSE 8000

CMD ["uv", "run", "--no-sync", "mcp-server-appwrite", "--transport", "http"]
