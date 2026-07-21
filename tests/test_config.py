"""Tests for :mod:`storeguard.config`.

Covers YAML loading via ``load_config``, merging of inline zones with a
``zones_file`` (resolved relative to the config file's directory), model
defaults and explicit overrides including action thresholds.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

from storeguard.config import AppCfg, load_config


def _write(path: Path, text: str) -> Path:
    """Write dedented YAML text to *path* (creating parent dirs) and return it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(text), encoding="utf-8")
    return path


def test_load_config_merges_inline_and_file_zones(tmp_path: Path) -> None:
    """Inline zones come first, zones_file zones are concatenated after them."""
    _write(
        tmp_path / "zones" / "cam1.yaml",
        """\
        zones:
          - name: exit
            points: [[0.8, 0.8], [1.0, 0.8], [1.0, 1.0], [0.8, 1.0]]
          - name: checkout
            points: [[0.5, 0.5], [0.8, 0.5], [0.8, 0.8], [0.5, 0.8]]
        """,
    )
    cfg_path = _write(
        tmp_path / "config.yaml",
        """\
        cameras:
          - name: cam1
            source: videos/test.mp4
            scenarios: [exit_no_pay]
            zones:
              - name: shelf-1
                points: [[0.1, 0.1], [0.4, 0.1], [0.4, 0.4], [0.1, 0.4]]
            zones_file: zones/cam1.yaml
        """,
    )

    cfg = load_config(str(cfg_path))

    assert isinstance(cfg, AppCfg)
    assert len(cfg.cameras) == 1
    cam = cfg.cameras[0]
    assert cam.name == "cam1"
    assert cam.source == "videos/test.mp4"
    assert cam.scenarios == ["exit_no_pay"]
    # Inline zone first, then the two file zones, order preserved.
    assert [z.name for z in cam.zones] == ["shelf-1", "exit", "checkout"]
    assert cam.zones[0].points == [(0.1, 0.1), (0.4, 0.1), (0.4, 0.4), (0.1, 0.4)]
    assert cam.zones[1].points[0] == (0.8, 0.8)
    assert cam.zones[2].points[-1] == (0.5, 0.8)


def test_load_config_defaults(tmp_path: Path) -> None:
    """A minimal config gets every documented default."""
    cfg_path = _write(
        tmp_path / "config.yaml",
        """\
        cameras:
          - name: hall-1
            source: "rtsp://admin:pw@192.168.1.64:554/Streaming/Channels/101"
        """,
    )

    cfg = load_config(cfg_path)  # Path is accepted too

    cam = cfg.cameras[0]
    assert cam.scenarios == []
    assert cam.zones == []
    assert cam.zones_file is None

    assert cfg.detector.model == "yolo11n.pt"
    assert cfg.detector.conf == 0.35
    assert cfg.detector.imgsz == 640
    assert cfg.detector.device == "auto"

    assert cfg.action.weights == "models/action.pt"
    assert cfg.action.clip_len == 16
    assert cfg.action.stride == 2
    assert cfg.action.size == 112
    assert cfg.action.classes == ["normal", "pocket", "take_cash"]
    assert cfg.action.thresholds == {"pocket": 0.75, "take_cash": 0.80}

    assert cfg.telegram.enabled is False
    assert cfg.telegram.bot_token == ""
    assert cfg.telegram.chat_id == ""

    assert cfg.notify.enabled is False
    assert cfg.notify.url == ""
    assert cfg.notify.kinds == ["exit_no_pay"]

    assert cfg.events_dir == "events"
    assert cfg.process_every == 1


def test_load_config_overrides(tmp_path: Path) -> None:
    """Explicit YAML values (including thresholds) override the defaults."""
    cfg_path = _write(
        tmp_path / "config.yaml",
        """\
        detector:
          model: yolo11s.pt
          conf: 0.5
          imgsz: 960
          device: cpu
        action:
          weights: models/custom.pt
          clip_len: 8
          stride: 1
          size: 96
          classes: [normal, pocket]
          thresholds: {pocket: 0.6}
        telegram:
          enabled: true
          bot_token: "123:abc"
          chat_id: "42"
        notify:
          enabled: true
          url: "https://hooks.example/alert"
          kinds: [exit_no_pay, pocket]
          headers: {Authorization: "Bearer x"}
          timeout_sec: 5
        events_dir: out_events
        process_every: 3
        cameras:
          - name: cashier-1
            source: "rtsp://example/2"
            scenarios: [cashier]
        """,
    )

    cfg = load_config(str(cfg_path))

    assert cfg.detector.model == "yolo11s.pt"
    assert cfg.detector.conf == 0.5
    assert cfg.detector.imgsz == 960
    assert cfg.detector.device == "cpu"

    assert cfg.action.weights == "models/custom.pt"
    assert cfg.action.clip_len == 8
    assert cfg.action.stride == 1
    assert cfg.action.size == 96
    assert cfg.action.classes == ["normal", "pocket"]
    assert cfg.action.thresholds == {"pocket": 0.6}

    assert cfg.telegram.enabled is True
    assert cfg.telegram.bot_token == "123:abc"
    assert cfg.telegram.chat_id == "42"

    assert cfg.notify.enabled is True
    assert cfg.notify.url == "https://hooks.example/alert"
    assert cfg.notify.kinds == ["exit_no_pay", "pocket"]
    assert cfg.notify.headers == {"Authorization": "Bearer x"}
    assert cfg.notify.timeout_sec == 5.0

    assert cfg.events_dir == "out_events"
    assert cfg.process_every == 3

    cam = cfg.cameras[0]
    assert cam.name == "cashier-1"
    assert cam.scenarios == ["cashier"]
