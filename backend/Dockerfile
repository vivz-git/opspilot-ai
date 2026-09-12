# ---------------------------------------------------------------------------
# OpsPilot AI backend. Single stage: the app is pure Python, the wheels we
# need ship binaries, so a builder stage buys us nothing yet.
# ---------------------------------------------------------------------------
FROM python:3.12-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /srv

# Dependency layer first so source edits do not invalidate the install.
COPY pyproject.toml ./
RUN pip install --upgrade pip && pip install -e ".[dev]"

COPY . .

# Run as non-root: the container has no business writing to its own image.
RUN useradd --create-home --uid 10001 opspilot && chown -R opspilot:opspilot /srv
USER opspilot

EXPOSE 8000
# Readiness is checked by the API's own /readyz (DB reachable + migrations current).
HEALTHCHECK --interval=10s --timeout=3s --start-period=20s --retries=5 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8000/readyz', timeout=2).status==200 else 1)"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
