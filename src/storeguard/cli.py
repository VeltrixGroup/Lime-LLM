"""Command-line interface for storeguard.

Subcommands:

* ``run`` — run the detection pipeline from a YAML config.
* ``camera-check`` — check that cameras connect and send frames.
* ``load`` — how hard the PC is working for the cameras, and what limits it.
* ``dashboard`` — local web UI to upload a video and watch person detection.
* ``draw-zones`` — interactively draw polygon zones over a camera frame.
* ``annotate`` — keyboard labeler producing a labels CSV from raw videos.
* ``make-dataset`` — cut labeled segments into per-class training clips.
* ``train`` — fine-tune the action classifier on the cut clips.

Heavy dependencies (torch, ultralytics, cv2 pipelines) are imported inside
each subcommand handler so ``storeguard --help`` stays instant.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from rich.console import Console

from . import __version__

console = Console()

DEFAULT_CLASSES = "normal,pocket,take_cash"


def _cmd_run(args: argparse.Namespace) -> None:
    """Handler for ``storeguard run``."""
    from .config import load_config

    console.print(f"[bold]storeguard run[/bold] — loading config '{args.config}'")
    cfg = load_config(args.config)

    from .runner import run  # heavy: torch + ultralytics

    run(cfg, show=args.show)


def _dashboard_kwargs(args: argparse.Namespace, url: str) -> dict:
    """Detector/zones/agent settings shared by ``dashboard`` and ``cloud --with-dashboard``."""
    import os

    from .config import DetectorCfg, ZoneCfg, load_config
    from .geometry import zones_from_cfg

    agent_server = args.agent_server or os.environ.get("STOREGUARD_AGENT_SERVER", "")
    agent_key = args.agent_key or os.environ.get("STOREGUARD_AGENT_KEY", "")

    from .config import ActionCfg

    detector = DetectorCfg(device=args.device)
    action = ActionCfg()
    zones = []
    if args.config:
        cfg = load_config(args.config)
        detector = cfg.detector
        action = cfg.action
        if args.device != "auto":
            detector = detector.model_copy(update={"device": args.device})
        # Prefer the first camera that has zones (paid / not-paid needs checkout).
        for cam in cfg.cameras:
            if cam.zones:
                zones = zones_from_cfg(cam.zones)
                break
    if args.zones:
        import yaml

        zpath = Path(args.zones)
        with zpath.open("r", encoding="utf-8") as fh:
            zraw = yaml.safe_load(fh) or {}
        zones = zones_from_cfg(
            [ZoneCfg.model_validate(z) for z in zraw.get("zones", [])]
        )

    data_dir = args.data
    console.print(
        f"[bold]storeguard dashboard[/bold] — open [cyan]{url}[/cyan] "
        f"(device={detector.device}, data={data_dir})"
    )
    console.print(
        f"[dim]Drop videos into [bold]{data_dir}/[/bold], then pick one in the UI.[/dim]"
    )
    if zones:
        names = ", ".join(z.name for z in zones)
        console.print(f"[dim]Paid status zones: {names}[/dim]")
    else:
        console.print(
            "[yellow]No checkout zones loaded — everyone stays "
            "'not paid'. Pass --config or --zones with a checkout* zone.[/yellow]"
        )
    if agent_server and agent_key:
        console.print(
            f"[dim]Shoplifting alerts: confirmed exit-without-paying events + "
            f"clips will be pushed to {agent_server}.[/dim]"
        )
    else:
        console.print(
            "[yellow]No cloud agent key configured — evidence clips are saved "
            "locally only; no Telegram / Lime CRM notifications will be sent. "
            "Pass --agent-server/--agent-key or set STOREGUARD_AGENT_SERVER/"
            "STOREGUARD_AGENT_KEY (create a key in the cabinet's Devices "
            "page).[/yellow]"
        )
    return {
        "detector": detector,
        "data_dir": data_dir,
        "zones": zones,
        "agent_server": agent_server or None,
        "agent_key": agent_key or None,
        "action": action,
    }


def _cmd_camera_check(args: argparse.Namespace) -> None:
    """Handler for ``storeguard camera-check``: do the cameras work?"""
    from rich.table import Table

    from .camera_check import check_camera, show_live

    cams: list[tuple[str, str]] = []
    if args.config:
        import yaml

        with open(args.config, encoding="utf-8") as fh:
            raw = yaml.safe_load(fh) or {}
        for i, cam in enumerate(raw.get("cameras") or []):
            cams.append((str(cam.get("name") or f"camera{i + 1}"), str(cam.get("source", ""))))
    for i, src in enumerate(args.source or []):
        cams.append((f"source{i + 1}", src))
    if not cams:
        console.print("[red]Give --source URL (repeatable) or --config YAML.[/red]")
        raise SystemExit(2)

    snap = Path(args.snapshot) if args.snapshot else None
    table = Table(title="Camera check")
    for col in ("camera", "status", "resolution", "fps", "first frame", "details"):
        table.add_column(col)
    failed = 0
    working: list[tuple[str, str]] = []
    for name, src in cams:
        with console.status(f"checking {name}..."):
            r = check_camera(name, src, seconds=args.seconds, timeout=args.timeout, snapshot_dir=snap)
        if r.ok:
            working.append((name, src))
            table.add_row(
                name, "[green]OK[/green]", f"{r.width}x{r.height}", f"{r.fps:.1f}",
                f"{r.first_frame_sec:.1f}s", r.snapshot or r.source,
            )
        else:
            failed += 1
            table.add_row(name, "[red]FAIL[/red]", "-", "-", "-", f"{r.error}\n{r.source}")
    console.print(table)
    if not args.no_show:
        for name, src in working:
            console.print(f"Showing [bold]{name}[/bold] live — press q to close.")
            show_live(name, src, timeout=args.timeout)
    if failed:
        console.print(f"[red]{failed} of {len(cams)} camera(s) not working.[/red]")
        raise SystemExit(1)
    console.print(f"[green]All {len(cams)} camera(s) working.[/green]")


def _cmd_dashboard(args: argparse.Namespace) -> None:
    """Handler for ``storeguard dashboard`` (standalone, its own port)."""
    from .dashboard.app import serve

    kwargs = _dashboard_kwargs(args, f"http://{args.host}:{args.port}")
    serve(host=args.host, port=args.port, **kwargs)


def _print_load(report: dict) -> None:
    """Render one load reading (see :mod:`storeguard.dashboard.load`)."""
    import time

    from rich.table import Table

    age = time.time() - float(report.get("ts", 0))
    console.print(
        f"[bold]StoreGuard load[/bold] — {time.strftime('%d.%m %H:%M:%S', time.localtime(report.get('ts', 0)))}"
        f" (measured over {report.get('window_sec', 0):g} s, {age:.0f} s ago)"
    )
    pc, process = report.get("pc") or {}, report.get("process") or {}
    line = f"PC          {pc.get('cpu_cores', '?')} CPU cores"
    if "cpu_percent" in pc:
        line += f", {pc['cpu_percent']:g}% busy"
    if "ram_used_gb" in pc:
        line += f" · memory {pc['ram_used_gb']:g} of {pc['ram_total_gb']:g} GB used"
    console.print(line)
    line = "StoreGuard  "
    if "cpu_cores_used" in process:
        line += f"uses {process['cpu_cores_used']:g} CPU cores"
    if "ram_gb" in process:
        line += f" · {process['ram_gb']:g} GB of memory"
    console.print(line)
    gpu = report.get("gpu")
    if gpu:
        console.print(
            f"Graphics    {gpu['name']} · {gpu['util_percent']:g}% busy · "
            f"memory {gpu['mem_used_gb']:g} of {gpu['mem_total_gb']:g} GB used"
        )
    for d in report.get("detectors") or []:
        line = f"Detector    {d['model']} on {d['device']} ({d['runtime']})"
        if "busy_percent" in d:
            line += (
                f" · busy {d['busy_percent']}% of the time, resting {d['rest_percent']}% · "
                f"{d['frames_per_sec']:g} frames/s in batches of {d['frames_per_batch']:g}"
            )
        console.print(line)

    cameras = report.get("cameras") or []
    if cameras:
        table = Table(title=f"{len(cameras)} cameras (frames per second)")
        for col in ("camera", "camera sends", "looked at", "detector ran", "people", "state"):
            table.add_column(col)
        for c in cameras:
            state = "[red]" + str(c["error"]) + "[/red]" if c.get("error") else (
                "[green]running[/green]" if c.get("running") else "stopped"
            )
            table.add_row(
                str(c["name"]), f"{c['camera_fps']:g}" if c.get("camera_fps") else "-",
                f"{c['handled_fps']:g}", f"{c['detect_fps']:g}", str(c["people"]), state,
            )
        console.print(table)
    for note in report.get("notes") or []:
        console.print(f"[bold]→[/bold] {note}")


def _cmd_load(args: argparse.Namespace) -> None:
    """Handler for ``storeguard load``: how hard is the PC working for the cameras?"""
    import time

    from .dashboard.load import REPORT_NAME, STALE_AFTER_SEC, read_report

    while True:
        report = read_report(args.data)
        if report is None or time.time() - float(report.get("ts", 0)) > STALE_AFTER_SEC:
            console.print(
                f"[yellow]No current reading in {Path(args.data) / REPORT_NAME}.[/yellow] "
                "StoreGuard writes one every 10 seconds while it runs "
                "(.\\make.ps1 serve, or bg-start) — start it, wait a little and try again."
            )
            if not args.watch:
                raise SystemExit(1)
        else:
            if args.watch:
                console.clear()
            _print_load(report)
        if not args.watch:
            return
        time.sleep(5)


def _cmd_gpu_check(args: argparse.Namespace) -> None:
    """Handler for ``storeguard gpu-check``: why is detection on the CPU?"""
    import shutil
    import subprocess

    import torch

    from .config import cuda_help

    console.print(f"torch {torch.__version__}, CUDA build: {torch.version.cuda or 'none (CPU-only)'}")
    smi = shutil.which("nvidia-smi")
    driver = None
    if smi:
        try:
            out = subprocess.run(
                [smi, "--query-gpu=name,driver_version", "--format=csv,noheader"],
                capture_output=True, text=True, timeout=15,
            )
            driver = out.stdout.strip() or out.stderr.strip()
        except Exception as exc:  # noqa: BLE001
            driver = f"nvidia-smi failed: {exc}"
    console.print(f"nvidia-smi: {driver or 'not found (no NVIDIA GPU / driver)'}")

    ok = False
    try:
        ok = torch.cuda.is_available()
        if ok:
            name = torch.cuda.get_device_name(0)
            cap = ".".join(map(str, torch.cuda.get_device_capability(0)))
            x = torch.ones(1024, 1024, device="cuda")
            float((x @ x).sum())  # a real kernel: catches "no kernel image" errors
            console.print(f"[green]CUDA OK: {name} (compute {cap})[/green]")
    except Exception as exc:  # noqa: BLE001
        ok = False
        console.print(f"[red]CUDA error: {exc}[/red]")
    if ok:
        return
    if not smi:
        console.print("[yellow]No NVIDIA GPU found — detection runs on the CPU.[/yellow]")
        raise SystemExit(1)
    console.print(f"[red]{cuda_help()}[/red]")
    raise SystemExit(1)


def _cmd_draw_zones(args: argparse.Namespace) -> None:
    """Handler for ``storeguard draw-zones``."""
    from .draw_zones import draw_zones

    console.print(
        f"[bold]storeguard draw-zones[/bold] — source '{args.source}', "
        f"output '{args.out}'"
    )
    draw_zones(args.source, args.out)
    console.print("[green]draw-zones finished.[/green]")


def _cmd_annotate(args: argparse.Namespace) -> None:
    """Handler for ``storeguard annotate``."""
    from .annotate import annotate

    classes = [c.strip() for c in args.classes.split(",") if c.strip()]
    if not classes:
        raise SystemExit("--classes must contain at least one class name")
    console.print(
        f"[bold]storeguard annotate[/bold] — videos '{args.videos}', "
        f"labels '{args.out}', classes: {', '.join(classes)}"
    )
    annotate(args.videos, args.out, classes)
    console.print(
        f"[green]Annotation session finished.[/green] Labels CSV: '{args.out}'"
    )


def _cmd_prelabel(args: argparse.Namespace) -> None:
    """Handler for ``storeguard prelabel``."""
    from .prelabel import pending_frames, prelabel, yolo_predictor

    todo = len(pending_frames(args.dir, redo=args.redo))
    if args.limit:
        todo = min(todo, args.limit)
    if not todo:
        console.print(f"Nothing to pre-label in '{args.dir}'.")
        return
    console.print(
        f"[bold]storeguard prelabel[/bold] — {todo} frame(s) in '{args.dir}' with "
        f"{args.model} at {args.imgsz}px{' + TTA' if not args.no_tta else ''} "
        "(the model is downloaded on first use)"
    )
    predict = yolo_predictor(
        args.model, imgsz=args.imgsz, conf=args.conf, device=args.device,
        augment=not args.no_tta,
    )

    def progress(done: int, total: int) -> None:
        console.print(f"  {done}/{total}", end="\r")

    summary = prelabel(
        args.dir, predict, model_name=args.model, imgsz=args.imgsz, conf=args.conf,
        batch=args.batch, redo=args.redo, limit=args.limit or None, on_progress=progress,
    )
    console.print()  # end the progress line
    console.print(
        f"[green]Pre-labelled {summary.done} frame(s)[/green]"
        + (f", [red]{summary.failed} failed[/red]" if summary.failed else "")
        + f". Mean disagreement with the live model: {summary.mean_disagreement:.2f}"
    )
    top = [(s, p) for s, p in summary.worst if s > 0][:10]
    if top:
        console.print("Review these first (live and teacher disagree most):")
        for score, path in top:
            console.print(f"  {score:.2f}  {path}", soft_wrap=True)


def _cmd_finetune(args: argparse.Namespace) -> None:
    """Handler for ``storeguard finetune``."""
    from .config import pick_device
    from .finetune import NotEnoughFrames, finetune

    device = pick_device(args.device)
    if device == "cpu":
        console.print(
            "[yellow]No GPU: training on CPU can take many hours. "
            "An NVIDIA GPU (--device cuda) is 20-50x faster.[/yellow]"
        )
    try:
        report = finetune(
            args.dir, base=args.base, epochs=args.epochs, imgsz=args.imgsz, conf=args.conf,
            device=device, min_train=args.min_train, min_val=args.min_val,
            log=lambda m: console.print(m, soft_wrap=True),
        )
    except NotEnoughFrames as exc:
        console.print(f"[red]Not enough reviewed frames yet:[/red] {exc}")
        raise SystemExit(1) from None

    o, n = report.old, report.new
    console.print(f"\nExam: {report.val_frames} frames the model never trained on")
    console.print(f"  {'':10}{'current':>10}{'new':>10}")
    for key, label in (("precision", "precision"), ("recall", "recall"), ("f1", "F1")):
        console.print(f"  {label:10}{o[key]:>10.3f}{n[key]:>10.3f}")
    console.print(f"  {'missed':10}{o['fn']:>10}{n['fn']:>10}   people not found")
    console.print(f"  {'false':10}{o['fp']:>10}{n['fp']:>10}   boxes on non-people")
    verdict = "[green]better — worth switching to (step 5)[/green]" if report.better else (
        "[yellow]not better — keep the current model; review more frames and try again[/yellow]"
    )
    console.print(f"\nCandidate: {report.candidate}\nVerdict: {verdict}", soft_wrap=True)


def _cmd_model(args: argparse.Namespace) -> None:
    """Handler for ``storeguard model status|use|rollback``."""
    from . import person_model as pm

    def show_active() -> None:
        cur = pm.active()
        if cur is None:
            console.print("Cameras use: [bold]the original model[/bold] for people and phones.")
        else:
            console.print(f"Cameras use for people: [bold]{cur['weights']}[/bold] (since {cur.get('at', '?')})", soft_wrap=True)
            console.print("Phones: the original model.")

    if args.action == "status":
        show_active()
        cands = pm.candidates()
        if not cands:
            console.print("No trained candidates yet — run `.\\make.ps1 finetune`.")
            return
        console.print("\nCandidates (newest first):")
        for c in cands:
            o, n = c["old"], c["new"]
            score = (f"F1 {o.get('f1', 0):.3f} -> {n.get('f1', 0):.3f} on {c['val_frames']} exam frames"
                     if c["has_report"] else "no exam report")
            mark = "[green]better[/green]" if c["better"] else "[yellow]not better[/yellow]"
            console.print(f"  {c['weights']}  {score}  {mark}", soft_wrap=True)
        return

    try:
        if args.action == "use":
            target = args.weights
            if target in (None, "latest"):
                better = [c for c in pm.candidates() if c["better"] or args.force]
                if not better:
                    raise pm.SwitchError("no candidate passed its exam yet")
                target = better[0]["weights"]
            pm.use(target, force=args.force)
            console.print(f"[green]Switched people detection to {target}.[/green]", soft_wrap=True)
        else:
            pm.rollback()
            console.print("[green]Switched back.[/green]")
    except pm.SwitchError as exc:
        console.print(f"[red]Not switched:[/red] {exc}")
        raise SystemExit(1) from None
    show_active()
    console.print("Restart the app to apply it: .\\make.ps1 bg-stop, then .\\make.ps1 bg-start")


def _cmd_make_dataset(args: argparse.Namespace) -> None:
    """Handler for ``storeguard make-dataset``."""
    from .actions.dataset import make_dataset
    from .config import DetectorCfg, load_config

    detector: DetectorCfg | None = None
    crop_size = args.crop_size
    if args.config:
        cfg = load_config(args.config)
        detector = cfg.detector
        if crop_size is None:
            crop_size = cfg.action.size
        console.print(
            f"[cyan]Using detector from '{args.config}' "
            f"(model={detector.model}, conf={detector.conf}, "
            f"imgsz={detector.imgsz}); crop_size={crop_size}[/cyan]"
        )
    if crop_size is None:
        crop_size = 112

    console.print(
        f"[bold]storeguard make-dataset[/bold] — videos '{args.videos}', "
        f"labels '{args.labels}', output '{args.out}'"
    )
    make_dataset(
        args.videos,
        args.labels,
        args.out,
        detector=detector,
        crop_size=crop_size,
    )
    console.print(f"[green]Dataset written to '{args.out}'.[/green]")


def _cmd_train(args: argparse.Namespace) -> None:
    """Handler for ``storeguard train``."""
    from .actions.train import train_action

    console.print(
        f"[bold]storeguard train[/bold] — data '{args.data}', "
        f"{args.epochs} epochs, batch {args.batch}, lr {args.lr}"
    )
    metrics = train_action(
        args.data,
        args.out,
        epochs=args.epochs,
        batch_size=args.batch,
        lr=args.lr,
    )
    console.print(f"[green]Training complete.[/green] Best checkpoint: '{args.out}'")
    scalars = {
        k: v
        for k, v in metrics.items()
        if isinstance(v, (int, float)) and not isinstance(v, bool)
    }
    if scalars:
        parts = [
            f"{k}={v:.4f}" if isinstance(v, float) else f"{k}={v}"
            for k, v in scalars.items()
        ]
        console.print("Final metrics: " + ", ".join(parts))


def _cmd_optimize(args: argparse.Namespace) -> None:
    """Handler for ``storeguard optimize``: build the TensorRT engine and compare speed."""
    from rich.table import Table

    from . import optimize as opt
    from .config import pick_device, resolve_model

    device = pick_device(args.device)
    weights = resolve_model(args.model, device)
    fmt = opt.target_format(device)
    if fmt is None:
        if device.startswith("cuda"):
            console.print(
                "[yellow]TensorRT is not installed — install it with "
                "[bold]uv sync --extra tensorrt[/bold] (needs an NVIDIA driver 570+).[/yellow]"
            )
        else:
            console.print(
                f"[yellow]Device is {device}: TensorRT needs an NVIDIA GPU, so detection "
                "keeps the PyTorch model (already the fastest option measured here).[/yellow]"
            )
        path = None
    else:
        path = opt.build(weights, device, args.imgsz, force=args.force)
        if path is None:
            raise SystemExit("TensorRT engine could not be built (see the message above).")
    if not args.benchmark:
        return
    table = Table(title=f"{weights} on {device}, imgsz {args.imgsz}")
    table.add_column("runtime")
    for b in (1, 4, 8):
        table.add_column(f"{b} camera{'s' if b > 1 else ''}, fps", justify="right")
    rows = [("PyTorch" + (" FP16" if device.startswith("cuda") else ""), weights)]
    if path is not None:
        rows.append(("TensorRT FP16", str(path)))
    for name, w in rows:
        table.add_row(name, *(f"{opt.benchmark(w, device, args.imgsz, batch=b):.0f}" for b in (1, 4, 8)))
    console.print(table)


def _cmd_cloud(args: argparse.Namespace) -> None:
    """Handler for ``storeguard cloud`` (the multi-tenant control plane)."""
    from .cloud.app import serve_cloud

    if args.dev:
        console.print(
            "[yellow]--dev: creating tables directly (skip Alembic). "
            "Do NOT use this in production.[/yellow]"
        )
    else:
        console.print(
            "[dim]Expecting an up-to-date schema — run "
            "[bold]alembic upgrade head[/bold] first (or pass --dev).[/dim]"
        )
    live_app = None
    if args.with_dashboard:
        from pathlib import Path as _Path

        from .dashboard.app import create_app

        # Same process, so alerts go straight to this server unless told otherwise.
        import os

        # Cameras started from the cabinet talk to this process directly
        # (port_factory below); an HTTP agent client to ourselves would be
        # checked before the server listens and always fail.
        if not os.environ.get("STOREGUARD_AGENT_SERVER"):
            args.agent_server = None
        kwargs = _dashboard_kwargs(args, f"http://{args.host}:{args.port}/live/")
        kwargs["data_dir"] = _Path(kwargs["data_dir"])
        # Same process: the dashboard talks to the cloud directly, as the
        # store of whoever is logged in — no agent key needed.
        holder: dict = {}
        kwargs["port_factory"] = lambda tenant_id: holder["make"](tenant_id)
        live_app = create_app(**kwargs)
    serve_cloud(
        host=args.host,
        port=args.port,
        create_tables=args.dev,
        live_app=live_app,
        on_app=(lambda app: holder.__setitem__("make", app.state.direct_port))
        if live_app is not None
        else None,
    )


def _cmd_agent(args: argparse.Namespace) -> None:
    """Handler for ``storeguard agent`` — enroll with the cloud and pull config."""
    import os

    from .cloud.agent_client import CloudClient

    token = args.key or os.environ.get("STOREGUARD_AGENT_KEY", "")
    if not token:
        raise SystemExit("provide --key or set STOREGUARD_AGENT_KEY")

    if args.run:
        # Full detection agent (heavy: torch + cv2 imported lazily here so the
        # plain enroll/config check stays instant).
        from .edge_agent import run_edge_agent

        run_edge_agent(
            args.server, token, device=args.device, process_every=args.process_every
        )
        return

    client = CloudClient(args.server, token)
    hb = client.heartbeat()
    console.print(f"[green]Enrolled[/green] — tenant {hb.get('tenant_id')}")
    cfg = client.get_config()
    cams = cfg.get("cameras", [])
    console.print(
        f"[bold]{cfg.get('tenant_name')}[/bold] — {len(cams)} camera(s):"
    )
    for c in cams:
        console.print(
            f"  • {c['name']}  [{c['label']}]  "
            f"every={c['process_every']} enabled={c['enabled']} "
            f"zones={len(c['zones'])}"
        )
    if args.test_event:
        ev = client.send_event(kind="test", message="hello from storeguard agent")
        console.print(f"[cyan]Sent test event[/cyan] id={ev['id']}")
        if args.clip:
            up = client.upload_clip(ev["id"], args.clip)
            console.print(f"[cyan]Uploaded clip[/cyan] has_clip={up['has_clip']}")


def _add_dashboard_args(p: argparse.ArgumentParser) -> None:
    """Detection options shared by ``dashboard`` and ``cloud --with-dashboard``."""
    p.add_argument(
        "--device",
        default="auto",
        help='detector device: "auto" | "cpu" | "cuda" | "mps" (default: auto)',
    )
    p.add_argument(
        "--config",
        default=None,
        help="optional app YAML; uses its detector settings",
    )
    p.add_argument(
        "--data",
        default="data",
        help="folder of local videos to list in the UI (default: data)",
    )
    p.add_argument(
        "--zones",
        default=None,
        help="zones YAML (shelf/checkout/exit) for paid / not-paid labels; "
        "overrides zones from --config",
    )
    p.add_argument(
        "--agent-server",
        default=None,
        help="cloud base URL to push confirmed exit-without-paying events + "
        "clips to, enabling Telegram / Lime CRM notifications (or set "
        "STOREGUARD_AGENT_SERVER); clips are always saved locally either way",
    )
    p.add_argument(
        "--agent-key",
        default=None,
        help="agent token for --agent-server, created in the cabinet's "
        "Devices page (or set STOREGUARD_AGENT_KEY)",
    )


def build_parser() -> argparse.ArgumentParser:
    """Create the top-level ``storeguard`` argument parser."""
    parser = argparse.ArgumentParser(
        prog="storeguard",
        description=(
            "Retail theft-detection video analytics: person tracking, zone "
            "control and action recognition on RTSP cameras or video files."
        ),
    )
    parser.add_argument(
        "--version", action="version", version=f"%(prog)s {__version__}"
    )
    sub = parser.add_subparsers(dest="command", required=True, metavar="command")

    p = sub.add_parser("run", help="run the detection pipeline from a YAML config")
    p.add_argument(
        "--config",
        required=True,
        help="path to the app config YAML (see configs/example.yaml)",
    )
    p.add_argument(
        "--show",
        action="store_true",
        help="open per-camera preview windows with overlays (local testing "
        "with video files)",
    )
    p.set_defaults(func=_cmd_run)

    p = sub.add_parser(
        "dashboard",
        help="open a local web UI to upload a video and watch person detection",
    )
    p.add_argument("--host", default="127.0.0.1", help="bind address (default: 127.0.0.1)")
    p.add_argument("--port", type=int, default=8765, help="HTTP port (default: 8765)")
    _add_dashboard_args(p)
    p.set_defaults(func=_cmd_dashboard)

    p = sub.add_parser(
        "camera-check",
        help="check that cameras connect and send frames (RTSP URL, file or webcam index)",
    )
    p.add_argument("--source", action="append", help="RTSP URL, video file or webcam index like 0 (repeatable)")
    p.add_argument("--config", help="check every camera listed in this app config YAML")
    p.add_argument("--seconds", type=float, default=3.0, help="how long to read frames per camera (default: 3)")
    p.add_argument("--timeout", type=float, default=10.0, help="give up connecting after N seconds (default: 10)")
    p.add_argument("--snapshot", metavar="DIR", help="save one frame per camera as DIR/<name>.jpg")
    p.add_argument("--no-show", action="store_true", help="only print the check result, don't open the live video window")
    p.set_defaults(func=_cmd_camera_check)

    p = sub.add_parser(
        "load",
        help="show how hard the PC is working for the cameras and what limits it",
    )
    p.add_argument("--data", default="data", help="StoreGuard's data folder (default: data)")
    p.add_argument("--watch", action="store_true", help="keep refreshing every 5 seconds")
    p.set_defaults(func=_cmd_load)

    p = sub.add_parser(
        "gpu-check",
        help="diagnose whether detection can use the NVIDIA GPU (CUDA)",
    )
    p.set_defaults(func=_cmd_gpu_check)

    p = sub.add_parser(
        "draw-zones",
        help="draw polygon zones over a frame and save them to a YAML file",
    )
    p.add_argument(
        "--source",
        required=True,
        help="RTSP URL, video file or image to grab the frame from",
    )
    p.add_argument(
        "--out",
        required=True,
        help="output zones YAML path, e.g. configs/zones/cam1.yaml",
    )
    p.set_defaults(func=_cmd_draw_zones)

    p = sub.add_parser(
        "prelabel",
        help="re-label saved hard frames with a large teacher model (step 2 of re-training)",
    )
    p.add_argument("--dir", default="data/hard_frames", help="hard frames folder")
    p.add_argument("--model", default="yolo26x.pt", help="teacher weights (yolo26l.pt is ~2x faster)")
    p.add_argument("--imgsz", type=int, default=1280, help="inference size (default 1280)")
    p.add_argument("--conf", type=float, default=0.25, help="min box score (default 0.25)")
    p.add_argument("--device", default="auto", help='"auto" | "cpu" | "cuda" | "mps"')
    p.add_argument("--batch", type=int, default=4, help="frames per batch")
    p.add_argument("--no-tta", action="store_true", help="skip test-time augmentation (~3x faster)")
    p.add_argument("--redo", action="store_true", help="also re-label frames done before")
    p.add_argument("--limit", type=int, default=0, help="stop after N frames (0 = all)")
    p.set_defaults(func=_cmd_prelabel)

    p = sub.add_parser(
        "finetune",
        help="train the person detector on frames approved in «Обучение ИИ» (step 4 of re-training)",
    )
    p.add_argument("--dir", default="data/hard_frames", help="hard frames folder")
    p.add_argument("--base", default="auto", help="model to start from; auto = the one the cameras use now")
    p.add_argument("--epochs", type=int, default=50, help="max passes over the data (stops early when it plateaus)")
    p.add_argument("--imgsz", type=int, default=640, help="training image size (same as the live detector)")
    p.add_argument("--conf", type=float, default=0.45, help="score threshold for the comparison (live default)")
    p.add_argument("--device", default="auto", help='"auto" | "cpu" | "cuda" | "mps"')
    p.add_argument("--min-train", type=int, default=100, help="refuse to train with fewer approved frames")
    p.add_argument("--min-val", type=int, default=20, help="refuse with fewer exam frames")
    p.set_defaults(func=_cmd_finetune)

    p = sub.add_parser(
        "model",
        help="which person model the cameras use: status / use a trained candidate / rollback (step 5)",
    )
    p.add_argument("action", choices=["status", "use", "rollback"])
    p.add_argument("weights", nargs="?", help='for "use": a candidate .pt, or "latest" (default)')
    p.add_argument("--force", action="store_true", help="switch even if its exam said it is not better")
    p.set_defaults(func=_cmd_model)

    p = sub.add_parser(
        "annotate",
        help="label action segments in raw videos with the keyboard",
    )
    p.add_argument(
        "--videos", required=True, help="directory with raw training videos"
    )
    p.add_argument(
        "--out",
        required=True,
        help="output labels CSV (appended to — safe to resume)",
    )
    p.add_argument(
        "--classes",
        default=DEFAULT_CLASSES,
        help="comma-separated class names mapped to digit keys 1..9 "
        f"(default: {DEFAULT_CLASSES})",
    )
    p.set_defaults(func=_cmd_annotate)

    p = sub.add_parser(
        "make-dataset",
        help="cut labeled segments into per-class clip folders",
    )
    p.add_argument(
        "--videos",
        required=True,
        help="directory with the raw videos referenced in the labels CSV",
    )
    p.add_argument(
        "--labels",
        required=True,
        help="labels CSV produced by 'storeguard annotate'",
    )
    p.add_argument(
        "--out", required=True, help="output dataset directory, e.g. data/clips"
    )
    p.add_argument(
        "--config",
        default=None,
        help="optional app YAML; uses its detector settings and action.size "
        "so training crops match the serve-time pipeline",
    )
    p.add_argument(
        "--crop-size",
        type=int,
        default=None,
        help="person-crop side length (default: action.size from --config, "
        "else 112); should match action.size at serve time",
    )
    p.set_defaults(func=_cmd_make_dataset)

    p = sub.add_parser(
        "train",
        help="fine-tune the action classifier on the cut clips",
    )
    p.add_argument(
        "--data",
        required=True,
        help="dataset directory produced by 'storeguard make-dataset'",
    )
    p.add_argument(
        "--out",
        required=True,
        help="output checkpoint path, e.g. models/action.pt",
    )
    p.add_argument("--epochs", type=int, default=30, help="training epochs (default: 30)")
    p.add_argument("--batch", type=int, default=8, help="batch size (default: 8)")
    p.add_argument("--lr", type=float, default=1e-4, help="learning rate (default: 1e-4)")
    p.set_defaults(func=_cmd_train)

    p = sub.add_parser(
        "optimize",
        help="build a TensorRT engine for the detector (NVIDIA) and compare speed",
    )
    p.add_argument("--model", default="auto", help="YOLO weights (default: auto for the device)")
    p.add_argument("--device", default="auto", help='"auto" | "cuda" | "mps" | "cpu"')
    p.add_argument("--imgsz", type=int, default=640, help="input size (default: 640)")
    p.add_argument("--force", action="store_true", help="rebuild even if cached")
    p.add_argument("--benchmark", action="store_true", help="print frames/second before and after")
    p.set_defaults(func=_cmd_optimize)

    p = sub.add_parser(
        "cloud",
        help="run the multi-tenant cloud control plane (accounts, tenants, API)",
    )
    p.add_argument("--host", default="127.0.0.1", help="bind address (default: 127.0.0.1)")
    p.add_argument("--port", type=int, default=8000, help="HTTP port (default: 8000)")
    p.add_argument(
        "--dev",
        action="store_true",
        help="create tables directly instead of via Alembic (local dev only)",
    )
    p.add_argument(
        "--with-dashboard",
        action="store_true",
        help="also run live detection in this same process, served at /live/ "
        "on the same port (for a store PC that runs everything locally)",
    )
    _add_dashboard_args(p)
    p.set_defaults(func=_cmd_cloud)

    p = sub.add_parser(
        "agent",
        help="edge agent: enroll with the cloud, pull camera config, push events",
    )
    p.add_argument(
        "--server", required=True, help="cloud base URL, e.g. https://cloud.example.com"
    )
    p.add_argument(
        "--key", default=None, help="agent token (or set STOREGUARD_AGENT_KEY)"
    )
    p.add_argument(
        "--test-event",
        action="store_true",
        help="send a test event to verify the push path",
    )
    p.add_argument(
        "--clip", default=None, help="path to a clip to attach to the test event"
    )
    p.add_argument(
        "--run",
        action="store_true",
        help="run detection: pull camera config and push events + clips to the cloud",
    )
    p.add_argument(
        "--device",
        default="auto",
        help='detector device for --run: "auto" | "cpu" | "cuda" | "mps"',
    )
    p.add_argument(
        "--process-every",
        type=int,
        default=None,
        help="for --run: process every Nth frame (default: from cloud config)",
    )
    p.set_defaults(func=_cmd_agent)

    return parser


def main(argv: list[str] | None = None) -> None:
    """Entry point for the ``storeguard`` console script."""
    args = build_parser().parse_args(argv)
    try:
        args.func(args)
    except KeyboardInterrupt:
        console.print("\n[yellow]Interrupted.[/yellow]")


if __name__ == "__main__":
    main()
