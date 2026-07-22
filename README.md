# Luckfox Ultra · GC9A01 · Python

Drive a **240×240 round GC9A01 SPI display** from a **Luckfox Pico Ultra W**
(Rockchip RV1106, Linux) in plain **CPython 3** — plus a growing toolkit of
things to *show* on it: demo graphics, a virtual speedometer, an on-board
video player, an RTSP/ONVIF camera viewer, and a set of PC→board streamers
that mirror your mouse, play any video, or stream **YouTube** to the panel
(with **audio** out of the board's built-in codec).

The display driver is a Linux/CPython port of Russ Hughes'
[`gc9a01py`](https://github.com/russhughes/gc9a01py) MicroPython module: SPI is
driven with **spidev** and GPIO with **python-periphery** instead of
MicroPython's `machine` module.

---

## ✨ What's inside

| Area | Runs on | What it does |
|---|---|---|
| **Driver** `lib/gc9a01py_linux.py` | Luckfox | GC9A01 driver: text, shapes, bitmaps, TrueType, scrolling, fast fills |
| **Demos** `examples/` | Luckfox | Hello text, fonts, lines, scrolling, flying-toaster sprites |
| **Speedometer** `examples/` | Luckfox | Animated round gauge (live-drawn and sprite/flipbook versions) |
| **Benchmark** `examples/` | Luckfox | Measures real SPI fps / throughput |
| **Video player** `video_player/` | Luckfox | Plays a video file on the panel (ffmpeg) with audio |
| **IP camera** `ip_cam_viewer/` | Luckfox | Standalone RTSP/ONVIF camera viewer |
| **Mouse mirror** `mouse_mirror/` | PC → Luckfox | Streams the 240×240 area around your cursor |
| **Video streamer** `video_streamer/` | PC → Luckfox | PC decodes any video/URL/camera; board shows video + plays audio |
| **YouTube streamer** `youtube_streamer/` | PC → Luckfox | Streams a YouTube URL (via yt-dlp) to the panel + audio |

---

## 🔌 Hardware & wiring

- **Board:** Luckfox Pico Ultra **W** (the *W* has the audio codec; audio
  features need it).
- **Display:** GC9A01 240×240 round LCD, 4-wire SPI.

Connect the display's SPI lines (**SCLK / MOSI / CS**) to the board's **SPI0**
bus (`/dev/spidev0.0`), and the control pins to these GPIOs (the defaults in
every script — edit the constants at the top of a script to change them):

| Display pin | Luckfox GPIO |
|---|---|
| DC  | **GPIO 54** |
| RST | **GPIO 42** |
| BL (backlight) | **GPIO 71** |
| SCLK / MOSI / CS | SPI0 (`/dev/spidev0.0`) |
| VCC / GND | 3V3 / GND |

Enable SPI0 with `luckfox-config` if it isn't already, and confirm the device
node exists: `ls /sys/bus/spi/devices/`.

> **GPIO numbering:** `pin = bank*32 + (group*8 + X)` — e.g. GPIO1_B1 = 41.

---

## 🚀 Quick start

### On the Luckfox (board side)

The Luckfox image already ships with `spidev` and `python-periphery`, so
there's nothing to install for the display itself — just run a script:

```bash
# Sanity check: a demo
python3 examples/luckfox_hello.py
```

> If you prefer a virtualenv, create it with system packages so it can see the
> pre-installed `spidev`/`periphery`: `python3 -m venv venv --system-site-packages`
>
> Keep each app folder next to `lib/` and `fonts/` — the scripts import the
> driver via `../lib`.

### On the PC (for the streaming apps)

```bash
# Mouse mirror
cd mouse_mirror/windows_app && pip install -r requirements.txt   # mss numpy requests

# Video / YouTube streamers
pip install requests yt-dlp          # + ffmpeg on PATH
```

---

## 🖥️ The apps

### Display demos & speedometer (on the board)

```bash
python3 examples/luckfox_hello.py
python3 examples/luckfox_speedometer_sprites.py
python3 examples/luckfox_benchmark.py          # how fast is your panel?
```

### Video player (on the board)

Decodes a file with ffmpeg and streams frames to the panel; plays audio on the
codec. Heavy sources are CPU-bound (single-core A7) — use `--fast-decode`,
`--fps-auto`, or a smaller source.

```bash
python3 video_player/luckfox_video.py video_player/song.mp4 --volume 10 --fast-decode --fps-auto
```

### IP camera viewer (on the board)

Standalone RTSP/ONVIF viewer. **Use the camera's low-res substream** — the
board can't decode a full 1080p main stream in real time.

```bash
python3 ip_cam_viewer/luckfox_rtsp.py "rtsp://user:pass@CAM_IP:554/Streaming/Channels/102" --fps 15
```

### Stream from your PC → the panel

The PC does the heavy lifting (screen grab or video decode) and sends the board
finished **240×240 RGB565 frames over HTTP**; audio (where supported) streams
over a second TCP connection to the board's codec. This is what makes even
1080p or YouTube play smoothly — the Luckfox never decodes.

**1) Start the receiver on the board** (note its IP with `ip addr`):

```bash
python3 video_streamer/luckfox_receiver.py --port 8000 --audio-port 8001 --volume 15
```

**2) Run a sender on the PC** (replace `172.32.0.93` with your board IP):

```bash
# Mirror the area around the mouse cursor (video only)
cd mouse_mirror/windows_app
python windows_sender.py 172.32.0.93 8000 --fps 20

# Stream any video / file / camera, with audio
cd video_streamer/windows_app
python video_sender.py 172.32.0.93 8000 video.mp4 --fps 20 --loop

# Stream a YouTube URL, with audio
cd youtube_streamer
python youtube_video_sender.py 172.32.0.93 8000 "https://www.youtube.com/watch?v=XXXX" --lowest --fps 15
```

---

## 🧠 How it works (and why)

The RV1106 is a **single-core Cortex-A7** — plenty for graphics, but it can't
software-decode large video in real time. Two ideas run through the toolkit:

1. **The panel is fed raw RGB565 frames** (240×240 = 115,200 bytes). That's the
   exact format the GC9A01 wants, so displaying a frame is just an SPI write —
   no decoding on the board.
2. **Push decoding off the board when you can.** For local playback we lean on
   ffmpeg tricks (`-skip_loop_filter`, small sources); for streaming we let a
   powerful PC decode and send the board only finished frames + audio.

The driver also includes a **datasheet-informed keep-awake heartbeat**
(re-sends the panel config in ~10 ms using the GC9A01's documented 5 ms
Sleep-Out timing) so long sessions don't blank the screen.

📄 **Full command reference:** [`COMMANDS.txt`](COMMANDS.txt) — every script,
every flag, tagged `[LUCKFOX]` / `[PC]`.

---

## 🧰 Requirements

- **Board:** Luckfox Pico Ultra W (Linux) — `python3`, `spidev` and
  `python-periphery` come pre-installed; `ffmpeg` + `aplay`/`amixer` for the
  video/audio features.
- **PC:** Python 3 with `requests` (+ `mss numpy` for the mouse mirror,
  `yt-dlp` for YouTube), and `ffmpeg` on `PATH`.

---

## 🙏 Credits

- GC9A01 driver ported from **Russ Hughes'**
  [`gc9a01py`](https://github.com/russhughes/gc9a01py), itself derived from
  **Ivan Belokobylskiy's** `st7789py_mpy`.
- Flying-toaster sprites from Adafruit's *CircuitPython Flying Toasters*.
- Fonts: classic VGA ROM fonts and Google **Noto** / **Chango** (OFL).

## 📜 License

MIT (see [`LICENSE`](LICENSE)). Bundled fonts keep their own licenses.
