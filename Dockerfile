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

# Every first-party package the server imports. tests/test_packaging.py fails
# if one is added to the repo and forgotten here — which is exactly how the
# first deploy of the linking pages broke.
COPY trinetra/ ./trinetra/
COPY trinetra_mcp/ ./trinetra_mcp/
COPY trinetra_web/ ./trinetra_web/
COPY migrations/ ./migrations/
COPY alembic.ini ./

# Never run as root.
RUN useradd --create-home --uid 10001 trinetra && chown -R trinetra:trinetra /app

# Docker's USER directive does not set HOME. Without it Path.home() has to fall
# back to the passwd database — which works, but session.data_root() depends on
# it on every request, and matplotlib would rebuild its font cache on each boot
# for want of a writable config dir. One line removes both doubts.
ENV HOME=/home/trinetra     MPLCONFIGDIR=/home/trinetra/.cache/matplotlib
RUN mkdir -p /home/trinetra/.cache/matplotlib     && chown -R trinetra:trinetra /home/trinetra
USER trinetra

EXPOSE 8000

# Migrations run on release, then the server starts. Failing the migration must
# fail the release rather than booting against a stale schema.
CMD ["sh", "-c", "alembic upgrade head && python -m trinetra_mcp"]
