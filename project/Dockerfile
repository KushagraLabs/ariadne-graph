# syntax=docker/dockerfile:1
FROM python:3.12-slim

WORKDIR /app

# Install build dependencies for native wheels (e.g. tree-sitter, sqlite-vec)
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*

# Copy package source and install with all optional extras
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir -e ".[all]"

# Default to running the MCP server over stdio
CMD ["code-hygiene", "mcp", "--transport", "stdio"]
