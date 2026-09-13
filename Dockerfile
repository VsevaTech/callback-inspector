ARG BASE_IMAGE=python:3.12-slim
FROM ${BASE_IMAGE}

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /srv

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY app ./app
COPY scripts ./scripts
# Vendor htmx from the npm registry so the UI works without a CDN.
# Non-fatal on purpose: in a build environment without egress the UI falls back to jsDelivr.
RUN python scripts/vendor_htmx.py || echo "WARN: could not vendor htmx, UI will use the CDN fallback"

RUN mkdir -p /srv/data
VOLUME ["/srv/data"]

EXPOSE 8000
ENV DATABASE_URL=sqlite:////srv/data/callback_inspector.db

HEALTHCHECK --interval=10s --timeout=3s --retries=5 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=2).status==200 else 1)"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
