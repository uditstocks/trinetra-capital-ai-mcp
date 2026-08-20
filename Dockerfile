# Hosted Trinetra MCP server.
#
# Deliberately built from requirements-mcp.txt plus the database/web extras, not
# requirements.txt: the LangChain/LangGraph/NVIDIA stack belongs to the legacy
# CLI and has no place in the server image. The host AI is the reasoning layer,
# so no LLM library ships here.

FROM python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    TRINETRA_TRANSPORT=http \
    PORT=8000

WORKDIR /app

# Dependencies first so a source-only change does not reinstall the world.
COPY requirements-mcp.txt requirements-server.txt ./
RUN pip install --no-cache-dir -r requirements-server.txt

COPY trinetra/ ./trinetra/
COPY trinetra_mcp/ ./trinetra_mcp/
COPY migrations/ ./migrations/
COPY alembic.ini ./

# Never run as root.
RUN useradd --create-home --uid 10001 trinetra && chown -R trinetra:trinetra /app
USER trinetra

EXPOSE 8000

# Migrations run on release, then the server starts. Failing the migration must
# fail the release rather than booting against a stale schema.
CMD ["sh", "-c", "alembic upgrade head && python -m trinetra_mcp"]
