"""Command-line interface for storeguard.

Subcommands:

* ``run`` — run the detection pipeline from a YAML config.
* ``draw-zones`` — interactively draw polygon zones over a camera frame.
* ``annotate`` — keyboard labeler producing a labels CSV from raw videos.
* ``make-dataset`` — cut labeled segments into per-class training clips.
* ``train`` — fine-tune the action classifier on the cut clips.

Heavy dependencies (torch, ultralytics, cv2 pipelines) are imported inside
each subcommand handler so ``storeguard --help`` stays instant.
"""

from __future__ import annotations

import argparse

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
