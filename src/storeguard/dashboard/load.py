"""Load report: how hard this PC is working for the cameras, and what limits it.

``storeguard load`` (``.\\make.ps1 load``) prints it on the store PC. The
running server takes a reading every few seconds and keeps the newest one in
``<data>/load.json``; the command only reads that file, so it needs no login
and works while the server runs in the background.

A reading says, for the last few seconds:

* the PC: CPU cores, how busy they are, memory used;
* this program: CPU cores it uses, its memory;
* the graphics card (NVIDIA only): how busy, memory used;
* the detector: share of the time it is busy, batch sizes;
* every camera: frames a second it sends, frames looked at, frames that went
  through the detector, people in view.

and names what is at its limit, if anything.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

#: File name of the newest reading, inside the dashboard's data directory.
REPORT_NAME = "load.json"
#: A reading older than this is not shown as current.
STALE_AFTER_SEC = 60.0

_GB = 1024**3


def _pc_and_process() -> tuple[dict[str, Any], float | None]:
    """PC-wide numbers and this process's memory in GB (``None`` without psutil)."""
    pc: dict[str, Any] = {"cpu_cores": os.cpu_count() or 1}
    try:
        import psutil
    except Exception:  # noqa: BLE001 — optional: the report just has fewer lines
        return pc, None
    try:
        # Since the previous call; the very first one has nothing to compare to.
        pc["cpu_percent"] = round(float(psutil.cpu_percent(interval=None)), 1)
        mem = psutil.virtual_memory()
        pc["ram_total_gb"] = round(mem.total / _GB, 1)
        pc["ram_used_gb"] = round((mem.total - mem.available) / _GB, 1)
        return pc, round(psutil.Process().memory_info().rss / _GB, 2)
    except Exception:  # noqa: BLE001
        return pc, None


def gpu_reading() -> dict[str, Any] | None:
    """NVIDIA card: name, how busy, memory — from ``nvidia-smi`` (``None`` if absent)."""
    exe = shutil.which("nvidia-smi")
    if exe is None:
        return None
    try:
        out = subprocess.run(
            [exe, "--query-gpu=name,utilization.gpu,memory.used,memory.total",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5, check=False,
        )
        name, util, used, total = (part.strip() for part in out.stdout.splitlines()[0].split(","))
        return {
            "name": name,
            "util_percent": float(util),
            "mem_used_gb": round(float(used) / 1024, 1),
            "mem_total_gb": round(float(total) / 1024, 1),
        }
    except Exception:  # noqa: BLE001 — no card, old driver, odd output
        return None


def _detector_totals() -> list[dict[str, Any]]:
    try:
        from storeguard.detector import engine_stats
    except Exception:  # noqa: BLE001 — detection stack not installed
        return []
    return engine_stats()


class LoadMonitor:
    """Turns running totals into "per second over the last few seconds"."""

    def __init__(self) -> None:
        self._at: float | None = None
        self._cpu = 0.0
        self._detectors: dict[tuple[str, str], dict[str, Any]] = {}
        self._cameras: dict[str, tuple[int, int]] = {}

    def sample(self, sessions: list[Any]) -> dict[str, Any]:
        """One reading. ``sessions``: the running ``DetectionSession`` objects."""
        now = time.monotonic()
        cpu = time.process_time()
        window = now - self._at if self._at is not None else 0.0
        pc, ram_gb = _pc_and_process()
        process: dict[str, Any] = {}
        if window > 0:
            process["cpu_cores_used"] = round((cpu - self._cpu) / window, 2)
        if ram_gb is not None:
            process["ram_gb"] = ram_gb

        detectors = []
        totals = {(d["model"], d["device"]): dict(d) for d in _detector_totals()}
        for key, cur in totals.items():
            prev = self._detectors.get(key)
            row: dict[str, Any] = {
                "model": Path(cur["model"]).name, "device": cur["device"], "runtime": cur["runtime"],
            }
            if prev is not None and window > 0:
                batches = cur["batches"] - prev["batches"]
                frames = cur["frames"] - prev["frames"]
                row.update(
                    busy_percent=round(100 * (cur["busy_sec"] - prev["busy_sec"]) / window),
                    rest_percent=round(100 * (cur["rest_sec"] - prev["rest_sec"]) / window),
                    frames_per_sec=round(frames / window, 1),
                    frames_per_batch=round(frames / batches, 1) if batches else 0.0,
                )
            detectors.append(row)
        self._at, self._cpu, self._detectors = now, cpu, totals

        cameras = []
        counts: dict[str, tuple[int, int]] = {}
        for session in sessions:
            stats = session.stats
            handled_fps = stats.fps
            detect_fps = getattr(stats, "detect_fps", stats.fps)
            # Averages over the whole window when we can: an idle camera's
            # one-look-a-second reads as 0 or 2 in the session's half-second.
            key = getattr(session, "id", None)
            now_counts = (stats.frame, getattr(stats, "detected", stats.frame))
            before = self._cameras.get(key) if key is not None else None
            if key is not None:
                counts[key] = now_counts
            if before is not None and window > 0 and now_counts[0] >= before[0]:
                handled_fps = round((now_counts[0] - before[0]) / window, 1)
                detect_fps = round(max(0, now_counts[1] - before[1]) / window, 1)
            cameras.append({
                "name": stats.filename,
                "camera_id": getattr(session, "camera_id", None),
                "camera_fps": getattr(stats, "camera_fps", 0.0),
                "handled_fps": handled_fps,
                "detect_fps": detect_fps,
                "people": stats.people,
                "running": stats.running,
                "error": stats.error,
            })
        self._cameras = counts

        report: dict[str, Any] = {
            "ts": time.time(),
            "window_sec": round(window, 1),
            "pc": pc,
            "process": process,
            "gpu": gpu_reading() if sessions else None,
            "detectors": detectors,
            "cameras": cameras,
        }
        report["notes"] = notes(report)
        return report


def notes(report: dict[str, Any]) -> list[str]:
    """What is at its limit, in plain words (most important first)."""
    cameras = report.get("cameras") or []
    if not cameras:
        return ["No cameras are running."]
    out: list[str] = []

    down = [c["name"] for c in cameras if c.get("error")]
    if down:
        out.append(f"{len(down)} camera(s) have a problem: {', '.join(down[:4])}.")

    pc = report.get("pc") or {}
    total, used = pc.get("ram_total_gb"), pc.get("ram_used_gb")
    if total and used is not None and used / total >= 0.9:
        out.append(
            f"Memory is almost full ({used} of {total} GB). Windows starts swapping to disk "
            "and everything on the PC gets slow — close other programs or add memory."
        )

    busy = [d for d in report.get("detectors") or [] if "busy_percent" in d]
    saturated = [d for d in busy if d["busy_percent"] + d["rest_percent"] >= 90]
    if saturated:
        slowest = min((c["handled_fps"] for c in cameras if c.get("running")), default=0.0)
        out.append(
            "The detector is at its limit: it works all the time, so each camera is looked at "
            f"about {slowest:g} times a second. Fewer cameras, a lower detector.max_fps, or a "
            "faster detector (.\\make.ps1 optimize builds TensorRT) would help."
        )

    cpu = pc.get("cpu_percent")
    if cpu is not None and cpu >= 85:
        out.append(
            f"The CPU is at its limit ({cpu:g}% busy). With many cameras most of it is decoding "
            "their video — the cameras' low-resolution sub-stream costs far less."
        )

    gpu = report.get("gpu")
    if gpu and gpu["mem_total_gb"] and gpu["mem_used_gb"] / gpu["mem_total_gb"] >= 0.95:
        out.append(f"The graphics card's memory is full ({gpu['mem_used_gb']} of {gpu['mem_total_gb']} GB).")

    if not out:
        out.append("The PC keeps up with the cameras: nothing is at its limit.")
    return out


def one_line(report: dict[str, Any]) -> str:
    """The reading as one log line."""
    cameras = report.get("cameras") or []
    parts = [f"{len(cameras)} cameras"]
    running = [c for c in cameras if c.get("running")]
    if running:
        handled = [c["handled_fps"] for c in running]
        detected = [c["detect_fps"] for c in running]
        parts.append(
            f"looked at {sum(handled) / len(handled):.1f}/s each (slowest {min(handled):g}), "
            f"detector on {sum(detected) / len(detected):.1f}/s each"
        )
    for d in report.get("detectors") or []:
        if "busy_percent" in d:
            parts.append(f"detector {d['model']} busy {d['busy_percent']}%")
    process = report.get("process") or {}
    if "cpu_cores_used" in process:
        parts.append(f"CPU {process['cpu_cores_used']:g} cores")
    if "ram_gb" in process:
        parts.append(f"RAM {process['ram_gb']:g} GB")
    pc = report.get("pc") or {}
    if "cpu_percent" in pc:
        parts.append(f"PC CPU {pc['cpu_percent']:g}%")
    if "ram_used_gb" in pc:
        parts.append(f"PC RAM {pc['ram_used_gb']:g}/{pc['ram_total_gb']:g} GB")
    gpu = report.get("gpu")
    if gpu:
        parts.append(f"GPU {gpu['util_percent']:g}%, {gpu['mem_used_gb']:g}/{gpu['mem_total_gb']:g} GB")
    return "load: " + " · ".join(parts)


def write_report(data_dir: Path, report: dict[str, Any]) -> None:
    """Save the reading where ``storeguard load`` looks for it (atomically)."""
    path = Path(data_dir) / REPORT_NAME
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(report, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def read_report(data_dir: str | Path) -> dict[str, Any] | None:
    """The newest reading, or ``None`` if there is none (or it can't be read)."""
    try:
        return json.loads((Path(data_dir) / REPORT_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
