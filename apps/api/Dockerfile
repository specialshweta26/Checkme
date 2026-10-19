FROM python:3.12-slim
WORKDIR /app
COPY packages /packages
COPY apps/api /app
RUN pip install --no-cache-dir -e /packages/connector_core -e /packages/gmail_mcp -e /packages/outlook_mcp -e .
ENV PYTHONUNBUFFERED=1
EXPOSE 8000
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
