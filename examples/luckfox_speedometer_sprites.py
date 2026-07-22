"""
luckfox_speedometer_sprites.py

The same dynamic virtual speedometer as luckfox_speedometer.py, but the moving
needle is drawn with the *sprite* technique from the flying-toasters example
instead of being rasterized live every frame.

How it mirrors toasters.py:

    toasters.py                         this script
    -----------                         -----------
    TOASTERS = [t1, t2, t3, t4]         NEEDLE_FRAMES = [frame0 .. frame100]
    bitmap = man.sprites[man.step]      frame = NEEDLE_FRAMES[speed]
    tft.fill_rect(... BLACK)  # erase   tft.fill_rect(prev box, BLACK)  # erase
    tft.bitmap(bitmap, x, y)  # draw    frame.blit(tft)                 # draw

At start-up we pre-render one needle sprite for every speed from 0 to 100
(a little "sprite sheet" held in RAM). Each frame is a tight bitmap of just
the needle's bounding box, stored as a ready-to-blit RGB565 buffer. Animating
is then only: erase the previous frame's box with black, blit the current
frame -- exactly the toaster pattern -- with no per-frame geometry math.

Because the needle floats in the black inner disc (radius 46 -> 88) it never
overlaps the ticks, the colored band or the central number, so erasing a
frame's box with black is always safe.

Runs on a Luckfox Pico Ultra W (RV1106) under Linux/CPython.

Wiring used below (adjust to match your board):
    DC pin        = GPIO 54
    RESET pin     = GPIO 42
    BACKLIGHT pin = GPIO 71
    SPI           = /dev/spidev0.0 (bus 0, device 0)
"""

import math
import os
import random
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import spidev
from periphery import GPIO

import gc9a01py_linux as gc9a01

from fonts.romfonts import vga2_bold_16x32 as bigfont
from fonts.romfonts import vga2_8x8 as smallfont

# ----------------------------------------------------------------------------
# Hardware configuration
# ----------------------------------------------------------------------------
DC_PIN = 54
RESET_PIN = 42
BACKLIGHT_PIN = 71

SPI_BUS = 0
SPI_DEVICE = 0
SPI_SPEED_HZ = 40000000

# Delay between loop iterations. 0.02 s caps the gauge at a smooth 50 fps.
# Set to 0.0 to run uncapped and print the raw loop rate the hardware allows.
FRAME_DELAY = 0.02

# ----------------------------------------------------------------------------
# Gauge geometry (240x240 display, circular)
# ----------------------------------------------------------------------------
CX = 120
CY = 120

MIN_SPEED = 0
MAX_SPEED = 100

START_ANGLE = 225.0
SWEEP = 270.0

R_BAND_IN = 104
R_BAND_OUT = 118
R_TICK_MINOR_IN = 104
R_TICK_MAJOR_IN = 98
R_TICK_OUT = 118
R_RING = 40
R_NEEDLE_TIP = 88
R_NEEDLE_BASE = 46
NEEDLE_HALF_W = 6

GRAY = gc9a01.color565(90, 90, 90)

NUM_BOX_X = 96
NUM_BOX_Y = 92
NUM_BOX_W = 48
NUM_BOX_H = 34

# White pixel encoded as big-endian RGB565 (matches the driver's _ENCODE_PIXEL)
_WHITE_HI = (gc9a01.WHITE >> 8) & 0xFF
_WHITE_LO = gc9a01.WHITE & 0xFF


def zone_color(speed):
    """Return the band/number color for a given speed."""
    if speed < 60:
        return gc9a01.GREEN
    if speed < 80:
        return gc9a01.YELLOW
    return gc9a01.RED


def speed_to_angle(speed):
    frac = (speed - MIN_SPEED) / (MAX_SPEED - MIN_SPEED)
    return START_ANGLE - SWEEP * frac


def _polar(angle_deg, radius):
    rad = math.radians(angle_deg)
    return CX + radius * math.cos(rad), CY - radius * math.sin(rad)


def radial_line(tft, angle_deg, r0, r1, width, color):
    """Draw a (thick) line running radially outward at the given angle."""
    rad = math.radians(angle_deg)
    dx = math.cos(rad)
    dy = -math.sin(rad)
    px, py = -dy, dx
    for off in range(-(width // 2), width // 2 + 1):
        x0 = int(round(CX + dx * r0 + px * off))
        y0 = int(round(CY + dy * r0 + py * off))
        x1 = int(round(CX + dx * r1 + px * off))
        y1 = int(round(CY + dy * r1 + py * off))
        tft.line(x0, y0, x1, y1, color)


def draw_circle(tft, radius, color, step=3):
    for a in range(0, 360, step):
        x, y = _polar(a, radius)
        tft.pixel(int(round(x)), int(round(y)), color)


class NeedleSprite:
    """A pre-rendered needle frame: a bitmap plus where to blit it."""

    def __init__(self, x, y, width, height, buf):
        self.x = x
        self.y = y
        self.width = width
        self.height = height
        self.buf = buf

    def blit(self, tft):
        tft.blit_buffer(self.buf, self.x, self.y, self.width, self.height)

    def erase(self, tft):
        tft.fill_rect(self.x, self.y, self.width, self.height, gc9a01.BLACK)


def _needle_vertices(speed):
    """Return the needle's three (x, y) vertices for a speed."""
    rad = math.radians(speed_to_angle(speed))
    dx = math.cos(rad)
    dy = -math.sin(rad)
    px, py = -dy, dx
    tip = (CX + dx * R_NEEDLE_TIP, CY + dy * R_NEEDLE_TIP)
    bx = CX + dx * R_NEEDLE_BASE
    by = CY + dy * R_NEEDLE_BASE
    b1 = (bx + px * NEEDLE_HALF_W, by + py * NEEDLE_HALF_W)
    b2 = (bx - px * NEEDLE_HALF_W, by - py * NEEDLE_HALF_W)
    return [
        (int(round(tip[0])), int(round(tip[1]))),
        (int(round(b1[0])), int(round(b1[1]))),
        (int(round(b2[0])), int(round(b2[1]))),
    ]


def render_needle_sprite(speed):
    """Rasterize the needle for a speed into a tight RGB565 sprite frame."""
    verts = _needle_vertices(speed)
    xs = [v[0] for v in verts]
    ys = [v[1] for v in verts]
    minx, maxx = max(0, min(xs)), min(239, max(xs))
    miny, maxy = max(0, min(ys)), min(239, max(ys))
    w = maxx - minx + 1
    h = maxy - miny + 1
    buf = bytearray(w * h * 2)          # zero-filled == black background

    # Scanline fill of the triangle, writing white pixels into the buffer.
    pts = sorted(((v[1], v[0]) for v in verts))
    (ya, xa), (yb, xb), (yc, xc) = pts

    def interp(yy0, xx0, yy1, xx1, y):
        if yy1 == yy0:
            return xx0
        return xx0 + (xx1 - xx0) * (y - yy0) / (yy1 - yy0)

    for y in range(ya, yc + 1):
        if y < miny or y > maxy:
            continue
        x_long = interp(ya, xa, yc, xc, y)
        if y < yb:
            x_short = interp(ya, xa, yb, xb, y)
        else:
            x_short = interp(yb, xb, yc, xc, y)
        xl = max(minx, int(round(min(x_long, x_short))))
        xr = min(maxx, int(round(max(x_long, x_short))))
        row = (y - miny) * w
        for x in range(xl, xr + 1):
            idx = (row + (x - minx)) * 2
            buf[idx] = _WHITE_HI
            buf[idx + 1] = _WHITE_LO

    return NeedleSprite(minx, miny, w, h, buf)


def build_needle_frames():
    """Pre-render one needle sprite per integer speed (the sprite sheet)."""
    return [render_needle_sprite(speed)
            for speed in range(MIN_SPEED, MAX_SPEED + 1)]


def draw_speed_value(tft, speed):
    """Redraw the big central number, colored by zone."""
    tft.fill_rect(NUM_BOX_X, NUM_BOX_Y, NUM_BOX_W, NUM_BOX_H, gc9a01.BLACK)
    s = str(speed)
    w = len(s) * bigfont.WIDTH
    tft.text(bigfont, s, CX - w // 2, NUM_BOX_Y + 2, zone_color(speed),
             gc9a01.BLACK)


def draw_static(tft):
    """Draw the parts of the gauge that never change."""
    tft.fill(gc9a01.BLACK)

    steps = 480
    for i in range(steps + 1):
        frac = i / steps
        angle = START_ANGLE - SWEEP * frac
        speed = MIN_SPEED + (MAX_SPEED - MIN_SPEED) * frac
        radial_line(tft, angle, R_BAND_IN, R_BAND_OUT, 1, zone_color(speed))

    for value in range(MIN_SPEED, MAX_SPEED + 1, 5):
        angle = speed_to_angle(value)
        if value % 10 == 0:
            radial_line(tft, angle, R_TICK_MAJOR_IN, R_TICK_OUT, 3, gc9a01.WHITE)
        else:
            radial_line(tft, angle, R_TICK_MINOR_IN, R_TICK_OUT, 1, gc9a01.WHITE)

    draw_circle(tft, R_RING, GRAY)
    units = "km/h"
    ux = CX - (len(units) * smallfont.WIDTH) // 2
    tft.text(smallfont, units, ux, 132, GRAY, gc9a01.BLACK)


def main():
    spi = spidev.SpiDev()
    dc = GPIO(DC_PIN, "out")
    reset = GPIO(RESET_PIN, "out")
    backlight = GPIO(BACKLIGHT_PIN, "out")

    try:
        spi.open(SPI_BUS, SPI_DEVICE)
        spi.max_speed_hz = SPI_SPEED_HZ
        spi.mode = 0

        tft = gc9a01.GC9A01(
            spi,
            dc=dc,
            reset=reset,
            backlight=backlight,
            rotation=0)

        # Pre-render the needle sprite sheet, then draw the static gauge.
        needle_frames = build_needle_frames()
        draw_static(tft)

        current = 0.0
        target = random.uniform(0, 100)
        dwell = 0
        prev_frame = None
        last_int = -1

        # Live frame-rate measurement, printed to the console once a second.
        fps_count = 0
        fps_start = time.monotonic()

        while True:
            # Ease toward a random target, linger, then pick a new one.
            diff = target - current
            current += diff * 0.06
            if abs(diff) < 0.6:
                dwell += 1
                if dwell > random.randint(8, 30):
                    target = random.uniform(0, 100)
                    dwell = 0
            current = max(0.0, min(100.0, current))

            speed = int(round(current))
            frame = needle_frames[speed]

            # Only touch the display when the needle actually changes frame.
            if frame is not prev_frame:
                if prev_frame is not None:
                    prev_frame.erase(tft)      # erase old sprite (toaster-style)
                frame.blit(tft)                # draw current sprite
                prev_frame = frame

            if speed != last_int:
                draw_speed_value(tft, speed)
                last_int = speed

            fps_count += 1
            now = time.monotonic()
            if now - fps_start >= 1.0:
                print("loop rate: {:6.1f} fps".format(fps_count / (now - fps_start)))
                fps_count = 0
                fps_start = now

            if FRAME_DELAY:
                time.sleep(FRAME_DELAY)
    finally:
        dc.close()
        reset.close()
        backlight.close()
        spi.close()


if __name__ == "__main__":
    main()
