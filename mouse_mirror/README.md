# Frame streaming to the Luckfox GC9A01

Stream pixels from your PC to the Luckfox GC9A01 display over HTTP. The Luckfox
runs one small **receiver** that just blits raw frames; the PC runs one of two
**senders**:

```
Windows PC                                   Luckfox Pico Ultra W
-----------                                  --------------------
windows_sender.py  --\                        /--> luckfox_receiver.py --> GC9A01
(mouse-area mirror)   >-- HTTP POST /frame --<     (blit raw RGB565,
video_sender.py    --/                        \     no decoding)
(any video / VLC)
```

Frames are raw **240×240 big-endian RGB565** (115,200 bytes each) — the exact
format the panel wants, so the Luckfox does no decoding, just `blit_buffer`.
All the heavy lifting (screen grab or video decode + scale) happens on the PC.

---

## 1. On the Luckfox (the receiver — always run this first)

Copy `lib/gc9a01py_linux.py` and `mouse_mirror/luckfox_receiver.py` to the
board, then:

```bash
python3 luckfox_receiver.py --port 8000
ip addr        # note the board's IP, e.g. 192.168.x.x
```

`--rotation 0..7` if the image lands sideways.

## PC setup (once)

```bash
pip install -r requirements.txt      # mss, numpy, requests
```

`video_sender.py` also needs **ffmpeg** on PATH.

---

## 2a. Sender: mirror the area around the mouse

```bash
python windows_sender.py <luckfox-ip> 8000
```

Options:
- `--fps 20` — target frame rate.
- `--zoom 2` — capture a larger area (240×zoom) around the cursor and shrink
  it to 240, so you see more context (Z=1 is pixel-exact).

The cursor stays centered on the display; screen edges/corners pad with black.

## 2b. Sender: stream a video (file, URL, or VLC)

The PC decodes and scales the video; the Luckfox just displays it, so even
heavy sources play smoothly. Needs `ffmpeg` on PATH.

**Direct file** (simplest):

```bash
python video_sender.py <luckfox-ip> 8000 movie.mp4 --fps 24 --loop
```

**VLC as the source.** VLC and `video_sender.py` both run on the PC: VLC serves
a local stream, the sender reads it, scales it, and forwards frames to the
Luckfox.

1. **Start VLC streaming.** Command line is the reliable way — pick one:

   HTTP (VLC = server; start it *before* the sender):
   ```
   "C:\Program Files\VideoLAN\VLC\vlc.exe" movie.mp4 ^
     --sout "#std{access=http,mux=ts,dst=:8080}" --sout-keep --loop
   ```
   UDP (lower latency; either start order works):
   ```
   "C:\Program Files\VideoLAN\VLC\vlc.exe" movie.mp4 ^
     --sout "#std{access=udp,mux=ts,dst=127.0.0.1:1234}"
   ```
   GUI equivalent: **Media → Stream → Add file → Stream →** destination
   **HTTP** (port 8080) or **UDP**, and turn **Transcoding OFF** (let the
   sender do the 240×240 scaling — don't transcode twice).

2. **Start the sender** with `--live` (drops ffmpeg's `-re` so latency doesn't
   build up), pointed at VLC's stream:
   ```bash
   # for the HTTP stream:
   python video_sender.py <luckfox-ip> 8000 http://127.0.0.1:8080 --live --fps 24

   # for the UDP stream:
   python video_sender.py <luckfox-ip> 8000 udp://@127.0.0.1:1234 --live --fps 24
   ```

Notes for VLC mode:
- **Order:** for HTTP, start VLC first (it's the server), then the sender. For
  UDP, either order works (connectionless).
- **Latency:** VLC's HTTP/TS buffering adds ~1–3 s; UDP is tighter.
- **Audio** plays from VLC on the PC as usual; nothing audio-related is sent to
  the device (the display is video-only).

Both sides print their frames/sec so you can watch throughput.

---

## Notes / troubleshooting

- **Connection refused / timeout:** make sure both devices are on the same
  network, the receiver is running, and the port matches. On Windows, allow
  Python through the firewall if prompted.
- **Throughput:** 20 fps × 115,200 bytes ≈ 2.3 MB/s — fine over Wi‑Fi/Ethernet.
  The display's SPI ceiling (~30–45 fps) is above 20 fps, so the panel keeps up.
- **Rotation:** pass `--rotation 0..7` to `luckfox_receiver.py` if the image is
  sideways/upside-down.
- **Colors look swapped:** the senders emit big-endian RGB565 to match the
  driver. If needed, the byte order can be flipped (`to_rgb565_be` in the
  Windows sender, or the ffmpeg `rgb565be`→`rgb565le` pixel format in the video
  sender).
- The screen stays awake while frames are streaming; if a sender stops, the
  last frame simply stays on the panel.
