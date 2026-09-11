# storeguard dashboard + edge agent — the two services that touch cameras
# and run detection on the GPU. Shared image; docker-compose.yml runs it
# twice with different commands.
#
# No CUDA base image needed: PyPI's default Linux torch wheel bundles its
# own CUDA runtime (confirmed by actually building this — a plain `uv sync`
# on Linux pulls nvidia-cudnn/nvidia-cusolver/triton etc. as dependencies of
# torch itself). That's different from Windows, where a plain `pip install
# torch` gets a CPU-only wheel — that gap was the actual bug we found and
# fixed natively (see detector.py's device-confirmation log). Inside this
# container, only the host's NVIDIA driver + GPU passthrough matter; the
# CUDA runtime rides in with torch.
#
# I could not run this against real GPU hardware — this Mac has no NVIDIA
# card. After it's up, confirm the GPU is actually being used the same way
# we already confirmed it natively: watch this container's logs for the
# "Detector device: ..." line when a camera connects — it should say
# "cuda (...)", not "cpu". If it says cpu, check GPU passthrough first
# (see docker-compose.yml's nvidia-smi sanity check) before suspecting this
# Dockerfile.
#
# Build:  docker build -f docker/edge.Dockerfile -t storeguard-edge .
# Run:    docker compose up dashboard   (needs GPU passthrough)
FROM python:3.13-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    libgl1 libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir uv

WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN uv sync --frozen --no-dev

ENV VIRTUAL_ENV="/app/.venv"
ENV PATH="$VIRTUAL_ENV/bin:$PATH"

# Pre-download the YOLO weights at build time so a fresh container never
# needs outbound network access just to start detecting.
RUN python -c "from ultralytics import YOLO; YOLO('yolo11n.pt')"

EXPOSE 8765

CMD ["storeguard", "dashboard", "--host", "0.0.0.0", "--port", "8765", "--device", "auto"]
