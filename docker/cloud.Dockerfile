# storeguard cloud control plane - CPU only. It never imports the detector
# (torch/ultralytics) at runtime, except a lazy `cv2` import for the
# per-camera "Test connection" probe, which is why libgl1/libglib2.0-0 are
# still needed even though there's no GPU here.
#
# Build:  docker build -f docker/cloud.Dockerfile -t storeguard-cloud .
# Run:    docker compose up cloud   (see docker-compose.yml)
FROM python:3.13-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    libgl1 libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir uv

WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
COPY alembic.ini ./
COPY src ./src
RUN uv sync --frozen --no-dev

ENV VIRTUAL_ENV="/app/.venv"
ENV PATH="$VIRTUAL_ENV/bin:$PATH"

EXPOSE 8000

# Production uses Alembic-managed schema, not --dev's create_all - see
# `storeguard cloud --help`.
CMD ["sh", "-c", "alembic upgrade head && exec storeguard cloud --host 0.0.0.0 --port 8000"]
