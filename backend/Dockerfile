# ---------------------------------------------------------------------------
# OpsPilot AI backend. Single stage: the app is pure Python, the wheels we
# need ship binaries, so a builder stage buys us nothing yet.
#
# Installed from uv.lock (FOUND-002) so the image gets exactly what CI tested
# against, not whatever pip's resolver picks on the day of the build.
# ---------------------------------------------------------------------------
FROM python:3.12-slim AS runtime

# Dev dependencies (pytest, mypy, ruff) are a local-compose convenience, not
# something a hosted image should carry: docker-compose passes true, a
# deployment build leaves it false.
ARG INSTALL_DEV=false

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_LINK_MODE=copy

COPY --from=ghcr.io/astral-sh/uv:0.11.19 /uv /uvx /usr/local/bin/

WORKDIR /srv

# Dependency layer first so source edits do not invalidate the install.
COPY pyproject.toml uv.lock ./
RUN if [ "$INSTALL_DEV" = "true" ]; then \
      uv sync --locked --extra dev --no-install-project; \
    else \
      uv sync --locked --no-install-project; \
    fi

COPY . .
RUN if [ "$INSTALL_DEV" = "true" ]; then \
      uv sync --locked --extra dev; \
    else \
      uv sync --locked; \
    fi

ENV PATH="/srv/.venv/bin:${PATH}"

# Run as non-root: the container has no business writing to its own image.
RUN useradd --create-home --uid 10001 opspilot && chown -R opspilot:opspilot /srv
USER opspilot

# The platform assigns the port; 8000 is only the local default.
ENV PORT=8000
EXPOSE 8000
# Readiness is checked by the API's own /readyz (DB reachable + migrations current).
HEALTHCHECK --interval=10s --timeout=3s --start-period=20s --retries=5 \
  CMD python -c "import os,urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:%s/readyz' % os.environ.get('PORT','8000'), timeout=2).status==200 else 1)"

# scripts/start.sh resolves $PORT, applies migrations when the deployment asks
# for it, and configures the reverse-proxy headers (docs/deployment.md).
CMD ["scripts/start.sh"]
