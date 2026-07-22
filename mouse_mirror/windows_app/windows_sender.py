"""
windows_sender.py  (runs on your Windows PC)

Grabs a 240x240 area around the mouse cursor and streams it to the Luckfox
over HTTP at a target frame rate. The Luckfox runs luckfox_receiver.py and
renders each frame on the GC9A01 display.

    PC (this)  --HTTP POST /frame-->  Luckfox (luckfox_receiver.py)  --> display

The capture window is always centered on the cursor, so the pointer stays in
the middle of the display even in screen corners; any part of the window that
falls off the desktop is shown as black.

Setup (once):
    pip install -r requirements.txt      (mss, numpy, requests)

Run:
    python windows_sender.py 192.168.1.50 8000
    python windows_sender.py 192.168.1.50 8000 --fps 20 --zoom 2

Arguments:
    ip, port           the Luckfox's address (as shown when the receiver starts)
    --fps    N         target frames per second (default 20)
    --zoom   Z         capture a (240*Z)x(240*Z) area around the cursor and
                       shrink it to 240x240 -- Z=1 is pixel-exact, Z=2 shows
                       more context (default 1)

Press Ctrl+C to stop.
"""

import argparse
import ctypes
import sys
import time
from ctypes import wintypes

try:
    import numpy as np
    import mss
    import requests
except ImportError as exc:
    print("Missing dependency:", exc)
    print("Install with:  pip install -r requirements.txt")
    sys.exit(1)

SIZE = 240
FRAME_BYTES = SIZE * SIZE * 2

user32 = ctypes.windll.user32


def set_dpi_aware():
    """Report physical pixels from the coordinate APIs.

    Windows virtualizes coordinates for non-DPI-aware processes when display
    scaling is > 100%, but mss captures physical pixels. Without this the
    cursor position and screen bounds would be in logical (scaled) units while
    the capture is physical, so the far (right/bottom) edges clamp wrong and
    get cropped. Call this BEFORE reading any coordinates or starting mss.
    """
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)   # per-monitor aware
    except Exception:
        try:
            user32.SetProcessDPIAware()                   # system aware
        except Exception:
            pass


def cursor_pos():
    pt = wintypes.POINT()
    user32.GetCursorPos(ctypes.byref(pt))
    return pt.x, pt.y


def virtual_bounds():
    """Bounding box of all monitors (supports multi-monitor, negative coords)."""
    SM_XVIRTUALSCREEN, SM_YVIRTUALSCREEN = 76, 77
    SM_CXVIRTUALSCREEN, SM_CYVIRTUALSCREEN = 78, 79
    x = user32.GetSystemMetrics(SM_XVIRTUALSCREEN)
    y = user32.GetSystemMetrics(SM_YVIRTUALSCREEN)
    w = user32.GetSystemMetrics(SM_CXVIRTUALSCREEN)
    h = user32.GetSystemMetrics(SM_CYVIRTUALSCREEN)
    return x, y, w, h


def to_rgb565_be(img):
    """Convert an (H, W, 4) BGRA uint8 array to big-endian RGB565 bytes."""
    b = img[:, :, 0].astype(np.uint16)
    g = img[:, :, 1].astype(np.uint16)
    r = img[:, :, 2].astype(np.uint16)
    rgb = ((r & 0xF8) << 8) | ((g & 0xFC) << 3) | (b >> 3)
    return rgb.astype(">u2").tobytes()


def resize_to_240(img, cap):
    """Nearest-neighbour shrink of a (cap, cap, 4) array to 240x240."""
    if cap == SIZE:
        return img
    idx = (np.arange(SIZE) * cap // SIZE)
    return img[idx][:, idx]


def grab_centered(sct, cx, cy, cap, bounds):
    """Return a (cap, cap, 4) BGRA array centered on the cursor.

    The capture window is always centered on (cx, cy), so the cursor stays in
    the middle of the display even in screen corners. Any part of the window
    that falls outside the desktop is left black (no data).
    """
    vx, vy, vw, vh = bounds
    left = cx - cap // 2
    top = cy - cap // 2

    # Intersect the desired window with the actual desktop bounds.
    gx0, gy0 = max(left, vx), max(top, vy)
    gx1, gy1 = min(left + cap, vx + vw), min(top + cap, vy + vh)

    canvas = np.zeros((cap, cap, 4), dtype=np.uint8)   # black BGRA
    if gx1 > gx0 and gy1 > gy0:
        shot = np.asarray(sct.grab({
            "left": gx0, "top": gy0,
            "width": gx1 - gx0, "height": gy1 - gy0}))
        oy, ox = gy0 - top, gx0 - left       # where it lands in the window
        canvas[oy:oy + (gy1 - gy0), ox:ox + (gx1 - gx0)] = shot[:, :, :4]
    return canvas


def main():
    parser = argparse.ArgumentParser(
        description="Stream the area around the mouse to a Luckfox display.")
    parser.add_argument("ip", help="Luckfox IP address")
    parser.add_argument("port", type=int, help="Luckfox receiver port")
    parser.add_argument("--fps", type=int, default=20,
                        help="target frames per second (default 20)")
    parser.add_argument("--zoom", type=int, default=1,
                        help="capture (240*zoom) px around the cursor and "
                             "shrink to 240 (default 1 = pixel-exact)")
    args = parser.parse_args()

    # Must run before any coordinate reads or mss init so everything is in
    # physical pixels (see set_dpi_aware).
    set_dpi_aware()

    url = "http://{}:{}/frame".format(args.ip, args.port)
    cap = SIZE * max(1, args.zoom)
    interval = 1.0 / args.fps

    bounds = virtual_bounds()
    session = requests.Session()

    print("Streaming {}x{} around cursor -> {}  at {} fps  (Ctrl+C to stop)"
          .format(cap, cap, url, args.fps))

    sent = 0
    warned = False
    fps_count = 0
    fps_start = time.monotonic()

    with mss.mss() as sct:
        while True:
            t0 = time.monotonic()

            cx, cy = cursor_pos()
            img = grab_centered(sct, cx, cy, cap, bounds)
            frame = to_rgb565_be(resize_to_240(img, cap))

            try:
                session.post(
                    url, data=frame,
                    headers={"Content-Type": "application/octet-stream"},
                    timeout=2)
                sent += 1
                fps_count += 1
                warned = False
            except requests.RequestException as exc:
                if not warned:
                    print("send failed ({}); retrying...".format(exc))
                    warned = True
                time.sleep(0.5)

            now = time.monotonic()
            if now - fps_start >= 1.0:
                print("sent {:5.1f} fps".format(fps_count / (now - fps_start)))
                fps_count = 0
                fps_start = now

            dt = interval - (time.monotonic() - t0)
            if dt > 0:
                time.sleep(dt)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nstopped")
