"""Check that detection will run on the NVIDIA GPU (used by make.ps1).

Exit codes:
  0  CUDA works - detection runs on the GPU
  1  no NVIDIA GPU on this machine - detection runs on CPU (nothing to fix)
  2  NVIDIA GPU present, but the installed torch is the CPU-only build
  3  CUDA build of torch installed, but the GPU can't be used (driver too old)
"""

from __future__ import annotations

import sys

from storeguard.config import cuda_help, nvidia_gpu_present


def main() -> int:
    import torch

    print(f"torch {torch.__version__} (CUDA build: {torch.version.cuda or 'none - CPU only'})")
    if torch.cuda.is_available():
        print(f"OK: detection will run on the GPU - {torch.cuda.get_device_name(0)}")
        return 0
    if not nvidia_gpu_present():
        print("No NVIDIA GPU found (nvidia-smi missing) - detection will run on CPU.")
        return 1
    print("NVIDIA GPU found, but torch can't use it. " + cuda_help())
    return 2 if torch.version.cuda is None else 3


if __name__ == "__main__":
    sys.exit(main())
