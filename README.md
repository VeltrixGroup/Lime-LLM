# StoreGuard

Real-time video analytics for a supermarket: it watches your Hikvision
cameras and sends a Telegram alert (with a video clip) when it sees
theft-related actions.

## What it detects

| # | Detection | How it works | Needs training? |
|---|-----------|--------------|-----------------|
| 1 | **People** — every person in the frame is detected and gets a track id | YOLO11 + ByteTrack | No |
| 2 | **`pocket`** — a shopper takes a product from the shelf and hides it in a pocket / clothes / bag | Action classifier (3D CNN) on person crops | **Yes** |
| 3 | **`exit_no_pay`** — a person spends time at the shelves and then goes to the exit **without** passing the checkout | Pure zone logic (shelf / checkout / exit polygons) | No |
| 4 | **`take_cash`** — an employee takes money out of the register drawer and carries it away | Action classifier (3D CNN) on person crops | **Yes** |

### Two-stage architecture (short)

- **Stage 1 — works out of the box.** YOLO11 detects people, ByteTrack gives
  each person a stable id, and polygon zones that you draw on the frame
  ("shelf…", "checkout", "exit", "register…") drive the `exit_no_pay` logic.
  No training needed.
- **Stage 2 — needs training on YOUR videos.** A 3D CNN
  (torchvision `r3d_18`, pretrained on Kinetics-400) looks at short clips of
  each tracked person and classifies the action: `normal`, `pocket`,
  `take_cash`. You train it once at home on videos from the store cameras,
  then copy the weights file (`models/action.pt`) to the store PC.

Every alert produces three things:

1. A **Telegram message** to your chat (if enabled).
2. A saved **mp4 clip** of the moment: `events/clips/<camera>_<kind>_<time>.mp4`.
3. A line in the **event log**: `events/events.jsonl`.

## How the whole workflow looks

Because the developer works from another country and only has the
Hik-Connect app, direct camera access from home is **not possible**
(Hik-Connect does not give RTSP over the internet). So the plan is:

1. **Get video files from the store** (see next section) → train the action
   model at home.
2. **Test everything at home** on video files with `--show`.
3. **Deploy the project + trained weights to the store PC**, which is on the
   same LAN/WiFi as the cameras — there RTSP works directly.

## Step 1 — Getting training videos remotely

You cannot pull RTSP from abroad through Hik-Connect. Instead:

- **(a) Ask the store to export recordings.** Recordings live on the
  NVR / camera SD-card. Store staff can export them with **iVMS-4200**
  (Remote Playback → select time range → Download) or download clips from
  the **Hik-Connect album** on a phone. Then they send the files to you via
  cloud storage (Google Drive, Yandex Disk) or Telegram.
- **(b) VERY IMPORTANT — ask staff to act out staged examples on camera.**
  Real theft is rare in recordings, so staged examples are how you get
  training data:
  - **150–300 short examples of `pocket`**: different people, different
    clothes (jackets, hoodies, bags), different shelf locations. Each
    example is just a few seconds: take an item, hide it in a
    pocket/jacket/bag.
  - **150–300 short examples of `take_cash`** at the register: open drawer,
    take cash, carry it away.
  - **Lots of `normal` footage**: regular shopping, regular cashier work,
    from the same cameras.

  The examples must be recorded by the **same cameras** that will run in
  production. Same camera angle = much better accuracy.

## Step 2 — Training at home

Install [uv](https://docs.astral.sh/uv/) and set up the project:

```bash
uv sync
```

Put all training videos in one folder, e.g. `~/store_videos/`.

### 2.1 Label segments (`annotate`)

```bash
uv run storeguard annotate --videos ~/store_videos --out labels.csv
```

A video window opens. Keys:

- `space` — pause / play
- `a` / `d` — jump −1s / +1s, `w` / `s` — jump +10s / −10s
- `m` — mark the **start** of a segment at the current time
- `1` / `2` / `3` — close the segment at the current time and label it
  (`1` = normal, `2` = pocket, `3` = take_cash by default)
- `n` — next video, `q` — quit

Labels are appended to `labels.csv`, so you can stop and continue later.

### 2.2 Cut clips (`make-dataset`)

```bash
uv run storeguard make-dataset --videos ~/store_videos --labels labels.csv --out data/clips
```

This cuts every labeled segment into a small mp4 under
`data/clips/<class>/` and prints a per-class count table.

### 2.3 Train (`train`)

```bash
uv run storeguard train --data data/clips --out models/action.pt --epochs 30
```

The best checkpoint (by validation balanced accuracy) is saved to
`models/action.pt`. Training works on CPU and on Apple Silicon (MPS), but is
much faster on an NVIDIA GPU. On CPU expect it to take a while — run it
overnight if needed.

## Step 3 — Testing on video files (before deploying)

You can run the full pipeline on ordinary video files — the `source` of a
camera can be a file path instead of an RTSP URL.

First draw zones on a frame from the test video:

```bash
uv run storeguard draw-zones --source ~/store_videos/hall_test.mp4 --out configs/zones/hall-1.yaml
```

- **left click** — add a polygon vertex
- `n` — finish the polygon and type its name in the terminal
- `u` — undo last vertex, `r` — reset current polygon
- `s` — save all zones to the YAML file and exit, `q` — quit without saving

**Zone names matter** — scenarios find zones by name prefix:

| Name starts with | Meaning | Used by |
|------------------|---------|---------|
| `shelf` (e.g. `shelf-1`, `shelf-dairy`) | shelf area | `exit_no_pay`, `pocket` |
| `checkout` | cashier / payment area | `exit_no_pay` |
| `exit` | exit door area | `exit_no_pay` |
| `register` (e.g. `register-1`) | cash register area | `cashier` |

Then make a test config, e.g. `configs/test.yaml`:

```yaml
cameras:
  - name: hall-test
    source: "/Users/you/store_videos/hall_test.mp4"
    scenarios: [pocket, exit_no_pay]
    zones_file: zones/hall-1.yaml
```

and run with a preview window:

```bash
uv run storeguard run --config configs/test.yaml --show
```

You will see boxes with track ids, your zone polygons, and a red banner when
an event fires. Press `q` in the window (or Ctrl+C in the terminal) to stop.
Check that `events/events.jsonl` and `events/clips/` appear.

## Step 4 — Deploying to the store PC

The store PC is on the same LAN/WiFi as the cameras, so RTSP works there.

### 4.1 Install Python / uv

**Windows** (most likely case):

1. Open PowerShell and install uv:
   ```powershell
   powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
   ```
   uv downloads the right Python (3.11+) by itself.
2. Copy the whole project folder to the PC, e.g. `C:\storeguard\`
   — **including your trained `models\action.pt`**.
3. In the project folder run:
   ```powershell
   cd C:\storeguard
   uv sync
   ```

**Linux**: same idea — `curl -LsSf https://astral.sh/uv/install.sh | sh`,
copy the project to e.g. `/opt/storeguard`, then `uv sync`.

> Note: on the first `run`, ultralytics downloads `yolo11n.pt`
> automatically (needs internet once). If the store PC is offline, copy your
> local `yolo11n.pt` into the project folder too.

### 4.2 Camera RTSP URLs

Ask the store admin for each camera's **local IP address** and the
**camera / NVR password**. The Hikvision RTSP URL format is:

```
rtsp://user:password@CAMERA_IP:554/Streaming/Channels/101
```

- `101` = camera 1, main stream (full quality)
- `102` = camera 1, substream (lower quality — lighter on CPU)
- On an NVR, channels go `101`, `201`, `301`, … (`201` = camera 2 main
  stream, etc.)

Test a URL quickly with VLC (Media → Open Network Stream) on the store PC.

### 4.3 Draw zones for each real camera

```powershell
uv run storeguard draw-zones --source "rtsp://admin:PASSWORD@192.168.1.64:554/Streaming/Channels/101" --out configs/zones/hall-1.yaml
```

Repeat for every camera. Remember the name prefixes (`shelf…`, `checkout`,
`exit`, `register…`).

### 4.4 Fill the config

Create `configs/storeguard.yaml` (a full realistic example — also see
`configs/example.yaml`):

```yaml
detector:
  model: yolo11n.pt
  conf: 0.35
  imgsz: 640
  device: auto          # auto -> cuda if available, else mps, else cpu
action:
  weights: models/action.pt
  clip_len: 16
  stride: 2
  size: 112
  classes: [normal, pocket, take_cash]
  thresholds: {pocket: 0.75, take_cash: 0.80}
telegram:
  enabled: true
  bot_token: "123456789:AAF...your-token..."
  chat_id: "987654321"
events_dir: events
process_every: 2        # process every 2nd frame — good CPU relief
cameras:
  - name: hall-1
    source: "rtsp://admin:PASSWORD@192.168.1.64:554/Streaming/Channels/101"
    scenarios: [pocket, exit_no_pay]
    zones_file: zones/hall-1.yaml
  - name: cashier-1
    source: "rtsp://admin:PASSWORD@192.168.1.65:554/Streaming/Channels/101"
    scenarios: [cashier]
    zones_file: zones/cashier-1.yaml
```

Tips:

- `zones_file` paths are resolved **relative to the config file folder**.
- If false alarms are too frequent, raise the thresholds
  (e.g. `pocket: 0.85`); if it misses events, lower them.

### 4.5 Test, then run headless

Test with a window first (on the store PC, with a monitor connected):

```powershell
uv run storeguard run --config configs/storeguard.yaml --show
```

If alerts arrive in Telegram — run it headless (no `--show`):

```powershell
uv run storeguard run --config configs/storeguard.yaml
```

### 4.6 Autostart on Windows (Task Scheduler)

1. Create `C:\storeguard\run_storeguard.bat`:
   ```bat
   @echo off
   cd /d C:\storeguard
   uv run storeguard run --config configs\storeguard.yaml >> storeguard.log 2>&1
   ```
2. Press `Win + R`, type `taskschd.msc`, press Enter.
3. Right panel → **Create Task…**
4. **General** tab: Name `StoreGuard`; select **"Run whether user is logged
   on or not"**.
5. **Triggers** tab: **New…** → Begin the task: **"At startup"** → OK.
6. **Actions** tab: **New…** → Program/script:
   `C:\storeguard\run_storeguard.bat` → OK.
7. **Settings** tab: check **"If the task fails, restart every: 1 minute"**,
   attempts: 3 (or more). Uncheck "Stop the task if it runs longer than…".
8. OK → enter the Windows user password. Reboot the PC to verify it starts.

### 4.7 Autostart on Linux (systemd)

Create `/etc/systemd/system/storeguard.service`:

```ini
[Unit]
Description=StoreGuard video analytics
After=network-online.target
Wants=network-online.target

[Service]
WorkingDirectory=/opt/storeguard
ExecStart=/usr/local/bin/uv run storeguard run --config configs/storeguard.yaml
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

Then:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now storeguard.service
journalctl -u storeguard -f   # watch the logs
```

## Telegram alerts setup

1. In Telegram, open **@BotFather** → send `/newbot` → choose a name and a
   username → BotFather gives you the **bot token**
   (looks like `123456789:AAF...`).
2. Open a chat with your new bot and press **Start** (send any message).
3. To find your **chat_id**, open in a browser:
   `https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates`
   and look for `"chat":{"id":987654321,...}` in the response.
4. Put the token and chat_id into the config and set `enabled: true`:
   ```yaml
   telegram:
     enabled: true
     bot_token: "123456789:AAF..."
     chat_id: "987654321"
   ```
5. For a group chat (e.g. store security group): add the bot to the group,
   send a message there, and use the group's id from `getUpdates` (it is
   negative, like `-100123456789`).

Each alert is a text message plus the saved mp4 clip of the event.

## Hardware guidance

- **1–2 cameras**: a modern desktop CPU is enough. Use `yolo11n.pt` and
  `process_every: 2` or `3` in the config (process every 2nd–3rd frame).
  Using the camera substream (`…/Channels/102`) also reduces load.
- **4+ cameras**, or if you need faster reaction: use an NVIDIA GPU
  (e.g. **RTX 3050 or better**) and keep `device: auto` — CUDA will be
  picked up automatically.
- RAM: 8 GB minimum, 16 GB comfortable.

## Legal note

Video monitoring of staff and customers must follow local personal-data
law. In Russia the standard practice is: put up **visible signs** that video
surveillance is in place, and **notify employees in writing** (an order /
addendum acknowledging the monitoring). Discuss the details with the store's
management before going live.

## Command reference

```text
storeguard run          --config configs/storeguard.yaml [--show]
storeguard draw-zones   --source <rtsp|video|image> --out configs/zones/cam1.yaml
storeguard annotate     --videos <dir> --out labels.csv [--classes normal,pocket,take_cash]
storeguard make-dataset --videos <dir> --labels labels.csv --out data/clips
storeguard train        --data data/clips --out models/action.pt [--epochs 30] [--batch 8] [--lr 1e-4]
```

---

## Кратко по-русски

**Что делает система:** находит и отслеживает людей на камерах;
детектирует три события: `pocket` — покупатель прячет товар в
карман/одежду/сумку; `exit_no_pay` — человек побыл у полок и пошёл к выходу
мимо кассы; `take_cash` — сотрудник достаёт деньги из кассы и уносит их.
При событии — сообщение в Telegram + сохранённый mp4-ролик + запись в
журнал `events/events.jsonl`.

**Порядок работы:**

1. **Собрать видео для обучения.** Напрямую по RTSP из другой страны через
   Hik-Connect подключиться нельзя. Попросите магазин выгрузить записи с
   регистратора (iVMS-4200 или альбом Hik-Connect) и прислать их через
   облако/Telegram. Обязательно попросите сотрудников **разыграть примеры
   перед камерами**: 150–300 коротких примеров `pocket` (разные люди,
   одежда, полки) и `take_cash` на кассе, плюс много обычного «нормального»
   видео с тех же камер. Тот же ракурс камеры = заметно выше точность.
2. **Обучение дома:** `storeguard annotate` (разметка клавишами) →
   `storeguard make-dataset` (нарезка клипов) → `storeguard train`
   (обучение, результат — `models/action.pt`).
3. **Проверка дома** на видеофайлах: в конфиге вместо RTSP указать путь к
   файлу и запустить `storeguard run --config configs/test.yaml --show`.
4. **Развёртывание в магазине:** на ПК магазина установить uv (Python
   поставится сам), скопировать проект вместе с `models/action.pt`,
   выполнить `uv sync`. Узнать у администратора IP и пароли камер; формат
   RTSP для Hikvision:
   `rtsp://user:password@IP:554/Streaming/Channels/101` (`102` — субпоток,
   `201` — вторая камера регистратора). Нарисовать зоны каждой камеры
   (`storeguard draw-zones`; имена зон: `shelf…` — полки, `checkout` —
   касса, `exit` — выход, `register…` — денежный ящик), заполнить
   `configs/storeguard.yaml`, проверить с `--show`, затем запустить без
   окна и настроить автозапуск (Планировщик заданий Windows или systemd).
5. **Telegram:** создать бота через @BotFather, получить токен, узнать свой
   chat_id через `getUpdates`, вписать в конфиг, `enabled: true`.
6. **Железо:** 1–2 камеры — достаточно обычного CPU (`process_every: 2-3`,
   модель `yolo11n`); 4+ камер или быстрее реакция — нужна NVIDIA GPU
   (например, RTX 3050+).
7. **Юридически:** повесить таблички о видеонаблюдении и письменно
   уведомить сотрудников — стандартная практика по закону о персональных
   данных.
