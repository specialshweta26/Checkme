FROM python:3.12-slim
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates curl \
 && arch="$(dpkg --print-architecture)" \
 && case "$arch" in amd64) node_arch=x64 ;; arm64) node_arch=arm64 ;; *) echo "unsupported $arch"; exit 1 ;; esac \
 && curl -fsSL "https://nodejs.org/dist/v22.21.1/node-v22.21.1-linux-${node_arch}.tar.gz" | tar -xz -C /usr/local --strip-components=1 \
 && rm -rf /var/lib/apt/lists/*
COPY packages /packages
COPY apps/api /app
RUN pip install --no-cache-dir -e /packages/connector_core -e /packages/gmail_mcp -e /packages/outlook_mcp -e . \
 && npm ci --prefix /app/pptx --omit=dev
ENV PYTHONUNBUFFERED=1
ENV PPTX_NODE_MODULES=/app/pptx/node_modules
EXPOSE 8000
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
