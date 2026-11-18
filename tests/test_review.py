"""Reviewing saved hard frames (re-training step 3)."""

from __future__ import annotations

import json
from pathlib import Path

import cv2
import httpx
import numpy as np
import pytest
from fastapi.testclient import TestClient

from storeguard.cloud.app import create_cloud_app
from storeguard.config import DetectorCfg
from storeguard.dashboard.app import create_app as create_dashboard_app
from storeguard.review import FrameNotFound, frame_path, get_frame, list_frames, save_review

DAY, CAM = "2026-11-18", "192.168.1.169"


def _frame(root: Path, stem: str, people, teacher=None, score=None) -> str:
    d = root / DAY / CAM
    d.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(d / f"{stem}.jpg"), np.zeros((100, 200, 3), np.uint8))
    meta = {"camera": CAM, "time": f"{DAY}T19:10:{stem[-2:]}", "reasons": ["uncertain"],
            "people": [{"id": i, "box": b, "conf": 0.5} for i, b in enumerate(people)]}
    if teacher is not None:
        meta["teacher"] = {"model": "yolo26x.pt", "boxes": [b + [0.9] for b in teacher]}
        meta["disagreement"] = {"score": score}
    (d / f"{stem}.json").write_text(json.dumps(meta))
    (d / f"{stem}.txt").write_text("")
    return f"{DAY}/{CAM}/{stem}"


def test_ids_cannot_leave_the_folder(tmp_path) -> None:
    fid = _frame(tmp_path, "191010_010", [])
    assert frame_path(tmp_path, fid).name == "191010_010.jpg"
    for bad in ["../x", f"{DAY}/../../etc/191010_010", "", f"{DAY}/{CAM}/191010_011", "/etc/passwd"]:
        with pytest.raises(FrameNotFound):
            frame_path(tmp_path, bad)


def test_queue_order_and_starting_boxes(tmp_path) -> None:
    a = _frame(tmp_path, "191010_001", [[10, 10, 50, 90]])                              # no teacher
    b = _frame(tmp_path, "191010_002", [[10, 10, 50, 90]], [[100, 0, 140, 80]], 1.0)    # big disagreement
    c = _frame(tmp_path, "191010_003", [[10, 10, 50, 90]], [[10, 10, 50, 90]], 0.0)
    res = list_frames(tmp_path)
    assert res["counts"] == {"todo": 3, "approved": 0, "rejected": 0}
    assert [f["id"] for f in res["items"]] == [b, a, c]

    f = get_frame(tmp_path, b)
    assert (f["width"], f["height"]) == (200, 100)
    assert f["boxes"] == [[0.5, 0.0, 0.7, 0.8]]       # starts from the teacher
    assert f["live"] == [[0.05, 0.1, 0.25, 0.9]]
    assert get_frame(tmp_path, a)["boxes"] == [[0.05, 0.1, 0.25, 0.9]]  # no teacher: live boxes


def test_approve_writes_labels_reject_excludes(tmp_path) -> None:
    a = _frame(tmp_path, "191010_001", [])
    b = _frame(tmp_path, "191010_002", [[10, 10, 50, 90]])
    out = save_review(tmp_path, a, "approved", [[0.5, 0.0, 0.7, 0.8], [0.6, 0.6, 0.6, 0.6], [1.2, 1, 0.9, 0.5]])
    assert out == {"id": a, "status": "approved", "boxes": 2}  # the zero-size click is dropped
    txt = (tmp_path / f"{a}.txt").read_text().splitlines()
    assert txt[0] == "0 0.600000 0.400000 0.200000 0.800000"
    assert txt[1] == "0 0.950000 0.750000 0.100000 0.500000"  # clamped + sorted corners
    assert get_frame(tmp_path, a)["boxes"][0] == [0.5, 0.0, 0.7, 0.8]  # reopens with your boxes

    save_review(tmp_path, b, "rejected")
    meta = json.loads((tmp_path / f"{b}.json").read_text())
    assert meta["review"]["status"] == "rejected" and "do not train" in meta["labels"]
    assert list_frames(tmp_path)["counts"] == {"todo": 0, "approved": 1, "rejected": 1}
    assert [f["id"] for f in list_frames(tmp_path, "approved")["items"]] == [a]
    with pytest.raises(ValueError):
        save_review(tmp_path, a, "maybe")


def test_dashboard_api(tmp_path) -> None:
    root = tmp_path / "hf"
    fid = _frame(root, "191010_001", [[10, 10, 50, 90]])
    app = create_dashboard_app(DetectorCfg(hard_frames_dir=str(root)), upload_dir=tmp_path / "up", data_dir=tmp_path / "d")
    with TestClient(app) as c:
        assert c.get("/api/hard-frames").json()["counts"]["todo"] == 1
        assert c.get("/api/hard-frames", params={"status": "bad"}).status_code == 422
        assert c.get("/api/hard-frames/frame", params={"id": fid}).json()["width"] == 200
        img = c.get("/api/hard-frames/image", params={"id": fid})
        assert img.status_code == 200 and img.headers["content-type"] == "image/jpeg"
        assert c.get("/api/hard-frames/image", params={"id": "../../x"}).status_code == 404
        r = c.post("/api/hard-frames/review", json={"id": fid, "status": "approved", "boxes": [[0.1, 0.1, 0.3, 0.9]]})
        assert r.json()["boxes"] == 1
        assert c.post("/api/hard-frames/review", json={"id": fid, "status": "nope"}).status_code == 422


def _members(app):
    owner = TestClient(app)
    owner.post("/api/auth/signup", json={"email": "o@a.com", "password": "ownerpass1", "org_name": "Store A"})
    owner.post("/api/org/members", json={"email": "g@a.com", "password": "guardpass1", "role": "security"})
    guard = TestClient(app)
    guard.post("/api/auth/login", json={"email": "g@a.com", "password": "guardpass1"})
    return owner, guard


def test_one_port_mode_needs_cams_edit(tmp_path) -> None:
    root = tmp_path / "hf"
    _frame(root, "191010_001", [])
    live = create_dashboard_app(DetectorCfg(hard_frames_dir=str(root)), upload_dir=tmp_path / "up", data_dir=tmp_path / "d")
    app = create_cloud_app(database_url=f"sqlite:///{tmp_path / 'c.db'}", secret_key="k" * 16,
                           create_tables=True, live_app=live)
    owner, guard = _members(app)
    assert owner.get("/live/api/hard-frames").status_code == 200
    assert guard.get("/live/api/sessions").status_code == 200        # security still sees cameras
    assert guard.get("/live/api/hard-frames").status_code == 401     # but not training frames
    assert TestClient(app).get("/live/api/hard-frames").status_code == 401


def test_proxy_mode_needs_cams_edit(tmp_path, monkeypatch) -> None:
    app = create_cloud_app(database_url=f"sqlite:///{tmp_path / 'c.db'}", secret_key="k" * 16, create_tables=True)
    owner, guard = _members(app)
    calls = []

    async def fake_request(self, method, url, **kwargs):
        calls.append((method, url))
        from types import SimpleNamespace
        return SimpleNamespace(content=b"{}", status_code=200, headers={})

    monkeypatch.setattr(httpx.AsyncClient, "request", fake_request)
    assert guard.get("/live/api/hard-frames").status_code == 403
    assert guard.post("/live/api/hard-frames/review", json={}).status_code == 403
    assert calls == []  # never forwarded
    assert owner.get("/live/api/hard-frames/frame", params={"id": "x"}).status_code == 200
    assert calls[-1] == ("GET", "http://127.0.0.1:8765/api/hard-frames/frame")
