"""
luckfox_rtsp.py

View an RTSP / ONVIF camera stream on the GC9A01 display, standalone on the
Luckfox (no PC needed). ffmpeg on the board pulls the stream, scales it to
240x240 and hands raw RGB565 frames to the display.

ONVIF note: ONVIF is just discovery/control -- the video is a normal RTSP
H.264/H.265 stream. You only need the camera's RTSP URL. Use the camera's
low-resolution SUBSTREAM; the single-core RV1106 can't decode a full 1080p
main stream in real time, but a ~640x360 substream is fine.

Common substream URL patterns (check your camera's manual/ONVIF tool):
    Hikvision:  rtsp://user:pass@IP:554/Streaming/Channels/102
    Dahua:      rtsp://user:pass@IP:554/cam/realmonitor?channel=1&subtype=1
    Generic:    rtsp://user:pass@IP:554/onvif2
                rtsp://user:pass@IP:554/live/ch01_1

Run:
    python3 luckfox_rtsp.py "rtsp://admin:pass@192.168.1.64:554/Streaming/Channels/102"
    python3 luckfox_rtsp.py "rtsp://..." --fps 15 --transport tcp --rotation 0

Wiring (adjust to match your board):
    DC pin        = GPIO 54
    RESET pin     = GPIO 42
    BACKLIGHT pin = GPIO 71
    SPI           = /dev/spidev0.0 (bus 0, device 0)
"""

import argparse
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))

import spidev
from periphery import GPIO

import gc9a01py_linux as gc9a01

DC_PIN = 54
RESET_PIN = 42
BACKLIGHT_PIN = 71

SPI_BUS = 0
SPI_DEVICE = 0
SPI_SPEED_HZ = 60000000

WIDTH = 240
HEIGHT = 240
FRAME_BYTES = WIDTH * HEIGHT * 2


def build_ffmpeg_cmd(url, fps, transport):
    """Pull the RTSP stream, scale to 240x240, emit raw big-endian RGB565."""
    vf = ("scale=240:240:force_original_aspect_ratio=increase:"
          "flags=fast_bilinear,crop=240:240,fps={}".format(fps))
    return [
        "ffmpeg",
        "-hide_banner",
        "-loglevel", "error",
        # Low-latency input options for a live camera:
        "-rtsp_transport", transport,   # tcp is reliable over Wi-Fi; udp lower latency
        "-fflags", "nobuffer",
        "-flags", "low_delay",
        "-skip_loop_filter", "all",     # faster H.264 decode (slight blockiness)
        "-i", url,
        "-an",
        "-vf", vf,
        "-f", "rawvideo",
        "-pix_fmt", "rgb565be",
        "-",
    ]


def main():
    parser = argparse.ArgumentParser(
        description="Show an RTSP/ONVIF camera on the GC9A01 display.")
    parser.add_argument("url", help="camera RTSP URL (use the substream)")
    parser.add_argument("--fps", type=int, default=15,
                        help="frames per second to render (default 15)")
    parser.add_argument("--transport", default="tcp", choices=["tcp", "udp"],
                        help="RTSP transport (default tcp)")
    parser.add_argument("--rotation", type=int, default=0,
                        help="display rotation 0-7 (default 0)")
    parser.add_argument("--no-reconnect", dest="reconnect",
                        action="store_false",
                        help="exit instead of retrying if the stream drops")
    args = parser.parse_args()

    spi = spidev.SpiDev()
    dc = GPIO(DC_PIN, "out")
    reset = GPIO(RESET_PIN, "out")
    backlight = GPIO(BACKLIGHT_PIN, "out")

    try:
        spi.open(SPI_BUS, SPI_DEVICE)
        spi.max_speed_hz = SPI_SPEED_HZ
        spi.mode = 0

        tft = gc9a01.GC9A01(
            spi, dc=dc, reset=reset, backlight=backlight,
            rotation=args.rotation)
        tft.fill(gc9a01.BLACK)
        tft._set_window(0, 0, WIDTH - 1, HEIGHT - 1)

        fps_count = 0
        fps_start = time.monotonic()

        while True:
            print("connecting to", args.url)
            try:
                proc = subprocess.Popen(
                    build_ffmpeg_cmd(args.url, args.fps, args.transport),
                    stdout=subprocess.PIPE)
            except FileNotFoundError:
                print("ffmpeg not found on the board.")
                return

            try:
                while True:
                    data = proc.stdout.read(FRAME_BYTES)
                    if len(data) < FRAME_BYTES:
                        break                       # stream ended / dropped

                    dc.write(True)
                    spi.writebytes2(data)

                    fps_count += 1
                    now = time.monotonic()
                    if now - fps_start >= 1.0:
                        print("rendering {:4.1f} fps".format(
                            fps_count / (now - fps_start)))
                        fps_count = 0
                        fps_start = now
            finally:
                proc.stdout.close()
                proc.terminate()
                proc.wait()

            if not args.reconnect:
                break
            print("stream ended; reconnecting in 2s...")
            # keep the panel awake across the gap, then restart the window
            tft.keep_awake()
            tft._set_window(0, 0, WIDTH - 1, HEIGHT - 1)
            time.sleep(2)
    finally:
        dc.close()
        reset.close()
        backlight.close()
        spi.close()


if __name__ == "__main__":
    main()
