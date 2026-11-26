"""The load report: per-second numbers from running totals, and a plain verdict."""

from __future__ import annotations

import json
import time
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import storeguard.dashboard.app as dashboard_app
import storeguard.dashboard.load as load
from storeguard.cli import main
from storeguard.dashboard.pipeline import SessionStats


def _session(name="Aisle", fps=12.5, detect=12.5, camera=25.0, people=2, error=None,
             frame=0, detected=0):
    stats = SessionStats(
        filename=name, fps=fps, detect_fps=detect, camera_fps=camera, people=people,
        running=error is None, error=error, frame=frame, detected=detected,
    )
    return SimpleNamespace(id="s1", stats=stats, camera_id="cam-1")


def _totals(busy, rest, batches, frames):
    return [{
        "model": "models/yolo26m.pt", "device": "cuda", "runtime": "tensorrt",
        "busy_sec": busy, "rest_sec": rest, "batches": batches, "frames": frames,
    }]


def test_sample_turns_totals_into_per_second_numbers(monkeypatch) -> None:
    clock = SimpleNamespace(mono=100.0, cpu=5.0)
    monkeypatch.setattr(load, "time", SimpleNamespace(
        monotonic=lambda: clock.mono, process_time=lambda: clock.cpu, time=lambda: 1_700_000_000.0,
    ))
    monkeypatch.setattr(load, "gpu_reading", lambda: None)
    totals = _totals(1.0, 0.2, 10, 80)
    monkeypatch.setattr(load, "_detector_totals", lambda: totals)

    monitor = load.LoadMonitor()
    first = monitor.sample([_session(frame=100, detected=100)])
    assert "busy_percent" not in first["detectors"][0]  # nothing to compare to yet
    assert first["cameras"][0]["handled_fps"] == 12.5  # the session's own reading meanwhile

    clock.mono += 10.0
    clock.cpu += 32.0  # 3.2 cores for 10 s
    totals[0].update(busy_sec=8.6, rest_sec=2.1, batches=98, frames=1310)
    # 10 s later: 125 more frames handled, the detector looked at 10 of them (idle camera)
    report = monitor.sample([_session(fps=12.0, detect=2.0, frame=225, detected=110)])

    assert report["window_sec"] == 10.0
    assert report["process"]["cpu_cores_used"] == 3.2
    det = report["detectors"][0]
    assert det["model"] == "yolo26m.pt" and det["runtime"] == "tensorrt"
    assert det["busy_percent"] == 76 and det["rest_percent"] == 19
    assert det["frames_per_sec"] == 123.0
    assert det["frames_per_batch"] == pytest.approx(14.0, abs=0.1)
    cam = report["cameras"][0]
    assert (cam["camera_fps"], cam["handled_fps"], cam["detect_fps"], cam["people"]) == (25.0, 12.5, 1.0, 2)
    json.dumps(report)  # must be plain data


def _report(**over):
    base = {
        "pc": {"cpu_cores": 8, "cpu_percent": 40.0, "ram_total_gb": 16.0, "ram_used_gb": 8.0},
        "detectors": [{"model": "yolo26m.pt", "busy_percent": 30, "rest_percent": 5}],
        "cameras": [{"name": "Aisle", "handled_fps": 12.5, "running": True, "error": None}],
        "gpu": None,
    }
    base.update(over)
    return base


def test_notes_name_what_is_at_its_limit() -> None:
    assert load.notes({"cameras": []}) == ["No cameras are running."]
    assert "keeps up" in load.notes(_report())[0]

    full = load.notes(_report(pc={"cpu_cores": 8, "cpu_percent": 40.0, "ram_total_gb": 8.0, "ram_used_gb": 7.6}))
    assert any("Memory is almost full" in n for n in full)

    busy = load.notes(_report(detectors=[{"model": "yolo26m.pt", "busy_percent": 78, "rest_percent": 19}]))
    assert any("detector is at its limit" in n for n in busy)

    cpu = load.notes(_report(pc={"cpu_cores": 8, "cpu_percent": 96.0, "ram_total_gb": 16.0, "ram_used_gb": 8.0}))
    assert any("CPU is at its limit" in n for n in cpu)

    down = load.notes(_report(cameras=[{"name": "Door", "handled_fps": 0, "running": False, "error": "no video"}]))
    assert any("Door" in n for n in down)


def test_report_file_round_trip_and_command(tmp_path, monkeypatch, capsys) -> None:
    assert load.read_report(tmp_path) is None
    report = _report(ts=time.time(), window_sec=10.0, process={"cpu_cores_used": 1.5, "ram_gb": 1.2})
    report["cameras"][0].update(camera_fps=25.0, detect_fps=1.0, people=0)
    report["detectors"][0].update(device="cuda", runtime="pytorch", frames_per_sec=12.0, frames_per_batch=1.0)
    report["notes"] = load.notes(report)
    load.write_report(tmp_path, report)
    assert load.read_report(tmp_path)["cameras"][0]["name"] == "Aisle"

    main(["load", "--data", str(tmp_path)])
    out = capsys.readouterr().out
    assert "Aisle" in out and "yolo26m.pt" in out and "keeps up" in out


def test_command_says_so_when_nothing_is_running(tmp_path, capsys) -> None:
    with pytest.raises(SystemExit):
        main(["load", "--data", str(tmp_path)])
    assert "No current reading" in capsys.readouterr().out

    stale = _report(ts=time.time() - 3600, notes=[])
    load.write_report(tmp_path, stale)
    with pytest.raises(SystemExit):
        main(["load", "--data", str(tmp_path)])


def test_running_server_keeps_the_report_fresh(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(dashboard_app, "_LOAD_EVERY_SEC", 0.05)
    monkeypatch.setattr(load, "gpu_reading", lambda: None)
    app = dashboard_app.create_app(upload_dir=tmp_path / "uploads", data_dir=tmp_path / "data")
    with TestClient(app) as client:
        for _ in range(100):
            if load.read_report(tmp_path / "data") is not None:
                break
            time.sleep(0.05)
        on_disk = load.read_report(tmp_path / "data")
        assert on_disk is not None and on_disk["notes"] == ["No cameras are running."]
        assert client.get("/api/load").json()["cameras"] == []
