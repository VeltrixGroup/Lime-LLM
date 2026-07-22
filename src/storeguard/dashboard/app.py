"""FastAPI app for the storeguard detection dashboard."""

from __future__ import annotations

import shutil
import tempfile
import threading
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from storeguard.config import DetectorCfg
from storeguard.dashboard.pipeline import DetectionSession
from storeguard.geometry import Zone

_STATIC_DIR = Path(__file__).resolve().parent / "static"
_VIDEO_EXTS = {".mp4", ".avi", ".mkv", ".mov", ".webm", ".m4v"}


def _list_videos(data_dir: Path) -> list[dict[str, str | int]]:
    """Return video files under ``data_dir`` (non-recursive + one level deep)."""
    if not data_dir.is_dir():
        return []
    found: list[Path] = []
    for path in sorted(data_dir.iterdir()):
        if path.is_file() and path.suffix.lower() in _VIDEO_EXTS:
            found.append(path)
        elif path.is_dir():
            for child in sorted(path.iterdir()):
                if child.is_file() and child.suffix.lower() in _VIDEO_EXTS:
                    found.append(child)
    items: list[dict[str, str | int]] = []
    for path in found:
        rel = path.relative_to(data_dir).as_posix()
        try:
            size = path.stat().st_size
        except OSError:
            size = 0
        items.append({"name": rel, "path": rel, "size": size})
    return items


def _resolve_data_video(data_dir: Path, rel: str) -> Path:
    """Resolve a relative path inside ``data_dir``; reject path traversal."""
    if not rel or rel.startswith("/") or ".." in Path(rel).parts:
        raise HTTPException(status_code=400, detail="invalid video path")
    root = data_dir.resolve()
    candidate = (data_dir / rel).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="path outside data dir") from exc
    if not candidate.is_file():
        raise HTTPException(status_code=404, detail=f"video not found: {rel}")
    if candidate.suffix.lower() not in _VIDEO_EXTS:
        raise HTTPException(status_code=400, detail="unsupported video type")
    return candidate


class LocalSessionRequest(BaseModel):
    """Open a video that already lives under the dashboard data directory."""

    path: str = Field(..., description="Relative path under data/, e.g. clip.mp4")
    process_every: int = 1
    loop: bool = True


class CameraSessionRequest(BaseModel):
    """Open a live camera / RTSP (or HTTP) stream URL."""

    url: str = Field(
        ...,
        description="Camera URL, e.g. rtsp://user:pass@192.168.1.64:554/Streaming/Channels/101",
    )
    process_every: int = 1


def create_app(
    detector: DetectorCfg | None = None,
    upload_dir: Path | None = None,
    data_dir: Path | None = None,
    zones: list[Zone] | None = None,
    checkout_dwell_sec: float = 2.0,
) -> FastAPI:
    """Build the dashboard FastAPI application.

    Args:
        detector: YOLO settings for the person tracker (defaults to DetectorCfg).
        upload_dir: Where browser uploads are stored; a temp dir is used when None.
        data_dir: Folder of local videos to list/open (default: ``./data``).
        zones: Checkout / shelf / exit polygons for paid status (optional).
        checkout_dwell_sec: Seconds in a checkout zone before status becomes paid.
    """
    cfg = detector or DetectorCfg()
    own_upload_dir = upload_dir is None
    root = (
        Path(upload_dir)
        if upload_dir is not None
        else Path(tempfile.mkdtemp(prefix="storeguard-dash-"))
    )
    root.mkdir(parents=True, exist_ok=True)
    videos_root = Path(data_dir) if data_dir is not None else Path("data")
    videos_root.mkdir(parents=True, exist_ok=True)
    zone_list = list(zones or [])

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        yield
        with app.state.session_lock:
            if app.state.session is not None:
                app.state.session.stop()
                app.state.session = None
        if app.state.own_upload_dir and app.state.upload_root.exists():
            shutil.rmtree(app.state.upload_root, ignore_errors=True)

    app = FastAPI(
        title="storeguard dashboard",
        docs_url=None,
        redoc_url=None,
        lifespan=lifespan,
    )
    app.state.detector = cfg
    app.state.upload_root = root
    app.state.own_upload_dir = own_upload_dir
    app.state.data_dir = videos_root
    app.state.zones = zone_list
    app.state.checkout_dwell_sec = checkout_dwell_sec
    app.state.session: DetectionSession | None = None
    app.state.session_lock = threading.Lock()

    def _make_session(
        session_id: str,
        source: str,
        filename: str,
        process_every: int,
        loop: bool,
    ) -> DetectionSession:
        return DetectionSession(
            session_id=session_id,
            source=source,
            filename=filename,
            detector=app.state.detector,
            process_every=process_every,
            loop=loop,
            zones=app.state.zones,
            checkout_dwell_sec=app.state.checkout_dwell_sec,
        )

    def _activate(session: DetectionSession) -> None:
        with app.state.session_lock:
            if app.state.session is not None:
                app.state.session.stop()
            app.state.session = session

    @app.get("/")
    def index() -> FileResponse:
        index_path = _STATIC_DIR / "index.html"
        if not index_path.is_file():
            raise HTTPException(status_code=500, detail="dashboard UI missing")
        return FileResponse(index_path)

    @app.get("/api/videos")
    def list_local_videos() -> JSONResponse:
        """List videos dropped into the ``data/`` folder."""
        items = _list_videos(app.state.data_dir)
        return JSONResponse(
            {
                "data_dir": str(app.state.data_dir.resolve()),
                "videos": items,
            }
        )

    @app.post("/api/session/local")
    def create_local_session(body: LocalSessionRequest) -> JSONResponse:
        """Open a video from ``data/`` without re-uploading through the browser."""
        video_path = _resolve_data_video(app.state.data_dir, body.path)
        session_id = uuid.uuid4().hex[:12]
        session = _make_session(
            session_id=session_id,
            source=str(video_path),
            filename=body.path,
            process_every=body.process_every,
            loop=body.loop,
        )
        _activate(session)
        return JSONResponse(
            {
                "id": session_id,
                "filename": body.path,
                "process_every": session.process_every,
                "loop": body.loop,
                "source": "local",
            }
        )

    @app.post("/api/session/camera")
    def create_camera_session(body: CameraSessionRequest) -> JSONResponse:
        """Open a live RTSP/HTTP camera URL for detection."""
        url = body.url.strip()
        if not url.lower().startswith(("rtsp://", "http://", "https://")):
            raise HTTPException(
                status_code=400,
                detail="url must start with rtsp://, http:// or https://",
            )
        session_id = uuid.uuid4().hex[:12]
        # Short label for the HUD (hide credentials).
        label = url.split("@")[-1] if "@" in url else url
        if len(label) > 64:
            label = label[:61] + "..."
        session = _make_session(
            session_id=session_id,
            source=url,
            filename=label,
            process_every=body.process_every,
            loop=False,
        )
        _activate(session)
        return JSONResponse(
            {
                "id": session_id,
                "filename": label,
                "process_every": session.process_every,
                "loop": False,
                "source": "camera",
            }
        )

    @app.post("/api/session")
    async def create_session(
        file: UploadFile = File(...),
        process_every: int = Form(1),
        loop: bool = Form(True),
    ) -> JSONResponse:
        name = file.filename or "video.mp4"
        suffix = Path(name).suffix.lower()
        if suffix not in _VIDEO_EXTS:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"unsupported file type {suffix!r}; "
                    f"expected one of {sorted(_VIDEO_EXTS)}"
                ),
            )
        session_id = uuid.uuid4().hex[:12]
        dest = app.state.upload_root / f"{session_id}{suffix}"
        try:
            with dest.open("wb") as out:
                while True:
                    chunk = await file.read(1024 * 1024)
                    if not chunk:
                        break
                    out.write(chunk)
        except Exception as exc:  # noqa: BLE001
            dest.unlink(missing_ok=True)
            raise HTTPException(
                status_code=500, detail=f"failed to save upload: {exc}"
            ) from exc

        session = _make_session(
            session_id=session_id,
            source=str(dest),
            filename=name,
            process_every=process_every,
            loop=loop,
        )
        _activate(session)

        return JSONResponse(
            {
                "id": session_id,
                "filename": name,
                "process_every": session.process_every,
                "loop": loop,
                "source": "upload",
            }
        )

    def _require_session(session_id: str) -> DetectionSession:
        with app.state.session_lock:
            session = app.state.session
        if session is None or session.id != session_id:
            raise HTTPException(status_code=404, detail="session not found")
        return session

    @app.post("/api/session/{session_id}/start")
    def start_session(session_id: str, process_every: int | None = None) -> JSONResponse:
        session = _require_session(session_id)
        if process_every is not None:
            session.set_process_every(process_every)
        session.start()
        return JSONResponse({"id": session_id, "running": True})

    @app.post("/api/session/{session_id}/stop")
    def stop_session(session_id: str) -> JSONResponse:
        session = _require_session(session_id)
        session.stop()
        return JSONResponse({"id": session_id, "running": False})

    @app.get("/api/session/{session_id}/stats")
    def session_stats(session_id: str) -> JSONResponse:
        session = _require_session(session_id)
        s = session.stats
        return JSONResponse(
            {
                "id": session_id,
                "filename": s.filename,
                "people": s.people,
                "fps": s.fps,
                "frame": s.frame,
                "total_frames": s.total_frames,
                "tracks": s.tracks,
                "people_status": [
                    {"track_id": p.track_id, "status": p.status}
                    for p in s.people_status
                ],
                "paid": s.paid,
                "not_paid": s.not_paid,
                "running": s.running,
                "error": s.error,
            }
        )

    @app.get("/api/session/{session_id}/stream")
    def session_stream(session_id: str) -> StreamingResponse:
        session = _require_session(session_id)
        boundary = "frame"

        def generate():
            seq = 0
            idle_rounds = 0
            while True:
                result = session.wait_next_jpeg(after_seq=seq, timeout=1.0)
                if result is None:
                    idle_rounds += 1
                    if not session.is_alive() and idle_rounds >= 2:
                        break
                    continue
                idle_rounds = 0
                jpeg, seq = result
                yield (
                    b"--"
                    + boundary.encode()
                    + b"\r\n"
                    b"Content-Type: image/jpeg\r\n"
                    b"Content-Length: "
                    + str(len(jpeg)).encode()
                    + b"\r\n\r\n"
                    + jpeg
                    + b"\r\n"
                )

        return StreamingResponse(
            generate(),
            media_type=f"multipart/x-mixed-replace; boundary={boundary}",
        )

    if _STATIC_DIR.is_dir():
        app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")

    return app


def serve(
    host: str = "127.0.0.1",
    port: int = 8765,
    detector: DetectorCfg | None = None,
    data_dir: str | Path | None = None,
    zones: list[Zone] | None = None,
    checkout_dwell_sec: float = 2.0,
) -> None:
    """Run the dashboard with uvicorn (blocking)."""
    import uvicorn

    app = create_app(
        detector=detector,
        data_dir=Path(data_dir) if data_dir is not None else Path("data"),
        zones=zones,
        checkout_dwell_sec=checkout_dwell_sec,
    )
    uvicorn.run(app, host=host, port=port, log_level="info")
