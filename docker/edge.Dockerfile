# storeguard dashboard + edge agent - the two services that touch cameras
# and run detection on the GPU. Shared image; docker-compose.yml runs it
# twice with different commands.
#
# No CUDA base image needed: the torch wheel bundles its own CUDA runtime;
# inside the container only the host's NVIDIA driver + GPU passthrough
# matter.
#
# BUT the torch in uv.lock (PyPI's default Linux wheel) is built for CUDA 13,
# which needs an NVIDIA driver >= 580 and dropped older GPUs. With an older
# host / WSL driver, `nvidia-smi` works inside the container but torch still
# reports no GPU and detection silently runs on the CPU. So this image
# installs torch from PyTorch's own index for the CUDA version picked by
# TORCH_CUDA instead:
#   cu128 (default) - driver >= 570, RTX 20xx and newer (incl. RTX 50xx)
#   cu126           - driver >= 560, also GTX 10xx / 16xx
# e.g.  TORCH_CUDA=cu126 docker compose build dashboard
#
# After it's up, check it with:
#   docker compose exec dashboard storeguard gpu-check
# and watch the logs for "Detector device: cuda (...)" when a camera connects.
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
# Everything from the lockfile except torch and its CUDA 13 runtime...
RUN uv sync --frozen --no-dev \
    --no-install-package torch --no-install-package torchvision \
    --no-install-package triton \
    --no-install-package nvidia-cublas --no-install-package nvidia-cuda-cupti \
    --no-install-package nvidia-cuda-nvrtc --no-install-package nvidia-cuda-runtime \
    --no-install-package nvidia-cudnn-cu13 --no-install-package nvidia-cufft \
    --no-install-package nvidia-cufile --no-install-package nvidia-curand \
    --no-install-package nvidia-cusolver --no-install-package nvidia-cusparse \
    --no-install-package nvidia-cusparselt-cu13 --no-install-package nvidia-nccl-cu13 \
    --no-install-package nvidia-nvjitlink --no-install-package nvidia-nvshmem-cu13 \
    --no-install-package nvidia-nvtx

# ...then torch + torchvision built for TORCH_CUDA (see the top of this file).
ARG TORCH_CUDA=cu128
RUN uv pip install --python /app/.venv/bin/python \
    --index-url "https://download.pytorch.org/whl/${TORCH_CUDA}" \
    torch torchvision \
 && /app/.venv/bin/python -c "import torch; print('torch', torch.__version__, 'CUDA build', torch.version.cuda)"

ENV VIRTUAL_ENV="/app/.venv"
ENV PATH="$VIRTUAL_ENV/bin:$PATH"

# Pre-download the YOLO weights at build time so a fresh container never
# needs outbound network access just to start detecting.
RUN python -c "from ultralytics import YOLO; [YOLO(m) for m in ('yolo11n.pt', 'yolo11s.pt', 'yolo11m.pt')]" \
 && python -c "import torchvision as tv; tv.models.resnet18(weights='DEFAULT'); tv.models.resnet50(weights='DEFAULT')"

EXPOSE 8765

CMD ["storeguard", "dashboard", "--host", "0.0.0.0", "--port", "8765", "--device", "auto"]
