"""Command-line interface for storeguard.

Subcommands:

* ``run`` — run the detection pipeline from a YAML config.
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

    detector = DetectorCfg(device=args.device)
    zones = []
    if args.config:
        cfg = load_config(args.config)
        detector = cfg.detector
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
    }


def _cmd_dashboard(args: argparse.Namespace) -> None:
    """Handler for ``storeguard dashboard`` (standalone, its own port)."""
    from .dashboard.app import serve

    kwargs = _dashboard_kwargs(args, f"http://{args.host}:{args.port}")
    serve(host=args.host, port=args.port, **kwargs)


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
