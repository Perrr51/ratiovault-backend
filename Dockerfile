FROM python:3.12-slim

WORKDIR /app

# System deps: curl for healthcheck, build-essential for native wheels (numpy/pandas)
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Build-time metadata for /version endpoint. Pass via --build-arg.
ARG GIT_SHA=unknown
ARG BUILT_AT=unknown
ARG ENVIRONMENT=unknown
ENV GIT_SHA=$GIT_SHA
ENV BUILT_AT=$BUILT_AT
ENV ENVIRONMENT=$ENVIRONMENT

ENV PYTHONUNBUFFERED=1
ENV PORT=8000

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
  CMD curl -fsS http://localhost:8000/health || exit 1

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
